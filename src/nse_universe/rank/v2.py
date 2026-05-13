"""Momentum universe v2 builder.

Parallel to monthly.py but heavier — applies a filter stack instead of just
ranking by turnover. The v2 table is consumed by momentum strategies that
want a pre-trash-filtered candidate pool.

Pipeline per as_of_date:
  1. Compute per-symbol filter metrics over the lookback windows.
  2. Compute behavioural surveillance proxy stages from bhav_daily.
  3. Upsert proxy stages into surveillance_daily (audit trail).
  4. Look up final GSM/ASM stages (prefer 'nse_live' over 'behavioral_proxy').
  5. Apply filter stack; record exclude_reason for fails.
  6. Rank survivors by med_turnover_126d desc; keep top K.
  7. Upsert all symbols (passers AND failers) into universe_v2 for audit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from nse_universe.core.db import db
from nse_universe.core.export import export_all
from nse_universe.rank.deny import is_non_equity
from nse_universe.rank.filters import _metrics_from_con, _proxy_from_con

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class V2Config:
    """Threshold parameters for momentum universe v2.

    Defaults locked at design time (2026-05-13) for ₹20-80L AUM positional
    momentum strategy. Tune via auto-tune loop in fortress backtest if the
    13-yr CAGR baseline regresses.
    """
    min_trading_days: int = 252
    min_traded_pct_60d: float = 0.95
    min_med_turnover_60d: float = 50_00_000       # ₹50L
    min_med_turnover_126d: float = 25_00_000      # ₹25L
    min_close: float = 50.0
    max_cv_turnover_126d: float = 3.0
    max_circuit_pct_60d: float = 0.05
    max_gsm_stage: int = 1   # exclude if > this (i.e. stage ≥ 2)
    max_asm_stage: int = 2   # exclude if > this (i.e. stage ≥ 3)
    vol_ceiling: float | None = None              # set e.g. 0.90 to enable
    top_k: int = 1000


DEFAULT_V2_CONFIG = V2Config()


@dataclass
class V2BatchStats:
    as_of_dates: int = 0
    total_passers: int = 0


def _exclude_reason(m: dict, gsm: int, asm: int, cfg: V2Config, *, symbol: str | None = None) -> str | None:
    """Return the first failing filter (or None if all pass)."""
    if symbol is not None and is_non_equity(symbol):
        return "non_equity"
    if (m.get("trading_days_history") or 0) < cfg.min_trading_days:
        return f"history<{cfg.min_trading_days}d"
    if (m.get("traded_pct_60d") or 0.0) < cfg.min_traded_pct_60d:
        return f"traded_pct_60d<{cfg.min_traded_pct_60d}"
    if (m.get("med_turnover_60d") or 0.0) < cfg.min_med_turnover_60d:
        return f"med_turnover_60d<{cfg.min_med_turnover_60d:.0f}"
    if (m.get("med_turnover_126d") or 0.0) < cfg.min_med_turnover_126d:
        return f"med_turnover_126d<{cfg.min_med_turnover_126d:.0f}"
    if (m.get("close_asof") or 0.0) < cfg.min_close:
        return f"close_asof<{cfg.min_close}"
    cv = m.get("cv_turnover_126d")
    if cv is not None and cv > cfg.max_cv_turnover_126d:
        return f"cv_turnover_126d>{cfg.max_cv_turnover_126d}"
    if (m.get("circuit_pct_60d") or 0.0) > cfg.max_circuit_pct_60d:
        return f"circuit_pct_60d>{cfg.max_circuit_pct_60d}"
    if gsm > cfg.max_gsm_stage:
        return f"gsm_stage>{cfg.max_gsm_stage}"
    if asm > cfg.max_asm_stage:
        return f"asm_stage>{cfg.max_asm_stage}"
    if cfg.vol_ceiling is not None:
        va = m.get("vol_annualized_60d") or 0.0
        if va > cfg.vol_ceiling:
            return f"vol_annualized_60d>{cfg.vol_ceiling}"
    return None


def _gsm_asm_for(con, as_of_date: date) -> dict[str, tuple[int, int]]:
    """{symbol: (gsm_stage, asm_stage)} as of as_of_date.

    Prefers source='nse_live' over 'behavioral_proxy' when both exist for
    the same (date, symbol). Uses the most recent record on or before
    as_of_date.
    """
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT symbol, gsm_stage, asm_stage, source,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol
                       ORDER BY CASE source WHEN 'nse_live' THEN 0 ELSE 1 END,
                                date DESC
                   ) AS rn
              FROM surveillance_daily
             WHERE date <= ?
        )
        SELECT symbol, gsm_stage, asm_stage FROM ranked WHERE rn = 1
        """,
        [as_of_date],
    ).fetchall()
    return {sym: (int(g or 0), int(a or 0)) for (sym, g, a) in rows}


def recompute_v2_for(as_of_date: date, cfg: V2Config = DEFAULT_V2_CONFIG) -> int:
    """Compute + upsert universe_v2 for one as_of_date.

    Returns the count of passing symbols written (passes = TRUE).
    """
    with db() as con:
        metrics = _metrics_from_con(con, as_of_date)
        proxy = _proxy_from_con(con, as_of_date)

        # Persist proxy stages (batched executemany — 100x faster than per-row INSERTs).
        proxy_rows = [
            (as_of_date, sym, int(stage), None, "behavioral_proxy")
            for sym, stage in proxy.items()
        ]
        if proxy_rows:
            con.executemany(
                """INSERT OR REPLACE INTO surveillance_daily
                          (date, symbol, gsm_stage, asm_stage, source)
                   VALUES (?, ?, ?, ?, ?)""",
                proxy_rows,
            )

        gsm_asm = _gsm_asm_for(con, as_of_date)

        scored: list[tuple[str, dict, int, int, str | None]] = []
        for sym, m in metrics.items():
            g, a = gsm_asm.get(sym, (0, 0))
            reason = _exclude_reason(m, g, a, cfg, symbol=sym)
            scored.append((sym, m, g, a, reason))

        survivors = [s for s in scored if s[4] is None]
        survivors.sort(
            key=lambda s: (-(s[1].get("med_turnover_126d") or 0.0), s[0])
        )
        rank_by_sym = {s[0]: i + 1 for i, s in enumerate(survivors[: cfg.top_k])}

        con.execute("DELETE FROM universe_v2 WHERE as_of_date = ?", [as_of_date])
        v2_rows = []
        for sym, m, g, a, reason in scored:
            passes = (reason is None) and (sym in rank_by_sym)
            rank = rank_by_sym.get(sym, 0)
            final_reason = reason if reason else (
                None if passes else f"rank>{cfg.top_k}"
            )
            v2_rows.append((
                as_of_date, sym, rank, passes,
                m.get("med_turnover_60d"), m.get("med_turnover_126d"),
                m.get("traded_pct_60d"), m.get("trading_days_history"),
                m.get("close_asof"), m.get("cv_turnover_126d"),
                m.get("circuit_pct_60d"), g, a,
                m.get("vol_annualized_60d"), final_reason,
            ))
        if v2_rows:
            con.executemany(
                """INSERT INTO universe_v2(
                       as_of_date, symbol, rank, passes,
                       med_turnover_60d, med_turnover_126d, traded_pct_60d,
                       trading_days_history, close_asof, cv_turnover_126d,
                       circuit_pct_60d, gsm_stage, asm_stage,
                       vol_annualized_60d, exclude_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                v2_rows,
            )

        n = sum(1 for r in v2_rows if r[3])  # r[3] = passes
    return int(n)


def _first_trading_days(con) -> list[date]:
    rows = con.execute(
        """
        SELECT MIN(date) AS d
          FROM bhav_daily
         GROUP BY year, month
         ORDER BY d
        """
    ).fetchall()
    return [r[0] for r in rows]


def recompute_v2_all(
    *,
    cfg: V2Config = DEFAULT_V2_CONFIG,
    force: bool = False,
    progress_cb=None,
) -> V2BatchStats:
    """Recompute universe_v2 for every viable first-trading-day-of-month.

    Skips dates already populated unless force=True. Exports to parquet at
    the end. Safe to interrupt — each as_of_date is transactional.
    """
    stats = V2BatchStats()
    with db() as con:
        candidates = _first_trading_days(con)
        if not candidates:
            return stats
        first_viable_idx = 0
        for i, d in enumerate(candidates):
            prior = con.execute(
                "SELECT COUNT(DISTINCT date) FROM bhav_daily WHERE date < ?", [d]
            ).fetchone()[0]
            if prior >= cfg.min_trading_days:
                first_viable_idx = i
                break
        viable = candidates[first_viable_idx:]
        existing: set[date] = set()
        if not force:
            existing = {
                r[0] for r in con.execute(
                    "SELECT DISTINCT as_of_date FROM universe_v2"
                ).fetchall()
            }
        todo = [d for d in viable if d not in existing] if not force else viable

    for i, d in enumerate(todo):
        n = recompute_v2_for(d, cfg)
        stats.as_of_dates += 1
        stats.total_passers += n
        if progress_cb:
            progress_cb(i + 1, len(todo), d, n)

    try:
        export_all()
    except Exception as e:
        log.warning("export_all failed after v2 recompute: %s", e)
    return stats
