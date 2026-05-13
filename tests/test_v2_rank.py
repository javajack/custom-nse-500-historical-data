"""Unit tests for the v2 universe builder.

Tests seed real hive-partitioned parquet files so the standard bhav_daily
view picks them up (rather than fighting the view-registration machinery
in core/db.py).
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta

import duckdb
import pytest


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point nse_universe at a fresh data dir and reload modules that
    captured PARQUET_DIR / DB_PATH at import time."""
    monkeypatch.setenv("NSE_UNIVERSE_DATA_DIR", str(tmp_path))

    import nse_universe.paths
    import nse_universe.core.db
    import nse_universe.core.export
    import nse_universe.rank.filters
    import nse_universe.rank.v2
    importlib.reload(nse_universe.paths)
    importlib.reload(nse_universe.core.db)
    importlib.reload(nse_universe.core.export)
    importlib.reload(nse_universe.rank.filters)
    importlib.reload(nse_universe.rank.v2)
    yield tmp_path


def _write_bhav_parquet(parquet_dir, symbol_specs):
    """Write hive-partitioned bhav parquet files into parquet_dir.

    symbol_specs: list of dicts with keys
        symbol, base_date, n_days, close, turnover, [prev_close]
    """
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE seed (
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
    rows = []
    for spec in symbol_specs:
        sym = spec["symbol"]
        base_date = spec["base_date"]
        n_days = spec["n_days"]
        close = spec["close"]
        prev = spec.get("prev_close", close)
        turnover = spec["turnover"]
        d = base_date
        added = 0
        while added < n_days:
            if d.weekday() < 5:
                rows.append((sym, d, close, close, close, close, prev,
                             10_000, turnover, 10, d.year, d.month))
                added += 1
            d += timedelta(days=1)
    con.executemany(
        "INSERT INTO seed VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    parquet_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM seed)
        TO '{parquet_dir}'
        (FORMAT PARQUET, PARTITION_BY (year, month), OVERWRITE_OR_IGNORE)
    """)


def test_clean_liquid_symbol_passes(isolated_data_dir):
    from nse_universe.paths import PARQUET_DIR
    _write_bhav_parquet(PARQUET_DIR, [{
        "symbol": "CLEAN", "base_date": date(2023, 1, 2),
        "n_days": 300, "close": 300.0, "turnover": 2_00_00_000,
    }])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.db import db

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason, rank "
            "FROM universe_v2 WHERE symbol='CLEAN' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None, "CLEAN missing from universe_v2"
    passes, reason, rank = row
    assert passes is True, f"CLEAN should pass; got reason={reason!r}"
    assert rank == 1


def test_thin_turnover_excluded(isolated_data_dir):
    from nse_universe.paths import PARQUET_DIR
    _write_bhav_parquet(PARQUET_DIR, [{
        "symbol": "THIN", "base_date": date(2023, 1, 2),
        "n_days": 300, "close": 300.0, "turnover": 10_00_000,  # ₹10L
    }])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.db import db

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason "
            "FROM universe_v2 WHERE symbol='THIN' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None
    passes, reason = row
    assert passes is False
    assert "turnover" in (reason or "").lower(), f"reason={reason!r}"


def test_low_price_excluded(isolated_data_dir):
    from nse_universe.paths import PARQUET_DIR
    _write_bhav_parquet(PARQUET_DIR, [{
        "symbol": "PENNY", "base_date": date(2023, 1, 2),
        "n_days": 300, "close": 30.0, "turnover": 2_00_00_000,
    }])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.db import db

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason "
            "FROM universe_v2 WHERE symbol='PENNY' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None
    passes, reason = row
    assert passes is False
    assert "close" in (reason or "").lower(), f"reason={reason!r}"


def test_short_history_excluded(isolated_data_dir):
    from nse_universe.paths import PARQUET_DIR
    _write_bhav_parquet(PARQUET_DIR, [{
        "symbol": "RECENT", "base_date": date(2023, 1, 2),
        "n_days": 150, "close": 300.0, "turnover": 2_00_00_000,
    }])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.db import db

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason "
            "FROM universe_v2 WHERE symbol='RECENT' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None
    passes, reason = row
    assert passes is False
    assert "history" in (reason or "").lower(), f"reason={reason!r}"


def test_rank_orders_by_med_turnover_126d(isolated_data_dir):
    from nse_universe.paths import PARQUET_DIR
    _write_bhav_parquet(PARQUET_DIR, [
        {"symbol": "HIGH", "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover": 10_00_00_000},
        {"symbol": "MID",  "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover":  5_00_00_000},
        {"symbol": "LOW",  "base_date": date(2023, 1, 2), "n_days": 300,
         "close": 300.0, "turnover":  1_00_00_000},
    ])
    from nse_universe.rank.v2 import recompute_v2_for, DEFAULT_V2_CONFIG
    from nse_universe.core.db import db

    as_of = date(2024, 1, 2)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)

    with db(read_only=True) as con:
        rows = con.execute(
            "SELECT symbol, rank FROM universe_v2 "
            "WHERE as_of_date=? AND passes ORDER BY rank",
            [as_of],
        ).fetchall()
    assert [r[0] for r in rows] == ["HIGH", "MID", "LOW"]
    assert [r[1] for r in rows] == [1, 2, 3]
