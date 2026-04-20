"""Export DuckDB-derived tables to parquet for git-committed durability.

`universe_rank` and `adj_events` live in DuckDB (which is gitignored because it
churns on every write). Without export, a fresh clone + CI build has no access
to these. These small parquet files close the gap — commit them, CI reads them,
build_docs.py picks up live stats regardless of whether DuckDB is present.

Run automatically by the ranker and the actions fetcher; also exposed as a
menu action for manual resync.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nse_universe.core.db import db
from nse_universe.paths import DATA_DIR, ensure_dirs

log = logging.getLogger(__name__)

DERIVED_DIR = DATA_DIR / "derived"


def _write_table(name: str, df) -> Path:
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    path = DERIVED_DIR / f"{name}.parquet"
    if df is None or len(df) == 0:
        # still emit an empty file so readers can trust its existence
        # (zero rows, right schema)
        tbl = pa.Table.from_pandas(df) if df is not None else pa.table({})
    else:
        tbl = pa.Table.from_pandas(df.reset_index(drop=True), preserve_index=False)
    tbl = tbl.replace_schema_metadata({})
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(tbl, tmp, compression="zstd", compression_level=3,
                   write_statistics=False)
    tmp.replace(path)
    return path


def export_all() -> dict[str, int]:
    """Export universe_rank + adj_events to data/derived/*.parquet.

    Idempotent. Sorted rows so parquet bytes are deterministic.
    """
    ensure_dirs()
    counts: dict[str, int] = {}
    with db(read_only=True) as con:
        ur = con.execute(
            """
            SELECT as_of_date, symbol, rank, metric_value, metric_kind
              FROM universe_rank
             ORDER BY as_of_date, rank, symbol
            """
        ).fetchdf()
        ae = con.execute(
            """
            SELECT symbol, event_date, kind, ratio, source
              FROM adj_events
             ORDER BY symbol, event_date, kind
            """
        ).fetchdf()
    _write_table("universe_rank", ur)
    _write_table("adj_events", ae)
    counts["universe_rank"] = int(len(ur))
    counts["adj_events"] = int(len(ae))
    log.info("exported universe_rank=%d adj_events=%d rows", *counts.values())
    return counts


def import_all_if_missing() -> dict[str, int]:
    """If DuckDB doesn't have rows in these tables, repopulate from parquet.

    Used at CI build time: DuckDB is re-created from scratch each run, but the
    derived parquet is committed, so we load it back in.
    """
    counts: dict[str, int] = {"universe_rank": 0, "adj_events": 0}
    ur_path = DERIVED_DIR / "universe_rank.parquet"
    ae_path = DERIVED_DIR / "adj_events.parquet"
    with db() as con:
        if ur_path.exists():
            have = con.execute("SELECT COUNT(*) FROM universe_rank").fetchone()[0]
            if have == 0:
                con.execute(f"INSERT INTO universe_rank SELECT * FROM read_parquet('{ur_path}')")
                counts["universe_rank"] = con.execute(
                    "SELECT COUNT(*) FROM universe_rank"
                ).fetchone()[0]
        if ae_path.exists():
            have = con.execute("SELECT COUNT(*) FROM adj_events").fetchone()[0]
            if have == 0:
                con.execute(f"INSERT INTO adj_events SELECT * FROM read_parquet('{ae_path}')")
                counts["adj_events"] = con.execute(
                    "SELECT COUNT(*) FROM adj_events"
                ).fetchone()[0]
    return counts
