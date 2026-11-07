"""
Wave 142 — Settlement Negotiation Advisor  POST /api/oracle/advise-settlement-negotiation

Tests:
  1. Low p_win + big offer → ACCEPT
  2. High p_win + small offer → REJECT_AND_FIGHT or COUNTEROFFER
  3. ev_accept and ev_reject are floats
  4. settlement_threshold > 0
  5. optimal_counteroffer when COUNTEROFFER
  6. paypal_cb_savings_usd > 0
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/advise-settlement-negotiation", json=body).json()


def test_low_pwin_accept():
    d = _post({
        "dispute_id": "PP-D-001",
        "claimed_amount": 200.0,
        "settlement_offer_amount": 70.0,
        "p_win": 0.2,
    })
    assert d["recommended_decision"] in ("ACCEPT", "COUNTEROFFER")
    assert d["wave"] == 142


def test_high_pwin_reject():
    d = _post({
        "dispute_id": "PP-D-002",
        "claimed_amount": 200.0,
        "settlement_offer_amount": 180.0,
        "p_win": 0.85,
    })
    assert d["recommended_decision"] in ("REJECT_AND_FIGHT", "COUNTEROFFER")


def test_ev_floats():
    d = _post({
        "dispute_id": "PP-D-003",
        "claimed_amount": 100.0,
        "settlement_offer_amount": 50.0,
        "p_win": 0.5,
    })
    assert isinstance(d["ev_accept"], float)
    assert isinstance(d["ev_reject"], float)


def test_settlement_threshold_positive():
    d = _post({
        "dispute_id": "PP-D-004",
        "claimed_amount": 150.0,
        "settlement_offer_amount": 60.0,
        "p_win": 0.4,
    })
    assert d["settlement_threshold"] > 0


def test_counteroffer_present_when_counteroffer():
    d = _post({
        "dispute_id": "PP-D-005",
        "claimed_amount": 300.0,
        "settlement_offer_amount": 280.0,
        "p_win": 0.6,
    })
    if d["recommended_decision"] == "COUNTEROFFER":
        assert d["optimal_counteroffer"] is not None
        assert d["optimal_counteroffer"] > 0


def test_paypal_cb_savings():
    d = _post({
        "dispute_id": "PP-D-006",
        "claimed_amount": 200.0,
        "settlement_offer_amount": 80.0,
        "p_win": 0.5,
    })
    assert isinstance(d["paypal_cb_savings_usd"], float)
    assert d["paypal_cb_savings_usd"] > 0


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claimed_amount": 100.0,
        "settlement_offer_amount": 40.0,
        "p_win": 0.5,
    })
    required = {
        "dispute_id", "ev_accept", "ev_reject", "settlement_threshold",
        "recommended_decision", "optimal_counteroffer",
        "total_savings_vs_worst_case", "paypal_cb_savings_usd",
        "recommendation", "paypal_signal", "wave",
    }
    assert required.issubset(d.keys())
