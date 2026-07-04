"""Non-equity symbol filter for the v2 ranker.

NSE bhavcopy classifies ETFs, gold/silver funds, debt funds, and index funds
under SctySrs='EQ' — same as equities. So the ingester's SERIES=='EQ' filter
admits all of them into bhav_daily. The v2 momentum-universe ranker, however,
is meant to score *individual equities* only — ETFs and debt funds shouldn't
appear in the rank list at all.

This module supplies the canonical deny-list. Two-layer design:

  1. Suffix patterns (``_NON_EQUITY_SUFFIXES``) catch the regular families:
     *BEES (Goldman/Nippon ETF brand), *ETF (generic), *IETF (index ETF).
     ~75% of known non-equity instruments end with one of these.

  2. Explicit set (``_NON_EQUITY_EXPLICIT``) catches the remaining brand-named
     instruments where the suffix is ambiguous or absent — chiefly gold,
     silver, liquid-fund, and a few index funds with custom names.

Add new symbols to the explicit set when you encounter an ETF that slips
through. To audit which symbols currently pass the filter, run
``recompute_v2_all`` and inspect rows with ``exclude_reason='non_equity'``
in ``universe_v2``.
"""
from __future__ import annotations

# Suffixes that unambiguously denote NSE ETF / fund families. No NSE equity
# ends in any of these across 13 years of bhavcopy history — the suffixes are
# reserved by AMCs for their fund products:
#   BEES/ETF/IETF  — exchange-traded funds
#   ADD            — ETF "additional units" creation tickers (GOLDADD, NIFTYADD…)
#   CASE           — basket / smallcase-style fund products (GOLDCASE, LTGILTCASE…)
# Verified: SELECT DISTINCT symbol … WHERE symbol LIKE '%ADD'/'%CASE' returns
# only fund tickers, never a real company.
_NON_EQUITY_SUFFIXES: tuple[str, ...] = ("BEES", "ETF", "IETF", "ADD", "CASE")

# Brand-named gold, silver, liquid, and index funds where the symbol does
# not end in one of the suffixes above. Curated from 13 years of universe_v2
# history (every symbol whose fortress sector tag is
# DEFENSIVE/COMMODITIES/DEBT/INTERNATIONAL that does not match a suffix).
_NON_EQUITY_EXPLICIT: frozenset[str] = frozenset({
    # Liquid / money-market funds
    "ABSLLIQUID", "AONELIQUID", "ELIQUID", "HDFCLIQUID",
    "LIQUID", "LIQUID1", "LIQUIDADD", "LIQUIDCASE", "LIQUIDPLUS",
    # Gold funds (non-ETF suffix)
    "AXISGOLD", "GOLD1", "GOLDCASE", "GOLDSHARE",
    "GROWWGOLD", "HDFCGOLD", "LICMFGOLD",
    "MOGOLD", "SETFGOLD", "SKYGOLD", "TATAGOLD",
    # Silver funds (non-ETF suffix)
    "AONESILVER", "AXISILVER", "ESILVER", "GROWWSLVR",
    "HDFCSILVER", "MASILVER", "MOSILVER", "NETFSILVER",
    "SBISILVER", "SILVER", "SILVER1", "SILVERADD",
    "SILVERCASE", "TATSILV",
    # Index funds / index ETFs with brand-specific names (no ETF/BEES suffix)
    "HDFCSML250", "MON100", "N100", "SETFNIF50",
    "NIFMID150", "UTINEXT50", "UTISXN50",
})


def is_non_equity(symbol: str) -> bool:
    """Return True if ``symbol`` is a known NSE ETF / fund instrument.

    Identifies symbols by either matching one of the standard ETF suffixes
    (*BEES, *ETF, *IETF) or appearing in the curated explicit deny-list.
    """
    s = symbol.strip().upper()
    if s in _NON_EQUITY_EXPLICIT:
        return True
    return s.endswith(_NON_EQUITY_SUFFIXES)
