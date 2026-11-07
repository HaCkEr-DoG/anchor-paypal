"""
Wave 132 — HDR Forecast Engine  POST /api/oracle/forecast-hdr

Tests:
  1. High dispute rate → higher tier
  2. Normal rate → NORMAL tier
  3. hdr_budget_remaining >= 0
  4. Scenario fight_all vs accept_all present
  5. current_rate computed correctly
  6. next_tier populated when not SEVERE
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/forecast-hdr", json=body).json()


def test_high_rate_elevated_tier():
    d = _post({
        "merchant_id": "M001",
        "transactions_last_90_days": 1000,
        "disputes_last_90_days": 60,  # 6% rate — SEVERE
    })
    assert d["current_tier"] in ("SEVERE", "FEE_TIER", "LIMITED")
    assert d["wave"] == 132


def test_normal_rate_tier():
    d = _post({
        "merchant_id": "M002",
        "transactions_last_90_days": 10000,
        "disputes_last_90_days": 10,  # 0.1% — NORMAL
    })
    assert d["current_tier"] == "NORMAL"


def test_budget_remaining_non_negative():
    d = _post({
        "merchant_id": "M003",
        "transactions_last_90_days": 1000,
        "disputes_last_90_days": 3,
    })
    assert d["hdr_budget_remaining"] >= 0


def test_scenarios_present():
    d = _post({
        "merchant_id": "M004",
        "transactions_last_90_days": 500,
        "disputes_last_90_days": 2,
        "pending_disputes_count": 3,
    })
    assert "projected_tier" in d["scenario_fight_all"]
    assert "projected_tier" in d["scenario_accept_all"]


def test_current_rate_computed():
    d = _post({
        "merchant_id": "M005",
        "transactions_last_90_days": 1000,
        "disputes_last_90_days": 5,
    })
    assert abs(d["current_rate"] - 0.005) < 0.001


def test_next_tier_when_not_severe():
    d = _post({
        "merchant_id": "M006",
        "transactions_last_90_days": 1000,
        "disputes_last_90_days": 2,
    })
    if d["current_tier"] != "SEVERE":
        assert d["next_tier"] is not None


def test_response_shape():
    d = _post({
        "merchant_id": "M007",
        "transactions_last_90_days": 500,
        "disputes_last_90_days": 1,
    })
    required = {
        "merchant_id", "current_rate", "current_tier",
        "disputes_until_next_tier", "next_tier",
        "hdr_budget_remaining", "scenario_fight_all",
        "scenario_accept_all", "recommendation", "summary", "wave",
    }
    assert required.issubset(d.keys())
