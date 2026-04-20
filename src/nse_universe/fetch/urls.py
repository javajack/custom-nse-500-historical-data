"""URL and local-path derivation for NSE bhavcopy archives.

NSE retired the legacy URL around 2024-07-08 and moved to a new URL + CSV
schema ("CM_UDiFF" / BhavCopy_NSE_CM_*). Both URLs are tried on each fetch
so we don't depend on a hardcoded cutoff — whichever returns 200 wins,
and the ingester detects schema from columns.

Legacy (≤ ~2024-07-05):
  https://archives.nseindia.com/content/historical/EQUITIES/YYYY/MMM/cmDDMMMYYYYbhav.csv.zip

New (≥ 2024-07-08):
  https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip

Local file naming: we preserve whichever format was fetched — the filename
encodes the schema. `bhav_local_path_any` returns either if present.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from nse_universe.paths import RAW_DIR

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

LEGACY_BASE = "https://archives.nseindia.com/content/historical/EQUITIES"
NEW_BASE = "https://archives.nseindia.com/content/cm"

# Dates strictly before this use legacy first; on/after this use new first.
# Both are still attempted via fallback — this just controls order.
URL_FORMAT_CUTOVER = date(2024, 7, 8)


def bhav_filename_legacy(d: date) -> str:
    return f"cm{d.day:02d}{MONTHS[d.month - 1]}{d.year}bhav.csv.zip"


def bhav_filename_new(d: date) -> str:
    return f"BhavCopy_NSE_CM_0_0_0_{d.year}{d.month:02d}{d.day:02d}_F_0000.csv.zip"


def bhav_url_legacy(d: date) -> str:
    return f"{LEGACY_BASE}/{d.year}/{MONTHS[d.month - 1]}/{bhav_filename_legacy(d)}"


def bhav_url_new(d: date) -> str:
    return f"{NEW_BASE}/{bhav_filename_new(d)}"


def bhav_local_path_legacy(d: date) -> Path:
    return RAW_DIR / f"{d.year}" / MONTHS[d.month - 1] / bhav_filename_legacy(d)


def bhav_local_path_new(d: date) -> Path:
    return RAW_DIR / f"{d.year}" / MONTHS[d.month - 1] / bhav_filename_new(d)


def url_candidates(d: date) -> list[tuple[str, Path]]:
    """Return (url, local_path) pairs in the order we should try them."""
    legacy = (bhav_url_legacy(d), bhav_local_path_legacy(d))
    new = (bhav_url_new(d), bhav_local_path_new(d))
    return [new, legacy] if d >= URL_FORMAT_CUTOVER else [legacy, new]


def find_existing_local(d: date) -> Path | None:
    """Return whichever local file exists, preferring the expected format."""
    for _, p in url_candidates(d):
        if p.exists():
            return p
    return None


# Back-compat shims — existing call sites use these names
bhav_filename = bhav_filename_legacy
bhav_url = bhav_url_legacy
bhav_local_path = bhav_local_path_legacy
