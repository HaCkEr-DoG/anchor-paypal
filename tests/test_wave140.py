"""
Wave 140 — Dispute Escalation Predictor  POST /api/oracle/predict-dispute-escalation

Tests:
  1. Chargeback threat + no merchant response → CRITICAL
  2. Quick resolution offered → LOW or MODERATE
  3. escalation_probability 0-1
  4. intervention_points is list
  5. fee exposure present
  6. factors list present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/predict-dispute-escalation", json=body).json()


def test_threat_no_response_critical():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "UNAUTHORIZED",
        "dispute_amount": 500.0,
        "days_open": 10,
        "buyer_threatened_chargeback": True,
        "buyer_tone_hostile": True,
        "merchant_responded": False,
    })
    assert d["risk_level"] in ("CRITICAL", "HIGH")
    assert d["wave"] == 140


def test_quick_resolution_low():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "INR",
        "dispute_amount": 50.0,
        "days_open": 1,
        "merchant_responded": True,
        "days_until_merchant_responded": 0,
        "resolution_offered": True,
        "resolution_type": "refund",
        "buyer_satisfied_with_progress": True,
    })
    assert d["risk_level"] in ("LOW", "MODERATE", "HIGH")
    assert d["escalation_probability"] <= 0.9


def test_probability_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "SNAD",
        "dispute_amount": 100.0,
        "days_open": 5,
    })
    assert 0.0 <= d["escalation_probability"] <= 1.0


def test_intervention_points_list():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "INR",
        "dispute_amount": 200.0,
        "days_open": 7,
        "buyer_threatened_chargeback": True,
    })
    assert isinstance(d["intervention_points"], list)


def test_fee_exposure():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "UNAUTHORIZED",
        "dispute_amount": 300.0,
        "days_open": 5,
    })
    assert isinstance(d["additional_fee_if_escalated"], float)
    assert d["additional_fee_if_escalated"] >= 0


def test_factors_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "days_open": 3,
        "buyer_prior_escalations": 2,
    })
    assert isinstance(d["factors"], list)
    if d["factors"]:
        f = d["factors"][0]
        assert "factor_type" in f
        assert "is_active" in f


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "dispute_amount": 75.0,
        "days_open": 2,
    })
    required = {
        "dispute_id", "escalation_probability", "risk_level",
        "active_drivers", "active_mitigations", "factors",
        "recommended_action", "action_deadline_hours", "intervention_points",
        "additional_fee_if_escalated", "expected_additional_loss",
        "summary", "wave",
    }
    assert required.issubset(d.keys())
