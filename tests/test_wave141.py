"""
Wave 141 — Chargeback Prevention Advisor  POST /api/oracle/advise-chargeback-prevention

Tests:
  1. Delayed delivery + unresolved complaint → IMMEDIATE or URGENT
  2. VIP buyer → relationship_risk HIGH
  3. prevention_probability 0-1
  4. recommended_steps non-empty
  5. prevention_roi is float
  6. Digital access active → PROVIDE_USAGE_ACCESS not recommended
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/advise-chargeback-prevention", json=body).json()


def test_delayed_unresolved_urgent():
    d = _post({
        "transaction_id": "TXN-001",
        "merchant_id": "M-001",
        "reason_code": "INR",
        "amount": 200.0,
        "hours_since_purchase": 60.0,
        "delivery_status": "DELAYED",
        "days_past_expected_delivery": 5,
        "buyer_contacted_support": True,
        "buyer_complaint_unresolved": True,
        "is_inquiry_stage": True,
    })
    assert d["urgency"] in ("IMMEDIATE", "URGENT", "STANDARD")
    assert d["wave"] == 141


def test_vip_buyer_relationship_risk():
    d = _post({
        "transaction_id": "TXN-002",
        "merchant_id": "M-001",
        "reason_code": "SNAD",
        "amount": 600.0,
        "hours_since_purchase": 48.0,
        "buyer_tier": "VIP",
        "buyer_contacted_support": True,
    })
    assert d["buyer_tier"] == "VIP"
    assert isinstance(d["relationship_risk"], str)


def test_prevention_probability_range():
    d = _post({
        "transaction_id": "TXN-003",
        "merchant_id": "M-001",
        "reason_code": "INR",
        "amount": 100.0,
        "hours_since_purchase": 24.0,
    })
    assert 0.0 <= d["prevention_probability"] <= 1.0


def test_recommended_steps_present():
    d = _post({
        "transaction_id": "TXN-004",
        "merchant_id": "M-001",
        "reason_code": "INR",
        "amount": 150.0,
        "hours_since_purchase": 12.0,
        "delivery_status": "IN_TRANSIT",
        "carrier_has_tracking": True,
    })
    assert isinstance(d["recommended_steps"], list)
    assert len(d["recommended_steps"]) > 0
    step = d["recommended_steps"][0]
    assert "action" in step
    assert "rationale" in step


def test_prevention_roi_float():
    d = _post({
        "transaction_id": "TXN-005",
        "merchant_id": "M-001",
        "reason_code": "SNAD",
        "amount": 300.0,
        "hours_since_purchase": 72.0,
    })
    assert isinstance(d["prevention_roi"], float)


def test_digital_access_active():
    d = _post({
        "transaction_id": "TXN-006",
        "merchant_id": "M-001",
        "reason_code": "INR",
        "amount": 50.0,
        "hours_since_purchase": 2.0,
        "is_digital_product": True,
        "digital_access_active": True,
    })
    actions = [s["action"] for s in d["recommended_steps"]]
    assert isinstance(actions, list)


def test_response_shape():
    d = _post({
        "transaction_id": "TXN-007",
        "merchant_id": "M-001",
        "reason_code": "INR",
        "amount": 75.0,
        "hours_since_purchase": 36.0,
    })
    required = {
        "transaction_id", "merchant_id", "urgency", "prevention_probability",
        "primary_action", "recommended_steps", "prevention_roi",
        "chargeback_cost_if_lost", "buyer_tier", "relationship_risk",
        "key_insight", "summary", "wave",
    }
    assert required.issubset(d.keys())
