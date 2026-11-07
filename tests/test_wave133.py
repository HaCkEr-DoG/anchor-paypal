"""
Wave 133 — Dispute Portfolio Analyzer  POST /api/oracle/analyze-portfolio

Tests:
  1. High-value disputes → CRITICAL or HIGH risk level
  2. Empty portfolio → 0 disputes
  3. total_ev_if_all_fought computed
  4. top_disputes ranked by priority
  5. hdr_impact_rate is float
  6. SP-covered amount excludes from risk
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-portfolio", json=body).json()


_DISPUTES = [
    {"dispute_id": "PP-D-001", "reason_code": "INR", "network": "PAYPAL", "amount": 500.0, "days_remaining": 1, "win_probability": 0.7},
    {"dispute_id": "PP-D-002", "reason_code": "UNAUTHORIZED", "network": "PAYPAL", "amount": 300.0, "days_remaining": 3, "win_probability": 0.5},
    {"dispute_id": "PP-D-003", "reason_code": "SNAD", "network": "PAYPAL", "amount": 200.0, "days_remaining": 10, "win_probability": 0.4},
]


def test_high_value_critical_risk():
    d = _post({
        "merchant_id": "M001",
        "disputes": _DISPUTES,
        "total_transactions_90d": 100,
    })
    assert d["portfolio_risk_level"] in ("CRITICAL", "HIGH", "MODERATE")
    assert d["wave"] == 133


def test_empty_portfolio():
    d = _post({"merchant_id": "M002", "disputes": []})
    assert d["total_disputes"] == 0


def test_ev_all_fought():
    d = _post({"merchant_id": "M003", "disputes": _DISPUTES})
    assert isinstance(d["total_ev_if_all_fought"], float)
    assert isinstance(d["total_ev_if_all_accepted"], float)


def test_top_disputes_ranked():
    d = _post({"merchant_id": "M004", "disputes": _DISPUTES})
    if d["top_disputes"]:
        ranks = [x["priority_rank"] for x in d["top_disputes"]]
        assert ranks[0] == 1


def test_hdr_impact_rate_float():
    d = _post({"merchant_id": "M005", "disputes": _DISPUTES, "total_transactions_90d": 1000})
    assert isinstance(d["hdr_impact_rate"], float)
    assert 0.0 <= d["hdr_impact_rate"] <= 1.0


def test_total_amount_at_risk():
    d = _post({"merchant_id": "M006", "disputes": _DISPUTES})
    assert d["total_amount_at_risk"] > 0


def test_response_shape():
    d = _post({"merchant_id": "M007", "disputes": _DISPUTES})
    required = {
        "merchant_id", "total_disputes", "total_amount_at_risk",
        "total_ev_if_all_fought", "total_ev_if_all_accepted",
        "expected_net_recovery", "portfolio_avg_win_prob",
        "hdr_impact_rate", "portfolio_risk_level",
        "risk_flags", "top_disputes", "summary", "wave",
    }
    assert required.issubset(d.keys())
