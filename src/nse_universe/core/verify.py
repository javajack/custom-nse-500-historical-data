"""Data integrity verification.

Walks every committed artifact and checks it is readable + internally
consistent. On failure, quarantines the file and expunges its fetch_log
row so the next `sync` picks it up cleanly.

Two passes:
  - zip pass: CRC-check every zip in data/raw/. Corrupt → quarantine.
  - parquet pass: read every parquet in data/parquet/ via pyarrow. On
    failure, delete + mark parent day as un-ingested.

Both passes are idempotent and safe to run repeatedly.
"""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from nse_universe.core.db import db
from nse_universe.paths import PARQUET_DIR, QUARANTINE_DIR, RAW_DIR

log = logging.getLogger(__name__)


@dataclass
class VerifyReport:
    zips_checked: int = 0
    zips_ok: int = 0
    zips_quarantined: list[str] = field(default_factory=list)
    parquets_checked: int = 0
    parquets_ok: int = 0
    parquets_removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "zips_checked": self.zips_checked,
            "zips_ok": self.zips_ok,
            "zips_quarantined": len(self.zips_quarantined),
            "parquets_checked": self.parquets_checked,
            "parquets_ok": self.parquets_ok,
            "parquets_removed": len(self.parquets_removed),
            "issues": self.zips_quarantined[:20] + self.parquets_removed[:20],
        }


def _valid_zip(path: Path) -> str | None:
    """Return None if valid, else a short error string."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return f"crc-fail:{bad}"
            if not any(n.lower().endswith(".csv") for n in zf.namelist()):
                return "no-csv"
    except zipfile.BadZipFile:
        return "bad-zip"
    except OSError as e:
        return f"os:{e}"
    return None


def _date_from_zip(path: Path) -> date | None:
    # filename: cmDDMMMYYYYbhav.csv.zip
    name = path.name
    if not name.startswith("cm") or not name.endswith("bhav.csv.zip"):
        return None
    core = name[2:-len("bhav.csv.zip")]  # DDMMMYYYY
    try:
        from datetime import datetime
        return datetime.strptime(core, "%d%b%Y").date()
    except ValueError:
        return None


def _date_from_parquet(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def verify_zips(*, progress_cb=None) -> VerifyReport:
    report = VerifyReport()
    zips = sorted(RAW_DIR.glob("*/*/*.zip"))
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        for i, z in enumerate(zips):
            report.zips_checked += 1
            err = _valid_zip(z)
            if progress_cb:
                progress_cb(i + 1, len(zips), z.name, "ok" if err is None else err)
            if err is None:
                report.zips_ok += 1
                continue
            d = _date_from_zip(z)
            dest = QUARANTINE_DIR / f"{z.stem}.{err.replace(':', '_')}.zip"
            try:
                z.replace(dest)
            except OSError:
                pass
            if d is not None:
                con.execute("DELETE FROM fetch_log WHERE date = ?", [d])
            report.zips_quarantined.append(f"{z.name}:{err}")
            log.warning("quarantined %s: %s", z.name, err)
    return report


def verify_parquets(*, progress_cb=None) -> VerifyReport:
    report = VerifyReport()
    pqs = sorted(PARQUET_DIR.glob("year=*/month=*/*.parquet"))
    with db() as con:
        for i, p in enumerate(pqs):
            report.parquets_checked += 1
            ok = True
            reason = ""
            try:
                # Single-file open, NOT dataset scan (dataset scan merges
                # sibling schemas and trips on partition dtype differences).
                pf = pq.ParquetFile(p)
                required = {"symbol", "date", "open", "close", "turnover"}
                schema_cols = set(pf.schema_arrow.names)
                if not required.issubset(schema_cols):
                    ok = False
                    reason = f"schema-missing:{sorted(required - schema_cols)}"
                elif pf.metadata.num_rows == 0:
                    ok = False
                    reason = "empty"
                else:
                    # cheap full roundtrip — catches truncation / corrupt pages
                    tbl = pf.read()
                    _ = tbl.num_rows
            except Exception as e:
                ok = False
                reason = f"read:{type(e).__name__}:{str(e)[:80]}"

            if progress_cb:
                progress_cb(i + 1, len(pqs), p.name, "ok" if ok else reason)

            if ok:
                report.parquets_ok += 1
                continue

            d = _date_from_parquet(p)
            try:
                p.unlink()
            except OSError:
                pass
            if d is not None:
                con.execute(
                    "UPDATE fetch_log SET ingested = FALSE, ingested_rows = NULL WHERE date = ?",
                    [d],
                )
            report.parquets_removed.append(f"{p.name}:{reason}")
            log.warning("removed corrupt parquet %s: %s", p.name, reason)
    return report


def verify_all(*, progress_cb=None) -> VerifyReport:
    """Run zip + parquet passes. Combined report."""
    rz = verify_zips(progress_cb=progress_cb)
    rp = verify_parquets(progress_cb=progress_cb)
    combined = VerifyReport(
        zips_checked=rz.zips_checked,
        zips_ok=rz.zips_ok,
        zips_quarantined=rz.zips_quarantined,
        parquets_checked=rp.parquets_checked,
        parquets_ok=rp.parquets_ok,
        parquets_removed=rp.parquets_removed,
    )
    return combined
