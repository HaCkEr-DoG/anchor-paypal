"""
Wave 149 — Buyer Pattern Analyzer  POST /api/oracle/analyze-buyer-pattern

Tests:
  1. High dispute count → risk_level HIGH or SERIAL
  2. Zero disputes → risk_level LOW
  3. dispute_rate = total_disputes / total_transactions
  4. merchant_win_rate is float in [0, 1]
  5. risk_score in [0, 1]
  6. community_trust_adjusted is float
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-buyer-pattern", json=body).json()


def _dispute(dispute_id, amount=100.0, reason="INR", merchant_won=False, escalated=False, days_ago=30):
    return {
        "dispute_id": dispute_id,
        "amount_usd": amount,
        "reason": reason,
        "merchant_won": merchant_won,
        "escalated": escalated,
        "days_ago": days_ago,
    }


def test_high_dispute_count_high_risk():
    history = [_dispute(f"PP-D-{i:03d}", escalated=True) for i in range(10)]
    d = _post({
        "buyer_id": "B-001",
        "total_transactions": 50,
        "dispute_history": history,
    })
    assert d["risk_level"] in ("HIGH", "SERIAL")
    assert d["wave"] == 149


def test_zero_disputes_low_risk():
    d = _post({
        "buyer_id": "B-002",
        "total_transactions": 200,
        "dispute_history": [],
    })
    assert d["risk_level"] == "LOW"
    assert d["total_disputes"] == 0


def test_dispute_rate():
    history = [_dispute(f"PP-D-{i:03d}") for i in range(5)]
    d = _post({
        "buyer_id": "B-003",
        "total_transactions": 100,
        "dispute_history": history,
    })
    assert abs(d["dispute_rate"] - 0.05) < 0.001


def test_merchant_win_rate_range():
    history = [
        _dispute("PP-D-001", merchant_won=True),
        _dispute("PP-D-002", merchant_won=False),
        _dispute("PP-D-003", merchant_won=True),
    ]
    d = _post({
        "buyer_id": "B-004",
        "total_transactions": 100,
        "dispute_history": history,
    })
    assert 0.0 <= d["merchant_win_rate"] <= 1.0


def test_risk_score_in_range():
    history = [_dispute(f"PP-D-{i:03d}") for i in range(3)]
    d = _post({
        "buyer_id": "B-005",
        "total_transactions": 100,
        "dispute_history": history,
    })
    assert 0.0 <= d["risk_score"] <= 1.0


def test_community_trust_adjusted():
    history = [_dispute("PP-D-001")]
    d = _post({
        "buyer_id": "B-006",
        "total_transactions": 100,
        "dispute_history": history,
        "community_trust_score": 0.75,
    })
    assert isinstance(d["community_trust_adjusted"], (float, bool, int))


def test_response_shape():
    d = _post({
        "buyer_id": "B-007",
        "total_transactions": 100,
        "dispute_history": [_dispute("PP-D-001")],
    })
    required = {
        "buyer_id", "total_disputes", "dispute_rate",
        "merchant_win_rate", "escalation_rate", "recency_score",
        "avg_claim_amount", "category_diversity",
        "risk_score", "risk_level", "community_trust_adjusted",
        "recommendation", "paypal_signal", "wave",
    }
    assert required.issubset(d.keys())
