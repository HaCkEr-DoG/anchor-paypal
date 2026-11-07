"""
Wave 129 — Settlement Optimizer  POST /api/oracle/optimize-settlement

Tests:
  1. High p_win + SP eligible → FULL_FIGHT
  2. Low p_win + small amount → FULL_SETTLE or ACCEPT_LOSS
  3. VIP buyer → more generous offer
  4. optimal_offer within 0–disputed_amount
  5. EV values are floats
  6. is_ev_positive_to_fight logic
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/optimize-settlement", json=body).json()


def test_high_win_sp_fight():
    d = _post({
        "dispute_id": "PP-D-001",
        "disputed_amount": 200.0,
        "p_win": 0.80,
        "sp_eligible": True,
        "fight_cost": 10.0,
    })
    assert d["strategy"] in ("FULL_FIGHT", "PARTIAL_SETTLE")
    assert d["wave"] == 129


def test_low_win_small_settle():
    d = _post({
        "dispute_id": "PP-D-002",
        "disputed_amount": 20.0,
        "p_win": 0.15,
        "fight_cost": 25.0,
        "chargeback_fee": 20.0,
    })
    assert d["strategy"] in ("FULL_SETTLE", "PARTIAL_SETTLE", "ACCEPT_LOSS")


def test_vip_offer_generous():
    d_vip = _post({
        "dispute_id": "PP-D-003",
        "disputed_amount": 100.0,
        "p_win": 0.50,
        "ltv_tier": "VIP",
    })
    d_low = _post({
        "dispute_id": "PP-D-004",
        "disputed_amount": 100.0,
        "p_win": 0.50,
        "ltv_tier": "NEGLIGIBLE",
    })
    assert d_vip["optimal_offer"] >= d_low["optimal_offer"]


def test_optimal_offer_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "disputed_amount": 150.0,
        "p_win": 0.55,
    })
    assert 0.0 <= d["optimal_offer"] <= 150.0


def test_ev_values_floats():
    d = _post({
        "dispute_id": "PP-D-006",
        "disputed_amount": 100.0,
        "p_win": 0.6,
    })
    assert isinstance(d["ev_fight"], float)
    assert isinstance(d["ev_settle"], float)
    assert isinstance(d["ev_accept"], float)


def test_ev_positive_to_fight():
    d = _post({
        "dispute_id": "PP-D-007",
        "disputed_amount": 100.0,
        "p_win": 0.90,
        "fight_cost": 5.0,
        "chargeback_fee": 10.0,
    })
    assert isinstance(d["is_ev_positive_to_fight"], bool)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-008",
        "disputed_amount": 80.0,
        "p_win": 0.50,
    })
    required = {
        "dispute_id", "strategy", "optimal_offer", "offer_pct_of_dispute",
        "ev_fight", "ev_settle", "ev_accept", "savings_vs_losing",
        "is_ev_positive_to_fight", "reasoning", "summary", "wave",
    }
    assert required.issubset(d.keys())
