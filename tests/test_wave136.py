"""
Wave 136 — Policy Defensibility Scorer  POST /api/oracle/score-policy-defensibility

Tests:
  1. Prominent disclosure + buyer check → STRONG defensibility
  2. Missing disclosure → UNUSABLE or WEAK
  3. Policy overrides network rules → non-compliant
  4. total_score 0–100
  5. win_rate_adjustment in reasonable range
  6. strengths/weaknesses lists present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-policy-defensibility", json=body).json()


def test_strong_disclosure():
    d = _post({
        "dispute_id": "PP-D-001",
        "policy_type": "RETURN_REFUND",
        "reason_code": "SNAD",
        "disclosure_level": "PROMINENT",
        "buyer_checked_policy_box": True,
        "policy_text_at_checkout": True,
        "policy_linked_from_checkout": True,
        "policy_in_order_confirmation": True,
        "has_clear_timeframe": True,
        "has_specific_conditions": True,
        "policy_applied_consistently": True,
    })
    assert d["defensibility"] in ("STRONG", "ADEQUATE")
    assert d["wave"] == 136


def test_missing_disclosure_unusable():
    d = _post({
        "dispute_id": "PP-D-002",
        "policy_type": "RETURN_REFUND",
        "reason_code": "SNAD",
        "disclosure_level": "MISSING",
    })
    assert d["defensibility"] in ("UNUSABLE", "WEAK")


def test_network_override_non_compliant():
    d = _post({
        "dispute_id": "PP-D-003",
        "policy_type": "FINAL_SALE",
        "reason_code": "SNAD",
        "policy_overrides_network_rules": True,
    })
    assert d["network_compliance_ok"] is False


def test_score_range():
    d = _post({
        "dispute_id": "PP-D-004",
        "policy_type": "RETURN_REFUND",
        "reason_code": "INR",
    })
    assert 0 <= d["total_score"] <= 100


def test_win_rate_adjustment_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "policy_type": "SUBSCRIPTION",
        "reason_code": "SUBSCRIPTION",
    })
    assert -0.20 <= d["win_rate_adjustment"] <= 0.15


def test_strengths_weaknesses_lists():
    d = _post({
        "dispute_id": "PP-D-006",
        "policy_type": "RETURN_REFUND",
        "reason_code": "SNAD",
        "disclosure_level": "PROMINENT",
    })
    assert isinstance(d["strengths"], list)
    assert isinstance(d["weaknesses"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "policy_type": "RETURN_REFUND",
        "reason_code": "INR",
    })
    required = {
        "dispute_id", "policy_type", "total_score", "defensibility",
        "strengths", "weaknesses", "network_compliance_ok",
        "win_rate_adjustment", "recommendation", "summary", "wave",
    }
    assert required.issubset(d.keys())
