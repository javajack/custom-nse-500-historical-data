"""Persist scraped NSE GSM/ASM into surveillance_daily."""
from __future__ import annotations

import logging
from datetime import date

from nse_universe.core.db import db
from nse_universe.fetch.surveillance import fetch_today_surveillance

log = logging.getLogger(__name__)


def ingest_today(*, as_of: date | None = None) -> int:
    """Fetch live NSE surveillance and upsert into surveillance_daily.

    Returns the number of symbols ingested.
    """
    as_of = as_of or date.today()
    records = fetch_today_surveillance()
    with db() as con:
        for r in records:
            con.execute(
                """INSERT OR REPLACE INTO surveillance_daily
                          (date, symbol, gsm_stage, asm_stage, source)
                   VALUES (?, ?, ?, ?, 'nse_live')""",
                [as_of, r.symbol, r.gsm_stage, r.asm_stage],
            )
    log.info("surveillance ingest: %d symbols on %s", len(records), as_of)
    return len(records)
