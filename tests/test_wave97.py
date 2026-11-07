"""
Wave 97 — Dispute Win Rate Benchmarker  POST /api/oracle/win-rate-benchmark

Tests:
  1. No results → 0 disputes, no improvements needed
  2. 100% win rate → above_average, appears in strengths
  3. 0% win rate → below_average, improvement opportunity listed
  4. Mixed claim types tracked separately in by_claim_type
  5. Unknown claim type normalized to OTHER
  6. Settled outcomes not counted as wins
  7. Response shape complete
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/win-rate-benchmark", json=body).json()


def _r(claim="ITEM_NOT_RECEIVED", outcome="won"):
    return {"claim_type": claim, "outcome": outcome}


# ---------------------------------------------------------------------------
# 1. No results
# ---------------------------------------------------------------------------

def test_no_results():
    d = _post({"results": []})
    assert d["total_disputes"] == 0
    assert d["by_claim_type"] == {}


# ---------------------------------------------------------------------------
# 2. Perfect win rate → above_average or top_quartile
# ---------------------------------------------------------------------------

def test_perfect_win_rate():
    # 5 wins on ITEM_NOT_RECEIVED → 100% > top_quartile (75%)
    results = [_r("ITEM_NOT_RECEIVED", "won") for _ in range(5)]
    d = _post({"results": results})
    assert d["overall_win_rate"] == 1.0
    assert d["overall_percentile"] == "above_average"
    ct = d["by_claim_type"]["ITEM_NOT_RECEIVED"]
    assert ct["performance"] == "top_quartile"
    assert len(d["strengths"]) >= 1


# ---------------------------------------------------------------------------
# 3. 0% win rate → below_average
# ---------------------------------------------------------------------------

def test_zero_win_rate():
    results = [_r("ITEM_NOT_RECEIVED", "lost") for _ in range(5)]
    d = _post({"results": results})
    assert d["overall_win_rate"] == 0.0
    assert d["overall_percentile"] == "below_average"
    assert len(d["improvement_opportunities"]) >= 1


# ---------------------------------------------------------------------------
# 4. Mixed claim types
# ---------------------------------------------------------------------------

def test_mixed_claim_types():
    results = [
        _r("ITEM_NOT_RECEIVED", "won"),
        _r("ITEM_NOT_RECEIVED", "won"),
        _r("UNAUTHORIZED", "lost"),
        _r("UNAUTHORIZED", "lost"),
    ]
    d = _post({"results": results})
    assert "ITEM_NOT_RECEIVED" in d["by_claim_type"]
    assert "UNAUTHORIZED" in d["by_claim_type"]
    assert d["by_claim_type"]["ITEM_NOT_RECEIVED"]["win_rate"] == 1.0
    assert d["by_claim_type"]["UNAUTHORIZED"]["win_rate"] == 0.0


# ---------------------------------------------------------------------------
# 5. Unknown claim type → OTHER
# ---------------------------------------------------------------------------

def test_unknown_claim_type():
    results = [_r("MYSTERY_TYPE", "won")]
    d = _post({"results": results})
    assert "OTHER" in d["by_claim_type"]
    assert "MYSTERY_TYPE" not in d["by_claim_type"]


# ---------------------------------------------------------------------------
# 6. Settled not counted as win
# ---------------------------------------------------------------------------

def test_settled_not_win():
    results = [
        _r("ITEM_NOT_RECEIVED", "won"),
        _r("ITEM_NOT_RECEIVED", "settled"),
        _r("ITEM_NOT_RECEIVED", "settled"),
    ]
    d = _post({"results": results})
    ct = d["by_claim_type"]["ITEM_NOT_RECEIVED"]
    assert ct["wins"] == 1
    assert ct["settled"] == 2
    assert ct["win_rate"] == pytest.approx(1 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    results = [_r()]
    d = _post({"results": results})
    required = {
        "merchant_id", "total_disputes", "overall_win_rate",
        "overall_benchmark_avg", "overall_percentile",
        "by_claim_type", "improvement_opportunities", "strengths",
    }
    assert required.issubset(d.keys())
