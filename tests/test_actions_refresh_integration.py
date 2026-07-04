"""End-to-end wiring test for refresh_actions.

Drives the real refresh_actions() control flow (main pass → retry pass → tally
→ coverage persistence) with yfinance and the DB stubbed out, verifying the
three behaviours the fix introduces:

  * a live stock that fails the main pass but succeeds on retry is *recovered*
  * a symbol that returns no data on PARK_THRESHOLD consecutive runs is *parked*
  * a parked symbol is *skipped* on the next run (until its reprobe window)
"""
from __future__ import annotations

import contextlib
from collections import defaultdict
from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

import nse_universe.actions.fetch as fetch
from nse_universe.actions.fetch import ActionsStats, refresh_actions


def _con_with_symbols(symbols):
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
            symbol VARCHAR PRIMARY KEY, status VARCHAR NOT NULL,
            consecutive_no_data INTEGER NOT NULL DEFAULT 0,
            parked BOOLEAN NOT NULL DEFAULT FALSE,
            last_checked DATE, last_ok DATE
        )
    """)
    end = date(2026, 6, 30)
    rows, d, added = [], end, 0
    while added < 30:
        if d.weekday() < 5:
            for s in symbols:
                rows.append((s, d, 100.0, 100.0, 100.0, 100.0, 100.0,
                             1000, 1_00_00_000, 5, d.year, d.month))
            added += 1
        d -= timedelta(days=1)
    con.executemany("INSERT INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return con


class _FakeFetch:
    """Returns a scripted status sequence per symbol, advancing on each call."""
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = defaultdict(int)

    def __call__(self, symbol, *, sleep_s=0.0, limiter=None):
        seq = self.script.get(symbol, ["no_data"])
        i = min(self.calls[symbol], len(seq) - 1)
        self.calls[symbol] += 1
        status = seq[i]
        stats = ActionsStats(symbol=symbol, status=status)
        if status == "ok":
            stats.splits = 1
            df = pd.DataFrame({"symbol": [symbol], "event_date": [date(2020, 1, 1)],
                               "kind": ["split"], "ratio": [2.0], "source": ["yfinance"]})
            return stats, df
        if status == "error":
            stats.error = "boom"
        return stats, None


@pytest.fixture
def wired(monkeypatch):
    """Patch fetch's DB + side effects; yield a handle whose .con persists
    across refresh_actions calls so coverage carries over between runs."""
    con = _con_with_symbols(["LIVE", "DEAD"])

    @contextlib.contextmanager
    def fake_db(*, read_only=False):
        yield con

    monkeypatch.setattr(fetch, "db", fake_db)
    monkeypatch.setattr(fetch, "_write_symbol_parquet", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_upsert_events", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "export_all", lambda *a, **k: None)
    # isolate parking from the real data/actions dir: no symbol has a parquet,
    # so DEAD (never ok) parks while LIVE stays governed by its scripted outcome
    monkeypatch.setattr(fetch, "_has_actions_data", lambda s: False)

    class Handle:
        pass
    h = Handle()
    h.con = con
    h.monkeypatch = monkeypatch
    return h


def test_retry_recovers_transient_then_parks_persistent(wired):
    con = wired.con

    # LIVE: main pass no_data, retry ok (rate-limit transient rescued)
    # DEAD: always no_data
    wired.monkeypatch.setattr(
        fetch, "_fetch_one",
        _FakeFetch({"LIVE": ["no_data", "ok"], "DEAD": ["no_data"]}),
    )
    # fresh_days=0 isolates parking from the freshness cache (which would
    # otherwise skip LIVE on runs 2-3 after its run-1 success)
    r1 = refresh_actions(today=date(2026, 7, 1), fresh_days=0)
    assert r1.total == 2
    assert r1.ok == 1 and r1.recovered == 1        # LIVE rescued by retry
    assert r1.no_data == 1                          # DEAD still missing
    assert r1.skipped_parked == 0

    cov = dict((s, (c, p)) for s, c, p in con.execute(
        "SELECT symbol, consecutive_no_data, parked FROM yf_coverage").fetchall())
    assert cov["LIVE"] == (0, False)                # reset on success
    assert cov["DEAD"] == (1, False)                # one miss, not yet parked

    # Run 2: DEAD misses again → crosses PARK_THRESHOLD=2 → parked
    wired.monkeypatch.setattr(
        fetch, "_fetch_one",
        _FakeFetch({"LIVE": ["ok"], "DEAD": ["no_data"]}),
    )
    r2 = refresh_actions(today=date(2026, 7, 2), fresh_days=0)
    assert r2.skipped_parked == 0                    # not parked at run start
    parked = con.execute("SELECT parked FROM yf_coverage WHERE symbol='DEAD'").fetchone()[0]
    assert parked is True

    # Run 3: DEAD now parked & inside reprobe window → skipped entirely
    fake3 = _FakeFetch({"LIVE": ["ok"], "DEAD": ["ok"]})
    wired.monkeypatch.setattr(fetch, "_fetch_one", fake3)
    r3 = refresh_actions(today=date(2026, 7, 3), fresh_days=0)
    assert r3.total == 1                             # only LIVE fetched
    assert r3.skipped_parked == 1                    # DEAD skipped via cache
    assert fake3.calls["DEAD"] == 0                  # DEAD never hit yfinance


def test_fresh_symbol_skipped_but_explicit_list_bypasses(wired):
    con = wired.con
    # LIVE fetched successfully yesterday → inside the 7-day freshness window
    con.execute(
        "INSERT INTO yf_coverage VALUES ('LIVE','ok',0,FALSE,?,?)",
        [date(2026, 7, 4), date(2026, 7, 4)],
    )
    fake = _FakeFetch({"LIVE": ["ok"], "DEAD": ["no_data"]})
    wired.monkeypatch.setattr(fetch, "_fetch_one", fake)

    # default universe: LIVE skipped as fresh, only DEAD fetched
    r = refresh_actions(today=date(2026, 7, 5), fresh_days=7)
    assert r.skipped_fresh == 1
    assert r.total == 1
    assert fake.calls["LIVE"] == 0
    assert fake.calls["DEAD"] >= 1

    # explicit symbol list ignores the freshness cache — user asked for it
    fake2 = _FakeFetch({"LIVE": ["ok"]})
    wired.monkeypatch.setattr(fetch, "_fetch_one", fake2)
    r2 = refresh_actions(symbols=["LIVE"], today=date(2026, 7, 5), fresh_days=7)
    assert r2.skipped_fresh == 0
    assert fake2.calls["LIVE"] == 1


def test_no_actions_symbol_answered_not_retried_and_fresh(wired):
    con = wired.con
    fake = _FakeFetch({"LIVE": ["no_actions"], "DEAD": ["no_data"]})
    wired.monkeypatch.setattr(fetch, "_fetch_one", fake)
    r = refresh_actions(today=date(2026, 7, 5), fresh_days=7)
    assert r.no_actions == 1
    assert r.no_data == 1
    assert fake.calls["LIVE"] == 1        # answered once, NOT retried
    assert fake.calls["DEAD"] == 2        # real miss retried
    # LIVE now carries last_ok → freshness-eligible next run
    row = con.execute(
        "SELECT status, last_ok FROM yf_coverage WHERE symbol='LIVE'").fetchone()
    assert row == ("no_actions", date(2026, 7, 5))


def test_parked_symbol_reprobed_after_window(wired):
    con = wired.con
    con.execute(
        "INSERT INTO yf_coverage VALUES ('DEAD','no_data',4,TRUE,?,NULL)",
        [date(2026, 5, 1)],   # last checked 2 months before the run
    )
    fake = _FakeFetch({"LIVE": ["ok"], "DEAD": ["ok"]})  # DEAD is back
    wired.monkeypatch.setattr(fetch, "_fetch_one", fake)
    r = refresh_actions(today=date(2026, 7, 3), reprobe_days=30)
    assert fake.calls["DEAD"] == 1                   # reprobed
    assert r.skipped_parked == 0
    cov = con.execute(
        "SELECT status, consecutive_no_data, parked FROM yf_coverage WHERE symbol='DEAD'"
    ).fetchone()
    assert cov == ("ok", 0, False)                   # recovered → un-parked
