"""Tests for the Yahoo symbol remap + multi-candidate fetch fallback.

Some NSE tickers return empty history on `{SYM}.NS` because Yahoo moved the
listing (demerger/rename) or only serves the BSE feed. `_fetch_one` tries each
candidate (`.NS` → `.BO`, or an explicit remap) until one answers, storing the
recovered actions under the original NSE symbol.
"""
from __future__ import annotations

import pandas as pd
import pytest

import nse_universe.actions.fetch as fetch
from nse_universe.actions.fetch import SYMBOL_REMAP, _candidates, _fetch_one


def test_candidates_default_is_ns_then_bo():
    assert _candidates("RELIANCE") == ["RELIANCE.NS", "RELIANCE.BO"]


def test_candidates_uses_explicit_remap():
    assert _candidates("TATAMOTORS") == SYMBOL_REMAP["TATAMOTORS"]
    assert _candidates("TATAMOTORS")[0] == "TMPV.NS"


@pytest.mark.parametrize("nse,ytk", [
    ("TATAMOTORS", "TMPV.NS"), ("LTIM", "LTM.NS"), ("PEL", "PIRAMALFIN.NS"),
    ("SWANENERGY", "SWANCORP.NS"), ("AKZOINDIA", "JSWDULUX.NS"),
    ("ITDCEM", "CEMPRO.NS"), ("SEQUENT", "VIYASH.NS"), ("SMLISUZU", "SMLMAH.NS"),
    ("SUNDARMHLD", "TSFINV.NS"), ("UDAICEMENT", "JKLAKSHMI.NS"),
    ("ARISINFRA", "ARISINFRA.BO"),   # .BO-only: Yahoo .NS not live
])
def test_verified_remaps_registered(nse, ytk):
    assert _candidates(nse)[0] == ytk


def test_remap_bse_fallback_appended():
    # non-.BO-only entries keep a BSE fallback after the primary .NS ticker
    assert _candidates("ITDCEM") == ["CEMPRO.NS", "CEMPRO.BO"]
    assert _candidates("SUNDARMHLD") == ["TSFINV.NS"]   # no verified .BO


def _actions_df():
    idx = pd.to_datetime(["2020-01-01", "2021-06-01"])
    idx.name = "Date"
    return pd.DataFrame(
        {"Dividends": [1.5, 0.0], "Stock Splits": [0.0, 2.0]}, index=idx
    )


class _FakeTicker:
    def __init__(self, beh):
        self._beh = beh

    @property
    def actions(self):
        if isinstance(self._beh, str):
            if self._beh == "empty_history":
                raise AttributeError(
                    "'PriceHistory' object has no attribute '_dividends'")
            if self._beh == "no_actions":
                return pd.DataFrame()
        return self._beh


class _FakeYF:
    """Stands in for the yfinance module: Ticker(ytk) behaviour keyed by ticker."""
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def Ticker(self, ytk):
        self.calls.append(ytk)
        return _FakeTicker(self.behavior.get(ytk, "empty_history"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *a, **k: None)


def test_falls_back_to_bo_when_ns_empty(monkeypatch):
    # GENERICEQ is not in SYMBOL_REMAP → default [.NS, .BO] candidates
    fake = _FakeYF({"GENERICEQ.NS": "empty_history", "GENERICEQ.BO": _actions_df()})
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("GENERICEQ")
    assert stats.status == "ok"
    assert stats.matched == "GENERICEQ.BO"
    assert stats.splits == 1 and stats.dividends == 1
    assert list(df["symbol"].unique()) == ["GENERICEQ"]   # stored under NSE symbol
    assert fake.calls == ["GENERICEQ.NS", "GENERICEQ.BO"]


def test_first_candidate_ok_skips_fallback(monkeypatch):
    fake = _FakeYF({"RELIANCE.NS": _actions_df()})
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("RELIANCE")
    assert stats.status == "ok" and stats.matched == "RELIANCE.NS"
    assert fake.calls == ["RELIANCE.NS"]   # .BO never attempted


def test_no_actions_frame_does_not_fall_through(monkeypatch):
    # .NS has history but no dividends/splits → definitive, don't try .BO
    fake = _FakeYF({"ADANIGREEN.NS": "no_actions", "ADANIGREEN.BO": _actions_df()})
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("ADANIGREEN")
    assert stats.status == "no_actions"
    assert fake.calls == ["ADANIGREEN.NS"]


def test_all_candidates_empty_is_no_data(monkeypatch):
    fake = _FakeYF({})  # everything empty_history
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("DEADSTOCK")
    assert stats.status == "no_data"
    assert fake.calls == ["DEADSTOCK.NS", "DEADSTOCK.BO"]


def test_remapped_symbol_fetches_remap_ticker(monkeypatch):
    fake = _FakeYF({"TMPV.NS": _actions_df()})
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("TATAMOTORS")
    assert stats.status == "ok" and stats.matched == "TMPV.NS"
    assert list(df["symbol"].unique()) == ["TATAMOTORS"]   # NSE symbol, not TMPV
    assert fake.calls[0] == "TMPV.NS"


def test_error_on_all_candidates_is_error(monkeypatch):
    class _BoomYF:
        def __init__(self): self.calls = []
        def Ticker(self, ytk):
            self.calls.append(ytk)
            raise ValueError("network down")
    fake = _BoomYF()
    monkeypatch.setattr(fetch, "yf", fake)
    stats, df = _fetch_one("X")
    assert stats.status == "error"
    assert "network down" in stats.error
