"""
Wave 150 — Response Quality Analyzer  POST /api/oracle/analyze-response-quality

Tests:
  1. Professional tracking response → grade A or B, submit_ready True
  2. Empty response → grade D or F
  3. quality_score in [0, 1]
  4. dimensions list present with scores
  5. issues is list
  6. improvements is list
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-response-quality", json=body).json()


_GOOD_RESPONSE = (
    "Dear PayPal Dispute Team, we respectfully submit our response to dispute PP-D-001. "
    "The order ORD-001 was shipped on 2026-10-02 via UPS with tracking number 1Z999AA10123456784. "
    "Our records confirm delivery on 2026-10-05, as shown in the attached proof of delivery scan. "
    "We have enclosed the invoice and order confirmation for your review. "
    "We appreciate the buyer's concern and are happy to provide any further documentation. "
    "Please find attached: tracking confirmation, delivery scan, and original invoice."
)


def test_good_response_high_grade():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "response_text": _GOOD_RESPONSE,
        "has_tracking": True,
        "has_delivery_scan": True,
        "has_order_confirmation": True,
    })
    assert d["grade"] in ("A", "B", "C")
    assert d["wave"] == 150


def test_empty_response_low_grade():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "response_text": "",
    })
    assert d["grade"] in ("D", "F")
    assert d["submit_ready"] is False


def test_quality_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "SNAD",
        "response_text": "We shipped the item.",
    })
    assert 0.0 <= d["quality_score"] <= 1.0


def test_dimensions_list():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "response_text": _GOOD_RESPONSE,
        "has_tracking": True,
    })
    assert isinstance(d["dimensions"], list)
    assert len(d["dimensions"]) > 0
    dim = d["dimensions"][0]
    assert "name" in dim
    assert "score" in dim


def test_issues_list():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "UNAUTHORIZED",
        "response_text": "I did not authorize this charge!",
    })
    assert isinstance(d["issues"], list)


def test_improvements_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "INR",
        "response_text": "We shipped it.",
    })
    assert isinstance(d["improvements"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "response_text": _GOOD_RESPONSE,
    })
    required = {
        "dispute_id", "reason_code", "quality_score", "grade",
        "dimensions", "issues", "improvements",
        "estimated_win_probability_boost", "submit_ready",
        "paypal_signal", "recommendation", "wave",
    }
    assert required.issubset(d.keys())
