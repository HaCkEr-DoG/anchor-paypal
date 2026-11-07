"""
Wave 95 — Dispute Cost Estimator  POST /api/oracle/dispute-cost

Tests:
  1. Basic fight EV calculation
  2. Do-nothing is always worse than fighting when win_prob is high
  3. Settle scenario included when settlement_offer_usd provided
  4. Settlement saves money vs losing
  5. Best action = settle when offer beats fight EV
  6. Sunk costs reflected in P&L but not in net
  7. Response shape complete
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/dispute-cost", json=body).json()


# ---------------------------------------------------------------------------
# 1. Basic fight EV: win_prob=0.8, amount=100, hourly=0, pp_fee=0
# ---------------------------------------------------------------------------

def test_fight_ev_basic():
    # fight_win_net = 100 - 0 = 100; fight_lose_net = -(100+0+0) = -100
    # EV = 0.8*100 + 0.2*(-100) = 80 - 20 = 60
    d = _post({"amount_usd": 100, "win_probability": 0.8, "hourly_rate_usd": 0,
               "paypal_dispute_fee_usd": 0, "hours_to_respond": 0})
    assert d["expected_outcome"]["fight_ev_usd"] == 60.0


# ---------------------------------------------------------------------------
# 2. Fight always better than do-nothing when win_prob is very high
# ---------------------------------------------------------------------------

def test_fight_better_than_nothing_high_win():
    d = _post({"amount_usd": 500, "win_probability": 0.9})
    fight_ev = d["expected_outcome"]["fight_ev_usd"]
    nothing_net = d["scenarios"]["do_nothing"]["net_outcome_usd"]
    assert fight_ev > nothing_net


# ---------------------------------------------------------------------------
# 3. Settle scenario present when offer provided
# ---------------------------------------------------------------------------

def test_settle_scenario_present():
    d = _post({"amount_usd": 100, "win_probability": 0.5, "settlement_offer_usd": 40})
    assert "settle" in d["scenarios"]
    assert d["scenarios"]["settle"]["settlement_offer_usd"] == 40.0


# ---------------------------------------------------------------------------
# 4. Settlement savings vs losing
# ---------------------------------------------------------------------------

def test_settle_saves_vs_losing():
    d = _post({
        "amount_usd": 200,
        "win_probability": 0.5,
        "settlement_offer_usd": 60,
        "paypal_dispute_fee_usd": 15,
        "hourly_rate_usd": 0,
    })
    settle = d["scenarios"]["settle"]
    assert settle["savings_vs_fight_loss_usd"] > 0


# ---------------------------------------------------------------------------
# 5. Best action = settle when it beats fight EV
# ---------------------------------------------------------------------------

def test_best_action_settle():
    # Low win prob → fight EV is negative, settle at $1 wins
    d = _post({
        "amount_usd": 100,
        "win_probability": 0.1,
        "settlement_offer_usd": 1,
        "hourly_rate_usd": 0,
        "paypal_dispute_fee_usd": 0,
        "hours_to_respond": 0,
    })
    assert d["expected_outcome"]["best_action"] == "settle"


# ---------------------------------------------------------------------------
# 6. Sunk costs in P&L
# ---------------------------------------------------------------------------

def test_sunk_costs_pnl():
    d = _post({
        "amount_usd": 100,
        "win_probability": 1.0,
        "cost_of_goods_usd": 30,
        "shipping_cost_usd": 10,
        "hourly_rate_usd": 0,
        "hours_to_respond": 0,
        "paypal_dispute_fee_usd": 0,
    })
    # net_outcome = 100 (keep the money, win)
    # pnl = 100 - 40 = 60
    assert d["scenarios"]["fight_and_win"]["net_outcome_usd"] == 100.0
    assert d["scenarios"]["fight_and_win"]["total_pnl_usd"] == 60.0
    assert d["cost_breakdown"]["sunk_costs_usd"] == 40.0


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({"amount_usd": 150, "win_probability": 0.6})
    assert "scenarios" in d
    assert "expected_outcome" in d
    assert "cost_breakdown" in d
    assert "fight_and_win" in d["scenarios"]
    assert "fight_and_lose" in d["scenarios"]
    assert "do_nothing" in d["scenarios"]
