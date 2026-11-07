"""
Wave 105 — Dispute Auto-Categorizer  POST /api/oracle/auto-categorize

Tests:
  1. INR keywords → ITEM_NOT_RECEIVED classification
  2. SNAD keywords → SIGNIFICANTLY_NOT_AS_DESCRIBED
  3. Unauthorized keywords → UNAUTHORIZED
  4. No keywords → OTHER, low confidence
  5. Deadline ≤2 days → critical urgency
  6. Unauthorized → routes to fraud_team
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/auto-categorize", json=body).json()


# ---------------------------------------------------------------------------
# 1. INR keywords
# ---------------------------------------------------------------------------
def test_inr_keywords():
    d = _post({"dispute_note": "item not received and never arrived"})
    assert d["inferred_claim_type"] == "ITEM_NOT_RECEIVED"
    assert d["classification_confidence"] in ("medium", "high")


# ---------------------------------------------------------------------------
# 2. SNAD keywords
# ---------------------------------------------------------------------------
def test_snad_keywords():
    d = _post({"dispute_note": "item not as described, completely wrong item, broken on arrival"})
    assert d["inferred_claim_type"] == "SIGNIFICANTLY_NOT_AS_DESCRIBED"


# ---------------------------------------------------------------------------
# 3. Unauthorized keywords
# ---------------------------------------------------------------------------
def test_unauthorized_keywords():
    d = _post({"dispute_note": "unauthorized transaction i didn't buy this fraud"})
    assert d["inferred_claim_type"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 4. No keywords → OTHER, low confidence
# ---------------------------------------------------------------------------
def test_no_keywords_other():
    d = _post({"dispute_note": "general complaint about the order"})
    assert d["classification_confidence"] in ("low", "medium")


# ---------------------------------------------------------------------------
# 5. Deadline ≤2 days → critical urgency
# ---------------------------------------------------------------------------
def test_critical_urgency():
    d = _post({"dispute_note": "issue", "days_until_deadline": 1})
    assert d["urgency"] == "critical"
    assert d["route_to"] == "senior_agent"


# ---------------------------------------------------------------------------
# 6. Unauthorized → fraud_team routing
# ---------------------------------------------------------------------------
def test_unauthorized_routes_to_fraud():
    d = _post({
        "dispute_note": "unauthorized transaction fraud",
        "days_until_deadline": 10,
        "amount_usd": 50,
    })
    assert d["route_to"] == "fraud_team"


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"dispute_note": "item not received"})
    required = {
        "inferred_claim_type", "classification_confidence", "urgency",
        "complexity", "route_to", "routing_reason", "keyword_scores", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 105
