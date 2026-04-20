"""Universe — point-in-time custom-index membership oracle.

Public library surface. All methods resolve an `as_of_date` = most recent
monthly rank snapshot ≤ the query date, then project the index window on top.

A stock is a member of index I on date D if:
  R = rank(stock, as_of=as_of_date_for(D))
  I.rank_lo <= R <= I.rank_hi

Membership is stable between as_of_dates (monthly rebalance cadence).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator, Mapping

import pandas as pd

from nse_universe.core.config import IndexSpec, load_indices
from nse_universe.core.db import db


class UnknownIndexError(KeyError):
    pass


class Universe:
    def __init__(self) -> None:
        self._indices: Mapping[str, IndexSpec] = load_indices()

    # ------------- introspection -------------

    def indices(self) -> list[str]:
        return list(self._indices.keys())

    def index_spec(self, name: str) -> IndexSpec:
        try:
            return self._indices[name]
        except KeyError as e:
            raise UnknownIndexError(f"unknown index {name!r}; have {sorted(self._indices)}") from e

    def reload_indices(self) -> None:
        """Re-read indices.yml. Useful during interactive exploration."""
        self._indices = load_indices()

    # ------------- date resolution -------------

    def as_of_for(self, d: date) -> date | None:
        with db(read_only=True) as con:
            row = con.execute(
                "SELECT MAX(as_of_date) FROM universe_rank WHERE as_of_date <= ?", [d]
            ).fetchone()
        return row[0] if row and row[0] else None

    # ------------- core queries -------------

    def members(self, d: date, index: str) -> list[str]:
        spec = self.index_spec(index)
        asof = self.as_of_for(d)
        if asof is None:
            return []
        with db(read_only=True) as con:
            rows = con.execute(
                """
                SELECT symbol
                  FROM universe_rank
                 WHERE as_of_date = ? AND rank BETWEEN ? AND ?
                 ORDER BY rank
                """,
                [asof, spec.rank_lo, spec.rank_hi],
            ).fetchall()
        return [r[0] for r in rows]

    def rank(self, symbol: str, d: date) -> int | None:
        asof = self.as_of_for(d)
        if asof is None:
            return None
        with db(read_only=True) as con:
            row = con.execute(
                "SELECT rank FROM universe_rank WHERE as_of_date = ? AND symbol = ?",
                [asof, symbol],
            ).fetchone()
        return int(row[0]) if row else None

    def is_member(self, symbol: str, d: date, index: str) -> bool:
        spec = self.index_spec(index)
        r = self.rank(symbol, d)
        return r is not None and spec.contains(r)

    def universe_at(self, d: date) -> pd.DataFrame:
        """Full ranked universe on date d (columns: rank, symbol, metric_value)."""
        asof = self.as_of_for(d)
        if asof is None:
            return pd.DataFrame(columns=["rank", "symbol", "metric_value", "as_of_date"])
        with db(read_only=True) as con:
            df = con.execute(
                """
                SELECT rank, symbol, metric_value
                  FROM universe_rank
                 WHERE as_of_date = ?
                 ORDER BY rank
                """,
                [asof],
            ).fetchdf()
        df["as_of_date"] = asof
        return df

    def members_df(self, start: date, end_inclusive: date, index: str) -> pd.DataFrame:
        """Long-form DataFrame of (date, symbol, rank) for every trading day in range.

        Uses the bhav_daily view as the calendar: row per (trading_day, member).
        Good for factor-backtest joins.
        """
        spec = self.index_spec(index)
        with db(read_only=True) as con:
            df = con.execute(
                """
                WITH days AS (
                    SELECT DISTINCT date AS trading_day
                      FROM bhav_daily
                     WHERE date BETWEEN ? AND ?
                ),
                asof_for_day AS (
                    SELECT days.trading_day,
                           (SELECT MAX(ur.as_of_date)
                              FROM universe_rank ur
                             WHERE ur.as_of_date <= days.trading_day) AS as_of_date
                      FROM days
                )
                SELECT a.trading_day AS date,
                       ur.symbol,
                       ur.rank,
                       a.as_of_date
                  FROM asof_for_day a
                  JOIN universe_rank ur ON ur.as_of_date = a.as_of_date
                 WHERE ur.rank BETWEEN ? AND ?
                 ORDER BY a.trading_day, ur.rank
                """,
                [start, end_inclusive, spec.rank_lo, spec.rank_hi],
            ).fetchdf()
        return df

    def walk(
        self,
        start: date,
        end_inclusive: date,
        index: str,
        *,
        freq: str = "D",
    ) -> Iterator[tuple[date, list[str]]]:
        """Yield (date, members) for each trading day (freq='D') or each first
        trading day of month (freq='M')."""
        if freq not in ("D", "M"):
            raise ValueError(f"freq must be 'D' or 'M', got {freq!r}")
        spec = self.index_spec(index)
        with db(read_only=True) as con:
            if freq == "D":
                dates = [r[0] for r in con.execute(
                    "SELECT DISTINCT date FROM bhav_daily WHERE date BETWEEN ? AND ? ORDER BY date",
                    [start, end_inclusive],
                ).fetchall()]
            else:
                dates = [r[0] for r in con.execute(
                    """
                    SELECT MIN(date)
                      FROM bhav_daily
                     WHERE date BETWEEN ? AND ?
                     GROUP BY year, month
                     ORDER BY 1
                    """,
                    [start, end_inclusive],
                ).fetchall()]
            # Memoize asof per unique as_of_date to avoid re-querying per day
            asof_cache: dict[date, list[str]] = {}
            for d in dates:
                asof = con.execute(
                    "SELECT MAX(as_of_date) FROM universe_rank WHERE as_of_date <= ?", [d]
                ).fetchone()[0]
                if asof is None:
                    yield d, []
                    continue
                if asof not in asof_cache:
                    rows = con.execute(
                        """
                        SELECT symbol FROM universe_rank
                         WHERE as_of_date = ? AND rank BETWEEN ? AND ?
                         ORDER BY rank
                        """,
                        [asof, spec.rank_lo, spec.rank_hi],
                    ).fetchall()
                    asof_cache[asof] = [r[0] for r in rows]
                yield d, asof_cache[asof]

    # ------------- health / stats -------------

    def health(self) -> dict:
        with db(read_only=True) as con:
            row = con.execute(
                """
                SELECT
                    (SELECT COUNT(DISTINCT date) FROM bhav_daily),
                    (SELECT MIN(date)             FROM bhav_daily),
                    (SELECT MAX(date)             FROM bhav_daily),
                    (SELECT COUNT(*)              FROM bhav_daily),
                    (SELECT COUNT(DISTINCT symbol) FROM bhav_daily),
                    (SELECT COUNT(*)              FROM non_trading_days),
                    (SELECT COUNT(DISTINCT as_of_date) FROM universe_rank),
                    (SELECT COUNT(*)              FROM adj_events),
                    (SELECT COUNT(DISTINCT symbol) FROM adj_events)
                """
            ).fetchone()
        keys = [
            "trading_days", "first_date", "last_date", "rows_bhav", "distinct_symbols",
            "non_trading_days_recorded", "rank_snapshots",
            "adj_events", "symbols_with_actions",
        ]
        return dict(zip(keys, row))
