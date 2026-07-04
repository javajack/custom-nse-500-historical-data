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


@dataclass
class ActionsStats:
    symbol: str
    splits: int = 0
    dividends: int = 0
    status: str = "ok"       # ok | no_data | error
    error: str | None = None


@dataclass
class RefreshResult:
    total: int = 0
    ok: int = 0
    no_data: int = 0
    errors: int = 0
    splits: int = 0
    dividends: int = 0
    recovered: int = 0          # failures rescued by the gentle retry pass
    skipped_parked: int = 0     # symbols skipped via the yf_coverage cache
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

    'ok' resets the streak and un-parks; anything else ('no_data' / 'error')
    increments the consecutive-miss counter and parks at `park_threshold`.
    """
    if not outcomes:
        return
    prior = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "SELECT symbol, consecutive_no_data, last_ok FROM yf_coverage"
        ).fetchall()
    }
    rows = []
    for sym, status in outcomes.items():
        prev_streak, prev_last_ok = prior.get(sym, (0, None))
        if status == "ok":
            rows.append((sym, "ok", 0, False, today, today))
        else:
            streak = prev_streak + 1
            rows.append((sym, "no_data", streak, streak >= park_threshold,
                         today, prev_last_ok))
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


def _fetch_one(symbol: str, *, sleep_s: float = 0.25) -> tuple[ActionsStats, pd.DataFrame | None]:
    """Pull splits + dividends for one NSE symbol via yfinance."""
    stats = ActionsStats(symbol=symbol)
    try:
        tkr = yf.Ticker(f"{symbol}.NS")
        actions = tkr.actions
    except AttributeError as e:
        # yfinance 1.3.x raises "'PriceHistory' object has no attribute
        # '_dividends'" when the underlying price history came back empty —
        # a Yahoo throttle transient or a genuinely delisted symbol. That's
        # "no usable data", not an unexpected error: classify as no_data so it
        # flows through the retry pass and the yf_coverage cache cleanly instead
        # of polluting the gaps list with a stack-trace string.
        if "_dividends" in str(e) or "_splits" in str(e):
            stats.status = "no_data"
        else:
            stats.status = "error"
            stats.error = str(e)[:200]
        return stats, None
    except Exception as e:
        stats.status = "error"
        stats.error = str(e)[:200]
        return stats, None
    finally:
        time.sleep(sleep_s)

    if actions is None or actions.empty:
        stats.status = "no_data"
        return stats, None

    # tidy: flatten to (event_date, kind, ratio) rows
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
        stats.status = "no_data"
        return stats, None

    df = pd.concat(frames)
    df = df.reset_index().rename(columns={"Date": "event_date"})
    # Normalize timestamp → date
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    df["symbol"] = symbol
    df["source"] = "yfinance"
    df = df[["symbol", "event_date", "kind", "ratio", "source"]]
    df = df.sort_values(["event_date", "kind"]).reset_index(drop=True)

    stats.splits = int((df["kind"] == "split").sum())
    stats.dividends = int((df["kind"] == "dividend").sum())
    return stats, df


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
    if stats.status == "no_data":
        outcomes[sym] = "no_data"
        return "no_data"
    outcomes[sym] = "error"
    err_msg[sym] = stats.error or "error"
    return "error"


def refresh_actions(
    symbols: list[str] | None = None,
    *,
    max_workers: int = 4,
    progress_cb=None,
    retry_failed: bool = True,
    today: date | None = None,
    park_threshold: int = PARK_THRESHOLD,
    reprobe_days: int = REPROBE_DAYS,
) -> RefreshResult:
    """Pull yfinance actions for `symbols` (default: recently active equities).

    Pipeline per run:
      1. Universe = active NSE equities, minus ETFs/funds (deny-list) and
         symbols parked in the yf_coverage cache (unless due for reprobe).
      2. Main threaded pass fetches splits + dividends per symbol.
      3. Gentle retry pass (half the workers, longer sleep) re-attempts every
         failure once — recovers live stocks that merely hit a Yahoo rate-limit
         ("no timezone found") rather than being genuinely delisted.
      4. Outcomes persist to yf_coverage so repeat no-data symbols self-park.

    Writes per-symbol parquet + upserts into adj_events. Degrades gracefully
    per-symbol: Yahoo 404 / timeout → `gaps` list.
    """
    ensure_dirs()
    today = today or date.today()

    with db() as con:
        if symbols is not None:
            syms = symbols
            skipped_parked = 0
        else:
            active = _active_equity_from_con(con, today=today)
            parked = _parked_skip_set(con, today, reprobe_days)
            syms = [s for s in active if s not in parked]
            skipped_parked = len(active) - len(syms)

        result = RefreshResult(total=len(syms), skipped_parked=skipped_parked)
        if not syms:
            return result

        outcomes: dict[str, str] = {}
        err_msg: dict[str, str] = {}

        # --- main pass (threaded) ---
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, s): s for s in syms}
            done = 0
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                status = _consume(fut, sym, con, result, outcomes, err_msg)
                if progress_cb:
                    progress_cb(done, len(syms), sym, status)

        # --- retry pass (gentle) — rescue transient rate-limit failures ---
        failed = [s for s in syms if outcomes.get(s) != "ok"]
        if retry_failed and failed:
            retry_workers = max(1, max_workers // 2)
            with ThreadPoolExecutor(max_workers=retry_workers) as pool:
                futures = {pool.submit(_fetch_one, s, sleep_s=0.75): s for s in failed}
                done = 0
                for fut in as_completed(futures):
                    sym = futures[fut]
                    done += 1
                    status = _consume(fut, sym, con, result, outcomes, err_msg)
                    if status == "ok":
                        result.recovered += 1
                    if progress_cb:
                        progress_cb(done, len(failed), sym, f"retry:{status}")

        # --- tally no_data / errors / gaps from FINAL outcomes ---
        for sym in syms:
            status = outcomes.get(sym, "no_data")
            if status == "ok":
                continue
            if status == "error":
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
