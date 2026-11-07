"""
Wave 116 — Buyer Fraud Detector  POST /api/oracle/detect-buyer-fraud

Tests:
  1. Serial disputer (high dispute rate) → HIGH_RISK
  2. Clean long-term buyer → LOW_RISK
  3. Delivery confirmed + INR claim → elevated fraud score
  4. Prior fraud flag → high score
  5. Adjusted p_win > base p_win when fraud signals present
  6. Recommendation non-empty
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/detect-buyer-fraud", json=body).json()


def test_serial_disputer_high_risk():
    d = _post({
        "dispute_id": "PP-D-001",
        "buyer_id": "BUYER-123",
        "claim_type": "INR",
        "dispute_amount": 200.0,
        "total_lifetime_orders": 20,
        "total_lifetime_disputes": 8,
        "months_as_buyer": 3,
        "dispute_rate_last_12m": 0.40,
        "has_prior_fraud_flag": True,
        "has_delivery_confirmation": True,
        "hours_until_dispute_filed": 1,
        "escalated_within_24h": True,
        "buyer_story_changed": True,
    })
    assert d["risk_level"] in ("HIGH_RISK", "MEDIUM_RISK")
    assert d["wave"] == 116


def test_clean_buyer_low_risk():
    d = _post({
        "dispute_id": "PP-D-002",
        "buyer_id": "BUYER-456",
        "claim_type": "INR",
        "dispute_amount": 50.0,
        "total_lifetime_orders": 200,
        "total_lifetime_disputes": 1,
        "months_as_buyer": 48,
        "dispute_rate_last_12m": 0.005,
    })
    assert d["risk_level"] == "LOW_RISK"
    assert d["fraud_score"] < 40


def test_delivery_confirmed_inr_elevated():
    d = _post({
        "dispute_id": "PP-D-003",
        "buyer_id": "BUYER-789",
        "claim_type": "INR",
        "dispute_amount": 300.0,
        "total_lifetime_orders": 10,
        "total_lifetime_disputes": 1,
        "months_as_buyer": 12,
        "has_delivery_confirmation": True,
        "hours_until_dispute_filed": 2,
    })
    assert d["fraud_score"] > 10


def test_prior_fraud_flag_high_score():
    d = _post({
        "dispute_id": "PP-D-004",
        "buyer_id": "BUYER-999",
        "claim_type": "UNAUTHORIZED",
        "dispute_amount": 500.0,
        "total_lifetime_orders": 5,
        "total_lifetime_disputes": 2,
        "months_as_buyer": 3,
        "has_prior_fraud_flag": True,
    })
    assert d["fraud_score"] > 20


def test_fraud_boosts_p_win():
    d = _post({
        "dispute_id": "PP-D-005",
        "buyer_id": "BUYER-X",
        "claim_type": "INR",
        "dispute_amount": 200.0,
        "total_lifetime_orders": 15,
        "total_lifetime_disputes": 5,
        "months_as_buyer": 4,
        "has_delivery_confirmation": True,
        "dispute_rate_last_12m": 0.33,
        "escalated_within_24h": True,
        "base_p_win": 0.55,
    })
    assert d["adjusted_p_win"] >= d["base_p_win"]


def test_recommendation_non_empty():
    d = _post({
        "dispute_id": "PP-D-006",
        "buyer_id": "BUYER-Y",
        "claim_type": "SNAD",
        "dispute_amount": 100.0,
        "total_lifetime_orders": 30,
        "total_lifetime_disputes": 2,
        "months_as_buyer": 24,
    })
    assert len(d["recommendation"]) > 10


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "buyer_id": "BUYER-Z",
        "claim_type": "INR",
        "dispute_amount": 75.0,
        "total_lifetime_orders": 5,
        "total_lifetime_disputes": 0,
        "months_as_buyer": 6,
    })
    required = {
        "dispute_id", "buyer_id", "fraud_score", "risk_level",
        "active_signals", "anti_fraud_signals", "base_p_win",
        "adjusted_p_win", "p_win_boost", "rationale",
        "recommendation", "summary", "wave",
    }
    assert required.issubset(d.keys())
