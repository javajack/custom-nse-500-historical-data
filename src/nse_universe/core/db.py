"""DuckDB connection + schema management.

Strategy:
  - Internal tables (universe_rank, non_trading_days, fetch_log, adj_events,
    symbol_master) live inside the DuckDB file — fast random access.
  - `bhav_daily` is a VIEW over partitioned parquet files. Raw data is the
    source of truth; the DuckDB file is an index and can be rebuilt from
    parquet at any time via rebuild_from_parquet().

This separation keeps the DuckDB file out of git (it churns on every write)
while all durable data — raw zips, normalized parquet, adjustments — stays
versioned.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

import duckdb

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

from nse_universe.paths import DB_PATH, PARQUET_DIR, ensure_dirs

# bhav_daily is a view over one parquet file per trading day (~5k and growing).
# Some scans — notably the surveillance-proxy query in rank/filters.py — make
# DuckDB open a handle for *every* partition file at once, which overflows the
# common 1024 soft NOFILE limit and raises "IO Error: ... Too many open files".
# Lift the soft limit toward the hard cap so scans scale with the dataset.
_MIN_FD_SOFT = 65536


def _raise_fd_limit(minimum: int = _MIN_FD_SOFT) -> None:
    """Raise the process soft NOFILE limit to at least `minimum` (capped by the
    hard limit). Idempotent and best-effort — never raises."""
    if resource is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    if soft == resource.RLIM_INFINITY:
        return
    target = minimum if hard == resource.RLIM_INFINITY else min(minimum, hard)
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass

SCHEMA_VERSION = 3

_INTERNAL_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS non_trading_days (
        date   DATE PRIMARY KEY,
        reason VARCHAR NOT NULL,              -- 'weekend' | 'holiday_404' | 'pre_history'
        noted_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fetch_log (
        date          DATE PRIMARY KEY,
        zip_path      VARCHAR NOT NULL,
        bytes         BIGINT  NOT NULL,
        fetched_at    TIMESTAMP NOT NULL,
        ingested      BOOLEAN NOT NULL DEFAULT FALSE,
        ingested_rows INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_rank (
        as_of_date   DATE    NOT NULL,
        symbol       VARCHAR NOT NULL,
        rank         INTEGER NOT NULL,
        metric_value DOUBLE  NOT NULL,
        metric_kind  VARCHAR NOT NULL DEFAULT 'turnover_median_126d',
        PRIMARY KEY (as_of_date, symbol)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_rank_date_rank
        ON universe_rank(as_of_date, rank)
    """,
    """
    CREATE TABLE IF NOT EXISTS adj_events (
        symbol     VARCHAR NOT NULL,
        event_date DATE    NOT NULL,
        kind       VARCHAR NOT NULL,          -- 'split' | 'dividend'
        ratio      DOUBLE,                    -- split: new/old (2:1 → 2.0); dividend: amount per share
        source     VARCHAR NOT NULL DEFAULT 'yfinance',
        PRIMARY KEY (symbol, event_date, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_master (
        symbol     VARCHAR PRIMARY KEY,
        first_seen DATE NOT NULL,
        last_seen  DATE NOT NULL,
        days_seen  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_v2 (
        as_of_date           DATE    NOT NULL,
        symbol               VARCHAR NOT NULL,
        rank                 INTEGER NOT NULL,
        passes               BOOLEAN NOT NULL,
        med_turnover_60d     DOUBLE,
        med_turnover_126d    DOUBLE,
        traded_pct_60d       DOUBLE,
        trading_days_history INTEGER,
        close_asof           DOUBLE,
        cv_turnover_126d     DOUBLE,
        circuit_pct_60d      DOUBLE,
        gsm_stage            INTEGER,
        asm_stage            INTEGER,
        vol_annualized_60d   DOUBLE,
        exclude_reason       VARCHAR,
        PRIMARY KEY (as_of_date, symbol)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_v2_passes
        ON universe_v2(as_of_date, passes)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_v2_rank
        ON universe_v2(as_of_date, rank)
    """,
    """
    CREATE TABLE IF NOT EXISTS surveillance_daily (
        date       DATE    NOT NULL,
        symbol     VARCHAR NOT NULL,
        gsm_stage  INTEGER,
        asm_stage  INTEGER,
        source     VARCHAR NOT NULL,
        PRIMARY KEY (date, symbol, source)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_surv_date ON surveillance_daily(date)
    """,
    # yfinance coverage cache: parks symbols that repeatedly return no data
    # (ETFs missed by the deny-list, delisted / renamed tickers) so the
    # corporate-actions job stops re-fetching them every run. Symbols are
    # re-probed once `last_checked` ages past the reprobe window.
    """
    CREATE TABLE IF NOT EXISTS yf_coverage (
        symbol              VARCHAR PRIMARY KEY,
        status              VARCHAR NOT NULL,          -- 'ok' | 'no_data'
        consecutive_no_data INTEGER NOT NULL DEFAULT 0,
        parked              BOOLEAN NOT NULL DEFAULT FALSE,
        last_checked        DATE,
        last_ok             DATE
    )
    """,
)


_EMPTY_BHAV_VIEW = """
CREATE OR REPLACE VIEW bhav_daily AS
SELECT
    CAST(NULL AS VARCHAR) AS symbol,
    CAST(NULL AS DATE)    AS date,
    CAST(NULL AS DOUBLE)  AS open,
    CAST(NULL AS DOUBLE)  AS high,
    CAST(NULL AS DOUBLE)  AS low,
    CAST(NULL AS DOUBLE)  AS close,
    CAST(NULL AS DOUBLE)  AS prev_close,
    CAST(NULL AS BIGINT)  AS volume,
    CAST(NULL AS DOUBLE)  AS turnover,
    CAST(NULL AS INTEGER) AS trades,
    CAST(NULL AS INTEGER) AS year,
    CAST(NULL AS INTEGER) AS month
WHERE FALSE
"""


def _register_bhav_view(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the bhav_daily view over whatever parquet files exist.

    DuckDB's read_parquet errors when glob matches zero files, so fall back
    to an empty typed view in that case. Shape is restored once any parquet
    lands and register is called again.
    """
    parquet_glob = str(PARQUET_DIR / "year=*" / "month=*" / "*.parquet")
    if not has_any_parquet():
        con.execute(_EMPTY_BHAV_VIEW)
        return
    con.execute(
        f"""
        CREATE OR REPLACE VIEW bhav_daily AS
        SELECT *
        FROM read_parquet('{parquet_glob}', hive_partitioning = TRUE, union_by_name = TRUE)
        """
    )


def _migrate(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in _INTERNAL_DDL:
        con.execute(stmt)
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        [str(SCHEMA_VERSION)],
    )


def open_db(path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB database, applying migrations.

    View registration only happens on writable opens (read-only opens inherit
    whatever view was last persisted). Callers that have just mutated parquet
    should open writable once to refresh the `bhav_daily` view; the ingester
    does this automatically.
    """
    ensure_dirs()
    _raise_fd_limit()
    target = path or DB_PATH
    if read_only and not target.exists():
        # bootstrap: read-only open requires the file to exist
        duckdb.connect(str(target)).close()
    con = duckdb.connect(str(target), read_only=read_only)
    if not read_only:
        _migrate(con)
        _register_bhav_view(con)
    return con


@contextlib.contextmanager
def db(*, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_db(read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def has_any_parquet() -> bool:
    return any(PARQUET_DIR.glob("year=*/month=*/*.parquet"))


def rebuild_from_parquet() -> dict[str, int]:
    """Drop derived tables and repopulate them from parquet + raw state.

    Useful after a `git pull` that brings new parquet files, or if the
    DuckDB file is lost. Keeps universe_rank / adj_events since those are
    not derived from parquet — they're computed separately.
    """
    with db() as con:
        _register_bhav_view(con)
        if not has_any_parquet():
            return {"symbols": 0, "rows": 0}
        con.execute(
            """
            DELETE FROM symbol_master;
            INSERT INTO symbol_master
            SELECT symbol, MIN(date), MAX(date), COUNT(*)
            FROM bhav_daily
            GROUP BY symbol
            """
        )
        stats = con.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(*) FROM bhav_daily"
        ).fetchone()
        return {"symbols": int(stats[0]), "rows": int(stats[1])}
