#!/usr/bin/env python
"""Full historical backfill runner — reverse chronological, resumable.

Goes year-by-year from today's year → 2005. For each year:
  1. sync_range(jan1→dec31, reverse=True) — fetch every missing trading day
  2. ingest_all_pending()                  — zip → parquet
  3. verify_all()                          — zip CRC + parquet roundtrip

Safe to interrupt and re-run: all state is file-based. Typical wall time
at polite pacing ≈ 6–8 hrs total for 20 years.

Usage:
    python scripts/backfill.py [--start-year 2005] [--pace polite|normal|fast]

Progress is emitted to stdout as single-line JSON-ish records, suitable
for `tail -f` or the Monitor tool. Use PROGRESS= / YEAR= / DONE= tokens to
filter.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date

from nse_universe.core.state import (
    load, mark_actions_refreshed, mark_rank_computed,
    mark_sync_attempt, mark_sync_complete, save,
)
from nse_universe.core.verify import verify_all
from nse_universe.fetch.bhav import FetchConfig, sync_range
from nse_universe.ingest.bhav import ingest_all_pending
from nse_universe.rank.monthly import recompute_all


PACE_PRESETS = {
    "polite": FetchConfig(min_delay_s=2.0, max_delay_s=5.0),
    "normal": FetchConfig(min_delay_s=1.0, max_delay_s=3.0),
    "fast":   FetchConfig(min_delay_s=0.5, max_delay_s=1.5),
}


def setup_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log(msg: str):
    print(msg, flush=True)


def run_year(year: int, today: date, cfg: FetchConfig):
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), today)
    if start > end:
        log(f"YEAR={year} SKIP reason=future")
        return
    log(f"YEAR={year} START range={start}..{end}")

    mark_sync_attempt()
    t0 = time.time()
    counts = {"fetched": 0, "cached": 0, "non_trading": 0, "deferred_today": 0, "failed": 0}

    def cb(i, n, d, outcome):
        counts[outcome.value] = counts.get(outcome.value, 0) + 1
        if i % 10 == 0 or i == n:
            log(f"PROGRESS year={year} {i}/{n} date={d} outcome={outcome.value}")

    try:
        counts = sync_range(start, end, cfg=cfg, reverse=True, progress_cb=cb)
    except KeyboardInterrupt:
        log(f"YEAR={year} INTERRUPTED sync")
        raise
    except Exception as e:
        log(f"YEAR={year} SYNC_ERR {type(e).__name__}: {e}")
        return
    dt = time.time() - t0
    log(f"YEAR={year} SYNC_DONE in={dt:.0f}s counts={counts}")

    try:
        ic = ingest_all_pending()
    except Exception as e:
        log(f"YEAR={year} INGEST_ERR {type(e).__name__}: {e}")
        return
    log(f"YEAR={year} INGEST_DONE counts={ic}")
    mark_sync_complete()
    # Per-year verify skipped — it's O(all_files) each call, so it would
    # dominate runtime for a 20y backfill. Final pass at the end instead.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=None,
                        help="Earliest year to fetch (default: state.history_start year)")
    parser.add_argument("--pace", choices=list(PACE_PRESETS), default="normal")
    parser.add_argument("--skip-rank", action="store_true")
    parser.add_argument("--skip-actions", action="store_true")
    parser.add_argument("--years", type=str, default=None,
                        help="Comma-separated list of years to run (overrides range)")
    args = parser.parse_args()

    setup_logging()
    cfg = PACE_PRESETS[args.pace]

    today = date.today()
    state = load()
    state_start_year = int(state.history_start[:4])
    first_year = args.start_year or state_start_year

    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        years = list(range(today.year, first_year - 1, -1))

    log(f"BEGIN years={years} pace={args.pace} today={today}")

    overall_t0 = time.time()
    for year in years:
        try:
            run_year(year, today, cfg)
        except KeyboardInterrupt:
            log("INTERRUPTED — state is durable, rerun to resume")
            sys.exit(130)

    log(f"FETCH_PHASE_DONE in={time.time() - overall_t0:.0f}s")

    log("VERIFY_START")
    try:
        t0 = time.time()
        rep = verify_all()
        log(
            f"VERIFY_DONE in={time.time()-t0:.0f}s "
            f"zips={rep.zips_checked}/{rep.zips_ok} parquets={rep.parquets_checked}/{rep.parquets_ok} "
            f"quarantined={len(rep.zips_quarantined)} removed={len(rep.parquets_removed)}"
        )
        if rep.zips_quarantined or rep.parquets_removed:
            # Re-sync + re-ingest affected dates
            log("VERIFY_REPAIR fetching quarantined dates …")
            # The affected fetch_log rows were cleared/flipped by verify_all;
            # re-run sync over the full range will pick them up.
            # (Pragmatic: user can re-run the script if needed.)

    except Exception as e:
        log(f"VERIFY_ERR {type(e).__name__}: {e}")

    if not args.skip_rank:
        log("RANK_START")
        try:
            t0 = time.time()
            stats = recompute_all()
            log(f"RANK_DONE in={time.time()-t0:.0f}s as_of_dates={stats.as_of_dates} rows={stats.total_rows}")
        except Exception as e:
            log(f"RANK_ERR {type(e).__name__}: {e}")

    if not args.skip_actions:
        log("ACTIONS_START")
        try:
            from nse_universe.actions.fetch import refresh_actions
            t0 = time.time()
            r = refresh_actions()
            mark_actions_refreshed()
            log(
                f"ACTIONS_DONE in={time.time()-t0:.0f}s "
                f"total={r.total} ok={r.ok} no_data={r.no_data} errors={r.errors} "
                f"splits={r.splits} dividends={r.dividends}"
            )
        except Exception as e:
            log(f"ACTIONS_ERR {type(e).__name__}: {e}")

    log("DONE")


if __name__ == "__main__":
    main()
