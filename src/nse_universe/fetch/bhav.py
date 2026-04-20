"""Bhavcopy fetcher — orchestrates session, retry, rate-limit, state.

Semantics:
  - Idempotent: re-running over the same date range is a no-op if all zips
    are already on disk and logged.
  - Safe to interrupt: Ctrl-C between requests leaves state consistent.
  - Rate-limit proof: jittered delay, exponential backoff, Retry-After honored.
  - Non-trading discovery: 404 on a weekday past → recorded permanently.
  - Today tolerance: 404 on today is treated as "not yet published", NOT
    as a non-trading day; the run just moves on.
"""
from __future__ import annotations

import logging
import random
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

import duckdb

from nse_universe.core.db import db
from nse_universe.fetch.calendar import weekday_range
from nse_universe.fetch.session import NSESession
from nse_universe.fetch.urls import find_existing_local, url_candidates
from nse_universe.paths import QUARANTINE_DIR, ensure_dirs

log = logging.getLogger(__name__)


class FetchOutcome(str, Enum):
    CACHED = "cached"
    FETCHED = "fetched"
    NON_TRADING = "non_trading"
    DEFERRED_TODAY = "deferred_today"
    FAILED = "failed"


@dataclass
class FetchConfig:
    min_delay_s: float = 2.0
    max_delay_s: float = 5.0
    max_retries: int = 5
    base_backoff_s: float = 1.0
    max_backoff_s: float = 32.0
    rotate_after_failures: int = 3
    timeout_s: float = 60.0


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            return bad is None and any(n.lower().endswith(".csv") for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def _mark_non_trading(con: duckdb.DuckDBPyConnection, d: date, reason: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO non_trading_days(date, reason) VALUES (?, ?)",
        [d, reason],
    )


def _record_fetch(con: duckdb.DuckDBPyConnection, d: date, path: Path, n_bytes: int) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO fetch_log(date, zip_path, bytes, fetched_at, ingested)
        VALUES (?, ?, ?, ?, FALSE)
        """,
        [d, str(path), n_bytes, datetime.utcnow()],
    )


def _already_known(con: duckdb.DuckDBPyConnection, d: date) -> str | None:
    row = con.execute("SELECT reason FROM non_trading_days WHERE date = ?", [d]).fetchone()
    if row:
        return f"non_trading:{row[0]}"
    row = con.execute("SELECT zip_path FROM fetch_log WHERE date = ?", [d]).fetchone()
    if row:
        p = Path(row[0])
        if p.exists() and _valid_zip(p):
            return "cached"
    return None


def _sleep_jittered(cfg: FetchConfig) -> None:
    time.sleep(random.uniform(cfg.min_delay_s, cfg.max_delay_s))


def _quarantine(path: Path, d: date) -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    dst = QUARANTINE_DIR / f"{d:%Y%m%d}-{int(time.time())}.zip"
    try:
        path.rename(dst)
    except OSError:
        pass


def _try_one_url(
    d: date,
    url: str,
    local: Path,
    session: NSESession,
    cfg: FetchConfig,
    con: duckdb.DuckDBPyConnection,
) -> tuple[str, int]:
    """Try fetching a single URL with retry. Returns (result, status_code).

    result ∈ {"fetched", "not_found", "failed"}. Caller decides how to treat
    "not_found" (multi-URL fallback vs. mark non-trading).
    """
    consecutive_failures = 0
    last_status = 0
    for attempt in range(cfg.max_retries):
        try:
            resp = session.get(url, timeout=cfg.timeout_s)
        except Exception as e:
            consecutive_failures += 1
            backoff = min(cfg.base_backoff_s * (2 ** attempt), cfg.max_backoff_s)
            log.warning("net-err %s for %s [%s]; sleep %.1fs: %s", attempt, d, url, backoff, e)
            time.sleep(backoff + random.uniform(0, 1))
            if consecutive_failures >= cfg.rotate_after_failures:
                session.rotate()
                consecutive_failures = 0
            continue

        last_status = resp.status_code
        if last_status == 200:
            content = resp.content
            if not content or not content.startswith(b"PK"):
                log.warning("non-zip body for %s [%s] (len=%d); retry", d, url, len(content or b""))
                backoff = min(cfg.base_backoff_s * (2 ** attempt), cfg.max_backoff_s)
                time.sleep(backoff + random.uniform(0, 1))
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_suffix(".zip.tmp")
            tmp.write_bytes(content)
            if not _valid_zip(tmp):
                _quarantine(tmp, d)
                log.warning("bad zip for %s [%s]; quarantined", d, url)
                backoff = min(cfg.base_backoff_s * (2 ** attempt), cfg.max_backoff_s)
                time.sleep(backoff)
                continue
            tmp.replace(local)
            _record_fetch(con, d, local, len(content))
            return ("fetched", last_status)

        if last_status == 404:
            return ("not_found", last_status)

        if last_status == 429 or 500 <= last_status < 600:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.isdigit() else min(
                cfg.base_backoff_s * (2 ** attempt), cfg.max_backoff_s
            )
            wait += random.uniform(0, 1)
            log.warning("HTTP %s for %s; sleep %.1fs (attempt %d)", last_status, d, wait, attempt)
            time.sleep(wait)
            session.rotate()
            continue

        if last_status in (401, 403):
            log.warning("HTTP %s for %s [%s]; rotating session + backoff", last_status, d, url)
            session.rotate()
            time.sleep(min(cfg.base_backoff_s * (2 ** attempt), cfg.max_backoff_s))
            continue

        log.warning("unexpected HTTP %s for %s [%s]; giving up this URL", last_status, d, url)
        return ("failed", last_status)

    return ("failed", last_status)


def fetch_one(
    d: date,
    session: NSESession,
    cfg: FetchConfig,
    con: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
) -> FetchOutcome:
    today = today or date.today()

    # 1. Filesystem cache (source of truth) — check both URL formats
    existing = find_existing_local(d)
    if existing is not None and _valid_zip(existing):
        _record_fetch(con, d, existing, existing.stat().st_size)
        return FetchOutcome.CACHED

    # 2. DB-only known-state shortcut
    known = _already_known(con, d)
    if known and known.startswith("non_trading"):
        return FetchOutcome.NON_TRADING

    # 3. Try each URL candidate (new/legacy ordered by date preference).
    #    Only mark non-trading if ALL candidates 404.
    candidates = url_candidates(d)
    all_404 = True
    any_failed = False
    for url, local in candidates:
        result, status = _try_one_url(d, url, local, session, cfg, con)
        if result == "fetched":
            return FetchOutcome.FETCHED
        if result == "failed":
            any_failed = True
            all_404 = False
        elif result == "not_found":
            # stays True only if all are 404
            pass

    if all_404:
        if d >= today:
            log.info("not-yet-published: %s", d)
            return FetchOutcome.DEFERRED_TODAY
        _mark_non_trading(con, d, "holiday_404")
        return FetchOutcome.NON_TRADING

    if any_failed:
        log.warning("all URLs failed for %s", d)
        return FetchOutcome.FAILED

    return FetchOutcome.FAILED


def sync_range(
    start: date,
    end_inclusive: date,
    *,
    cfg: FetchConfig | None = None,
    reverse: bool = False,
    progress_cb=None,
) -> dict[str, int]:
    """Fetch every missing trading-day bhavcopy in [start, end_inclusive].

    reverse=True iterates latest-first — useful when recent data is most
    valuable and older data can trickle in later.

    Idempotent. Interrupt-safe. Progress callback invoked per date.
    """
    cfg = cfg or FetchConfig()
    ensure_dirs()
    today = date.today()

    counts = {o.value: 0 for o in FetchOutcome}
    session = NSESession()
    try:
        with db() as con:
            from datetime import timedelta
            d = start
            while d <= end_inclusive:
                if d.weekday() >= 5:
                    _mark_non_trading(con, d, "weekend")
                d += timedelta(days=1)

            candidates = list(weekday_range(start, end_inclusive))
            if reverse:
                candidates.reverse()
            for i, wd in enumerate(candidates):
                outcome = fetch_one(wd, session, cfg, con, today=today)
                counts[outcome.value] += 1
                if progress_cb:
                    progress_cb(i + 1, len(candidates), wd, outcome)
                if outcome == FetchOutcome.FETCHED:
                    _sleep_jittered(cfg)
                elif outcome == FetchOutcome.FAILED:
                    # back off a bit before moving on; don't poison the run
                    time.sleep(cfg.max_delay_s)
    finally:
        session.close()
    return counts
