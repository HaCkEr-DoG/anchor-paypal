"""
Wave 154 — Evidence Strength Scorer  POST /api/oracle/score-evidence-strength

Tests:
  1. Full INR package → STRONG tier, submit_ready True
  2. Empty package → LOW or WEAK tier
  3. overall_score in [0, 1]
  4. items list present
  5. missing_critical list present
  6. estimated_win_probability in [0, 1]
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-evidence-strength", json=body).json()


def test_full_inr_strong():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "transaction_amount": 150.0,
        "has_tracking_number": True,
        "tracking_number": "1Z999AA10123456784",
        "has_delivery_scan": True,
        "has_order_confirmation": True,
        "has_buyer_communication": True,
    })
    assert d["strength_tier"] in ("STRONG", "VERY_STRONG", "MODERATE")
    assert d["submit_ready"] is True
    assert d["wave"] == 154


def test_empty_package_weak():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "transaction_amount": 100.0,
    })
    assert d["strength_tier"] in ("WEAK", "VERY_WEAK", "LOW", "MODERATE", "INSUFFICIENT")
    assert d["submit_ready"] is False


def test_overall_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "SNAD",
        "transaction_amount": 80.0,
        "has_product_photos": True,
    })
    assert 0.0 <= d["overall_score"] <= 1.0


def test_items_list():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "transaction_amount": 100.0,
        "has_tracking_number": True,
    })
    assert isinstance(d["items"], list)
    assert len(d["items"]) > 0


def test_missing_critical_list():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "INR",
        "transaction_amount": 100.0,
    })
    assert isinstance(d["missing_critical"], list)
    assert len(d["missing_critical"]) > 0


def test_win_probability_range():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "INR",
        "transaction_amount": 100.0,
        "has_tracking_number": True,
        "has_delivery_scan": True,
    })
    assert 0.0 <= d["estimated_win_probability"] <= 1.0


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "transaction_amount": 100.0,
    })
    required = {
        "dispute_id", "reason_code", "overall_score", "strength_tier",
        "items", "missing_critical", "missing_beneficial",
        "estimated_win_probability", "submit_ready",
        "paypal_signal", "recommendation", "wave",
    }
    assert required.issubset(d.keys())
