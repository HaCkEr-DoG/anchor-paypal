"""
Wave 101 — Seller Performance Scorecard  POST /api/oracle/seller-performance

Tests:
  1. No disputes → grade depends on defaults
  2. High win rate → grade A
  3. Low win rate → grade F
  4. Slow response time penalizes score
  5. by_claim_type breakdown correct
  6. Response shape complete
  7. Recommendations generated for low performers
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/seller-performance", json=body).json()


def _r(outcome="won", claim="ITEM_NOT_RECEIVED", days=1, amount=100):
    return {"outcome": outcome, "claim_type": claim, "response_days": days, "amount_usd": amount}


# ---------------------------------------------------------------------------
# 1. No disputes → safe defaults
# ---------------------------------------------------------------------------
def test_no_disputes():
    d = _post({"dispute_results": []})
    assert d["total_disputes"] == 0
    assert d["win_rate"] == 0.0
    assert d["wave"] == 101


# ---------------------------------------------------------------------------
# 2. High win rate → grade A
# ---------------------------------------------------------------------------
def test_high_win_rate_grade_a():
    results = [_r("won") for _ in range(8)] + [_r("lost") for _ in range(2)]
    d = _post({"dispute_results": results})
    assert d["win_rate"] == pytest.approx(0.8, abs=0.01)
    assert d["grade"] in ("A", "B")
    assert d["composite_score"] >= 65


# ---------------------------------------------------------------------------
# 3. Low win rate → grade F or D
# ---------------------------------------------------------------------------
def test_low_win_rate_poor_grade():
    results = [_r("lost") for _ in range(10)]
    d = _post({"dispute_results": results})
    assert d["win_rate"] == 0.0
    assert d["grade"] in ("D", "F")


# ---------------------------------------------------------------------------
# 4. Slow response time penalizes
# ---------------------------------------------------------------------------
def test_slow_response_penalizes():
    fast = [_r("won", days=1) for _ in range(5)]
    slow = [_r("won", days=20) for _ in range(5)]
    d_fast = _post({"dispute_results": fast})
    d_slow = _post({"dispute_results": slow})
    assert d_fast["composite_score"] > d_slow["composite_score"]


# ---------------------------------------------------------------------------
# 5. by_claim_type breakdown
# ---------------------------------------------------------------------------
def test_by_claim_type_breakdown():
    results = [
        _r("won", "ITEM_NOT_RECEIVED"),
        _r("won", "ITEM_NOT_RECEIVED"),
        _r("lost", "UNAUTHORIZED"),
    ]
    d = _post({"dispute_results": results})
    assert "ITEM_NOT_RECEIVED" in d["by_claim_type"]
    assert "UNAUTHORIZED" in d["by_claim_type"]
    assert d["by_claim_type"]["ITEM_NOT_RECEIVED"]["win_rate"] == pytest.approx(1.0)
    assert d["by_claim_type"]["UNAUTHORIZED"]["win_rate"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"dispute_results": [_r()]})
    required = {
        "seller_id", "total_disputes", "win_rate", "composite_score",
        "grade", "summary", "by_claim_type", "recommendations", "wave",
    }
    assert required.issubset(d.keys())


# ---------------------------------------------------------------------------
# 7. Recommendations for low performers
# ---------------------------------------------------------------------------
def test_recommendations_low_performer():
    results = [_r("lost", days=15) for _ in range(10)]
    d = _post({"dispute_results": results})
    assert len(d["recommendations"]) >= 1
    assert any("win rate" in r.lower() or "slow" in r.lower() or "response" in r.lower()
               for r in d["recommendations"])
