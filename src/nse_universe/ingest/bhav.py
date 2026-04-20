"""Ingest raw bhavcopy zips into normalized, partitioned parquet.

Output layout: data/parquet/year=YYYY/month=MM/YYYY-MM-DD.parquet
One file per trading day — easy to re-ingest a single day, cheap to scan
a date range.

Normalized columns:
  symbol     VARCHAR
  date       DATE
  open       DOUBLE
  high       DOUBLE
  low        DOUBLE
  close      DOUBLE
  prev_close DOUBLE
  volume     BIGINT  (TOTTRDQTY)
  turnover   DOUBLE  (TOTTRDVAL, in rupees)
  trades     INTEGER (TOTALTRADES)
  year       INTEGER (hive partition)
  month      INTEGER (hive partition)

Filter: SERIES == 'EQ' only (per user spec).
"""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from nse_universe.core.db import db, _register_bhav_view
from nse_universe.fetch.urls import bhav_local_path, find_existing_local
from nse_universe.paths import PARQUET_DIR, QUARANTINE_DIR, ensure_dirs

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    date: date
    rows: int
    status: str  # 'ingested' | 'skipped' | 'empty' | 'quarantined' | 'missing'


# Required core columns. Older (pre-~2011) legacy CSVs lack TOTALTRADES;
# we treat that as optional and emit NULL trades for those days.
LEGACY_COLS = {
    "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE",
    "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP",
}
LEGACY_OPTIONAL = {"TOTALTRADES"}

NEW_COLS = {
    "TckrSymb", "SctySrs", "Sgmt", "OpnPric", "HghPric", "LwPric",
    "ClsPric", "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal", "TradDt",
}
NEW_OPTIONAL = {"TtlNbOfTxsExctd"}


def parquet_path_for(d: date) -> Path:
    return PARQUET_DIR / f"year={d.year}" / f"month={d.month:02d}" / f"{d.isoformat()}.parquet"


def _read_bhav_csv(zip_path: Path) -> tuple[pd.DataFrame, str]:
    """Return (dataframe, schema) where schema ∈ {'legacy', 'new'}."""
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"no CSV in {zip_path}")
        with zf.open(csv_names[0]) as f:
            df = pd.read_csv(f, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    cols = set(df.columns)
    if LEGACY_COLS.issubset(cols):
        return df, "legacy"
    if NEW_COLS.issubset(cols):
        return df, "new"
    missing_legacy = LEGACY_COLS - cols
    missing_new = NEW_COLS - cols
    raise ValueError(
        f"{zip_path.name}: unrecognized schema. "
        f"missing-legacy={sorted(missing_legacy)[:5]} missing-new={sorted(missing_new)[:5]}"
    )


def _parse_legacy_timestamp(series: pd.Series) -> pd.Series:
    """NSE legacy CSVs use mixed date formats across years: 4-digit year
    (e.g. '15-JAN-2024') in newer files, 2-digit year (e.g. '13-Jul-20')
    in older ones. Try 4-digit first; fall back to 2-digit."""
    try:
        return pd.to_datetime(series, format="%d-%b-%Y").dt.date
    except ValueError:
        pass
    try:
        return pd.to_datetime(series, format="%d-%b-%y").dt.date
    except ValueError:
        pass
    # last resort: let pandas infer per-row (slower, very tolerant)
    return pd.to_datetime(series, format="mixed", dayfirst=True).dt.date


def _normalize_legacy(df: pd.DataFrame, d: date) -> pd.DataFrame:
    df = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
    if df.empty:
        return df
    trades_col = (
        pd.to_numeric(df["TOTALTRADES"], errors="coerce").astype("Int32")
        if "TOTALTRADES" in df.columns
        else pd.array([pd.NA] * len(df), dtype="Int32")
    )
    return pd.DataFrame({
        "symbol":     df["SYMBOL"].astype(str).str.strip(),
        "date":       _parse_legacy_timestamp(df["TIMESTAMP"]),
        "open":       pd.to_numeric(df["OPEN"], errors="coerce"),
        "high":       pd.to_numeric(df["HIGH"], errors="coerce"),
        "low":        pd.to_numeric(df["LOW"], errors="coerce"),
        "close":      pd.to_numeric(df["CLOSE"], errors="coerce"),
        "prev_close": pd.to_numeric(df["PREVCLOSE"], errors="coerce"),
        "volume":     pd.to_numeric(df["TOTTRDQTY"], errors="coerce").astype("Int64"),
        "turnover":   pd.to_numeric(df["TOTTRDVAL"], errors="coerce"),
        "trades":     trades_col,
    })


def _normalize_new(df: pd.DataFrame, d: date) -> pd.DataFrame:
    # Sgmt=CM filters out derivatives/other segments; SctySrs=EQ picks equity only
    df = df[(df["Sgmt"].astype(str).str.strip() == "CM")
            & (df["SctySrs"].astype(str).str.strip() == "EQ")].copy()
    if df.empty:
        return df
    trades_col = (
        pd.to_numeric(df["TtlNbOfTxsExctd"], errors="coerce").astype("Int32")
        if "TtlNbOfTxsExctd" in df.columns
        else pd.array([pd.NA] * len(df), dtype="Int32")
    )
    return pd.DataFrame({
        "symbol":     df["TckrSymb"].astype(str).str.strip(),
        "date":       pd.to_datetime(df["TradDt"], format="%Y-%m-%d").dt.date,
        "open":       pd.to_numeric(df["OpnPric"], errors="coerce"),
        "high":       pd.to_numeric(df["HghPric"], errors="coerce"),
        "low":        pd.to_numeric(df["LwPric"], errors="coerce"),
        "close":      pd.to_numeric(df["ClsPric"], errors="coerce"),
        "prev_close": pd.to_numeric(df["PrvsClsgPric"], errors="coerce"),
        "volume":     pd.to_numeric(df["TtlTradgVol"], errors="coerce").astype("Int64"),
        "turnover":   pd.to_numeric(df["TtlTrfVal"], errors="coerce"),
        "trades":     trades_col,
    })


def _normalize(df: pd.DataFrame, d: date, schema: str) -> pd.DataFrame:
    if schema == "legacy":
        out = _normalize_legacy(df, d)
    elif schema == "new":
        out = _normalize_new(df, d)
    else:
        raise ValueError(f"unknown schema {schema}")
    if out.empty:
        return out
    mismatch = out["date"] != d
    if mismatch.any():
        bad = out.loc[mismatch, "date"].unique()
        raise ValueError(f"date mismatch: expected {d}, got {bad}")
    out = out.dropna(subset=["close", "volume"])
    out["year"] = d.year
    out["month"] = d.month
    return out


def _write_parquet(df: pd.DataFrame, d: date) -> Path:
    """Byte-deterministic write so that re-ingesting the same zip yields an
    identical parquet — keeps git diffs sane when we re-run the pipeline."""
    path = parquet_path_for(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    # deterministic row order: sort by symbol, drop pandas index metadata
    df = df.sort_values("symbol", kind="mergesort").reset_index(drop=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    # strip pandas-specific schema metadata so the file only depends on data
    table = table.replace_schema_metadata({})
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        compression_level=3,
        row_group_size=50_000,
        use_dictionary=True,
        write_statistics=False,  # stats embed file offsets — nondeterministic-adjacent
        data_page_size=1 << 20,
    )
    tmp.replace(path)
    return path


def ingest_one(d: date, con: duckdb.DuckDBPyConnection, *, force: bool = False) -> IngestResult:
    out_path = parquet_path_for(d)
    row = con.execute(
        "SELECT ingested, zip_path FROM fetch_log WHERE date = ?", [d]
    ).fetchone()
    if row is None:
        return IngestResult(d, 0, "missing")
    ingested, zip_path_str = bool(row[0]), row[1]
    if ingested and out_path.exists() and not force:
        return IngestResult(d, 0, "skipped")

    zip_path = Path(zip_path_str) if zip_path_str else None
    if zip_path is None or not zip_path.exists():
        zip_path = find_existing_local(d)
    if zip_path is None or not zip_path.exists():
        return IngestResult(d, 0, "missing")

    try:
        raw, schema = _read_bhav_csv(zip_path)
        norm = _normalize(raw, d, schema)
    except Exception as e:
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        qdest = QUARANTINE_DIR / f"{d.isoformat()}-ingest-fail.zip"
        try:
            zip_path.replace(qdest)
        except OSError:
            pass
        log.warning("quarantined %s: %s", d, e)
        con.execute("DELETE FROM fetch_log WHERE date = ?", [d])
        return IngestResult(d, 0, "quarantined")

    if norm.empty:
        return IngestResult(d, 0, "empty")

    _write_parquet(norm, d)
    con.execute(
        "UPDATE fetch_log SET ingested = TRUE, ingested_rows = ? WHERE date = ?",
        [int(len(norm)), d],
    )
    return IngestResult(d, len(norm), "ingested")


def ingest_all_pending(*, force: bool = False, progress_cb=None) -> dict[str, int]:
    """Ingest every fetched-but-not-yet-ingested zip. Idempotent."""
    ensure_dirs()
    counts = {"ingested": 0, "skipped": 0, "empty": 0, "quarantined": 0, "missing": 0}
    total_rows = 0
    with db() as con:
        if force:
            pending = [r[0] for r in con.execute(
                "SELECT date FROM fetch_log ORDER BY date"
            ).fetchall()]
        else:
            pending = [r[0] for r in con.execute(
                "SELECT date FROM fetch_log WHERE NOT ingested ORDER BY date"
            ).fetchall()]
        for i, d in enumerate(pending):
            res = ingest_one(d, con, force=force)
            counts[res.status] += 1
            total_rows += res.rows
            if progress_cb:
                progress_cb(i + 1, len(pending), d, res)
        # refresh view so readers pick up new parquet files
        _register_bhav_view(con)
    counts["rows"] = total_rows
    return counts
