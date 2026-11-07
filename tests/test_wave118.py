"""
Wave 118 — Evidence Quality Scorer  POST /api/oracle/score-evidence

Tests:
  1. INR with tracking + delivery → strong score, no critical gaps
  2. INR with nothing → weak score, critical gaps
  3. SNAD with product photos + return policy → score returned
  4. UNAUTHORIZED with IP log → score returned
  5. submit_recommended reflects evidence strength
  6. top_actions non-empty when gaps exist
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-evidence", json=body).json()


_BASE = {"dispute_id": "PP-D-001", "claim_type": "INR", "dispute_amount": 150.0}


def test_inr_full_evidence_strong():
    d = _post({**_BASE, "evidence_items": [
        {"evidence_type": "TRACKING_NUMBER", "is_present": True, "is_dated": True, "is_specific": True},
        {"evidence_type": "PROOF_OF_DELIVERY", "is_present": True, "is_dated": True, "is_specific": True},
        {"evidence_type": "CARRIER_CONFIRMATION", "is_present": True},
    ]})
    assert d["critical_gaps"] == 0
    assert d["strength"] in ("STRONG", "ADEQUATE")
    assert d["wave"] == 118


def test_inr_no_evidence_weak():
    d = _post({**_BASE, "evidence_items": []})
    assert d["critical_gaps"] > 0
    assert d["strength"] in ("WEAK", "POOR")


def test_snad_evidence():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "SNAD",
        "dispute_amount": 80.0,
        "evidence_items": [
            {"evidence_type": "PRODUCT_LISTING", "is_present": True, "is_specific": True},
            {"evidence_type": "ITEM_PHOTOS", "is_present": True},
            {"evidence_type": "RETURN_POLICY", "is_present": True},
        ],
    })
    assert d["claim_type"] == "SNAD"
    assert isinstance(d["composite_score"], float)


def test_unauthorized_evidence():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "UNAUTHORIZED",
        "dispute_amount": 500.0,
        "evidence_items": [
            {"evidence_type": "IP_ADDRESS_LOG", "is_present": True, "is_dated": True},
            {"evidence_type": "ORDER_HISTORY", "is_present": True},
        ],
    })
    assert d["wave"] == 118
    assert isinstance(d["critical_gaps"], int)


def test_submit_recommended_strong():
    d = _post({**_BASE, "evidence_items": [
        {"evidence_type": "TRACKING_NUMBER", "is_present": True, "is_dated": True, "is_specific": True},
        {"evidence_type": "PROOF_OF_DELIVERY", "is_present": True, "is_dated": True, "is_specific": True},
        {"evidence_type": "CARRIER_CONFIRMATION", "is_present": True, "is_dated": True},
    ]})
    assert d["submit_recommended"] is True


def test_top_actions_when_gaps():
    d = _post({**_BASE, "evidence_items": []})
    assert isinstance(d["top_actions"], list)
    assert len(d["top_actions"]) > 0


def test_response_shape():
    d = _post({**_BASE, "evidence_items": []})
    required = {
        "dispute_id", "claim_type", "composite_score", "strength",
        "critical_gaps", "major_gaps", "submit_recommended", "submission_risk",
        "top_actions", "gaps", "summary", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["gaps"], list)
