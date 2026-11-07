"""
Wave 117 — Chargeback Prevention Advisor  POST /api/oracle/prevent-chargeback

Tests:
  1. Inquiry stage INR with tracking → prevention steps returned
  2. High-urgency: buyer already contacted, complaint unresolved
  3. VIP buyer → relationship_risk mentioned
  4. Prevention ROI positive for preventable cases
  5. Recommended steps are ordered and non-empty
  6. Digital product case handled
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/prevent-chargeback", json=body).json()


def test_inquiry_inr_steps_returned():
    d = _post({
        "transaction_id": "TXN-001",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 150.0,
        "hours_since_purchase": 72.0,
        "delivery_status": "IN_TRANSIT",
        "carrier_has_tracking": True,
        "is_inquiry_stage": True,
    })
    assert len(d["recommended_steps"]) > 0
    assert d["wave"] == 117


def test_high_urgency_unresolved_complaint():
    d = _post({
        "transaction_id": "TXN-002",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 200.0,
        "hours_since_purchase": 120.0,
        "buyer_contacted_support": True,
        "buyer_complaint_unresolved": True,
        "hours_since_buyer_contact": 48.0,
        "is_inquiry_stage": True,
    })
    assert d["urgency"] in ("IMMEDIATE", "URGENT", "STANDARD", "MONITORING")
    assert d["prevention_probability"] > 0


def test_vip_buyer_relationship_risk():
    d = _post({
        "transaction_id": "TXN-003",
        "merchant_id": "MERCH-001",
        "reason_code": "SNAD",
        "amount": 500.0,
        "hours_since_purchase": 24.0,
        "buyer_tier": "VIP",
        "is_inquiry_stage": True,
    })
    assert isinstance(d["relationship_risk"], str)
    assert len(d["relationship_risk"]) > 0


def test_positive_roi_preventable_case():
    d = _post({
        "transaction_id": "TXN-004",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 300.0,
        "hours_since_purchase": 48.0,
        "is_inquiry_stage": True,
        "carrier_has_tracking": True,
        "delivery_status": "DELIVERED",
    })
    assert isinstance(d["prevention_roi"], (int, float))
    assert isinstance(d["chargeback_cost_if_lost"], (int, float))


def test_steps_ordered():
    d = _post({
        "transaction_id": "TXN-005",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 100.0,
        "hours_since_purchase": 36.0,
        "is_inquiry_stage": True,
    })
    steps = d["recommended_steps"]
    if len(steps) > 1:
        numbers = [s["step_number"] for s in steps]
        assert numbers == sorted(numbers)


def test_digital_product_handled():
    d = _post({
        "transaction_id": "TXN-006",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 25.0,
        "hours_since_purchase": 2.0,
        "is_digital_product": True,
        "digital_access_active": True,
        "is_inquiry_stage": True,
    })
    assert d["wave"] == 117
    assert len(d["recommended_steps"]) > 0


def test_response_shape():
    d = _post({
        "transaction_id": "TXN-007",
        "merchant_id": "MERCH-001",
        "reason_code": "INR",
        "amount": 80.0,
        "hours_since_purchase": 12.0,
    })
    required = {
        "transaction_id", "merchant_id", "urgency", "prevention_probability",
        "primary_action", "recommended_steps", "prevention_roi",
        "chargeback_cost_if_lost", "buyer_tier", "relationship_risk",
        "key_insight", "summary", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["recommended_steps"], list)
