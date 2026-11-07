"""
Wave 93 — Dispute Resolution Time Analyzer  POST /api/oracle/resolution-time

Tests:
  1. UNAUTHORIZED claim → high confidence, fastest resolution
  2. ITEM_NOT_RECEIVED → medium confidence
  3. High win prob + high amount → fight recommendation
  4. Low fight EV + small amount → settle recommendation
  5. Borderline case → negotiate
  6. days_open reduces days_remaining correctly
  7. Unknown claim_type → normalized to OTHER
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/resolution-time", json=body).json()


# ---------------------------------------------------------------------------
# 1. UNAUTHORIZED → high confidence, fast resolution
# ---------------------------------------------------------------------------

def test_unauthorized_high_confidence():
    d = _post({"claim_type": "UNAUTHORIZED", "amount_usd": 200, "win_probability": 0.8})
    assert d["confidence"] == "high"
    assert d["estimated_resolution_days"] <= 15  # unauthorized is fastest
    assert d["claim_type"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 2. ITEM_NOT_RECEIVED → medium confidence
# ---------------------------------------------------------------------------

def test_item_not_received_medium_confidence():
    d = _post({"claim_type": "ITEM_NOT_RECEIVED", "amount_usd": 150, "win_probability": 0.6})
    assert d["confidence"] == "medium"
    assert d["estimated_resolution_days"] >= 15


# ---------------------------------------------------------------------------
# 3. High win prob + high amount → fight
# ---------------------------------------------------------------------------

def test_high_ev_recommends_fight():
    # fight_ev = 0.9*1000 - 0.1*1000 = 800
    # time_cost = 1.5h * $5/h * 20 days = 150
    # 800 >= 1.5 * 150 (225) → fight
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "amount_usd": 1000,
        "win_probability": 0.9,
        "hourly_cost_usd": 5,  # very low-cost merchant
    })
    assert d["settle_recommendation"] == "fight"
    assert d["fight_ev_usd"] > 0


# ---------------------------------------------------------------------------
# 4. Low fight EV + small amount → settle
# ---------------------------------------------------------------------------

def test_low_ev_small_amount_recommends_settle():
    d = _post({
        "claim_type": "SIGNIFICANTLY_NOT_AS_DESCRIBED",
        "amount_usd": 30,
        "win_probability": 0.3,
        "hourly_cost_usd": 100,  # expensive merchant
    })
    assert d["settle_recommendation"] == "settle"
    assert d["fight_ev_usd"] < 0  # negative EV


# ---------------------------------------------------------------------------
# 5. days_open reduces days_remaining
# ---------------------------------------------------------------------------

def test_days_remaining_decreases():
    base = _post({"claim_type": "OTHER", "amount_usd": 100, "days_open": 0})
    partial = _post({"claim_type": "OTHER", "amount_usd": 100, "days_open": 5})
    assert partial["days_remaining"] == base["days_remaining"] - 5


# ---------------------------------------------------------------------------
# 6. Unknown claim_type normalized to OTHER
# ---------------------------------------------------------------------------

def test_unknown_claim_type_normalized():
    d = _post({"claim_type": "BOGUS_TYPE", "amount_usd": 100})
    assert d["claim_type"] == "OTHER"


# ---------------------------------------------------------------------------
# 7. Response has all expected keys
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({"claim_type": "ITEM_NOT_RECEIVED", "amount_usd": 200, "win_probability": 0.5})
    required = {
        "dispute_id", "claim_type", "amount_usd", "estimated_resolution_days",
        "confidence", "days_remaining", "urgency", "cost_of_delay_per_day_usd",
        "total_time_cost_usd", "fight_ev_usd", "settlement_break_even_usd",
        "settle_recommendation", "settle_max_usd", "reasoning",
    }
    assert required.issubset(d.keys())
