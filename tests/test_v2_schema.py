"""Schema migration tests for universe_v2 and surveillance_daily."""
from nse_universe.core.db import db


def test_universe_v2_table_columns():
    expected = {
        "as_of_date", "symbol", "rank", "passes",
        "med_turnover_60d", "med_turnover_126d",
        "traded_pct_60d", "trading_days_history",
        "close_asof", "cv_turnover_126d", "circuit_pct_60d",
        "gsm_stage", "asm_stage", "vol_annualized_60d",
        "exclude_reason",
    }
    with db() as con:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'universe_v2'"
        ).fetchall()
    cols = {r[0] for r in rows}
    missing = expected - cols
    assert not missing, f"universe_v2 missing columns: {missing}"


def test_surveillance_daily_table_columns():
    expected = {"date", "symbol", "gsm_stage", "asm_stage", "source"}
    with db() as con:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'surveillance_daily'"
        ).fetchall()
    cols = {r[0] for r in rows}
    missing = expected - cols
    assert not missing, f"surveillance_daily missing columns: {missing}"


def test_schema_version_bumped():
    with db() as con:
        v = con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    assert v is not None and int(v[0]) >= 2
