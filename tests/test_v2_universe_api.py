"""Public API tests for Universe(version='v1'|'v2')."""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

# isolated_data_dir fixture is provided by conftest.py


def _seed_parquet(parquet_dir, specs):
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE seed (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, prev_close DOUBLE, volume BIGINT, turnover DOUBLE,
            trades INTEGER, year INTEGER, month INTEGER
        )
    """)
    rows = []
    for s in specs:
        d = s["base_date"]
        added = 0
        while added < s["n_days"]:
            if d.weekday() < 5:
                rows.append((s["symbol"], d, s["close"], s["close"], s["close"],
                             s["close"], s["close"], 10_000, s["turnover"],
                             10, d.year, d.month))
                added += 1
            d += timedelta(days=1)
    con.executemany("INSERT INTO seed VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM seed)
        TO '{parquet_dir}'
        (FORMAT PARQUET, PARTITION_BY (year, month), OVERWRITE_OR_IGNORE)
    """)


def test_default_version_is_v1(isolated_data_dir):
    from nse_universe.core.universe import Universe
    u = Universe()
    assert u.version == "v1"


def test_v2_version_stored(isolated_data_dir):
    from nse_universe.core.universe import Universe
    u = Universe(version="v2")
    assert u.version == "v2"


def test_invalid_version_raises(isolated_data_dir):
    from nse_universe.core.universe import Universe
    with pytest.raises(ValueError):
        Universe(version="v3")


def test_v2_universe_at_returns_v2_table(isolated_data_dir):
    """End-to-end: build v2 universe, query it via Universe.universe_at."""
    from nse_universe.paths import PARQUET_DIR
    _seed_parquet(PARQUET_DIR, [
        {"symbol": "A", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover": 10_00_00_000},
        {"symbol": "B", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover":  5_00_00_000},
    ])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.universe import Universe

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    u_v2 = Universe(version="v2")
    df = u_v2.universe_at(as_of)
    assert len(df) == 2
    assert list(df["symbol"]) == ["A", "B"]
    assert list(df["rank"]) == [1, 2]

    # v1 sees nothing because we only populated universe_v2, not universe_rank
    u_v1 = Universe(version="v1")
    df1 = u_v1.universe_at(as_of)
    assert len(df1) == 0


def test_v2_members_for_index(isolated_data_dir):
    """nifty_50 from v2 should respect the rank window."""
    from nse_universe.paths import PARQUET_DIR
    specs = [
        {"symbol": f"S{i:02d}", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover": (100 - i) * 1_00_00_000}
        for i in range(60)
    ]
    _seed_parquet(PARQUET_DIR, specs)
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.universe import Universe

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    u = Universe(version="v2")
    members = u.members(as_of, "nifty_50")
    assert len(members) == 50
    # Top member should be S00 (highest turnover)
    assert members[0] == "S00"


def test_v2_excludes_non_passers(isolated_data_dir):
    """A symbol whose `passes = FALSE` row exists must not appear in queries."""
    from nse_universe.paths import PARQUET_DIR
    _seed_parquet(PARQUET_DIR, [
        {"symbol": "GOOD", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover": 5_00_00_000},
        {"symbol": "PENNY", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 30.0, "turnover": 5_00_00_000},  # fails close floor
    ])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.universe import Universe

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    u = Universe(version="v2")
    df = u.universe_at(as_of)
    syms = set(df["symbol"])
    assert "GOOD" in syms
    assert "PENNY" not in syms
