"""
Wave 152 — Merchant Risk Scorer  POST /api/oracle/score-merchant-risk

Tests:
  1. High dispute rate → FEE_TIER or SEVERE hdr_tier
  2. Low dispute rate → NORMAL or ELEVATED hdr_tier
  3. overall_risk_score 0–100
  4. dimensions list present
  5. recommendations list present
  6. revenue_at_risk > 0 when open_dispute_value > 0
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-merchant-risk", json=body).json()


def test_high_dispute_rate_fee_tier():
    d = _post({
        "merchant_id": "M-001",
        "total_transactions_90d": 1000,
        "total_transaction_value_90d": 100000.0,
        "total_disputes_90d": 25,
        "open_disputes": 10,
        "disputes_won_90d": 5,
        "disputes_lost_90d": 15,
        "disputes_accepted_90d": 5,
    })
    assert d["hdr_tier"] in ("FEE_TIER", "SEVERE", "LIMITED")
    assert d["wave"] == 152


def test_low_dispute_rate_normal():
    d = _post({
        "merchant_id": "M-002",
        "total_transactions_90d": 2000,
        "total_transaction_value_90d": 200000.0,
        "total_disputes_90d": 4,
        "open_disputes": 2,
        "disputes_won_90d": 2,
        "disputes_lost_90d": 1,
        "disputes_accepted_90d": 1,
    })
    assert d["hdr_tier"] in ("NORMAL", "ELEVATED")


def test_risk_score_range():
    d = _post({
        "merchant_id": "M-003",
        "total_transactions_90d": 500,
        "total_transaction_value_90d": 50000.0,
        "total_disputes_90d": 5,
        "open_disputes": 3,
        "disputes_won_90d": 2,
        "disputes_lost_90d": 2,
        "disputes_accepted_90d": 1,
    })
    assert 0 <= d["overall_risk_score"] <= 100


def test_dimensions_list():
    d = _post({
        "merchant_id": "M-004",
        "total_transactions_90d": 1000,
        "total_transaction_value_90d": 100000.0,
        "total_disputes_90d": 10,
        "open_disputes": 5,
        "disputes_won_90d": 4,
        "disputes_lost_90d": 4,
        "disputes_accepted_90d": 2,
    })
    assert isinstance(d["dimensions"], list)
    assert len(d["dimensions"]) > 0


def test_recommendations_list():
    d = _post({
        "merchant_id": "M-005",
        "total_transactions_90d": 1000,
        "total_transaction_value_90d": 100000.0,
        "total_disputes_90d": 20,
        "open_disputes": 8,
        "disputes_won_90d": 5,
        "disputes_lost_90d": 10,
        "disputes_accepted_90d": 5,
    })
    assert isinstance(d["recommendations"], list)


def test_revenue_at_risk():
    d = _post({
        "merchant_id": "M-006",
        "total_transactions_90d": 1000,
        "total_transaction_value_90d": 100000.0,
        "total_disputes_90d": 10,
        "open_disputes": 5,
        "disputes_won_90d": 4,
        "disputes_lost_90d": 4,
        "disputes_accepted_90d": 2,
        "open_dispute_value": 5000.0,
    })
    assert d["revenue_at_risk"] > 0


def test_response_shape():
    d = _post({
        "merchant_id": "M-007",
        "total_transactions_90d": 1000,
        "total_transaction_value_90d": 100000.0,
        "total_disputes_90d": 10,
        "open_disputes": 5,
        "disputes_won_90d": 4,
        "disputes_lost_90d": 4,
        "disputes_accepted_90d": 2,
    })
    required = {
        "merchant_id", "dispute_rate_90d", "hdr_tier", "hdr_tier_next",
        "fee_exposure_per_dispute", "dimensions", "overall_risk_score",
        "trend", "trend_dispute_rate_prior", "revenue_at_risk",
        "projected_fees_if_current_rate", "recommendations",
        "urgent_actions", "summary", "wave",
    }
    assert required.issubset(d.keys())
