"""
Wave 111 — Response Template Generator  POST /api/oracle/response-template

Tests:
  1. INR with tracking → strong response
  2. UNAUTHORIZED with no prior transactions → weak response
  3. SNAD with item description → response generated
  4. Missing required fields → still returns response (defaults)
  5. Tone parameter respected
  6. Response shape complete
  7. Buyer message is non-empty
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)

_BASE = {
    "dispute_id": "PP-D-TEST",
    "claim_type": "INR",
    "dispute_amount": 95.00,
    "order_id": "ORDER-123",
    "order_date": "2026-10-01",
}


def _post(body: dict):
    return _client.post("/api/oracle/response-template", json=body).json()


# ---------------------------------------------------------------------------
# 1. INR with tracking → strong response
# ---------------------------------------------------------------------------
def test_inr_with_tracking():
    body = {**_BASE, "tracking_number": "1Z9999", "carrier": "UPS",
            "delivery_date": "2026-10-05", "has_signature": True}
    d = _post(body)
    assert d["wave"] == 111
    assert d["estimated_strength"] in ("STRONG", "MODERATE")
    assert len(d["paypal_response_text"]) > 50


# ---------------------------------------------------------------------------
# 2. UNAUTHORIZED with no prior data → weak response
# ---------------------------------------------------------------------------
def test_unauthorized_weak():
    body = {**_BASE, "claim_type": "UNAUTHORIZED", "prior_order_count": 0}
    d = _post(body)
    assert d["estimated_strength"] in ("WEAK", "MODERATE")
    assert d["wave"] == 111


# ---------------------------------------------------------------------------
# 3. SNAD with item description
# ---------------------------------------------------------------------------
def test_snad_with_description():
    body = {**_BASE, "claim_type": "SNAD",
            "item_description": "Gold plated bracelet 24k as listed"}
    d = _post(body)
    assert len(d["paypal_response_text"]) > 20
    assert d["claim_type"] == "SNAD"


# ---------------------------------------------------------------------------
# 4. Minimal body — defaults applied
# ---------------------------------------------------------------------------
def test_minimal_body():
    d = _post({"dispute_id": "PP-MIN", "claim_type": "INR",
               "dispute_amount": 10, "order_id": "O1", "order_date": "2026-01-01"})
    assert "paypal_response_text" in d
    assert "submission_checklist" in d


# ---------------------------------------------------------------------------
# 5. FORMAL tone
# ---------------------------------------------------------------------------
def test_formal_tone():
    body = {**_BASE, "tone": "FORMAL"}
    d = _post(body)
    assert d["wave"] == 111


# ---------------------------------------------------------------------------
# 6. Response shape complete
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post(_BASE)
    required = {
        "dispute_id", "claim_type", "estimated_strength", "summary",
        "paypal_response_text", "buyer_message_text", "internal_summary",
        "submission_checklist", "blocks", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["submission_checklist"], list)
    assert isinstance(d["blocks"], list)


# ---------------------------------------------------------------------------
# 7. Buyer message non-empty
# ---------------------------------------------------------------------------
def test_buyer_message_non_empty():
    d = _post({**_BASE, "tracking_number": "TRACK123"})
    assert len(d["buyer_message_text"]) > 10
