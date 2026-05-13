"""Tests for v2 filter primitives.

Two layers:
- Pure-SQL tests against an in-memory DuckDB with hand-crafted bhav rows.
- Smoke check against the real DB (skipped if no data).
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from nse_universe.rank.filters import _metrics_from_con, _proxy_from_con


def _fresh_con():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE bhav_daily (
            symbol     VARCHAR,
            date       DATE,
            open       DOUBLE,
            high       DOUBLE,
            low        DOUBLE,
            close      DOUBLE,
            prev_close DOUBLE,
            volume     BIGINT,
            turnover   DOUBLE,
            trades     INTEGER,
            year       INTEGER,
            month      INTEGER
        )
    """)
    return con


def _insert(con, symbol, *, base_date, n_days,
            close=200.0, prev_close=200.0,
            high=None, low=None, turnover=5_00_00_000,
            volume=10_000):
    """Insert n_days of weekday rows starting at base_date for one symbol."""
    rows = []
    d = base_date
    while len(rows) < n_days:
        if d.weekday() < 5:
            h = close if high is None else high
            l = close if low is None else low
            rows.append((symbol, d, close, h, l, close, prev_close,
                         volume, turnover, 10, d.year, d.month))
        d += timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def test_metrics_clean_liquid_symbol():
    con = _fresh_con()
    _insert(con, "LIQUID", base_date=date(2023, 1, 2), n_days=252)
    as_of = date(2024, 1, 1)
    m = _metrics_from_con(con, as_of)
    assert "LIQUID" in m
    row = m["LIQUID"]
    assert row["trading_days_history"] >= 252
    assert row["med_turnover_60d"] == pytest.approx(5_00_00_000)
    assert row["med_turnover_126d"] == pytest.approx(5_00_00_000)
    assert row["traded_pct_60d"] == pytest.approx(1.0)
    assert row["close_asof"] == pytest.approx(200.0)
    # zero variance ⇒ CV is 0 (or None if mean is computed weirdly)
    cv = row["cv_turnover_126d"]
    assert cv is None or cv < 0.01
    # zero log-returns ⇒ vol == 0
    assert row["vol_annualized_60d"] == pytest.approx(0.0)
    # no single-print rows ⇒ no circuits
    assert row["circuit_pct_60d"] == pytest.approx(0.0)


def test_metrics_circuit_hit_detection():
    """Insert a symbol that has high == low (circuit) on alternate days."""
    con = _fresh_con()
    base = date(2023, 11, 1)
    rows = []
    d = base
    flip = 0
    while len(rows) < 60:
        if d.weekday() < 5:
            is_circuit = (flip % 2 == 0)
            if is_circuit:
                # high == low, close changed vs prev → single-print circuit
                rows.append(("CIRCUIT", d, 105.0, 105.0, 105.0, 105.0, 100.0,
                             1000, 1_00_00_000, 5, d.year, d.month))
            else:
                # normal range day
                rows.append(("CIRCUIT", d, 100.0, 102.0, 99.0, 100.0, 100.0,
                             1000, 1_00_00_000, 5, d.year, d.month))
            flip += 1
        d += timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = max(r[1] for r in rows) + timedelta(days=1)
    m = _metrics_from_con(con, as_of)
    # ~50% of days were circuits
    assert m["CIRCUIT"]["circuit_pct_60d"] == pytest.approx(0.5, abs=0.1)


def test_metrics_low_traded_pct():
    """A sparse-trading symbol gets traded_pct < 1."""
    con = _fresh_con()
    # Insert 30 rows over 252-day window (very sparse).
    base = date(2023, 1, 2)
    rows = []
    d = base
    inserted = 0
    while inserted < 30:
        if d.weekday() < 5:
            rows.append(("SPARSE", d, 100.0, 100.0, 100.0, 100.0, 100.0,
                         1000, 1_00_00_000, 5, d.year, d.month))
            inserted += 1
        d += timedelta(days=4)  # skip several weekdays
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = max(r[1] for r in rows) + timedelta(days=1)
    m = _metrics_from_con(con, as_of)
    # only 30 days exist in the bhav, traded_pct = 30/60 = 0.5
    assert m["SPARSE"]["traded_pct_60d"] == pytest.approx(0.5)


def test_proxy_clean_symbol_stage_zero():
    con = _fresh_con()
    _insert(con, "CLEAN", base_date=date(2023, 1, 2), n_days=252)
    as_of = date(2024, 1, 1)
    stages = _proxy_from_con(con, as_of)
    assert stages["CLEAN"] == 0


def test_proxy_circuit_heavy_symbol_stage_at_least_one():
    """A symbol with > 30% circuit days flags as stage ≥ 1."""
    con = _fresh_con()
    base = date(2023, 11, 1)
    rows = []
    d = base
    flip = 0
    while len(rows) < 60:
        if d.weekday() < 5:
            if (flip % 2) == 0:
                # circuit day (high == low, close ≠ prev_close)
                rows.append(("ZOOM", d, 110.0, 110.0, 110.0, 110.0, 100.0,
                             500, 50_00_000, 1, d.year, d.month))
            else:
                rows.append(("ZOOM", d, 100.0, 102.0, 99.0, 100.0, 100.0,
                             500, 50_00_000, 1, d.year, d.month))
            flip += 1
        d += timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = max(r[1] for r in rows) + timedelta(days=1)
    stages = _proxy_from_con(con, as_of)
    assert stages["ZOOM"] >= 1, f"expected ≥1, got {stages['ZOOM']}"


def test_proxy_lower_circuit_streak_flag():
    """5 consecutive lower-circuit days flags as a stage."""
    con = _fresh_con()
    base = date(2023, 11, 1)
    rows = []
    d = base
    inserted = 0
    # First 5 weekdays: lower circuits (cascading down 5% each day)
    prev = 100.0
    while inserted < 5:
        if d.weekday() < 5:
            close = prev * 0.95
            rows.append(("CRASH", d, close, close, close, close, prev,
                         500, 50_00_000, 1, d.year, d.month))
            prev = close
            inserted += 1
        d += timedelta(days=1)
    # Pad with normal days so total is 60
    while inserted < 60:
        if d.weekday() < 5:
            rows.append(("CRASH", d, prev, prev * 1.02, prev * 0.99,
                         prev, prev, 500, 50_00_000, 1, d.year, d.month))
            inserted += 1
        d += timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = max(r[1] for r in rows) + timedelta(days=1)
    stages = _proxy_from_con(con, as_of)
    assert stages["CRASH"] >= 1


def test_smoke_against_real_bhav():
    """Sanity check: filters run against the real bhav_daily without crashing.

    Skipped if no data is present (fresh checkout, CI bootstrap, etc.).
    """
    from nse_universe.core.db import db
    with db(read_only=True) as con:
        n = con.execute("SELECT COUNT(*) FROM bhav_daily").fetchone()[0]
        if n == 0:
            pytest.skip("no bhav_daily rows — run ingest first")
        max_date = con.execute("SELECT MAX(date) FROM bhav_daily").fetchone()[0]
        # use a date inside the data range so the function has history
        as_of = max_date
        m = _metrics_from_con(con, as_of)
        s = _proxy_from_con(con, as_of)
    assert isinstance(m, dict)
    assert isinstance(s, dict)
    assert len(m) > 0
