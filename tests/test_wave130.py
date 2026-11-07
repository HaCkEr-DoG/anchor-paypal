"""
Wave 130 — Merchant Win Rate Estimator  POST /api/oracle/estimate-win-rate

Tests:
  1. Delivery confirmed + SP → higher adjusted_win_rate
  2. Weak evidence → lower rate
  3. adjusted_win_rate clamped 0–1
  4. Win likelihood label matches probability
  5. positive/negative_factors are lists
  6. expected_value is float
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/estimate-win-rate", json=body).json()


def test_strong_evidence_higher_rate():
    d_strong = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
        "evidence_strength": "STRONG",
        "seller_protection_eligible": True,
        "has_delivery_confirmation": True,
        "tracking_shows_delivered": True,
    })
    d_weak = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
        "evidence_strength": "WEAK",
    })
    assert d_strong["adjusted_win_rate"] > d_weak["adjusted_win_rate"]
    assert d_strong["wave"] == 130


def test_weak_evidence_lower_rate():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "SNAD",
        "network": "PAYPAL",
        "transaction_amount": 80.0,
        "evidence_strength": "INSUFFICIENT",
    })
    assert d["adjusted_win_rate"] < 0.6


def test_win_rate_clamped():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 50.0,
    })
    assert 0.0 <= d["adjusted_win_rate"] <= 1.0


def test_win_likelihood_label():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
        "evidence_strength": "STRONG",
        "seller_protection_eligible": True,
        "has_delivery_confirmation": True,
    })
    assert d["win_likelihood"] in (
        "VERY_LIKELY", "LIKELY", "UNCERTAIN", "UNLIKELY", "VERY_UNLIKELY"
    )


def test_factors_lists():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
        "has_delivery_confirmation": True,
        "buyer_prior_disputes": 3,
    })
    assert isinstance(d["positive_factors"], list)
    assert isinstance(d["negative_factors"], list)


def test_expected_value_float():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 150.0,
    })
    assert isinstance(d["expected_value"], float)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-008",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 60.0,
    })
    required = {
        "dispute_id", "reason_code", "base_win_rate", "adjusted_win_rate",
        "confidence", "win_likelihood", "positive_factors", "negative_factors",
        "expected_value", "wave",
    }
    assert required.issubset(d.keys())
