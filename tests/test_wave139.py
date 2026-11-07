"""
Wave 139 — Transaction Risk Scorer  POST /api/oracle/score-transaction-risk

Tests:
  1. High-risk signals (disposable email + new account + digital) → HIGH/CRITICAL
  2. Clean transaction → LOW or MEDIUM
  3. risk_score 0-100
  4. chargeback_probability 0-1
  5. top_signals is list
  6. recommended_action is valid
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-transaction-risk", json=body).json()


def test_high_risk_critical():
    d = _post({
        "transaction_id": "TXN-001",
        "merchant_id": "M-001",
        "amount": 500.0,
        "buyer_account_age_days": 3,
        "is_digital_delivery": True,
        "buyer_disputes_last_90d": 3,
        "failed_payment_attempts": 3,
        "buyer_email_domain": "tempmail.org",
        "buyer_orders_last_24h": 8,
        "item_category": "GIFT_CARDS",
    })
    assert d["risk_tier"] in ("HIGH", "CRITICAL")
    assert d["wave"] == 139


def test_clean_transaction_low():
    d = _post({
        "transaction_id": "TXN-002",
        "merchant_id": "M-001",
        "amount": 50.0,
        "buyer_account_age_days": 730,
        "buyer_prior_transactions": 20,
        "has_3ds_authentication": True,
        "buyer_email_domain": "gmail.com",
        "is_first_transaction_with_merchant": False,
    })
    assert d["risk_tier"] in ("LOW", "MEDIUM")


def test_risk_score_range():
    d = _post({
        "transaction_id": "TXN-003",
        "merchant_id": "M-001",
        "amount": 100.0,
    })
    assert 0 <= d["risk_score"] <= 100


def test_chargeback_probability_range():
    d = _post({
        "transaction_id": "TXN-004",
        "merchant_id": "M-001",
        "amount": 100.0,
    })
    assert 0.0 <= d["chargeback_probability"] <= 1.0


def test_top_signals_list():
    d = _post({
        "transaction_id": "TXN-005",
        "merchant_id": "M-001",
        "amount": 200.0,
        "buyer_account_age_days": 5,
    })
    assert isinstance(d["top_signals"], list)


def test_recommended_action_valid():
    d = _post({
        "transaction_id": "TXN-006",
        "merchant_id": "M-001",
        "amount": 100.0,
    })
    valid_actions = {"APPROVE", "APPROVE_MONITOR", "REVIEW", "REQUIRE_3DS", "DECLINE"}
    assert d["recommended_action"] in valid_actions


def test_response_shape():
    d = _post({
        "transaction_id": "TXN-007",
        "merchant_id": "M-001",
        "amount": 75.0,
    })
    required = {
        "transaction_id", "merchant_id", "risk_score", "risk_tier",
        "chargeback_probability", "recommended_action", "action_reason",
        "top_signals", "address_risk_score", "card_risk_score",
        "buyer_risk_score", "velocity_risk_score", "item_risk_score",
        "mitigating_factors", "summary", "wave",
    }
    assert required.issubset(d.keys())
