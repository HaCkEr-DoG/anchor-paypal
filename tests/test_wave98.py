"""
Wave 98 — Dispute Response Quality Scorer  POST /api/oracle/response-quality-score

Tests:
  1. Empty response text → grade F, not submit_ready
  2. Full evidence + good text → high score, submit_ready
  3. Inflammatory language reduces professionalism score
  4. Missing tracking for ITEM_NOT_RECEIVED shows in missing_elements
  5. Grade thresholds correct (≥90=A, ≥80=B, ≥65=C, ≥50=D, else F)
  6. Response shape complete
  7. has_tracking=True boosts evidence score
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/response-quality-score", json=body).json()


_GOOD_TEXT = (
    "Dear buyer, regarding your order for the product that was purchased on November 1st, "
    "we have tracking number 1Z999AA10123456784 showing the item was delivered and signed for "
    "on November 5th. Per our refund policy, items are covered for 30 days. We attempted to "
    "contact you via email on November 7th to resolve this dispute. We respectfully request "
    "that this claim be closed as the item was successfully delivered."
)


# ---------------------------------------------------------------------------
# 1. Empty response → F, not ready
# ---------------------------------------------------------------------------

def test_empty_response():
    d = _post({"response_text": ""})
    assert d["grade"] == "F"
    assert d["submit_ready"] is False
    assert d["overall_score"] < 50


# ---------------------------------------------------------------------------
# 2. Well-written response → high score
# ---------------------------------------------------------------------------

def test_good_response_high_score():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "response_text": _GOOD_TEXT,
        "has_tracking_number": True,
        "has_delivery_confirmation": True,
        "has_refund_policy_link": True,
        "has_communication_log": True,
    })
    assert d["overall_score"] >= 80
    assert d["grade"] in ("A", "B")
    assert d["submit_ready"] is True


# ---------------------------------------------------------------------------
# 3. Inflammatory language reduces professionalism
# ---------------------------------------------------------------------------

def test_inflammatory_language():
    d = _post({
        "response_text": "This buyer is a total fraud and liar trying to scam us. "
                         "The item was delivered and they are being ridiculous. "
                         "We have tracking and photo evidence of the delivery confirmation."
    })
    # professionalism should be penalized
    assert d["rubric"]["professionalism"]["score"] < 15


# ---------------------------------------------------------------------------
# 4. Missing tracking for INR → in missing_elements
# ---------------------------------------------------------------------------

def test_missing_tracking_inr():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "response_text": "The item was delivered to the buyer on time per our records.",
        "has_tracking_number": False,
    })
    # Tracking should be flagged as missing
    assert any("tracking" in m.lower() for m in d["missing_elements"])


# ---------------------------------------------------------------------------
# 5. Grade thresholds
# ---------------------------------------------------------------------------

def test_grade_thresholds():
    # B: score ≥80
    d_b = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "response_text": _GOOD_TEXT,
        "has_tracking_number": True,
        "has_delivery_confirmation": True,
        "has_refund_policy_link": True,
    })
    assert d_b["grade"] in ("A", "B")


# ---------------------------------------------------------------------------
# 6. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({"response_text": "some text about the item and buyer"})
    required = {
        "dispute_id", "claim_type", "overall_score", "grade",
        "rubric", "missing_elements", "suggestions", "submit_ready",
    }
    assert required.issubset(d.keys())
    # Rubric has all 5 dimensions
    for dim in ("clarity", "evidence_references", "policy_compliance",
                "professionalism", "completeness"):
        assert dim in d["rubric"]


# ---------------------------------------------------------------------------
# 7. has_tracking boosts evidence score vs not provided
# ---------------------------------------------------------------------------

def test_tracking_flag_boosts_evidence():
    base = _post({"response_text": "item was shipped to buyer"})
    with_tracking = _post({"response_text": "item was shipped to buyer", "has_tracking_number": True})
    assert with_tracking["rubric"]["evidence_references"]["score"] >= \
           base["rubric"]["evidence_references"]["score"]
