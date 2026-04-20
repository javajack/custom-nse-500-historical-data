"""End-to-end smoke test — runs against whatever data happens to be in the repo.

These tests are intentionally *observational* rather than strict: they skip
gracefully when the pipeline hasn't been run yet. They exist so a new checkout
can pytest-verify that imports, SQL, and the library surface still work.
"""
from __future__ import annotations

from datetime import date

import pytest

from nse_universe import Universe
from nse_universe.core.config import load_indices
from nse_universe.core.db import db
from nse_universe.fetch.urls import bhav_filename, bhav_url
from nse_universe.ingest.bhav import parquet_path_for


def test_indices_config_loads():
    idx = load_indices()
    assert "nifty_50" in idx
    assert "midcap_150" in idx
    assert idx["nifty_50"].rank_lo == 1 and idx["nifty_50"].rank_hi == 50


def test_url_shape():
    d = date(2024, 1, 15)
    assert bhav_filename(d) == "cm15JAN2024bhav.csv.zip"
    assert bhav_url(d).endswith("/2024/JAN/cm15JAN2024bhav.csv.zip")


def test_parquet_path():
    d = date(2024, 1, 15)
    p = parquet_path_for(d)
    assert "year=2024" in str(p) and "month=01" in str(p)
    assert p.name == "2024-01-15.parquet"


def test_db_opens_and_tables_exist():
    with db(read_only=False) as con:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    expected = {
        "meta", "non_trading_days", "fetch_log", "universe_rank",
        "adj_events", "symbol_master", "bhav_daily",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_universe_constructs():
    u = Universe()
    assert "nifty_50" in u.indices()
    assert u.index_spec("nifty_50").rank_hi == 50


def test_membership_roundtrip_if_data_present():
    u = Universe()
    h = u.health()
    if h["rank_snapshots"] == 0:
        pytest.skip("no rank snapshots yet — run full pipeline first")
    with db(read_only=True) as con:
        asof = con.execute("SELECT MAX(as_of_date) FROM universe_rank").fetchone()[0]
    # pick a date just after the snapshot so as_of resolves
    query_d = asof
    members = u.members(query_d, "nifty_50")
    assert len(members) == 50, f"nifty_50 should have 50 members, got {len(members)}"
    # every nifty_50 member should be a member of nifty_100 too
    top100 = set(u.members(query_d, "nifty_100"))
    assert set(members).issubset(top100)
    # rank of first symbol should be 1
    assert u.rank(members[0], query_d) == 1
