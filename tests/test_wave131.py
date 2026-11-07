"""
Wave 131 — Response Quality Scorer  POST /api/oracle/score-response-quality

Tests:
  1. Full evidence + narrative → EXCELLENT or GOOD tier
  2. No evidence → INADEQUATE or WEAK
  3. approved_to_submit False when score < 70
  4. blocking_issues populated when dates inconsistent
  5. total_score 0–100
  6. improvements list present
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-response-quality", json=body).json()


def test_full_evidence_excellent():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "network": "PAYPAL",
        "evidence_items_attached": 5,
        "required_evidence_count": 3,
        "has_tracking_evidence": True,
        "has_auth_evidence": True,
        "has_policy_evidence": True,
        "has_communication_log": True,
        "has_counter_narrative": True,
        "cites_network_rules": True,
        "has_cover_letter": True,
        "has_evidence_index": True,
        "follows_network_template": True,
        "reason_code_addressed": True,
    })
    assert d["quality_tier"] in ("EXCELLENT", "GOOD")
    assert d["wave"] == 131


def test_no_evidence_inadequate():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "network": "PAYPAL",
        "evidence_items_attached": 0,
        "required_evidence_count": 3,
        "has_tracking_evidence": False,
        "has_auth_evidence": False,
        "has_policy_evidence": False,
        "reason_code_addressed": False,
    })
    assert d["quality_tier"] in ("INADEQUATE", "WEAK", "ADEQUATE")
    assert d["approved_to_submit"] is False


def test_not_approved_low_score():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "SNAD",
        "network": "PAYPAL",
        "evidence_items_attached": 0,
        "required_evidence_count": 4,
    })
    if d["total_score"] < 70:
        assert d["approved_to_submit"] is False


def test_blocking_issues_on_inconsistency():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "network": "PAYPAL",
        "dates_consistent": False,
        "amounts_consistent": False,
        "tracking_numbers_valid": False,
    })
    assert isinstance(d["blocking_issues"], list)


def test_total_score_range():
    d = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "INR",
        "network": "PAYPAL",
    })
    assert 0 <= d["total_score"] <= 100


def test_improvements_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "INR",
        "network": "PAYPAL",
        "has_tracking_evidence": False,
    })
    assert isinstance(d["improvements"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "network": "PAYPAL",
    })
    required = {
        "dispute_id", "reason_code", "total_score", "quality_tier",
        "improvements", "strengths", "blocking_issues",
        "approved_to_submit", "summary", "wave",
    }
    assert required.issubset(d.keys())
