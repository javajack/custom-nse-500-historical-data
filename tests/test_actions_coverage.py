"""Tests for the corporate-actions universe filter + persistent no-data cache.

Covers the two logic-level fixes:
  * _list_symbols_from_con  — excludes non-equity (ETF/fund) symbols and
    symbols "parked" in yf_coverage (repeated yfinance no_data), while still
    re-probing parked symbols once their reprobe window elapses.
  * _update_coverage        — parks a symbol after N consecutive no_data
    refreshes and un-parks / resets it the moment data returns.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

import nse_universe.actions.fetch as fetch
from nse_universe.actions.fetch import _list_symbols_from_con, _update_coverage


def test_fetch_one_empty_history_classified_no_data(monkeypatch):
    """yfinance 1.3.x empty-history AttributeError → no_data, not error."""
    class _EmptyHistoryTicker:
        def __init__(self, *a, **k):
            pass
        @property
        def actions(self):
            raise AttributeError("'PriceHistory' object has no attribute '_dividends'")

    monkeypatch.setattr(fetch.yf, "Ticker", _EmptyHistoryTicker)
    monkeypatch.setattr(fetch.time, "sleep", lambda *a, **k: None)
    stats, df = fetch._fetch_one("TATAMOTORS", sleep_s=0)
    assert stats.status == "no_data"
    assert stats.error is None
    assert df is None


def test_fetch_one_empty_frame_is_no_actions(monkeypatch):
    """History exists but no dividends/splits → empty frame → 'no_actions',
    distinct from the empty-history AttributeError ('no_data')."""
    import pandas as pd

    class _NoActionsTicker:
        def __init__(self, *a, **k):
            pass
        @property
        def actions(self):
            return pd.DataFrame()

    monkeypatch.setattr(fetch.yf, "Ticker", _NoActionsTicker)
    monkeypatch.setattr(fetch.time, "sleep", lambda *a, **k: None)
    stats, df = fetch._fetch_one("ADANIGREEN", sleep_s=0)
    assert stats.status == "no_actions"
    assert df is None


def test_update_coverage_no_actions_is_answered(monkeypatch):
    """no_actions is a definitive answer: stamps last_ok, resets, never parks."""
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: False)
    con = _fresh_con()
    _update_coverage(con, {"NODIV": "no_actions"}, TODAY, park_threshold=1)
    row = con.execute(
        "SELECT status, consecutive_no_data, parked, last_ok FROM yf_coverage WHERE symbol='NODIV'"
    ).fetchone()
    assert row == ("no_actions", 0, False, TODAY)


def test_fetch_one_genuine_error_stays_error(monkeypatch):
    class _BoomTicker:
        def __init__(self, *a, **k):
            pass
        @property
        def actions(self):
            raise ValueError("network exploded")

    monkeypatch.setattr(fetch.yf, "Ticker", _BoomTicker)
    monkeypatch.setattr(fetch.time, "sleep", lambda *a, **k: None)
    stats, df = fetch._fetch_one("X", sleep_s=0)
    assert stats.status == "error"
    assert "network exploded" in stats.error


def _fresh_con():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE bhav_daily (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, prev_close DOUBLE, volume BIGINT, turnover DOUBLE,
            trades INTEGER, year INTEGER, month INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE yf_coverage (
            symbol              VARCHAR PRIMARY KEY,
            status              VARCHAR NOT NULL,
            consecutive_no_data INTEGER NOT NULL DEFAULT 0,
            parked              BOOLEAN NOT NULL DEFAULT FALSE,
            last_checked        DATE,
            last_ok             DATE
        )
    """)
    return con


def _insert_active(con, symbol, *, end_date, n_days=30):
    """Insert n_days of recent weekday rows ending at end_date for one symbol."""
    rows, d, added = [], end_date, 0
    while added < n_days:
        if d.weekday() < 5:
            rows.append((symbol, d, 100.0, 100.0, 100.0, 100.0, 100.0,
                         1000, 1_00_00_000, 5, d.year, d.month))
            added += 1
        d -= timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


TODAY = date(2026, 7, 1)


def test_list_excludes_non_equity():
    con = _fresh_con()
    _insert_active(con, "RELIANCE", end_date=TODAY)
    _insert_active(con, "GOLDADD", end_date=TODAY)   # ETF add-on unit
    _insert_active(con, "NIFTYBEES", end_date=TODAY)  # ETF
    syms = _list_symbols_from_con(con, today=TODAY)
    assert "RELIANCE" in syms
    assert "GOLDADD" not in syms
    assert "NIFTYBEES" not in syms


def test_list_excludes_parked_symbol_within_reprobe_window():
    con = _fresh_con()
    _insert_active(con, "RELIANCE", end_date=TODAY)
    _insert_active(con, "DELISTED", end_date=TODAY)
    # DELISTED parked, checked 5 days ago → still inside 30d reprobe window → skip
    con.execute(
        "INSERT INTO yf_coverage VALUES (?,?,?,?,?,?)",
        ["DELISTED", "no_data", 3, True, TODAY - timedelta(days=5), None],
    )
    syms = _list_symbols_from_con(con, today=TODAY, reprobe_days=30)
    assert "RELIANCE" in syms
    assert "DELISTED" not in syms


def test_list_reprobes_parked_symbol_after_window():
    con = _fresh_con()
    _insert_active(con, "MAYBE_BACK", end_date=TODAY)
    # parked but last checked 40 days ago → past 30d window → re-probe (include)
    con.execute(
        "INSERT INTO yf_coverage VALUES (?,?,?,?,?,?)",
        ["MAYBE_BACK", "no_data", 4, True, TODAY - timedelta(days=40), None],
    )
    syms = _list_symbols_from_con(con, today=TODAY, reprobe_days=30)
    assert "MAYBE_BACK" in syms


def test_update_coverage_parks_after_threshold(monkeypatch):
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: False)
    con = _fresh_con()
    # first no_data → counted, not yet parked
    _update_coverage(con, {"GONE": "no_data"}, TODAY, park_threshold=2)
    row = con.execute(
        "SELECT consecutive_no_data, parked FROM yf_coverage WHERE symbol='GONE'"
    ).fetchone()
    assert row == (1, False)
    # second consecutive no_data → parked
    _update_coverage(con, {"GONE": "no_data"}, TODAY + timedelta(days=1), park_threshold=2)
    row = con.execute(
        "SELECT consecutive_no_data, parked FROM yf_coverage WHERE symbol='GONE'"
    ).fetchone()
    assert row == (2, True)


def test_update_coverage_reset_and_unpark_on_ok():
    con = _fresh_con()
    con.execute(
        "INSERT INTO yf_coverage VALUES (?,?,?,?,?,?)",
        ["BACK", "no_data", 5, True, TODAY - timedelta(days=1), None],
    )
    _update_coverage(con, {"BACK": "ok"}, TODAY, park_threshold=2)
    row = con.execute(
        "SELECT status, consecutive_no_data, parked, last_ok FROM yf_coverage WHERE symbol='BACK'"
    ).fetchone()
    assert row == ("ok", 0, False, TODAY)


def test_update_coverage_error_counts_as_no_data(monkeypatch):
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: False)
    con = _fresh_con()
    _update_coverage(con, {"X": "error"}, TODAY, park_threshold=2)
    _update_coverage(con, {"X": "error"}, TODAY + timedelta(days=1), park_threshold=2)
    parked = con.execute("SELECT parked FROM yf_coverage WHERE symbol='X'").fetchone()[0]
    assert parked is True


def test_update_coverage_never_parks_proven_coverage(monkeypatch):
    """A symbol with an existing actions parquet (proven coverage) is never
    parked, however many consecutive misses — its misses are throttling."""
    con = _fresh_con()
    # REALSTOCK has a parquet on disk; ETFX never has
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: s == "REALSTOCK")
    for i in range(4):
        _update_coverage(
            con, {"REALSTOCK": "no_data", "ETFX": "no_data"},
            TODAY + timedelta(days=i), park_threshold=2,
        )
    got = {s: (c, p) for s, c, p in con.execute(
        "SELECT symbol, consecutive_no_data, parked FROM yf_coverage").fetchall()}
    assert got["REALSTOCK"] == (4, False)   # 4 misses, still not parked
    assert got["ETFX"][1] is True           # parked


def test_update_coverage_prior_success_protects(monkeypatch):
    """A symbol whose cache row already has last_ok (succeeded before) is not
    parked on later misses even without a parquet on disk."""
    con = _fresh_con()
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: False)
    con.execute(
        "INSERT INTO yf_coverage VALUES ('WASOK','ok',0,FALSE,?,?)",
        [TODAY - timedelta(days=10), TODAY - timedelta(days=10)],
    )
    _update_coverage(con, {"WASOK": "no_data"}, TODAY, park_threshold=1)
    parked = con.execute("SELECT parked FROM yf_coverage WHERE symbol='WASOK'").fetchone()[0]
    assert parked is False
