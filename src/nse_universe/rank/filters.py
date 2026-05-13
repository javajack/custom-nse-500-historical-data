"""Filter primitives for momentum universe v2.

Pure SQL-driven metric computation. All windows are strictly point-in-time:
data with `date < as_of_date` only. Compute once per as_of_date and let v2.py
orchestrate filter application.

Two public entry points:
  * compute_per_symbol_filter_metrics(as_of_date) - returns metrics per symbol
  * behavioral_surveillance_stage(as_of_date)      - GSM/ASM proxy stage 0..3

Both open a read-only DuckDB connection internally; v2.py reuses them under
its own write transaction by passing through.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import duckdb

from nse_universe.core.db import db


WINDOW_60D_CAL = 95   # ~60 trading days fit inside 95 calendar days
WINDOW_126D_CAL = 200
LOWER_CIRCUIT_STREAK_THRESHOLD = 5


_METRICS_SQL = """
WITH bhav_window AS (
    SELECT symbol, date, close, prev_close, high, low, volume, turnover,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn_desc
      FROM bhav_daily
     WHERE date < ?
       AND date >= ?
),
last60 AS (
    SELECT * FROM bhav_window WHERE rn_desc <= 60
),
last126 AS (
    SELECT * FROM bhav_window WHERE rn_desc <= 126
),
stats60 AS (
    SELECT symbol,
           COUNT(*)                                            AS days_traded_60,
           MEDIAN(turnover)                                    AS med_turnover_60d,
           STDDEV_POP(LN(close / NULLIF(prev_close, 0))) * SQRT(252)
                                                               AS vol_annualized_60d,
           SUM(CASE
                 WHEN high = low AND volume > 0
                  AND prev_close IS NOT NULL
                  AND close <> prev_close
               THEN 1 ELSE 0 END)                              AS circuit_days_60
      FROM last60
     GROUP BY symbol
),
stats126 AS (
    SELECT symbol,
           MEDIAN(turnover)         AS med_turnover_126d,
           AVG(turnover)            AS mean_turnover_126d,
           STDDEV_POP(turnover)     AS sd_turnover_126d
      FROM last126
     GROUP BY symbol
),
history AS (
    SELECT symbol, COUNT(*) AS trading_days_history
      FROM bhav_daily
     WHERE date < ?
     GROUP BY symbol
),
latest_close AS (
    SELECT symbol, close AS close_asof
      FROM bhav_window
     WHERE rn_desc = 1
)
SELECT h.symbol,
       h.trading_days_history,
       COALESCE(s60.days_traded_60, 0)                  AS days_traded_60,
       s60.med_turnover_60d,
       s126.med_turnover_126d,
       CASE WHEN s126.mean_turnover_126d > 0
            THEN s126.sd_turnover_126d / s126.mean_turnover_126d
            ELSE NULL END                               AS cv_turnover_126d,
       COALESCE(s60.circuit_days_60, 0)                 AS circuit_days_60,
       COALESCE(s60.vol_annualized_60d, 0)              AS vol_annualized_60d,
       lc.close_asof
  FROM history h
  LEFT JOIN stats60      s60  ON s60.symbol  = h.symbol
  LEFT JOIN stats126     s126 ON s126.symbol = h.symbol
  LEFT JOIN latest_close lc   ON lc.symbol   = h.symbol
"""


_PROXY_SQL = """
WITH bhav_window AS (
    SELECT symbol, date, close, prev_close, high, low, volume, turnover,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn_desc
      FROM bhav_daily
     WHERE date < ?
       AND date >= ?
),
last60 AS (
    SELECT *,
           (high = low AND volume > 0
            AND prev_close IS NOT NULL
            AND close < prev_close) AS is_lower_circuit,
           (high = low AND volume > 0
            AND prev_close IS NOT NULL
            AND close <> prev_close) AS is_circuit
      FROM bhav_window
     WHERE rn_desc <= 60
),
agg AS (
    SELECT symbol,
           COUNT(*)::DOUBLE                              AS n_days,
           SUM(CASE WHEN is_circuit THEN 1 ELSE 0 END)::DOUBLE AS circuit_days,
           STDDEV_POP(LN(close / NULLIF(prev_close, 0))) * SQRT(252) AS vol_a,
           MAX(close)                                    AS hi60,
           MEDIAN(CASE WHEN rn_desc <= 30 THEN turnover END) AS med_t_30_recent,
           MEDIAN(CASE WHEN rn_desc > 30 AND rn_desc <= 60 THEN turnover END) AS med_t_30_prior
      FROM last60
     GROUP BY symbol
),
close_latest AS (
    SELECT symbol, close AS close_last
      FROM last60
     WHERE rn_desc = 1
),
streak_calc AS (
    -- Detect ≥5 consecutive lower-circuit days using a gaps-and-islands pattern.
    SELECT symbol, COUNT(*) AS streak_len
      FROM (
        SELECT symbol, rn_desc, is_lower_circuit,
               rn_desc - ROW_NUMBER() OVER (
                   PARTITION BY symbol, is_lower_circuit
                   ORDER BY rn_desc
               ) AS grp
          FROM last60
         WHERE is_lower_circuit
      )
     GROUP BY symbol, grp
)
SELECT a.symbol, a.n_days, a.circuit_days, a.vol_a, a.hi60, c.close_last,
       a.med_t_30_recent, a.med_t_30_prior,
       COALESCE((SELECT MAX(streak_len) FROM streak_calc s WHERE s.symbol = a.symbol), 0)
           AS max_lower_streak
  FROM agg a
  LEFT JOIN close_latest c ON c.symbol = a.symbol
"""


def _metrics_from_con(
    con: duckdb.DuckDBPyConnection, as_of_date: date
) -> dict[str, dict]:
    window_126 = as_of_date - timedelta(days=WINDOW_126D_CAL)
    rows = con.execute(_METRICS_SQL, [as_of_date, window_126, as_of_date]).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        (sym, hist, days60, med60, med126, cv, circ_d, vol_a, close_a) = r
        days60_f = float(days60 or 0)
        traded_pct = (days60_f / 60.0) if days60_f else 0.0
        circuit_pct = ((circ_d or 0) / days60_f) if days60_f else 0.0
        out[sym] = {
            "trading_days_history": int(hist) if hist is not None else 0,
            "med_turnover_60d":  float(med60) if med60 is not None else None,
            "med_turnover_126d": float(med126) if med126 is not None else None,
            "traded_pct_60d":    float(traded_pct),
            "cv_turnover_126d":  float(cv) if cv is not None else None,
            "circuit_pct_60d":   float(circuit_pct),
            "vol_annualized_60d": float(vol_a) if vol_a is not None else None,
            "close_asof":        float(close_a) if close_a is not None else None,
        }
    return out


def compute_per_symbol_filter_metrics(*, as_of_date: date) -> dict[str, dict]:
    """Return {symbol: {metric_name: value}} for every symbol with any history
    strictly before as_of_date.

    Metrics:
      - med_turnover_60d, med_turnover_126d  (₹ daily)
      - traded_pct_60d  (count of traded days / 60)
      - trading_days_history  (all-time count of dates < as_of_date)
      - close_asof  (most recent close strictly < as_of_date)
      - cv_turnover_126d  (stddev / mean of daily turnover over last 126d)
      - circuit_pct_60d  (fraction of last 60d that were single-print circuits)
      - vol_annualized_60d  (stdev of log returns × sqrt(252))
    """
    with db(read_only=True) as con:
        return _metrics_from_con(con, as_of_date)


def _proxy_from_con(
    con: duckdb.DuckDBPyConnection, as_of_date: date
) -> dict[str, int]:
    window_60 = as_of_date - timedelta(days=WINDOW_60D_CAL)
    rows = con.execute(_PROXY_SQL, [as_of_date, window_60]).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        (sym, n_days, circuit_d, vol_a, hi60, close_last,
         med_recent, med_prior, max_streak) = r
        n_days = float(n_days or 0)
        if n_days < 1:
            out[sym] = 0
            continue
        circuit_pct = (circuit_d or 0) / n_days
        dd = (1.0 - (close_last / hi60)) if (hi60 and close_last) else 0.0
        stage = 0
        if circuit_pct > 0.30:
            stage += 1
        if (max_streak or 0) >= LOWER_CIRCUIT_STREAK_THRESHOLD:
            stage += 1
        if (vol_a or 0) > 0.90 and dd > 0.50:
            stage += 1
        if med_prior and med_prior > 0 and (med_recent or 0) / med_prior < 0.30:
            stage += 1
        out[sym] = min(stage, 3)
    return out


def behavioral_surveillance_stage(*, as_of_date: date) -> dict[str, int]:
    """Heuristic GSM/ASM proxy stage 0..3 from bhav_daily only.

    Stage = sum of up to 4 red flags over last 60 trading days:
      a) circuit_pct_60d > 30%
      b) ≥ 5 consecutive lower-circuit days
      c) vol_annualized_60d > 90% AND drawdown_from_60d_high > 50%
      d) median turnover dropped > 70% comparing last 30d vs prior 30d

    Stage = min(sum, 3).
    """
    with db(read_only=True) as con:
        return _proxy_from_con(con, as_of_date)
