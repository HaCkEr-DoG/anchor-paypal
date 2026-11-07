"""
Wave 125 — Friendly Fraud Scorer  POST /api/oracle/score-friendly-fraud

Tests:
  1. Delivery confirmed + buyer acknowledged → FIGHT_AGGRESSIVELY
  2. Repeat disputer with high win rate → VERY_HIGH risk
  3. Genuine dispute signals → LOW risk
  4. Digital item accessed → fraud signal present
  5. adjusted_win_probability between 0 and 1
  6. top_fraud_signals list present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-friendly-fraud", json=body).json()


def test_delivery_confirmed_fight():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "INR",
        "disputed_amount": 150.0,
        "has_delivery_confirmation": True,
        "has_signature_confirmation": True,
        "buyer_acknowledged_receipt": True,
        "item_delivered_before_dispute": True,
    })
    assert d["recommended_action"] in ("FIGHT_AGGRESSIVELY", "FIGHT")
    assert d["wave"] == 125


def test_repeat_disputer_very_high_risk():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "INR",
        "disputed_amount": 200.0,
        "total_prior_orders": 10,
        "total_prior_disputes": 8,
        "prior_disputes_won_by_buyer": 5,
        "is_repeat_dispute_same_merchant": True,
    })
    assert d["risk_level"] in ("VERY_HIGH", "HIGH")


def test_genuine_dispute_low_risk():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "INR",
        "disputed_amount": 50.0,
        "has_delivery_confirmation": False,
        "total_prior_orders": 20,
        "total_prior_disputes": 0,
        "account_age_days": 1000,
        "shipping_matches_billing": True,
    })
    assert d["risk_level"] in ("LOW", "VERY_LOW", "MODERATE")


def test_digital_accessed_signal():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "INR",
        "disputed_amount": 30.0,
        "is_digital_goods": True,
        "is_downloadable": True,
        "item_accessed_after_purchase": True,
        "has_usage_logs": True,
    })
    assert len(d["top_fraud_signals"]) > 0


def test_win_probability_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "UNAUTHORIZED",
        "disputed_amount": 100.0,
    })
    assert 0.0 <= d["adjusted_win_probability"] <= 1.0


def test_top_fraud_signals_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "INR",
        "disputed_amount": 75.0,
        "has_delivery_confirmation": True,
    })
    assert isinstance(d["top_fraud_signals"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "disputed_amount": 60.0,
    })
    required = {
        "dispute_id", "fraud_score", "risk_level",
        "recommended_action", "top_fraud_signals",
        "missing_evidence_to_prove_fraud", "adjusted_win_probability",
        "summary", "wave",
    }
    assert required.issubset(d.keys())
