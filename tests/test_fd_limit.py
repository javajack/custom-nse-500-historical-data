"""Regression tests for the NOFILE-limit fix.

bhav_daily is a view over ~5k per-day parquet files. The surveillance-proxy
query (rank/filters._proxy_from_con) makes DuckDB open every partition file at
once, which overflowed the default 1024 soft NOFILE limit -> "IO Error: Too
many open files". open_db() now raises the soft limit before connecting.
"""
from __future__ import annotations

from datetime import date

import pytest

resource = pytest.importorskip("resource")

from nse_universe.core.db import _MIN_FD_SOFT, _raise_fd_limit, has_any_parquet, open_db


def test_raise_fd_limit_lifts_soft_toward_target():
    soft0, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        _raise_fd_limit()
        new_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        expected = _MIN_FD_SOFT if hard == resource.RLIM_INFINITY else min(_MIN_FD_SOFT, hard)
        assert new_soft >= expected
        assert new_soft >= 1024
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft0, hard))


def test_raise_fd_limit_is_idempotent_and_never_lowers():
    soft0, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        _raise_fd_limit()
        first, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        _raise_fd_limit()
        second, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert second == first  # no-op the second time, never reduces
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft0, hard))


def test_open_db_survives_low_starting_limit_on_heavy_scan():
    """The exact repro: start at 1024, open_db must lift the limit so the
    surveillance-proxy scan (opens every partition file) does not EMFILE."""
    if not has_any_parquet():
        pytest.skip("no parquet data — run ingest first")
    from nse_universe.rank.filters import _proxy_from_con

    soft0, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        con = open_db(read_only=True)
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 1024  # lifted
        max_date = con.execute("SELECT MAX(date) FROM bhav_daily").fetchone()[0]
        # must not raise "Too many open files"
        stages = _proxy_from_con(con, max_date)
        con.close()
        assert isinstance(stages, dict)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft0, hard))
