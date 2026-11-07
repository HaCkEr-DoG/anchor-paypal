"""
Wave 127 — Seller Protection Checker  POST /api/oracle/check-seller-protection

Tests:
  1. Physical shipment with tracking + delivery → ELIGIBLE
  2. Digital goods → INELIGIBLE for INR
  3. P2P money transfer → INELIGIBLE
  4. Unauthorized + confirmed payment → FIGHT action
  5. fight_confidence between 0 and 1
  6. ineligible_reasons list present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/check-seller-protection", json=body).json()


def test_physical_eligible():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "transaction_amount": 100.0,
        "fulfillment_method": "PHYSICAL_SHIPMENT",
        "has_tracking_number": True,
        "delivery_confirmed": True,
        "paid_via_paypal_checkout": True,
        "buyer_address_confirmed": True,
    })
    assert d["eligibility_status"] in ("ELIGIBLE", "PARTIALLY_ELIGIBLE")
    assert d["recommended_action"] in ("FIGHT", "CONDITIONAL_FIGHT")
    assert d["wave"] == 127


def test_digital_goods_ineligible():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "transaction_amount": 50.0,
        "intangible_goods": True,
    })
    assert d["eligibility_status"] in ("INELIGIBLE", "UNCERTAIN")


def test_p2p_transfer_ineligible():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "UNAUTHORIZED",
        "transaction_amount": 200.0,
        "is_money_transfer": True,
    })
    assert d["eligibility_status"] == "INELIGIBLE"
    assert len(d["ineligible_reasons"]) > 0


def test_unauthorized_confirmed_payment():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "UNAUTHORIZED",
        "transaction_amount": 150.0,
        "paid_via_paypal_checkout": True,
        "buyer_address_confirmed": True,
    })
    assert d["recommended_action"] in ("FIGHT", "CONDITIONAL_FIGHT", "ESCALATE")


def test_fight_confidence_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "INR",
        "transaction_amount": 80.0,
        "has_tracking_number": True,
        "delivery_confirmed": True,
    })
    assert 0.0 <= d["fight_confidence"] <= 1.0


def test_ineligible_reasons_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "INR",
        "transaction_amount": 100.0,
        "is_gambling": True,
    })
    assert isinstance(d["ineligible_reasons"], list)
    assert len(d["ineligible_reasons"]) > 0


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "transaction_amount": 60.0,
    })
    required = {
        "dispute_id", "reason_code", "eligibility_status", "protection_type",
        "recommended_action", "fight_confidence", "ineligible_reasons",
        "conditional_warnings", "paypal_signal", "action_detail", "wave",
    }
    assert required.issubset(d.keys())
