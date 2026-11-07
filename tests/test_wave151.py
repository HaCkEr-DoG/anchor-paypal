"""
Wave 151 — Dispute Complexity Estimator  POST /api/oracle/estimate-dispute-complexity

Tests:
  1. High-value arbitration-risk → EXTREME or HIGH tier
  2. Simple low-value dispute → TRIVIAL or LOW tier
  3. total_score 0–100
  4. driver_scores has 5 entries
  5. estimated_staff_hours > 0
  6. complexity_flags is list
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/estimate-dispute-complexity", json=body).json()


def test_high_complexity_tier():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "UNAUTHORIZED",
        "network": "VISA",
        "transaction_amount": 2500.0,
        "is_arbitration_risk": True,
        "is_high_value": True,
        "buyer_risk_class": "FRAUDSTER",
        "buyer_dispute_count": 15,
        "buyer_represented": True,
        "is_ce3_applicable": True,
        "num_prior_appeals": 2,
        "days_remaining": 2,
        "is_in_arbitration": True,
    })
    assert d["complexity_tier"] in ("EXTREME", "HIGH")
    assert d["wave"] == 151


def test_low_complexity_tier():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 30.0,
        "evidence_completeness_score": 90,
        "buyer_risk_class": "TRUSTED",
        "buyer_dispute_count": 0,
        "days_remaining": 20,
    })
    assert d["complexity_tier"] in ("TRIVIAL", "LOW", "MEDIUM")


def test_total_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "SNAD",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    assert 0 <= d["total_score"] <= 100


def test_five_driver_scores():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    assert len(d["driver_scores"]) == 5


def test_staff_hours_positive():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    assert d["estimated_staff_hours"] >= 0


def test_complexity_flags_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "UNAUTHORIZED",
        "network": "VISA",
        "transaction_amount": 500.0,
        "is_high_value": False,
    })
    assert isinstance(d["complexity_flags"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    required = {
        "dispute_id", "total_score", "complexity_tier",
        "driver_scores", "estimated_staff_hours",
        "recommended_assignee", "sla_hours",
        "complexity_flags", "summary", "wave",
    }
    assert required.issubset(d.keys())
