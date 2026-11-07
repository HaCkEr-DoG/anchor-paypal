"""
Wave 102 — Case Similarity Finder  POST /api/oracle/case-similarity

Tests:
  1. INR with tracking → matches INR-HIGH-TRACK archetype
  2. INR without tracking → matches INR-NO-TRACK, low win rate → settle
  3. Repeat buyer detected
  4. Unauthorized digital → matched archetype
  5. Response shape complete
  6. High similarity win rate → fight recommendation
  7. Low similarity win rate → settle recommendation
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/case-similarity", json=body).json()


# ---------------------------------------------------------------------------
# 1. INR with tracking → INR-HIGH-TRACK match
# ---------------------------------------------------------------------------
def test_inr_with_tracking():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": True,
        "amount_usd": 150,
    })
    ids = [m["archetype_id"] for m in d["similar_cases"]]
    assert "INR-HIGH-TRACK" in ids
    top = d["similar_cases"][0]
    assert top["typical_win_rate"] >= 0.6


# ---------------------------------------------------------------------------
# 2. INR without tracking → low win rate → settle
# ---------------------------------------------------------------------------
def test_inr_no_tracking_settle():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery_confirmation": False,
    })
    ids = [m["archetype_id"] for m in d["similar_cases"]]
    assert "INR-NO-TRACK" in ids
    assert d["recommendation"] in ("settle", "negotiate")


# ---------------------------------------------------------------------------
# 3. Repeat buyer → REPEAT-BUYER archetype surfaced
# ---------------------------------------------------------------------------
def test_repeat_buyer():
    d = _post({
        "claim_type": "OTHER",
        "buyer_history_disputes": 5,
    })
    ids = [m["archetype_id"] for m in d["similar_cases"]]
    assert "REPEAT-BUYER" in ids


# ---------------------------------------------------------------------------
# 4. Unauthorized digital goods
# ---------------------------------------------------------------------------
def test_unauthorized_digital():
    d = _post({
        "claim_type": "UNAUTHORIZED",
        "is_digital_goods": True,
    })
    ids = [m["archetype_id"] for m in d["similar_cases"]]
    assert "UNAUTH-DIGITAL" in ids


# ---------------------------------------------------------------------------
# 5. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"claim_type": "OTHER"})
    required = {
        "claim_type", "similar_cases", "predicted_win_rate",
        "recommendation", "confidence", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["similar_cases"], list)


# ---------------------------------------------------------------------------
# 6. High win rate → fight recommendation
# ---------------------------------------------------------------------------
def test_high_win_rate_fight():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": True,
    })
    assert d["recommendation"] == "fight"


# ---------------------------------------------------------------------------
# 7. Low win rate → settle recommendation
# ---------------------------------------------------------------------------
def test_low_win_rate_settle():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery_confirmation": False,
        "is_digital_goods": False,
        "buyer_history_disputes": 0,
    })
    assert d["recommendation"] in ("settle", "negotiate")
