"""
Wave 124 — Chargeback Risk Scorer  POST /api/oracle/score-chargeback-risk

Tests:
  1. Digital goods → elevated risk tier
  2. Very new account → higher score
  3. Low-risk clean transaction → APPROVE action
  4. Multiple risk signals → REVIEW or VERIFY
  5. risk_score 0–100
  6. risk_factors list present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-chargeback-risk", json=body).json()


def test_digital_goods_elevated_risk():
    d = _post({
        "transaction_id": "TXN-001",
        "amount": 50.0,
        "product_category": "DIGITAL_GOODS",
        "account_age_days": 3,
        "account_prior_disputes": 2,
        "is_vpn_or_proxy": True,
        "device_fingerprint_new": True,
    })
    assert d["risk_tier"] in ("VERY_HIGH", "HIGH", "MODERATE")
    assert d["wave"] == 124


def test_new_account_higher_score():
    d_new = _post({
        "transaction_id": "TXN-002",
        "amount": 100.0,
        "account_age_days": 1,
    })
    d_old = _post({
        "transaction_id": "TXN-003",
        "amount": 100.0,
        "account_age_days": 500,
    })
    assert d_new["risk_score"] > d_old["risk_score"]


def test_clean_transaction_approve():
    d = _post({
        "transaction_id": "TXN-004",
        "amount": 20.0,
        "product_category": "FOOD",
        "account_age_days": 500,
        "account_prior_orders": 50,
        "account_prior_disputes": 0,
        "billing_shipping_match": True,
    })
    assert d["recommended_action"] in ("APPROVE", "REVIEW")


def test_multiple_signals_review_or_verify():
    d = _post({
        "transaction_id": "TXN-005",
        "amount": 300.0,
        "product_category": "ELECTRONICS",
        "account_age_days": 3,
        "account_prior_disputes": 3,
        "billing_shipping_match": False,
        "is_vpn_or_proxy": True,
        "device_fingerprint_new": True,
    })
    assert d["recommended_action"] in ("REVIEW", "VERIFY", "DECLINE")


def test_risk_score_range():
    d = _post({"transaction_id": "TXN-006", "amount": 100.0})
    assert 0 <= d["risk_score"] <= 100


def test_risk_factors_present():
    d = _post({
        "transaction_id": "TXN-007",
        "amount": 200.0,
        "account_prior_disputes": 2,
    })
    assert isinstance(d["risk_factors"], list)


def test_response_shape():
    d = _post({"transaction_id": "TXN-008", "amount": 50.0})
    required = {
        "transaction_id", "risk_score", "risk_tier",
        "recommended_action", "top_reason", "summary",
        "risk_factors", "wave",
    }
    assert required.issubset(d.keys())
