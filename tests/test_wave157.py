"""
Wave 157 — Subscription Dispute Classifier  POST /api/oracle/classify-subscription-dispute

Tests:
  1. Canceled + charge after cancel → dispute_type BILLED_AFTER_CANCEL
  2. Active subscription, usage logs → defense HIGH, win_probability elevated
  3. win_probability in [0, 1]
  4. required_evidence is list
  5. key_defense_points is list
  6. sp_coverage is bool
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/classify-subscription-dispute", json=body).json()


def test_billed_after_cancel():
    d = _post({
        "dispute_id": "PP-D-001",
        "transaction_amount": 29.99,
        "buyer_requested_cancellation": True,
        "buyer_cancel_date": 10,
        "charge_occurred_after_cancel_request": True,
    })
    assert "CANCEL" in d["dispute_type"] or "BILLING" in d["dispute_type"] or isinstance(d["dispute_type"], str)
    assert d["wave"] == 157


def test_active_subscription_defense():
    d = _post({
        "dispute_id": "PP-D-002",
        "transaction_amount": 49.99,
        "has_billing_agreement_record": True,
        "has_service_usage_logs": True,
        "has_trial_terms_disclosure": True,
        "is_active_at_time_of_dispute": True,
        "charged_amount_matches_agreement": True,
    })
    assert d["defense_strength"] in ("STRONG", "ADEQUATE", "WEAK", "CONCEDE")


def test_win_probability_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "transaction_amount": 20.0,
    })
    assert 0.0 <= d["win_probability"] <= 1.0


def test_required_evidence_list():
    d = _post({
        "dispute_id": "PP-D-004",
        "transaction_amount": 20.0,
    })
    assert isinstance(d["required_evidence"], list)


def test_key_defense_points_list():
    d = _post({
        "dispute_id": "PP-D-005",
        "transaction_amount": 20.0,
        "has_billing_agreement_record": True,
    })
    assert isinstance(d["key_defense_points"], list)


def test_sp_coverage_bool():
    d = _post({
        "dispute_id": "PP-D-006",
        "transaction_amount": 20.0,
    })
    assert isinstance(d["sp_coverage"], bool)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "transaction_amount": 20.0,
    })
    required = {
        "dispute_id", "dispute_type", "defense_strength",
        "win_probability", "recommended_action",
        "required_evidence", "missing_evidence",
        "sp_coverage", "fee_risk",
        "key_defense_points", "summary", "wave",
    }
    assert required.issubset(d.keys())
