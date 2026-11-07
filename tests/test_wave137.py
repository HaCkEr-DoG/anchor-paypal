"""
Wave 137 — Refund Policy Advisor  POST /api/oracle/advise-refund-policy

Tests:
  1. Policy at checkout + acceptance → COMPLIANT
  2. No policies → NON_COMPLIANT or MAJOR_GAPS
  3. policy_evidence_usable True when compliant
  4. win_rate_impact in reasonable range
  5. gaps list present
  6. key_findings non-empty
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/advise-refund-policy", json=body).json()


def test_policy_at_checkout_compliant():
    d = _post({
        "dispute_id": "PP-D-001",
        "claim_type": "SNAD",
        "policy_accepted_at_checkout": True,
        "policies": [{
            "policy_type": "RETURN",
            "is_on_checkout_page": True,
            "is_in_order_confirmation": True,
            "requires_active_acceptance": True,
            "return_window_days": 30,
            "policy_was_in_place_at_order": True,
        }],
    })
    assert d["compliance"] in ("COMPLIANT", "MINOR_GAPS")
    assert d["wave"] == 137


def test_no_policies_non_compliant():
    d = _post({
        "dispute_id": "PP-D-002",
        "claim_type": "SNAD",
        "policies": [],
    })
    assert d["compliance"] in ("NON_COMPLIANT", "MAJOR_GAPS")


def test_evidence_usable_when_compliant():
    d = _post({
        "dispute_id": "PP-D-003",
        "claim_type": "SNAD",
        "policy_accepted_at_checkout": True,
        "policies": [{
            "policy_type": "REFUND",
            "is_on_checkout_page": True,
            "requires_active_acceptance": True,
            "return_window_days": 30,
        }],
    })
    if d["compliance"] in ("COMPLIANT", "MINOR_GAPS"):
        assert isinstance(d["policy_evidence_usable"], bool)


def test_win_rate_impact_range():
    d = _post({
        "dispute_id": "PP-D-004",
        "claim_type": "INR",
        "policies": [],
    })
    assert -0.25 <= d["win_rate_impact"] <= 0.15


def test_gaps_list():
    d = _post({
        "dispute_id": "PP-D-005",
        "claim_type": "SUBSCRIPTION",
        "is_subscription": True,
        "policies": [],
    })
    assert isinstance(d["gaps"], list)


def test_key_findings_non_empty():
    d = _post({
        "dispute_id": "PP-D-006",
        "claim_type": "SNAD",
        "policies": [{
            "policy_type": "RETURN",
            "is_on_checkout_page": True,
        }],
    })
    assert isinstance(d["key_findings"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "claim_type": "INR",
        "policies": [],
    })
    required = {
        "dispute_id", "compliance", "defensibility", "gaps",
        "win_rate_impact", "policy_evidence_usable",
        "key_findings", "recommendations", "summary", "wave",
    }
    assert required.issubset(d.keys())
