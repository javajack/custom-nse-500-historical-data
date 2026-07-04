"""Tests for the non-equity deny-list (rank/deny.py).

The deny-list is shared by the v2 momentum ranker AND the corporate-actions
fetch universe, so a false positive here silently drops a real equity from
BOTH. These tests pin the two safe suffix families (ADD add-on ETF units and
CASE basket funds) and guard the real equities that pattern-matching could
plausibly catch.
"""
from __future__ import annotations

import pytest

from nse_universe.rank.deny import is_non_equity


@pytest.mark.parametrize("sym", [
    # BEES / ETF / IETF (pre-existing families)
    "NIFTYBEES", "GOLDBEES", "BANKBEES", "NIFITETF",
    # ADD add-on ETF units (newly denied)
    "GOLDADD", "NIFTYADD", "BANKETFADD", "GOLDETFADD", "ITETFADD",
    "NIFTY50ADD", "SENSEXADD", "FMCGADD", "PSUBANKADD", "SILVERADD",
    # CASE basket funds (newly denied)
    "GOLDCASE", "SILVERCASE", "LIQUIDCASE", "LTGILTCASE", "MID150CASE",
    "NIFTYCASE", "SML100CASE", "TOP100CASE",
    # brand-named explicit funds
    "ABSLLIQUID", "TATAGOLD", "MOSILVER",
])
def test_known_non_equity_is_denied(sym):
    assert is_non_equity(sym) is True, f"{sym} should be flagged non-equity"


@pytest.mark.parametrize("sym", [
    # plain large/mid caps
    "RELIANCE", "TCS", "INFY", "TATAMOTORS", "LTIM", "PEL", "SWANENERGY",
    # real equities whose names COLLIDE with fund keywords — must NOT be denied
    "ALPHAGEO",    # Alphageo India — seismic surveys, not an ALPHA factor fund
    "BALPHARMA",   # Bal Pharma — contains "ALPHA" substring
    "PNBGILTS",    # PNB Gilts — a listed primary dealer, not a GILT fund
])
def test_real_equity_is_not_denied(sym):
    assert is_non_equity(sym) is False, f"{sym} is a real equity, must not be denied"


def test_case_insensitive_and_whitespace():
    assert is_non_equity("  goldadd ") is True
    assert is_non_equity("reliance") is False
