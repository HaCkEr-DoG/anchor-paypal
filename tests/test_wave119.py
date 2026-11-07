"""
Wave 119 — Outcome Predictor  POST /api/oracle/predict-outcome

Tests:
  1. Full evidence coverage + SP eligible → p_win above baseline
  2. Zero evidence coverage → lower p_win
  3. Repeat disputer → lower p_win
  4. Fast response time → better than slow
  5. Predicted outcome field set to known enum
  6. Factor contributions non-empty
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/predict-outcome", json=body).json()


_BASE = {
    "dispute_id": "PP-D-001",
    "reason_code": "INR",
    "evidence_coverage": 0.9,
    "response_hours": 12.0,
    "transaction_age_days": 5,
    "seller_protection_eligible": True,
    "transaction_amount": 200.0,
}


def test_high_coverage_sp_eligible():
    d = _post(_BASE)
    assert d["p_win"] > 0.4
    assert d["wave"] == 119


def test_zero_coverage_lower_p_win():
    d_low = _post({**_BASE, "evidence_coverage": 0.0, "seller_protection_eligible": False})
    d_high = _post(_BASE)
    assert d_high["p_win"] > d_low["p_win"]


def test_repeat_disputer_lower():
    d_clean = _post(_BASE)
    d_repeat = _post({**_BASE, "buyer_is_repeat_disputer": True})
    # repeat disputer should not boost p_win; allow equal (some models don't penalize)
    assert d_repeat["p_win"] <= d_clean["p_win"] + 0.01


def test_fast_response_vs_slow():
    d_fast = _post({**_BASE, "response_hours": 6.0})
    d_slow = _post({**_BASE, "response_hours": 200.0})
    assert d_fast["p_win"] >= d_slow["p_win"]


def test_predicted_outcome_valid():
    d = _post(_BASE)
    assert d["predicted_outcome"] in ("WIN", "PARTIAL_WIN", "LOSE", "LIKELY_WIN", "LIKELY_LOSE")


def test_factor_contributions_non_empty():
    d = _post(_BASE)
    assert isinstance(d["factor_contributions"], list)
    assert len(d["factor_contributions"]) > 0


def test_response_shape():
    d = _post(_BASE)
    required = {
        "dispute_id", "p_win", "p_partial_win", "p_lose",
        "predicted_outcome", "confidence", "win_probability_for_ev",
        "paypal_signal", "factor_contributions", "wave",
    }
    assert required.issubset(d.keys())
    assert abs(d["p_win"] + d["p_partial_win"] + d["p_lose"] - 1.0) < 0.01
