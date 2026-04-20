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

log = logging.getLogger(__name__)


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
    gaps: list[str] = field(default_factory=list)


def _list_symbols(lookback_days: int = 365, min_days_seen: int = 20) -> list[str]:
    """Return symbols we should fetch actions for — recently active NSE EQ."""
    cutoff = date.today() - timedelta(days=lookback_days)
    with db(read_only=True) as con:
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
    return [r[0] for r in rows]


def _fetch_one(symbol: str, *, sleep_s: float = 0.25) -> tuple[ActionsStats, pd.DataFrame | None]:
    """Pull splits + dividends for one NSE symbol via yfinance."""
    stats = ActionsStats(symbol=symbol)
    try:
        tkr = yf.Ticker(f"{symbol}.NS")
        actions = tkr.actions
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


def refresh_actions(
    symbols: list[str] | None = None,
    *,
    max_workers: int = 4,
    progress_cb=None,
) -> RefreshResult:
    """Pull yfinance actions for `symbols` (default: recently active universe).

    Writes per-symbol parquet + upserts into adj_events. Degrades gracefully
    per-symbol: Yahoo 404 / timeout → `gaps` list.
    """
    ensure_dirs()
    syms = symbols if symbols is not None else _list_symbols()
    result = RefreshResult(total=len(syms))
    if not syms:
        return result

    with db() as con, ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in syms}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                stats, df = fut.result()
            except Exception as e:
                result.errors += 1
                result.gaps.append(f"{sym}:exec:{type(e).__name__}")
                if progress_cb:
                    progress_cb(done, len(syms), sym, "error")
                continue
            if stats.status == "ok" and df is not None:
                _write_symbol_parquet(sym, df)
                _upsert_events(con, df)
                result.ok += 1
                result.splits += stats.splits
                result.dividends += stats.dividends
            elif stats.status == "no_data":
                result.no_data += 1
                result.gaps.append(f"{sym}:no_data")
            else:
                result.errors += 1
                result.gaps.append(f"{sym}:{stats.error or 'error'}")
            if progress_cb:
                progress_cb(done, len(syms), sym, stats.status)
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
