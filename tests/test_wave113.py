"""
Wave 113 — Auto-Response Strategy Planner  POST /api/oracle/auto-response-plan

Tests:
  1. INR with full evidence → FIGHT strategy
  2. Tiny amount → ACCEPT strategy
  3. UNAUTHORIZED with IP log → response generated
  4. SNAD hostile buyer → tone is DE_ESCALATE or FIRM
  5. SP eligible → sp_note present
  6. CE3.0 applicable → escalate strategy
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/auto-response-plan", json=body).json()


def test_inr_full_evidence_fight():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "INR",
        "transaction_amount": 150.0,
        "has_tracking_number": True,
        "has_delivery_confirmation": True,
        "p_win": 0.75,
    })
    assert d["strategy"] == "FIGHT"
    assert d["wave"] == 113


def test_tiny_amount_accept():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "INR",
        "transaction_amount": 5.0,
        "p_win": 0.5,
        "fight_cost": 10.0,
        "chargeback_fee": 20.0,
    })
    assert d["strategy"] == "ACCEPT"


def test_unauthorized_with_ip_log():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "UNAUTHORIZED",
        "transaction_amount": 200.0,
        "has_ip_log": True,
        "has_two_fa_record": True,
        "p_win": 0.65,
    })
    assert d["claim_type"] == "UNAUTHORIZED"
    assert len(d["key_points"]) > 0


def test_snad_hostile_buyer_tone():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "SNAD",
        "transaction_amount": 80.0,
        "buyer_hostile": True,
        "has_product_photos": True,
    })
    assert d["tone"] in ("DE_ESCALATE", "FIRM", "COOPERATIVE", "FORMAL")


def test_sp_eligible_note():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "INR",
        "transaction_amount": 300.0,
        "sp_eligible": True,
        "has_tracking_number": True,
        "has_delivery_confirmation": True,
    })
    assert d["sp_note"] is not None
    assert len(d["sp_note"]) > 10


def test_ce30_escalate():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "UNAUTHORIZED",
        "transaction_amount": 500.0,
        "ce30_applicable": True,
        "has_ip_log": True,
        "p_win": 0.70,
        "fight_cost": 10.0,
        "chargeback_fee": 20.0,
    })
    assert d["strategy"] == "ESCALATE"
    assert d["ce30_note"] is not None


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "transaction_amount": 100.0,
    })
    required = {
        "dispute_id", "claim_type", "strategy", "tone",
        "evidence_availability", "key_points", "evidence_checklist",
        "summary", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["key_points"], list)
    assert isinstance(d["evidence_checklist"], list)
    assert len(d["summary"]) > 20
