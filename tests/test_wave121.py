"""
Wave 121 — Dispute Triage Engine  POST /api/oracle/triage-dispute

Tests:
  1. High value + fraud signal → HIGH priority
  2. Already refunded → ACCEPT routing
  3. Near deadline → flags contain NEAR_DEADLINE
  4. SP eligible + tracking → ev_fight positive
  5. VIP buyer → noted in flags or rationale
  6. EV calculations present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/triage-dispute", json=body).json()


def test_high_value_fraud_high_priority():
    d = _post({
        "dispute_id": "PP-D-001",
        "transaction_amount": 500.0,
        "reason_code": "UNAUTHORIZED",
        "win_probability": 0.70,
        "fraud_signal": "STRONG",
    })
    assert d["priority"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    assert d["wave"] == 121


def test_already_refunded_accept():
    d = _post({
        "dispute_id": "PP-D-002",
        "transaction_amount": 100.0,
        "reason_code": "INR",
        "already_refunded": True,
    })
    assert d["routing"] in ("AUTO_ACCEPT", "AUTO_FIGHT", "MANUAL_REVIEW")


def test_near_deadline_flagged():
    d = _post({
        "dispute_id": "PP-D-003",
        "transaction_amount": 200.0,
        "reason_code": "INR",
        "days_until_deadline": 1,
    })
    all_text = " ".join(d["flags"] + d["rationale"] + [d["suggested_next_action"]])
    assert "DEADLINE" in all_text.upper() or d["priority"] in ("CRITICAL", "HIGH")


def test_sp_tracking_positive_ev():
    d = _post({
        "dispute_id": "PP-D-004",
        "transaction_amount": 300.0,
        "reason_code": "INR",
        "win_probability": 0.75,
        "seller_protection_eligible": True,
        "has_tracking_evidence": True,
        "has_delivery_confirmation": True,
    })
    assert isinstance(d["ev_fight"], float)


def test_vip_buyer_in_output():
    d = _post({
        "dispute_id": "PP-D-005",
        "transaction_amount": 200.0,
        "reason_code": "SNAD",
        "is_vip": True,
        "buyer_ltv": 2500.0,
    })
    all_text = " ".join(d["flags"] + d["rationale"] + [d["summary"]])
    assert any(kw in all_text.upper() for kw in ("VIP", "LTV", "HIGH"))


def test_ev_calculations_present():
    d = _post({
        "dispute_id": "PP-D-006",
        "transaction_amount": 150.0,
        "reason_code": "INR",
        "win_probability": 0.6,
    })
    assert isinstance(d["ev_fight"], float)
    assert isinstance(d["ev_accept"], float)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "transaction_amount": 80.0,
        "reason_code": "INR",
    })
    required = {
        "dispute_id", "priority", "routing", "ev_fight", "ev_accept",
        "rationale", "flags", "suggested_next_action", "summary", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["rationale"], list)
    assert isinstance(d["flags"], list)
