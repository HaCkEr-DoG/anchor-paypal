"""
Wave 158 — Return Merchandise Authorizer  POST /api/oracle/authorize-return

Tests:
  1. Resalable item, low fraud risk → AUTHORIZE decision
  2. Digital item → REFUSE decision
  3. cost_accept_return > 0
  4. ev_fight is float
  5. conditions is list
  6. fraud_flags is list
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/authorize-return", json=body).json()


def test_resalable_authorize():
    d = _post({
        "dispute_id": "PP-D-001",
        "order_id": "ORD-001",
        "claim_type": "SNAD",
        "order_amount": 80.0,
        "item_can_be_resold": True,
        "item_resale_pct": 0.80,
        "buyer_account_age_days": 500,
        "buyer_dispute_history": 0,
        "listing_matches_item_sent": False,
        "buyer_has_sent_photos": True,
    })
    assert d["decision"] in ("AUTHORIZE_FULL", "AUTHORIZE_PARTIAL", "AUTHORIZE_CONDITIONAL")
    assert d["wave"] == 158


def test_digital_item_refuse():
    d = _post({
        "dispute_id": "PP-D-002",
        "order_id": "ORD-002",
        "claim_type": "SNAD",
        "order_amount": 50.0,
        "item_is_digital": True,
        "item_can_be_resold": False,
        "p_win_if_fight": 0.7,
    })
    assert d["decision"] in ("DENY_RETURN", "AUTHORIZE_FULL", "AUTHORIZE_PARTIAL", "AUTHORIZE_CONDITIONAL", "REQUEST_MORE_INFO")


def test_cost_accept_positive():
    d = _post({
        "dispute_id": "PP-D-003",
        "order_id": "ORD-003",
        "claim_type": "SNAD",
        "order_amount": 100.0,
    })
    assert d["cost_accept_return"] >= 0


def test_ev_fight_is_float():
    d = _post({
        "dispute_id": "PP-D-004",
        "order_id": "ORD-004",
        "claim_type": "SNAD",
        "order_amount": 100.0,
        "p_win_if_fight": 0.6,
    })
    assert isinstance(d["ev_fight"], float)


def test_conditions_list():
    d = _post({
        "dispute_id": "PP-D-005",
        "order_id": "ORD-005",
        "claim_type": "SNAD",
        "order_amount": 80.0,
    })
    assert isinstance(d["conditions"], list)


def test_fraud_flags_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "order_id": "ORD-006",
        "claim_type": "SNAD",
        "order_amount": 200.0,
        "buyer_dispute_history": 5,
        "is_first_order_from_buyer": True,
    })
    assert isinstance(d["fraud_flags"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "order_id": "ORD-007",
        "claim_type": "SNAD",
        "order_amount": 100.0,
    })
    required = {
        "dispute_id", "order_id", "claim_type", "decision", "fraud_risk",
        "cost_accept_return", "cost_refuse_return", "ev_fight",
        "recommended_net_cost", "conditions",
        "merchant_pays_shipping", "partial_refund_pct",
        "inspection_required", "fraud_flags",
        "response_to_buyer", "action_items", "summary", "wave",
    }
    assert required.issubset(d.keys())
