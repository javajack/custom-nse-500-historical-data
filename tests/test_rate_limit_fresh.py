"""Tests for the client-side rate limiter + 7-day freshness cache.

yfinance 1.3.0 forbids requests_cache (curl_cffi only) and has no built-in
rate limiter, so throttling is controlled at our layer: a thread-safe minimum
-interval limiter caps requests/sec, and a freshness cache skips symbols whose
actions were fetched successfully within FRESH_DAYS (reusing yf_coverage.last_ok)
to cut request volume on repeat runs.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

import nse_universe.actions.fetch as fetch
from nse_universe.actions.fetch import _RateLimiter, _fresh_skip_set


class _FakeClock:
    """Deterministic monotonic clock: sleep() advances virtual time."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_rate_limiter_spaces_acquisitions():
    fc = _FakeClock()
    rl = _RateLimiter(2.0, clock=fc.now, sleep=fc.sleep)  # min interval 0.5s
    stamps = []
    for _ in range(4):
        rl.acquire()
        stamps.append(round(fc.t, 3))
    assert stamps == [0.0, 0.5, 1.0, 1.5]


def test_rate_limiter_disabled_when_rate_non_positive():
    fc = _FakeClock()
    rl = _RateLimiter(0, clock=fc.now, sleep=fc.sleep)
    for _ in range(5):
        rl.acquire()
    assert fc.t == 0.0  # never slept


def _con():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE yf_coverage (
            symbol VARCHAR PRIMARY KEY, status VARCHAR NOT NULL,
            consecutive_no_data INTEGER NOT NULL DEFAULT 0,
            parked BOOLEAN NOT NULL DEFAULT FALSE,
            last_checked DATE, last_ok DATE
        )
    """)
    return con


TODAY = date(2026, 7, 5)


def test_fresh_skip_set_selects_recent_success_only():
    con = _con()
    rows = [
        ("FRESH",   "ok",      0, False, TODAY, TODAY - timedelta(days=2)),   # within 7d
        ("EDGE",    "ok",      0, False, TODAY, TODAY - timedelta(days=7)),   # exactly 7d → still fresh
        ("STALE",   "ok",      0, False, TODAY, TODAY - timedelta(days=10)),  # older → refetch
        ("NEVEROK", "no_data", 3, False, TODAY, None),                        # no last_ok → refetch
    ]
    con.executemany("INSERT INTO yf_coverage VALUES (?,?,?,?,?,?)", rows)
    skip = _fresh_skip_set(con, TODAY, 7)
    assert skip == {"FRESH", "EDGE"}


def test_fresh_skip_set_disabled_when_ttl_zero():
    con = _con()
    con.execute("INSERT INTO yf_coverage VALUES ('FRESH','ok',0,FALSE,?,?)",
                [TODAY, TODAY])
    assert _fresh_skip_set(con, TODAY, 0) == set()
