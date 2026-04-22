"""Sector/sub-sector metadata for NSE equities.

Authoritative source: NSE's own sectoral index constituent CSVs
(https://nsearchives.nseindia.com/content/indices/ind_*list.csv).

Primary feed (`ind_niftytotalmarket_list.csv`, ~750 symbols) gives one
``Industry`` label per symbol — NSE's "Sector" level in their 4-tier
classification. Sub-sector granularity comes from membership in *narrower*
sectoral indices (e.g. presence in NIFTY PSU BANK → BANKING_PSU).

On refresh: ``fetch_and_store()`` downloads ~15 CSVs via the existing
anti-bot session, merges them, maps NSE industry names to this repo's
20-sector vocabulary, and writes ``data/sectors.parquet``.

Consumers read via ``Universe.sector(sym)`` / ``Universe.sub_sector(sym)``
or the module-level ``sector()`` / ``sub_sector()`` helpers. The parquet
is the single source of truth — git-versioned, reproducible.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

import pandas as pd

from nse_universe.fetch.session import NSESession
from nse_universe.paths import DATA_DIR

log = logging.getLogger(__name__)

SECTORS_PARQUET = DATA_DIR / "sectors.parquet"


# =============================================================================
# NSE CSV ENDPOINTS
# =============================================================================
_NSE_ARCHIVES = "https://nsearchives.nseindia.com/content/indices"

# Broad-coverage lists. First one to classify a symbol wins (so narrower
# lists come first when we want them to override the broad Industry label).
BROAD_COVERAGE_URLS = {
    "niftytotalmarket": f"{_NSE_ARCHIVES}/ind_niftytotalmarket_list.csv",  # ~750
    "nifty500":         f"{_NSE_ARCHIVES}/ind_nifty500list.csv",           # 500
    "niftymicrocap250": f"{_NSE_ARCHIVES}/ind_niftymicrocap250_list.csv",  # 250 (tail)
}

# Narrower sectoral indices — used for SUB-SECTOR refinement.
# Priority order: most-specific first. Banking nuance (private vs PSU) is
# inferred from set membership: NIFTY BANK minus NIFTY PSU BANK = private.
# (NSE doesn't expose a public `ind_niftyprivatebanklist.csv`.)
SECTORAL_URLS = {
    # Financials — banks
    "niftypsubank":     f"{_NSE_ARCHIVES}/ind_niftypsubanklist.csv",
    "niftybank":        f"{_NSE_ARCHIVES}/ind_niftybanklist.csv",
    # Financials — non-bank (fin services 25/50 is the usable public CSV)
    "niftyfinsvc":      f"{_NSE_ARCHIVES}/ind_niftyfinancialservices25_50list.csv",
    # Healthcare
    "niftypharma":      f"{_NSE_ARCHIVES}/ind_niftypharmalist.csv",
    "niftyhealthcare":  f"{_NSE_ARCHIVES}/ind_niftyhealthcarelist.csv",
    # IT
    "niftyit":          f"{_NSE_ARCHIVES}/ind_niftyitlist.csv",
    # FMCG / Consumer
    "niftyfmcg":        f"{_NSE_ARCHIVES}/ind_niftyfmcglist.csv",
    "niftyconsumerdur": f"{_NSE_ARCHIVES}/ind_niftyconsumerdurableslist.csv",
    # Industry / Materials / Metals / Auto
    "niftyauto":        f"{_NSE_ARCHIVES}/ind_niftyautolist.csv",
    "niftymetal":       f"{_NSE_ARCHIVES}/ind_niftymetallist.csv",
    # Energy / Utilities
    "niftyenergy":      f"{_NSE_ARCHIVES}/ind_niftyenergylist.csv",
    "niftyoilgas":      f"{_NSE_ARCHIVES}/ind_niftyoilgaslist.csv",
    # Realty / Media / Telecom / Infra
    "niftyrealty":      f"{_NSE_ARCHIVES}/ind_niftyrealtylist.csv",
    "niftymedia":       f"{_NSE_ARCHIVES}/ind_niftymedialist.csv",
    "niftyinfra":       f"{_NSE_ARCHIVES}/ind_niftyinfralist.csv",
}


# =============================================================================
# TAXONOMY — NSE industry label → this repo's sector vocabulary
# =============================================================================
# NSE's Industry field is inconsistent (some casing, some punctuation). We
# normalize to upper-case without punctuation when matching.

SECTORS_VOCAB = {
    "FINANCIALS",
    "INFORMATION_TECHNOLOGY",
    "HEALTHCARE",
    "CONSUMER_DISCRETIONARY",
    "CONSUMER_STAPLES",
    "INDUSTRIALS",
    "INFRASTRUCTURE",
    "AUTOMOBILES",
    "ENERGY",
    "UTILITIES",
    "MATERIALS",
    "METALS_MINING",
    "REAL_ESTATE",
    "TELECOM",
    "MEDIA",
    "COMMODITIES",
    "DEBT",
    "INTERNATIONAL",
    "DEFENSIVE",
    "UNCLASSIFIED",
}

# Map NSE's "Industry" field values to our sector + default sub_sector.
_NSE_INDUSTRY_MAP: dict[str, tuple[str, str]] = {
    "financial services":                   ("FINANCIALS", "FIN_SERVICES"),
    "information technology":               ("INFORMATION_TECHNOLOGY", "IT_SERVICES"),
    "healthcare":                           ("HEALTHCARE", "PHARMACEUTICALS"),
    "automobile and auto components":       ("AUTOMOBILES", "AUTO"),
    "fast moving consumer goods":           ("CONSUMER_STAPLES", "FMCG"),
    "capital goods":                        ("INDUSTRIALS", "CAPITAL_GOODS"),
    "consumer durables":                    ("CONSUMER_DISCRETIONARY", "CONSUMER_DURABLES"),
    "consumer services":                    ("CONSUMER_DISCRETIONARY", "SERVICES"),
    "services":                             ("INDUSTRIALS", "BUSINESS_SVCS"),
    "metals & mining":                      ("METALS_MINING", "METALS_MINING"),
    "oil gas & consumable fuels":           ("ENERGY", "OIL_GAS"),
    "chemicals":                            ("MATERIALS", "CHEMICALS"),
    "construction":                         ("INFRASTRUCTURE", "CONSTRUCTION"),
    "construction materials":               ("MATERIALS", "CEMENT"),
    "power":                                ("UTILITIES", "POWER"),
    "utilities":                            ("UTILITIES", "UTILITIES"),
    "realty":                               ("REAL_ESTATE", "REAL_ESTATE"),
    "telecommunication":                    ("TELECOM", "TELECOM_SVCS"),
    "media entertainment & publication":    ("MEDIA", "MEDIA"),
    "textiles":                             ("CONSUMER_DISCRETIONARY", "TEXTILES"),
    "diversified":                          ("INDUSTRIALS", "DIVERSIFIED"),
    "forest materials":                     ("MATERIALS", "FOREST_MATERIALS"),
}


# Map sectoral-index membership → (sector, sub_sector) override.
# Applied AFTER the broad Industry label, only if we get a tighter sub_sector.
# Same sector must match — we don't flip sector based on sectoral indices,
# only refine the sub_sector.
_SECTORAL_SUB_SECTOR: dict[str, tuple[str, str]] = {
    # Virtual key — synthesized below as (niftybank - niftypsubank)
    "niftypvtbank":     ("FINANCIALS", "BANKING_PRIVATE"),
    "niftypsubank":     ("FINANCIALS", "BANKING_PSU"),
    "niftybank":        ("FINANCIALS", "BANKING"),
    "niftyfinsvc":      ("FINANCIALS", "FIN_SERVICES"),
    "niftypharma":      ("HEALTHCARE", "PHARMACEUTICALS"),
    "niftyhealthcare":  ("HEALTHCARE", "HEALTHCARE_SVCS"),
    "niftyit":          ("INFORMATION_TECHNOLOGY", "IT_SERVICES"),
    "niftyfmcg":        ("CONSUMER_STAPLES", "FMCG"),
    "niftyconsumerdur": ("CONSUMER_DISCRETIONARY", "CONSUMER_DURABLES"),
    "niftyauto":        ("AUTOMOBILES", "AUTO"),
    "niftymetal":       ("METALS_MINING", "METALS_MINING"),
    "niftyenergy":      ("ENERGY", "OIL_GAS"),
    "niftyoilgas":      ("ENERGY", "OIL_GAS"),
    "niftyrealty":      ("REAL_ESTATE", "REAL_ESTATE"),
    "niftymedia":       ("MEDIA", "MEDIA"),
    "niftyinfra":       ("INFRASTRUCTURE", "INFRASTRUCTURE"),
}


def _normalize_industry(name: str) -> str:
    """Normalize NSE industry strings for matching."""
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def _classify_industry(nse_industry: str) -> tuple[str, str]:
    """Map NSE Industry string to our (sector, sub_sector)."""
    key = _normalize_industry(nse_industry)
    if key in _NSE_INDUSTRY_MAP:
        return _NSE_INDUSTRY_MAP[key]
    # Defensive fallback — log and return UNCLASSIFIED.
    log.warning("Unmapped NSE industry label: %r", nse_industry)
    return ("UNCLASSIFIED", "UNCLASSIFIED")


# =============================================================================
# FETCH — download NSE CSVs
# =============================================================================
@dataclass
class _SymbolInfo:
    symbol: str
    company: str
    nse_industry: str | None = None
    isin: str | None = None
    # Sectoral-index memberships (set of index keys)
    sectoral_indices: set[str] | None = None

    def __post_init__(self):
        if self.sectoral_indices is None:
            self.sectoral_indices = set()


def _download_csv(session: NSESession, url: str) -> pd.DataFrame | None:
    """Download one NSE CSV; return DataFrame or None on failure."""
    try:
        resp = session.get(url, timeout=30)
    except Exception as e:
        log.warning("Download error %s: %s", url, e)
        return None
    if resp.status_code != 200:
        log.warning("HTTP %d for %s", resp.status_code, url)
        return None
    try:
        df = pd.read_csv(io.BytesIO(resp.content))
    except Exception as e:
        log.warning("Parse error %s: %s", url, e)
        return None
    # Standardize column names — NSE CSV format is consistent but be defensive.
    df.columns = [c.strip() for c in df.columns]
    return df


def fetch_and_store(
    output_path: Path = SECTORS_PARQUET,
    *,
    session: NSESession | None = None,
) -> pd.DataFrame:
    """Download NSE CSVs, classify symbols, write sectors.parquet.

    Returns the DataFrame written, with columns:
        symbol, company, nse_industry, isin, sector, sub_sector,
        in_sectoral_indices, generated_at.
    """
    sess = session or NSESession()
    try:
        # -------- Phase 1: broad coverage --------
        symbols: dict[str, _SymbolInfo] = {}
        for key, url in BROAD_COVERAGE_URLS.items():
            df = _download_csv(sess, url)
            if df is None:
                continue
            log.info("%s: %d rows", key, len(df))
            for _, row in df.iterrows():
                sym = str(row.get("Symbol", "")).strip()
                if not sym:
                    continue
                # First seen wins — totalmarket is fetched first, has most rows.
                if sym in symbols:
                    continue
                symbols[sym] = _SymbolInfo(
                    symbol=sym,
                    company=str(row.get("Company Name", "")).strip(),
                    nse_industry=str(row.get("Industry", "")).strip() or None,
                    isin=str(row.get("ISIN Code", "")).strip() or None,
                )

        # -------- Phase 2: sectoral index memberships for sub_sector --------
        for key, url in SECTORAL_URLS.items():
            df = _download_csv(sess, url)
            if df is None:
                continue
            log.info("%s: %d rows", key, len(df))
            for _, row in df.iterrows():
                sym = str(row.get("Symbol", "")).strip()
                if not sym:
                    continue
                info = symbols.setdefault(sym, _SymbolInfo(
                    symbol=sym,
                    company=str(row.get("Company Name", "")).strip(),
                    nse_industry=str(row.get("Industry", "")).strip() or None,
                    isin=str(row.get("ISIN Code", "")).strip() or None,
                ))
                info.sectoral_indices.add(key)
                # If broad coverage missed the symbol but sectoral has it
                if not info.nse_industry:
                    info.nse_industry = str(row.get("Industry", "")).strip() or None
    finally:
        if session is None:
            sess.close()

    if not symbols:
        raise RuntimeError("NSE fetch returned zero symbols — anti-bot blocked or URLs changed?")

    # -------- Phase 3: classify --------
    # Synthesize BANKING_PRIVATE membership = (in niftybank AND NOT in niftypsubank).
    for info in symbols.values():
        if "niftybank" in info.sectoral_indices and "niftypsubank" not in info.sectoral_indices:
            info.sectoral_indices.add("niftypvtbank")

    # Priority order for sub_sector refinement: most-specific first.
    _SUB_SECTOR_PRIORITY = [
        "niftypvtbank", "niftypsubank", "niftybank", "niftyfinsvc",
        "niftypharma", "niftyhealthcare",
        "niftyit", "niftyfmcg", "niftyconsumerdur",
        "niftyauto", "niftymetal",
        "niftyoilgas", "niftyenergy",
        "niftyrealty", "niftymedia", "niftyinfra",
    ]

    out_rows = []
    for sym, info in symbols.items():
        if info.nse_industry:
            sector, sub_sector = _classify_industry(info.nse_industry)
        else:
            sector, sub_sector = ("UNCLASSIFIED", "UNCLASSIFIED")

        # Refine sub_sector via sectoral-index membership.
        for key in _SUB_SECTOR_PRIORITY:
            if key in info.sectoral_indices:
                sec_candidate, sub_candidate = _SECTORAL_SUB_SECTOR[key]
                # Only override if sector matches (or if we were UNCLASSIFIED).
                if sector == sec_candidate or sector == "UNCLASSIFIED":
                    sector = sec_candidate
                    sub_sector = sub_candidate
                    break

        out_rows.append({
            "symbol": sym,
            "company": info.company,
            "nse_industry": info.nse_industry,
            "isin": info.isin,
            "sector": sector,
            "sub_sector": sub_sector,
            "in_sectoral_indices": ",".join(sorted(info.sectoral_indices)),
            "generated_at": date.today().isoformat(),
        })

    df = pd.DataFrame(out_rows).sort_values("symbol").reset_index(drop=True)

    # Validate sectors are in vocab
    bad = set(df["sector"]) - SECTORS_VOCAB
    if bad:
        raise ValueError(f"Unknown sector(s) produced: {bad}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    log.info("Wrote %s: %d symbols, %d classified (%.1f%%)",
             output_path,
             len(df),
             (df["sector"] != "UNCLASSIFIED").sum(),
             100 * (df["sector"] != "UNCLASSIFIED").sum() / len(df))
    return df


# =============================================================================
# LOAD — in-memory lookup cache
# =============================================================================
_CACHE: dict[str, dict[str, str]] | None = None


def _ensure_loaded() -> dict[str, dict[str, str]]:
    """Load sectors.parquet once, memoize. Returns {symbol: {sector, sub_sector, ...}}."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not SECTORS_PARQUET.exists():
        log.warning(
            "Sectors parquet missing at %s. Run `nse-universe sectors refresh` "
            "or call nse_universe.sectors.fetch_and_store().",
            SECTORS_PARQUET,
        )
        _CACHE = {}
        return _CACHE
    df = pd.read_parquet(SECTORS_PARQUET)
    _CACHE = {
        row["symbol"]: {
            "sector": row["sector"],
            "sub_sector": row["sub_sector"],
            "nse_industry": row.get("nse_industry") or "",
            "company": row.get("company") or "",
            "isin": row.get("isin") or "",
        }
        for _, row in df.iterrows()
    }
    return _CACHE


def reload() -> None:
    """Force re-read of sectors.parquet (after refresh)."""
    global _CACHE
    _CACHE = None


def sector(symbol: str) -> str | None:
    """Sector for a symbol, or None if unclassified / unknown."""
    entry = _ensure_loaded().get(symbol)
    if entry is None:
        return None
    sec = entry.get("sector")
    return sec if sec and sec != "UNCLASSIFIED" else None


def sub_sector(symbol: str) -> str | None:
    """Sub-sector for a symbol, or None if unclassified / unknown."""
    entry = _ensure_loaded().get(symbol)
    if entry is None:
        return None
    ss = entry.get("sub_sector")
    return ss if ss and ss != "UNCLASSIFIED" else None


def classification(symbol: str) -> Mapping[str, str] | None:
    """Full classification row for a symbol — sector, sub_sector, nse_industry, company, isin."""
    return _ensure_loaded().get(symbol)


def all_classifications() -> pd.DataFrame:
    """All classifications as a DataFrame (loads parquet fresh)."""
    if not SECTORS_PARQUET.exists():
        return pd.DataFrame(columns=["symbol", "sector", "sub_sector", "nse_industry", "company", "isin"])
    return pd.read_parquet(SECTORS_PARQUET)
