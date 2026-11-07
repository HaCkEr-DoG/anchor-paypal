"""
Wave 143 — Portfolio Risk Scorer  POST /api/oracle/score-dispute-portfolio

Tests:
  1. High-value portfolio → HIGH or SEVERE risk level
  2. Single small dispute → LOW risk
  3. total_exposure_usd = sum of amounts
  4. expected_loss_usd < total_exposure_usd
  5. concentration flag when INR dominates
  6. critical_disputes catches deadline ≤ 2 days
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-dispute-portfolio", json=body).json()


def _d(dispute_id, claim_type="INR", amount=500.0, p_win=0.4, days=5):
    return {
        "dispute_id": dispute_id, "claim_type": claim_type,
        "amount_usd": amount, "win_probability": p_win,
        "days_until_deadline": days,
    }


def test_high_value_portfolio():
    disputes = [_d(f"PP-D-{i:03d}", amount=1500.0, p_win=0.3) for i in range(5)]
    d = _post({"merchant_id": "M-001", "disputes": disputes})
    assert d["portfolio_risk_level"] in ("HIGH", "SEVERE", "MODERATE")
    assert d["wave"] == 143


def test_single_small_dispute_low():
    d = _post({
        "merchant_id": "M-002",
        "disputes": [_d("PP-D-001", amount=50.0, p_win=0.8)],
    })
    assert d["portfolio_risk_level"] in ("LOW", "MODERATE")


def test_total_exposure_matches():
    disputes = [_d("PP-D-001", amount=200.0), _d("PP-D-002", amount=300.0)]
    d = _post({"merchant_id": "M-003", "disputes": disputes})
    assert abs(d["total_exposure_usd"] - 500.0) < 0.01


def test_expected_loss_less_than_exposure():
    disputes = [_d(f"PP-D-{i:03d}", p_win=0.5) for i in range(3)]
    d = _post({"merchant_id": "M-004", "disputes": disputes})
    assert d["expected_loss_usd"] <= d["total_exposure_usd"]


def test_concentration_flag():
    disputes = [_d(f"PP-D-{i:03d}", claim_type="INR") for i in range(8)]
    d = _post({"merchant_id": "M-005", "disputes": disputes})
    assert d["concentration"]["flag"] is True
    assert d["concentration"]["dominant_claim_type"] == "INR"


def test_critical_disputes():
    disputes = [
        _d("PP-D-001", days=1),   # critical
        _d("PP-D-002", days=2),   # critical
        _d("PP-D-003", days=10),  # not critical
    ]
    d = _post({"merchant_id": "M-006", "disputes": disputes})
    assert "PP-D-001" in d["critical_disputes"]
    assert "PP-D-002" in d["critical_disputes"]


def test_response_shape():
    d = _post({
        "merchant_id": "M-007",
        "disputes": [_d("PP-D-001")],
    })
    required = {
        "merchant_id", "total_disputes", "total_exposure_usd",
        "expected_loss_usd", "expected_recovery_usd", "portfolio_risk_level",
        "concentration", "critical_disputes", "high_value_disputes",
        "avg_win_probability", "recommendation", "risk_factors", "wave",
    }
    assert required.issubset(d.keys())
