"""
Wave 96 — Refund Leakage Detector  POST /api/oracle/refund-leakage

Tests:
  1. No refunds → leakage_level none
  2. High refund rate (5/5) → at least high
  3. Last-minute refunds → pattern detected
  4. No return refunds → pattern detected
  5. Policy abuse threshold → block_refunds = True
  6. Repeated claim type → pattern detected
  7. Response shape
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/refund-leakage", json=body).json()


def _r(claim="ITEM_NOT_RECEIVED", amount=50, days=5, outcome="refunded", returned=True):
    return {"claim_type": claim, "amount_usd": amount, "days_after_purchase": days,
            "outcome": outcome, "item_returned": returned}


# ---------------------------------------------------------------------------
# 1. No refunds
# ---------------------------------------------------------------------------

def test_no_refunds():
    d = _post({"buyer_email": "x@x.com", "refunds": []})
    assert d["leakage_level"] == "none"
    assert d["total_refunds_analyzed"] == 0
    assert d["block_refunds"] is False


# ---------------------------------------------------------------------------
# 2. All refunds granted → high rate flag
# ---------------------------------------------------------------------------

def test_high_refund_rate():
    refunds = [_r() for _ in range(5)]
    d = _post({"refunds": refunds})
    assert d["refund_rate_pct"] == 100.0
    assert d["leakage_level"] in ("high", "policy_abuse")
    assert any("refund rate" in p.lower() for p in d["patterns"])


# ---------------------------------------------------------------------------
# 3. Two last-minute refunds → pattern
# ---------------------------------------------------------------------------

def test_last_minute_pattern():
    refunds = [
        _r(days=28),
        _r(days=29),
        _r(days=5),
    ]
    d = _post({"refunds": refunds})
    assert d["last_minute_refunds"] == 2
    assert any("last-minute" in p.lower() for p in d["patterns"])


# ---------------------------------------------------------------------------
# 4. No item returned → pattern
# ---------------------------------------------------------------------------

def test_no_return_pattern():
    refunds = [_r(returned=False), _r(returned=False), _r(returned=False), _r(returned=True)]
    d = _post({"refunds": refunds})
    assert d["no_return_refunds"] == 3
    assert any("without item return" in p.lower() for p in d["patterns"])


# ---------------------------------------------------------------------------
# 5. Full abuse scenario → block_refunds = True
# ---------------------------------------------------------------------------

def test_policy_abuse_blocked():
    # High rate + last minute + no returns = very high score
    refunds = [
        _r(days=28, returned=False),
        _r(days=29, returned=False),
        _r(days=27, returned=False),
        _r(days=15, returned=False),
        _r(days=10, returned=False),
    ]
    d = _post({"refunds": refunds})
    assert d["leakage_level"] == "policy_abuse"
    assert d["block_refunds"] is True


# ---------------------------------------------------------------------------
# 6. Repeated same claim type → pattern
# ---------------------------------------------------------------------------

def test_repeated_claim_type():
    # 4 out of 4 refunds are ITEM_NOT_RECEIVED
    refunds = [_r(claim="ITEM_NOT_RECEIVED") for _ in range(4)]
    d = _post({"refunds": refunds})
    assert any("ITEM_NOT_RECEIVED" in p for p in d["patterns"])


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({"refunds": [_r()]})
    required = {
        "buyer_email", "total_refunds_analyzed", "total_refunded_usd",
        "leakage_score", "leakage_level", "patterns", "refund_rate_pct",
        "avg_days_after_purchase", "last_minute_refunds", "no_return_refunds",
        "recommendation", "block_refunds",
    }
    assert required.issubset(d.keys())
