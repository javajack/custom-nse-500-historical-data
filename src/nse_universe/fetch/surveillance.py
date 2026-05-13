"""NSE GSM / ASM surveillance feed scraper.

NSE publishes daily surveillance lists at these endpoints:
  - GSM (Graded Surveillance Measure): 6 stages, escalating restrictions
  - ASM (Additional Surveillance Measure) long-term: 4 stages

The endpoints return JSON when called with proper TLS + cookies; we reuse
NSESession (curl_cffi Chrome impersonation + warmup) for both.

NSE does NOT publish historical archives — capture is forward-only. The
behavioural proxy from rank.filters fills the gap for backtest history.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from nse_universe.fetch.session import NSESession

log = logging.getLogger(__name__)


GSM_URL = "https://www.nseindia.com/api/reportGsm"
ASM_URL = "https://www.nseindia.com/api/reportASM"


@dataclass
class SurveillanceRecord:
    symbol: str
    gsm_stage: int | None
    asm_stage: int | None


_GSM_ROMAN = {"0": 0, "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def _extract_stage(s: Any) -> int | None:
    """Parse a stage value into 0..6.

    Accepted shapes (NSE has used all of these at various times):
      * int / float: 0, 1, 2, ..., 6
      * "0" / "1" / ...
      * "I" / "II" / "III" / "IV" / "V" / "VI"
      * "Stage I" / "Stage II" / ...
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = int(s)
        return v if 0 <= v <= 6 else None
    text = str(s).strip()
    if not text:
        return None
    upper = text.upper()
    # Direct match
    if upper in _GSM_ROMAN:
        return _GSM_ROMAN[upper]
    # "Stage X" / "stage-X" / etc.
    if "STAGE" in upper:
        for tok in upper.replace("-", " ").replace("_", " ").split():
            if tok in _GSM_ROMAN:
                return _GSM_ROMAN[tok]
    # Bare digit fallback (limit to 0..6)
    digits = "".join(c for c in upper if c.isdigit())
    if digits:
        try:
            v = int(digits)
            return v if 0 <= v <= 6 else None
        except ValueError:
            return None
    return None


def _gsm_stage_from_row(row: dict) -> int | None:
    """Resolve the effective GSM stage for one NSE GSM row.

    Priority:
      1. Direct stage parse from gsmStage / stage (handles plain Roman + Stage X)
      2. Extract "Stage X" from survDesc (catches composite codes like LXII)
      3. If row is present in the GSM list at all but stage is unparseable,
         treat it as stage 4 — composite codes (IBC + GSM mix) are by
         definition non-routine surveillance and should be filtered.
    """
    direct = _extract_stage(
        row.get("gsmStage") or row.get("stage") or row.get("Stage")
    )
    if direct is not None:
        return direct
    desc = row.get("survDesc") or row.get("survdesc") or ""
    upper = str(desc).upper()
    # Look for explicit "GSM STAGE X" or "STAGE X"
    if "STAGE" in upper:
        for tok in upper.replace("-", " ").replace("_", " ").split():
            if tok in _GSM_ROMAN:
                return _GSM_ROMAN[tok]
    # Composite / unknown code — but present in surveillance list
    return 4


def _rows_from_payload(payload: Any) -> list[dict]:
    """Return a list of row dicts from a payload that might be a bare list,
    a dict-with-'data', or the nested longterm/shortterm shape used by /api/reportASM."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Bare data lists used by various endpoints
        for key in ("data", "GSMData", "ASMData", "longtermdata"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
        # /api/reportASM nests under "longterm": {"data": [...]}
        lt = payload.get("longterm") or payload.get("Longterm")
        if isinstance(lt, dict):
            v = lt.get("data")
            if isinstance(v, list):
                return v
        return []
    return []


def _parse_gsm(payload: Any) -> dict[str, int]:
    """Return {symbol: stage} from the GSM endpoint payload (/api/reportGsm).

    Stage normalisation:
      * Plain Roman (I..VI) → 1..6
      * "0" → 0
      * Composite codes (LXII etc) without a parseable "Stage X" in survDesc
        → 4 (treated as high-surveillance, well above the exclude threshold).
    """
    out: dict[str, int] = {}
    for row in _rows_from_payload(payload):
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol") or row.get("Symbol") or row.get("SYMBOL")
        if not sym:
            continue
        stage = _gsm_stage_from_row(row)
        if stage is None:
            continue
        out[str(sym).upper().strip()] = stage
    return out


def _parse_asm(payload: Any) -> dict[str, int]:
    """Return {symbol: longterm_stage} from /api/reportASM ("longterm" branch).

    Field used: `asmSurvIndicator` (canonical) with fallback to longtermStage / stage.
    """
    out: dict[str, int] = {}
    for row in _rows_from_payload(payload):
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol") or row.get("Symbol") or row.get("SYMBOL")
        if not sym:
            continue
        stage = _extract_stage(
            row.get("asmSurvIndicator")
            or row.get("longtermStage") or row.get("LONGTERM_STAGE")
            or row.get("stage") or row.get("Stage")
        )
        if stage is None:
            continue
        out[str(sym).upper().strip()] = stage
    return out


def _json_get(sess: NSESession, url: str) -> Any:
    """GET a URL and return parsed JSON, or None on failure."""
    try:
        resp = sess.get(url)
        if resp.status_code != 200:
            log.warning("surveillance: %s returned %d", url, resp.status_code)
            return None
        # Some NSE endpoints occasionally send HTML on failure even with 200
        text = resp.text.strip()
        if not text or text[0] not in ("{", "["):
            log.warning("surveillance: %s returned non-JSON shape", url)
            return None
        return json.loads(text)
    except Exception as e:
        log.warning("surveillance: %s failed: %s", url, e)
        return None


def fetch_today_surveillance() -> list[SurveillanceRecord]:
    """Scrape GSM + ASM (long-term) and merge into per-symbol records."""
    sess = NSESession()
    sess.warmup()
    gsm_payload = _json_get(sess, GSM_URL)
    asm_payload = _json_get(sess, ASM_URL)
    sess.close()
    gsm = _parse_gsm(gsm_payload)
    asm = _parse_asm(asm_payload)
    all_symbols = set(gsm) | set(asm)
    return [
        SurveillanceRecord(
            symbol=s,
            gsm_stage=gsm.get(s),
            asm_stage=asm.get(s),
        )
        for s in sorted(all_symbols)
    ]
