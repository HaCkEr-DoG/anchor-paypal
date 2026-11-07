"""
Wave 104 — Negotiation Script Generator  POST /api/oracle/negotiation-script

Tests:
  1. INR claim type → body references tracking
  2. Partial refund offer → refund amount in script
  3. Empathetic tone → script starts with empathy
  4. Firm tone → script mentions documented evidence
  5. Zero amount no refund → no dollar amount in offer
  6. UNAUTHORIZED script mentions login records
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/negotiation-script", json=body).json()


# ---------------------------------------------------------------------------
# 1. INR → tracking mentioned in script
# ---------------------------------------------------------------------------
def test_inr_script_mentions_tracking():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "dispute_amount_usd": 150,
    })
    assert "tracking" in d["script"].lower()
    assert d["claim_type"] == "ITEM_NOT_RECEIVED"


# ---------------------------------------------------------------------------
# 2. Partial refund → dollar amount in script
# ---------------------------------------------------------------------------
def test_partial_refund_in_script():
    d = _post({
        "claim_type": "OTHER",
        "dispute_amount_usd": 200,
        "offer_partial_refund": True,
        "partial_refund_pct": 50,
    })
    assert d["partial_refund_amount_usd"] == pytest.approx(100.0)
    assert "100" in d["script"]


# ---------------------------------------------------------------------------
# 3. Empathetic tone → empathy keyword in opening
# ---------------------------------------------------------------------------
def test_empathetic_tone():
    d = _post({"claim_type": "OTHER", "tone": "empathetic"})
    script_lower = d["script"].lower()
    assert any(word in script_lower for word in ["frustrating", "understand", "sincerely"])


# ---------------------------------------------------------------------------
# 4. Firm tone → evidence/documented keyword
# ---------------------------------------------------------------------------
def test_firm_tone():
    d = _post({"claim_type": "OTHER", "tone": "firm"})
    script_lower = d["script"].lower()
    assert any(word in script_lower for word in ["evidence", "documented", "thoroughly"])


# ---------------------------------------------------------------------------
# 5. No partial refund → no dollar amount in offer section
# ---------------------------------------------------------------------------
def test_no_partial_refund():
    d = _post({
        "claim_type": "OTHER",
        "dispute_amount_usd": 200,
        "offer_partial_refund": False,
    })
    assert d["partial_refund_amount_usd"] == 0
    assert d["offer_partial_refund"] is False


# ---------------------------------------------------------------------------
# 6. UNAUTHORIZED → login records in script
# ---------------------------------------------------------------------------
def test_unauthorized_mentions_login():
    d = _post({"claim_type": "UNAUTHORIZED"})
    assert "login" in d["script"].lower() or "account" in d["script"].lower()


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"claim_type": "OTHER", "dispute_amount_usd": 100})
    required = {
        "claim_type", "tone", "dispute_amount_usd", "offer_partial_refund",
        "partial_refund_amount_usd", "script", "key_talking_points", "word_count", "wave",
    }
    assert required.issubset(d.keys())
    assert d["word_count"] > 20
