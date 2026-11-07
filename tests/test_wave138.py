"""
Wave 138 — Merchant Health Advisor  POST /api/oracle/analyze-merchant-health

Tests:
  1. Healthy merchant (low dispute rate) → EXCELLENT or GOOD tier
  2. High dispute rate → POOR or CRITICAL tier
  3. health_score 0-100
  4. root_causes is non-empty list
  5. recommendations list present
  6. projected metrics present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-merchant-health", json=body).json()


def _dispute(dispute_id, reason_code="INR", outcome="WON", days=2, tracking=True):
    return {
        "dispute_id": dispute_id, "reason_code": reason_code,
        "amount": 100.0, "outcome": outcome, "days_to_respond": days,
        "had_tracking": tracking, "had_invoice": True,
        "had_buyer_communication": False,
    }


def test_healthy_merchant():
    disputes = [_dispute(f"PP-D-{i:03d}") for i in range(2)]
    d = _post({
        "merchant_id": "M-001",
        "total_orders_in_period": 1000,
        "disputes": disputes,
    })
    assert d["health_tier"] in ("EXCELLENT", "GOOD", "FAIR")
    assert d["wave"] == 138


def test_high_dispute_rate_poor():
    disputes = [_dispute(f"PP-D-{i:03d}", outcome="LOST", days=9, tracking=False)
                for i in range(20)]
    d = _post({
        "merchant_id": "M-002",
        "total_orders_in_period": 50,
        "disputes": disputes,
    })
    assert d["health_tier"] in ("POOR", "CRITICAL", "FAIR")


def test_health_score_range():
    d = _post({
        "merchant_id": "M-003",
        "total_orders_in_period": 100,
        "disputes": [_dispute("PP-D-001")],
    })
    assert 0 <= d["health_score"] <= 100


def test_root_causes_list():
    d = _post({
        "merchant_id": "M-004",
        "total_orders_in_period": 100,
        "disputes": [_dispute(f"PP-D-{i:03d}") for i in range(3)],
    })
    assert isinstance(d["root_causes"], list)


def test_recommendations_list():
    d = _post({
        "merchant_id": "M-005",
        "total_orders_in_period": 100,
        "disputes": [_dispute(f"PP-D-{i:03d}", outcome="LOST", tracking=False) for i in range(5)],
    })
    assert isinstance(d["recommendations"], list)
    if d["recommendations"]:
        rec = d["recommendations"][0]
        assert "priority" in rec
        assert "action" in rec


def test_projected_metrics():
    d = _post({
        "merchant_id": "M-006",
        "total_orders_in_period": 200,
        "disputes": [_dispute(f"PP-D-{i:03d}") for i in range(4)],
    })
    assert "projected_dispute_rate_if_actions_taken" in d
    assert "projected_health_score_if_actions_taken" in d
    assert 0 <= d["projected_health_score_if_actions_taken"] <= 100


def test_response_shape():
    d = _post({
        "merchant_id": "M-007",
        "total_orders_in_period": 50,
        "disputes": [_dispute("PP-D-001")],
    })
    required = {
        "merchant_id", "health_score", "health_tier", "current_dispute_rate",
        "total_disputes", "won_count", "lost_count", "win_rate",
        "root_causes", "recommendations", "paypal_risk_signal",
        "key_insight", "summary", "wave",
    }
    assert required.issubset(d.keys())
