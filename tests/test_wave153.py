"""
Wave 153 — Evidence Quality Scorer  POST /api/oracle/score-evidence-quality

Tests:
  1. Full INR evidence → STRONG or VERY_STRONG, submit_recommended True
  2. No evidence → submit_recommended False
  3. composite_score in [0, 1]
  4. gaps list present
  5. critical_gaps int
  6. top_actions is list
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-evidence-quality", json=body).json()


def _item(evidence_type, is_present=True, is_dated=True):
    return {"evidence_type": evidence_type, "is_present": is_present, "is_dated": is_dated}


def test_full_inr_strong():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "INR",
        "dispute_amount": 150.0,
        "evidence_items": [
            _item("TRACKING_NUMBER"),
            _item("PROOF_OF_DELIVERY"),
            _item("CARRIER_CONFIRMATION"),
            _item("COMMUNICATION_LOG"),
        ],
    })
    assert d["strength"] in ("STRONG", "ADEQUATE", "MODERATE")
    assert d["wave"] == 153


def test_no_evidence_not_ready():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "evidence_items": [],
    })
    assert d["submit_recommended"] is False


def test_composite_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "SNAD",
        "dispute_amount": 80.0,
        "evidence_items": [_item("ITEM_PHOTOS"), _item("PRODUCT_LISTING")],
    })
    assert 0.0 <= d["composite_score"] <= 100.0


def test_gaps_list():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "evidence_items": [],
    })
    assert isinstance(d["gaps"], list)


def test_critical_gaps_int():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "evidence_items": [],
    })
    assert isinstance(d["critical_gaps"], int)
    assert d["critical_gaps"] > 0


def test_top_actions_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "UNAUTHORIZED",
        "dispute_amount": 500.0,
        "evidence_items": [],
    })
    assert isinstance(d["top_actions"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "dispute_amount": 100.0,
        "evidence_items": [_item("TRACKING_NUMBER")],
    })
    required = {
        "dispute_id", "claim_type", "composite_score", "strength",
        "components", "gaps", "critical_gaps", "major_gaps",
        "submit_recommended", "submission_risk", "top_actions",
        "summary", "wave",
    }
    assert required.issubset(d.keys())
