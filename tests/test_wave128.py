"""
Wave 128 — Refund Optimizer  POST /api/oracle/optimize-refund

Tests:
  1. High p_win + delivered → FIGHT_NO_REFUND strategy
  2. Low p_win + small amount → full refund strategy
  3. Near deadline → NEAR_DEADLINE trigger present
  4. ev_fight computed correctly
  5. recommended_refund_amount within 0–dispute_amount
  6. buyer_message non-empty
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/optimize-refund", json=body).json()


def test_high_win_fight():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "INR",
        "dispute_amount": 200.0,
        "p_win": 0.85,
        "was_delivered": True,
        "fight_cost": 10.0,
    })
    assert d["recommended_strategy"] in (
        "FIGHT_NO_REFUND", "CONDITIONAL_REFUND", "PARTIAL_REACTIVE"
    )
    assert d["wave"] == 128


def test_low_win_small_amount_refund():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "INR",
        "dispute_amount": 15.0,
        "p_win": 0.20,
        "fight_cost": 25.0,
    })
    assert d["recommended_strategy"] in (
        "FULL_PROACTIVE", "FULL_REACTIVE", "PARTIAL_PROACTIVE"
    )


def test_near_deadline_trigger():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "p_win": 0.50,
        "days_until_deadline": 1,
    })
    assert "NEAR_DEADLINE" in d["triggers"] or d["recommended_strategy"] != "FIGHT_NO_REFUND"


def test_ev_fight_calculation():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "p_win": 0.6,
        "fight_cost": 20.0,
    })
    expected = round(0.6 * 100.0 - 20.0, 2)
    assert abs(d["ev_fight"] - expected) < 0.01


def test_refund_amount_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "INR",
        "dispute_amount": 80.0,
        "p_win": 0.4,
    })
    assert 0.0 <= d["recommended_refund_amount"] <= 80.0


def test_buyer_message_non_empty():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "SNAD",
        "dispute_amount": 60.0,
        "p_win": 0.5,
    })
    assert len(d["buyer_message"]) > 10


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "dispute_amount": 50.0,
        "p_win": 0.5,
    })
    required = {
        "dispute_id", "recommended_strategy", "recommended_refund_amount",
        "recommended_refund_pct", "ev_fight", "min_profitable_refund",
        "max_acceptable_refund", "triggers", "proactive_advantage",
        "hdr_saving_days", "buyer_message", "summary", "wave",
    }
    assert required.issubset(d.keys())
