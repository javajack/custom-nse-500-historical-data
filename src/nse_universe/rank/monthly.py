"""Monthly ranker.

On each "as_of_date" (first trading day of a month), compute a rank per
symbol by median daily turnover over the prior 126 trading days. Store the
top K (default 1000) to `universe_rank`.

IPO eligibility: a stock needs ≥126 actual trading days of history in the
lookback window before it can be ranked. This prevents 10-day-old IPOs with
crazy turnover from jumping into any custom index.

Rebalance cadence: monthly. Membership persists between as_of_dates — query
for any trading day D uses the most recent as_of_date ≤ D.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from nse_universe.core.db import db
from nse_universe.core.export import export_all
from nse_universe.core.state import mark_rank_computed

log = logging.getLogger(__name__)

LOOKBACK_TRADING_DAYS = 126  # ≈ 6 months
DEFAULT_TOP_K = 1000         # headroom beyond the widest custom index (nifty_500)


@dataclass
class RankBatchStats:
    as_of_dates: int = 0
    total_rows: int = 0


def _first_trading_days(con) -> list[date]:
    """Return first trading day of each (year, month) present in bhav_daily."""
    rows = con.execute(
        """
        SELECT MIN(date) AS d
          FROM bhav_daily
         GROUP BY year, month
         ORDER BY d
        """
    ).fetchall()
    return [r[0] for r in rows]


def _compute_rank_for(con, as_of_date: date, top_k: int) -> int:
    """Compute + upsert universe_rank for one as_of_date. Returns rows written."""
    window_start = as_of_date - timedelta(days=260)  # generous — ≥126 trading days fit inside
    con.execute("DELETE FROM universe_rank WHERE as_of_date = ?", [as_of_date])
    # One SQL: pick the last 126 trading days per symbol strictly before as_of_date,
    # compute median turnover, rank desc, keep top K.
    res = con.execute(
        """
        WITH window_rows AS (
            SELECT symbol, turnover, date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
              FROM bhav_daily
             WHERE date < ?
               AND date >= ?
               AND turnover IS NOT NULL
        ),
        last126 AS (
            SELECT symbol, turnover FROM window_rows WHERE rn <= ?
        ),
        eligible AS (
            SELECT symbol,
                   COUNT(*)      AS n_days,
                   MEDIAN(turnover) AS med_turnover
              FROM last126
             GROUP BY symbol
            HAVING COUNT(*) >= ?
        ),
        ranked AS (
            SELECT symbol,
                   med_turnover,
                   ROW_NUMBER() OVER (ORDER BY med_turnover DESC, symbol) AS rnk
              FROM eligible
        )
        INSERT INTO universe_rank(as_of_date, symbol, rank, metric_value, metric_kind)
        SELECT ?, symbol, rnk, med_turnover, 'turnover_median_126d'
          FROM ranked
         WHERE rnk <= ?
        """,
        [as_of_date, window_start, LOOKBACK_TRADING_DAYS, LOOKBACK_TRADING_DAYS, as_of_date, top_k],
    )
    # DuckDB's INSERT doesn't always return count in res.fetchall(); re-query
    n = con.execute(
        "SELECT COUNT(*) FROM universe_rank WHERE as_of_date = ?", [as_of_date]
    ).fetchone()[0]
    return int(n)


def recompute_all(*, top_k: int = DEFAULT_TOP_K, force: bool = False, progress_cb=None) -> RankBatchStats:
    """Compute universe_rank for every first-trading-day that isn't yet done.

    Skips as_of_dates that already have rows (unless force=True). Safe to
    interrupt — each as_of_date is transactional (delete-then-insert).
    """
    stats = RankBatchStats()
    with db() as con:
        candidates = _first_trading_days(con)
        if len(candidates) == 0:
            return stats
        # Must have at least LOOKBACK_TRADING_DAYS of history before an as_of_date
        # is rankable, so skip the first few months.
        first_viable_idx = 0
        for i, d in enumerate(candidates):
            prior = con.execute(
                "SELECT COUNT(DISTINCT date) FROM bhav_daily WHERE date < ?", [d]
            ).fetchone()[0]
            if prior >= LOOKBACK_TRADING_DAYS:
                first_viable_idx = i
                break
        viable = candidates[first_viable_idx:]

        existing: set[date] = set()
        if not force:
            existing = {
                r[0] for r in con.execute(
                    "SELECT DISTINCT as_of_date FROM universe_rank"
                ).fetchall()
            }

        todo = [d for d in viable if d not in existing] if not force else viable
        for i, d in enumerate(todo):
            n = _compute_rank_for(con, d, top_k)
            stats.as_of_dates += 1
            stats.total_rows += n
            if progress_cb:
                progress_cb(i + 1, len(todo), d, n)
    mark_rank_computed()
    # Export to committed parquet so CI builds + fresh clones have the data
    # without needing the DuckDB file.
    try:
        export_all()
    except Exception as e:
        log.warning("export_all failed after rank recompute: %s", e)
    return stats


def rank_asof_for(query_date: date) -> date | None:
    """Return the most recent as_of_date ≤ query_date (or None)."""
    with db(read_only=True) as con:
        row = con.execute(
            "SELECT MAX(as_of_date) FROM universe_rank WHERE as_of_date <= ?",
            [query_date],
        ).fetchone()
    return row[0] if row and row[0] else None
