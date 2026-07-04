"""Corporate actions (splits + dividends) via yfinance.

Path (b) from the design: use yfinance (`RELIANCE.NS` → `yf.Ticker(...)`) as
the primary source. This covers ~90% of what backtests need — splits and
cash dividends. It does NOT cover bonus issues distinct from splits, rights
issues, mergers, or name changes; coverage gaps are logged to data-health.

Output:
  - Row per (symbol, event_date, kind) in `adj_events` DuckDB table.
  - Per-symbol parquet snapshot at data/actions/{symbol}.parquet (git-versioned).

Concurrency: threaded with a small pool. Yahoo tolerates this well in practice.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yfinance as yf

from nse_universe.core.db import db
from nse_universe.core.export import export_all
from nse_universe.paths import ACTIONS_DIR, ensure_dirs
from nse_universe.rank.deny import is_non_equity

log = logging.getLogger(__name__)

# yfinance logs every delisted / rate-limited symbol at ERROR on its own
# logger ("$FOO.NS: possibly delisted; no timezone found"). We already track
# every failure per-symbol via RefreshResult.gaps + the yf_coverage cache, so
# silence its noise — a 2000-symbol refresh otherwise floods the console.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Persistent-cache tuning. A symbol that returns no usable yfinance data for
# PARK_THRESHOLD consecutive refreshes is "parked" and skipped, then re-probed
# once its last check ages past REPROBE_DAYS (so a re-listed / renamed ticker
# can rejoin automatically).
PARK_THRESHOLD = 2
REPROBE_DAYS = 30

# Throttle control. yfinance 1.3.0 mandates a curl_cffi session (requests_cache
# is rejected) and ships no rate limiter, so we pace requests ourselves:
#   * DEFAULT_RATE_PER_SEC — global cap on yfinance calls/sec across all workers.
#   * FRESH_DAYS — skip re-fetching a symbol whose actions succeeded within this
#     window (corporate actions are infrequent; this is the achievable stand-in
#     for the HTTP cache curl_cffi can't provide, and it makes repeat runs fast).
#   * YF_RETRIES / YF_TIMEOUT — yfinance's own transient-error retry + per-request
#     timeout (both default to off in 1.3.0).
DEFAULT_RATE_PER_SEC = 3.0
FRESH_DAYS = 7
YF_RETRIES = 3
YF_TIMEOUT = 30

# Yahoo symbol remap for NSE tickers whose Yahoo listing moved (renames,
# demergers, mergers) so `{SYMBOL}.NS` returns empty history. Each entry is the
# ordered list of Yahoo tickers to try *instead of* the default; recovered
# actions are still stored under the original NSE symbol. Every remap here was
# verified to return the security's history + actions.
#
# NOTE on mergers/amalgamations (ACLGATI, DHANI, MANGCHEFER, UDAICEMENT, PEL):
# the old symbol was absorbed into a successor, so the remap target is the
# *successor's* ticker. Its actions are the best available proxy for adjusting
# the old symbol's pre-suspension price series — accurate for pure renames,
# approximate where the successor's split/dividend history diverges.
SYMBOL_REMAP: dict[str, list[str]] = {
    # demergers / renames verified earlier
    "TATAMOTORS": ["TMPV.NS", "TMPV.BO"],          # 2025 demerger → Passenger Vehicles
    "LTIM":       ["LTM.NS", "LTM.BO"],            # LTIMindtree → LTM Limited
    "PEL":        ["PIRAMALFIN.NS", "PIRAMALFIN.BO"],  # Piramal Enterprises→Finance merger
    "SWANENERGY": ["SWANCORP.NS", "SWANCORP.BO"],  # Swan Energy → Swan Corp
    "AKZOINDIA":  ["JSWDULUX.NS", "JSWDULUX.BO"],  # Akzo Nobel India → JSW Dulux
    # renames / mergers (user-verified Yahoo tickers)
    "ACLGATI":    ["ALLCARGO.NS", "ALLCARGO.BO"],  # amalgamated into Allcargo Logistics
    "ARISINFRA":  ["ARISINFRA.BO"],                # 2025 IPO; Yahoo .NS not live
    "BARBEQUE":   ["UFBL.NS", "UFBL.BO"],          # → United Foodbrands
    "DHANI":      ["IBULLSLTD.NS", "IBULLSLTD.BO"],  # → Indiabulls Limited
    "EXCEL":      ["LANDSMILL.NS", "LANDSMILL.BO"],  # Excel Realty → Landsmill Green
    "GANESHHOUC": ["GANESHHOU.NS", "GANESHHOU.BO"],  # → Ganesh Housing Limited
    "GEPIL":      ["GVPIL.NS", "GVPIL.BO"],        # GE Power India symbol change
    "HEUBACHIND": ["SUDARCOLOR.NS", "SUDARCOLOR.BO"],  # → Sudarshan Colorants India
    "INDSWFTLTD": ["INDSWFTLTD-BE.NS", "524652.BO"],   # Yahoo NSE quote under -BE series
    "INFIBEAM":   ["CCAVENUE.NS", "CCAVENUE.BO"],  # Infibeam Avenues → AvenuesAI
    "ITDCEM":     ["CEMPRO.NS", "CEMPRO.BO"],      # ITD Cementation → Cemindia Projects
    "JCHAC":      ["BOSCH-HCIL.NS", "BOSCH-HCIL.BO", "523398.BO"],  # → Bosch Home Comfort
    "MANGCHEFER": ["PARADEEP.NS", "PARADEEP.BO"],  # merged into Paradeep Phosphates
    "MEGASOFT":   ["SIGMAADV.NS", "SIGMAADV.BO", "MEGASOFT.BO"],  # → Sigma Advanced Systems
    "SABTNL":     ["AQYLON.NS", "AQYLON.BO"],      # → Aqylon Nexus
    "SASTASUNDR": ["HEALTHX.NS", "SASTASUNDR.BO"],  # → Health X Platform (BSE lags)
    "SELAN":      ["ANTELOPUS.NS", "ANTELOPUS.BO"],  # → Antelopus Selan Energy
    "SEQUENT":    ["VIYASH.NS", "VIYASH.BO"],      # Sequent Scientific → Viyash Scientific
    "SGLTL":      ["SETL.NS", "SETL.BO"],          # Standard Glass Lining → Standard Engg Tech
    "SMLISUZU":   ["SMLMAH.NS", "SMLMAH.BO"],      # SML Isuzu → SML Mahindra
    "SMSLIFE":    ["HALEOSLABS.NS", "HALEOSLABS.BO"],  # SMS Lifesciences → Haleos Labs
    "SUNDARMHLD": ["TSFINV.NS"],                   # Sundaram Fin Holdings → TSF Investments
    "UDAICEMENT": ["JKLAKSHMI.NS", "JKLAKSHMI.BO"],  # merged into JK Lakshmi Cement
}


def _candidates(symbol: str) -> list[str]:
    """Ordered Yahoo tickers to try for one NSE symbol. Explicit remaps win;
    otherwise try the NSE listing then the BSE listing (`.BO`), which Yahoo
    often serves when the `.NS` feed is broken."""
    if symbol in SYMBOL_REMAP:
        return SYMBOL_REMAP[symbol]
    return [f"{symbol}.NS", f"{symbol}.BO"]


class _RateLimiter:
    """Thread-safe minimum-interval limiter: at most `rate_per_sec` acquisitions
    per second, globally, across all worker threads. Spacing (not bursting) is
    what keeps us under Yahoo's undocumented throttle. `clock`/`sleep` are
    injectable for deterministic testing."""

    def __init__(self, rate_per_sec: float, *, clock=time.monotonic, sleep=time.sleep):
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec and rate_per_sec > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self._min_interval


@dataclass
class ActionsStats:
    symbol: str
    splits: int = 0
    dividends: int = 0
    status: str = "ok"       # ok | no_actions | no_data | error
    error: str | None = None
    matched: str | None = None   # Yahoo ticker that actually served the data


@dataclass
class RefreshResult:
    total: int = 0
    ok: int = 0
    no_actions: int = 0         # has history but no dividends/splits (covered)
    no_data: int = 0            # empty history: throttle / gap / delisted
    errors: int = 0
    splits: int = 0
    dividends: int = 0
    recovered: int = 0          # failures rescued by the gentle retry pass
    skipped_parked: int = 0     # symbols skipped via the yf_coverage cache
    skipped_fresh: int = 0      # symbols skipped as recently-fetched (freshness cache)
    gaps: list[str] = field(default_factory=list)


def _parked_skip_set(con, today: date, reprobe_days: int) -> set[str]:
    """Symbols parked in yf_coverage that are NOT yet due for a re-probe.

    A parked symbol is skipped until `last_checked` ages past `reprobe_days`;
    once stale it drops out of this set and re-enters the fetch universe, so a
    re-listed / renamed ticker can recover on its own.
    """
    reprobe_cutoff = today - timedelta(days=reprobe_days)
    rows = con.execute(
        "SELECT symbol FROM yf_coverage WHERE parked = TRUE AND last_checked > ?",
        [reprobe_cutoff],
    ).fetchall()
    return {r[0] for r in rows}


def _fresh_skip_set(con, today: date, fresh_days: int) -> set[str]:
    """Symbols whose actions were fetched successfully within `fresh_days`.

    Corporate actions change rarely, so re-fetching a symbol whose data is only
    days old just burns request budget against Yahoo's throttle. Skipping them
    is our stand-in for the HTTP cache curl_cffi can't provide. `fresh_days <= 0`
    disables the freshness cache (always re-fetch).
    """
    if fresh_days <= 0:
        return set()
    cutoff = today - timedelta(days=fresh_days)
    rows = con.execute(
        "SELECT symbol FROM yf_coverage WHERE last_ok IS NOT NULL AND last_ok >= ?",
        [cutoff],
    ).fetchall()
    return {r[0] for r in rows}


def _active_equity_from_con(
    con,
    *,
    today: date,
    lookback_days: int = 365,
    min_days_seen: int = 20,
) -> list[str]:
    """Recently-active NSE symbols, minus non-equity (ETF / fund) instruments.

    "Recently active" = traded on at least `min_days_seen` days within the last
    `lookback_days`. The deny-list drops ETFs and gold / silver / liquid / index
    funds, which NSE files under the same EQ series as real equities.
    """
    cutoff = today - timedelta(days=lookback_days)
    rows = con.execute(
        """
        SELECT symbol
        FROM bhav_daily
        WHERE date >= ?
        GROUP BY symbol
        HAVING COUNT(*) >= ?
        ORDER BY symbol
        """,
        [cutoff, min_days_seen],
    ).fetchall()
    return [r[0] for r in rows if not is_non_equity(r[0])]


def _list_symbols_from_con(
    con,
    *,
    today: date,
    lookback_days: int = 365,
    min_days_seen: int = 20,
    reprobe_days: int = REPROBE_DAYS,
) -> list[str]:
    """Active NSE equities to fetch actions for, minus symbols parked in the
    yf_coverage cache that are not yet due for a re-probe."""
    active = _active_equity_from_con(
        con, today=today, lookback_days=lookback_days, min_days_seen=min_days_seen
    )
    parked = _parked_skip_set(con, today, reprobe_days)
    return [s for s in active if s not in parked]


def _list_symbols(lookback_days: int = 365, min_days_seen: int = 20) -> list[str]:
    """Convenience wrapper: open a read-only connection and list the universe."""
    with db(read_only=True) as con:
        return _list_symbols_from_con(
            con, today=date.today(),
            lookback_days=lookback_days, min_days_seen=min_days_seen,
        )


def _update_coverage(
    con,
    outcomes: dict[str, str],
    today: date,
    *,
    park_threshold: int = PARK_THRESHOLD,
) -> None:
    """Record each attempted symbol's outcome in the yf_coverage cache.

    A definitive answer — 'ok' (has actions) or 'no_actions' (has history, no
    dividends/splits) — resets the streak, un-parks, and stamps last_ok so the
    freshness cache can skip the symbol. A real miss ('no_data' / 'error')
    increments the consecutive-miss counter and parks at `park_threshold`, but
    only for symbols without proven coverage (see the parking guard below), so
    throttled misses never park a live stock.
    """
    if not outcomes:
        return
    prior = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "SELECT symbol, consecutive_no_data, last_ok FROM yf_coverage"
        ).fetchall()
    }
    answered = {"ok", "no_actions"}
    rows = []
    for sym, status in outcomes.items():
        prev_streak, prev_last_ok = prior.get(sym, (0, None))
        if status in answered:
            rows.append((sym, status, 0, False, today, today))
        else:
            streak = prev_streak + 1
            # Only park symbols with NO proven coverage. A symbol that has ever
            # returned actions (a prior 'ok', or an existing per-symbol parquet
            # from an earlier run) is a real, covered instrument — a current miss
            # is a Yahoo throttle, not a delisting, so it must not be parked.
            # This is the key guard against a heavily-throttled bulk run parking
            # live stocks (ADANIGREEN, AAVAS, …) alongside genuine ETFs / dead
            # tickers, which never produce actions and legitimately park.
            proven = prev_last_ok is not None or _has_actions_data(sym)
            parked = streak >= park_threshold and not proven
            rows.append((sym, "no_data", streak, parked, today, prev_last_ok))
    staging = pd.DataFrame(
        rows,
        columns=["symbol", "status", "consecutive_no_data",
                 "parked", "last_checked", "last_ok"],
    )
    con.register("_cov_staging", staging)
    con.execute(
        "DELETE FROM yf_coverage WHERE symbol IN (SELECT symbol FROM _cov_staging)"
    )
    con.execute(
        """
        INSERT INTO yf_coverage
            (symbol, status, consecutive_no_data, parked, last_checked, last_ok)
        SELECT symbol, status, consecutive_no_data, parked, last_checked, last_ok
        FROM _cov_staging
        """
    )
    con.unregister("_cov_staging")


def _fetch_candidate(
    ytk: str, symbol: str, limiter: "_RateLimiter | None",
) -> tuple[str, pd.DataFrame | None, str | None]:
    """Fetch actions for a single Yahoo ticker `ytk`, storing under NSE `symbol`.

    Returns (kind, df, err) where kind is one of:
      * 'ok'            — has dividends/splits (df populated)
      * 'no_actions'    — history exists but no dividends/splits (definitive)
      * 'empty_history' — Yahoo returned no price history (miss; try next ticker)
      * 'error'         — unexpected failure (err set; try next ticker)
    """
    if limiter is not None:
        limiter.acquire()
    try:
        actions = yf.Ticker(ytk).actions
    except AttributeError as e:
        # yfinance 1.3.x raises "'PriceHistory' object has no attribute
        # '_dividends'" when the underlying price history came back empty.
        if "_dividends" in str(e) or "_splits" in str(e):
            return "empty_history", None, None
        return "error", None, str(e)[:200]
    except Exception as e:
        return "error", None, str(e)[:200]

    if actions is None or actions.empty:
        return "no_actions", None, None

    frames = []
    if "Stock Splits" in actions.columns:
        sp = actions.loc[actions["Stock Splits"] != 0, ["Stock Splits"]].copy()
        sp = sp.rename(columns={"Stock Splits": "ratio"})
        sp["kind"] = "split"
        frames.append(sp)
    if "Dividends" in actions.columns:
        dv = actions.loc[actions["Dividends"] != 0, ["Dividends"]].copy()
        dv = dv.rename(columns={"Dividends": "ratio"})
        dv["kind"] = "dividend"
        frames.append(dv)

    if not frames:
        return "no_actions", None, None

    df = pd.concat(frames)
    df = df.reset_index().rename(columns={"Date": "event_date"})
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    df["symbol"] = symbol
    df["source"] = "yfinance"
    df = df[["symbol", "event_date", "kind", "ratio", "source"]]
    df = df.sort_values(["event_date", "kind"]).reset_index(drop=True)
    return "ok", df, None


def _fetch_one(
    symbol: str, *, sleep_s: float = 0.0, limiter: "_RateLimiter | None" = None,
) -> tuple[ActionsStats, pd.DataFrame | None]:
    """Pull splits + dividends for one NSE symbol via yfinance.

    Tries each Yahoo candidate (`_candidates`: NSE listing, then BSE, or an
    explicit remap) until one yields a definitive answer, so a symbol whose
    `.NS` feed is broken is recovered from `.BO` or its remapped ticker. A
    definitive answer ('ok' / 'no_actions') stops the search; only an empty
    history or transient error falls through to the next candidate.
    """
    stats = ActionsStats(symbol=symbol)
    saw_empty_history = False
    last_err = None
    for ytk in _candidates(symbol):
        kind, df, err = _fetch_candidate(ytk, symbol, limiter)
        if sleep_s:
            time.sleep(sleep_s)
        if kind == "ok":
            stats.status = "ok"
            stats.matched = ytk
            stats.splits = int((df["kind"] == "split").sum())
            stats.dividends = int((df["kind"] == "dividend").sum())
            return stats, df
        if kind == "no_actions":
            stats.status = "no_actions"
            stats.matched = ytk
            return stats, None
        if kind == "empty_history":
            saw_empty_history = True
        else:  # error
            last_err = err

    # No candidate had usable data. Prefer 'no_data' (a clean empty history is
    # more informative than a transient error) unless every attempt errored.
    if saw_empty_history or last_err is None:
        stats.status = "no_data"
    else:
        stats.status = "error"
        stats.error = last_err
    return stats, None


def _has_actions_data(symbol: str) -> bool:
    """True if a per-symbol actions parquet already exists — i.e. the symbol
    has produced real corporate actions at least once. Used as a durable
    "proven coverage" signal so throttled misses don't park live stocks."""
    return (ACTIONS_DIR / f"{symbol}.parquet").exists()


def _write_symbol_parquet(symbol: str, df: pd.DataFrame) -> Path:
    p = ACTIONS_DIR / f"{symbol}.parquet"
    tmp = p.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp, compression="zstd")
    tmp.replace(p)
    return p


def _upsert_events(con, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_staging_actions", df)
    con.execute(
        """
        DELETE FROM adj_events
         WHERE (symbol, event_date, kind) IN (
             SELECT symbol, event_date, kind FROM _staging_actions
         )
        """
    )
    con.execute(
        """
        INSERT INTO adj_events (symbol, event_date, kind, ratio, source)
        SELECT symbol, event_date, kind, ratio, source FROM _staging_actions
        """
    )
    con.unregister("_staging_actions")


def _consume(
    fut, sym: str, con, result: RefreshResult,
    outcomes: dict[str, str], err_msg: dict[str, str],
) -> str:
    """Apply one completed future: write data on success, record the outcome.

    Returns the outcome bucket ('ok' | 'no_data' | 'error'). ok / splits /
    dividends are tallied here (guarded so a retry that flips no_data→ok counts
    once); no_data / error totals are tallied later from the final outcomes so a
    recovered symbol isn't double-counted.
    """
    try:
        stats, df = fut.result()
    except Exception as e:
        outcomes[sym] = "error"
        err_msg[sym] = f"exec:{type(e).__name__}"
        return "error"
    if stats.status == "ok" and df is not None:
        _write_symbol_parquet(sym, df)
        _upsert_events(con, df)
        if outcomes.get(sym) != "ok":
            result.ok += 1
            result.splits += stats.splits
            result.dividends += stats.dividends
        outcomes[sym] = "ok"
        return "ok"
    if stats.status == "no_actions":
        outcomes[sym] = "no_actions"
        return "no_actions"
    if stats.status == "no_data":
        outcomes[sym] = "no_data"
        return "no_data"
    outcomes[sym] = "error"
    err_msg[sym] = stats.error or "error"
    return "error"


def _configure_yfinance() -> None:
    """Enable yfinance's own transient-error retry + per-request timeout (both
    default to off in 1.3.0). Idempotent; safe to call every run."""
    try:
        yf.config.network.retries = YF_RETRIES
        yf.config.network.timeout = YF_TIMEOUT
    except Exception as e:  # pragma: no cover - defensive against API drift
        log.debug("could not set yfinance network config: %s", e)


def refresh_actions(
    symbols: list[str] | None = None,
    *,
    max_workers: int = 4,
    progress_cb=None,
    retry_failed: bool = True,
    today: date | None = None,
    park_threshold: int = PARK_THRESHOLD,
    reprobe_days: int = REPROBE_DAYS,
    rate_per_sec: float = DEFAULT_RATE_PER_SEC,
    fresh_days: int = FRESH_DAYS,
) -> RefreshResult:
    """Pull yfinance actions for `symbols` (default: recently active equities).

    Pipeline per run:
      1. Universe = active NSE equities, minus ETFs/funds (deny-list), symbols
         parked in yf_coverage (unless due for reprobe), and symbols fetched
         successfully within `fresh_days` (freshness cache).
      2. Main threaded pass fetches splits + dividends per symbol, paced by a
         global `rate_per_sec` limiter to stay under Yahoo's throttle.
      3. Gentle retry pass (half the workers) re-attempts every failure once —
         recovers live stocks that merely hit a transient rate-limit.
      4. Outcomes persist to yf_coverage so repeat no-data symbols self-park and
         successful fetches feed the freshness cache.

    Writes per-symbol parquet + upserts into adj_events. Degrades gracefully
    per-symbol: Yahoo 404 / timeout → `gaps` list. Passing an explicit `symbols`
    list bypasses all skip filters (parked + freshness) — you asked for them.
    """
    ensure_dirs()
    today = today or date.today()
    _configure_yfinance()
    limiter = _RateLimiter(rate_per_sec)

    with db() as con:
        if symbols is not None:
            syms = symbols
            skipped_parked = skipped_fresh = 0
        else:
            active = _active_equity_from_con(con, today=today)
            active_set = set(active)
            fresh = _fresh_skip_set(con, today, fresh_days) & active_set
            parked = _parked_skip_set(con, today, reprobe_days) & active_set
            skip = fresh | parked
            syms = [s for s in active if s not in skip]
            skipped_fresh = len(fresh)
            skipped_parked = len(parked - fresh)

        result = RefreshResult(
            total=len(syms),
            skipped_parked=skipped_parked,
            skipped_fresh=skipped_fresh,
        )
        if not syms:
            return result

        outcomes: dict[str, str] = {}
        err_msg: dict[str, str] = {}

        # --- main pass (threaded, rate-limited) ---
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, s, limiter=limiter): s for s in syms}
            done = 0
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                status = _consume(fut, sym, con, result, outcomes, err_msg)
                if progress_cb:
                    progress_cb(done, len(syms), sym, status)

        # --- retry pass (gentle) — rescue transient rate-limit failures ---
        # Only real misses ('no_data'/'error') retry; 'ok' and 'no_actions' are
        # already definitive answers.
        failed = [s for s in syms if outcomes.get(s) not in ("ok", "no_actions")]
        if retry_failed and failed:
            retry_workers = max(1, max_workers // 2)
            with ThreadPoolExecutor(max_workers=retry_workers) as pool:
                futures = {pool.submit(_fetch_one, s, limiter=limiter): s for s in failed}
                done = 0
                for fut in as_completed(futures):
                    sym = futures[fut]
                    done += 1
                    status = _consume(fut, sym, con, result, outcomes, err_msg)
                    if status == "ok":
                        result.recovered += 1
                    if progress_cb:
                        progress_cb(done, len(failed), sym, f"retry:{status}")

        # --- tally no_actions / no_data / errors / gaps from FINAL outcomes ---
        for sym in syms:
            status = outcomes.get(sym, "no_data")
            if status == "ok":
                continue
            if status == "no_actions":
                result.no_actions += 1
            elif status == "error":
                result.errors += 1
                result.gaps.append(f"{sym}:{err_msg.get(sym, 'error')}")
            else:
                result.no_data += 1
                result.gaps.append(f"{sym}:no_data")

        # --- persist coverage so repeat-failures self-park next run ---
        _update_coverage(con, outcomes, today, park_threshold=park_threshold)

    try:
        export_all()
    except Exception as e:
        log.warning("export_all failed after actions refresh: %s", e)
    return result


def compute_adj_factor(symbol: str) -> pd.DataFrame:
    """Return a DataFrame of (date, factor) that multiplies the raw close to
    yield the split-adjusted close. Reverse-cumulative over splits.

    factor(d) = product of (1/split_ratio) for all splits with event_date > d
    So earlier dates get divided down, matching modern share counts.
    """
    with db(read_only=True) as con:
        rows = con.execute(
            """
            SELECT event_date, ratio
              FROM adj_events
             WHERE symbol = ? AND kind = 'split'
             ORDER BY event_date
            """,
            [symbol],
        ).fetchall()
    if not rows:
        return pd.DataFrame({"date": [], "factor": []})
    df = pd.DataFrame(rows, columns=["event_date", "ratio"])
    # factor just before each split = product of all later splits' 1/ratio
    df["after_split_factor"] = (1.0 / df["ratio"]).iloc[::-1].cumprod().iloc[::-1]
    return df[["event_date", "after_split_factor"]]
