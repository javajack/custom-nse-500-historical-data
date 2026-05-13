"""Tests for NSE GSM/ASM surveillance parser.

Pure-function tests only — no live network. The end-to-end ingest is
exercised via the smoke test in test_surveillance_smoke (best-effort).
"""
from __future__ import annotations

import pytest

from nse_universe.fetch.surveillance import (
    _extract_stage, _parse_asm, _parse_gsm,
)


def test_parse_gsm_basic_roman():
    """Real NSE /api/reportGsm returns a bare list with gsmStage in Roman."""
    payload = [
        {"symbol": "ABC", "gsmStage": "II"},
        {"symbol": "XYZ", "gsmStage": "IV"},
        {"symbol": "ZERO", "gsmStage": "0"},
    ]
    assert _parse_gsm(payload) == {"ABC": 2, "XYZ": 4, "ZERO": 0}


def test_parse_gsm_composite_code_falls_back_to_survdesc():
    """Composite codes like LXII still appear with survDesc containing the stage."""
    payload = [
        {"symbol": "COMP1", "gsmStage": "LXIII",
         "survDesc": "GSM stage I and Insolvency and Bankruptcy Code"},
        {"symbol": "PURE", "gsmStage": "VI",
         "survDesc": "Graded Surveillance Measure - Stage VI"},
    ]
    out = _parse_gsm(payload)
    assert out["COMP1"] == 1   # extracted "stage I" from survDesc
    assert out["PURE"] == 6    # direct parse


def test_parse_gsm_composite_without_survdesc_defaults_to_4():
    """If a row has an unparseable stage AND no survDesc, default to 4."""
    payload = [{"symbol": "MYSTERY", "gsmStage": "XYZ"}]
    assert _parse_gsm(payload) == {"MYSTERY": 4}


def test_parse_gsm_drops_rows_without_symbol():
    payload = [
        {"symbol": "OK", "gsmStage": "I"},
        {"gsmStage": "II"},  # no symbol
    ]
    assert _parse_gsm(payload) == {"OK": 1}


def test_parse_gsm_handles_empty():
    assert _parse_gsm({}) == {}
    assert _parse_gsm({"data": None}) == {}
    assert _parse_gsm({"data": []}) == {}
    assert _parse_gsm(None) == {}


def test_parse_asm_canonical_nested_longterm():
    """Real NSE /api/reportASM nests under longterm.data and uses asmSurvIndicator."""
    payload = {"longterm": {"data": [
        {"symbol": "FOO", "asmSurvIndicator": "Stage I"},
        {"symbol": "BAR", "asmSurvIndicator": "Stage III"},
    ]}}
    assert _parse_asm(payload) == {"FOO": 1, "BAR": 3}


def test_parse_asm_alternative_payload_keys():
    """Fallback keys for ASM payload shapes that NSE has used in the past."""
    payload = {"longtermdata": [
        {"SYMBOL": "Q1", "LONGTERM_STAGE": "II"},
        {"Symbol": "Q2", "stage": "I"},
    ]}
    assert _parse_asm(payload) == {"Q1": 2, "Q2": 1}


def test_extract_stage_handles_various_shapes():
    assert _extract_stage("Stage I") == 1
    assert _extract_stage("3") == 3
    assert _extract_stage("II") == 2
    assert _extract_stage(4) == 4
    assert _extract_stage("0") == 0
    assert _extract_stage("VI") == 6
    assert _extract_stage(None) is None
    assert _extract_stage("") is None
    assert _extract_stage("nonsense") is None
    # Out-of-range numerics
    assert _extract_stage(7) is None
    assert _extract_stage(-1) is None


def test_parse_symbol_case_normalisation():
    payload = [
        {"symbol": "  abc  ", "gsmStage": "I"},
    ]
    assert _parse_gsm(payload) == {"ABC": 1}
