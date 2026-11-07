"""
Wave 110 — Dispute Trend Analyzer  POST /api/oracle/dispute-trend

Tests:
  1. No buckets → insufficient_data
  2. Insufficient buckets for trend window → insufficient_data
  3. Increasing volume trend detected
  4. Decreasing volume trend detected
  5. Improving win rate trend detected
  6. Declining win rate trend detected
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/dispute-trend", json=body).json()


def _bucket(label, count, wins, amount=1000):
    return {"period_label": label, "dispute_count": count, "win_count": wins, "total_amount_usd": amount}


# ---------------------------------------------------------------------------
# 1. No buckets → insufficient_data
# ---------------------------------------------------------------------------
def test_no_buckets():
    d = _post({"buckets": []})
    assert d["trend"] == "insufficient_data"
    assert d["wave"] == 110


# ---------------------------------------------------------------------------
# 2. Too few buckets for trend window
# ---------------------------------------------------------------------------
def test_too_few_buckets():
    buckets = [_bucket(f"W{i}", 10, 5) for i in range(4)]
    d = _post({"buckets": buckets, "trend_window": 3})
    assert d["volume_trend"] == "insufficient_data"
    assert d["win_rate_trend"] == "insufficient_data"


# ---------------------------------------------------------------------------
# 3. Increasing volume
# ---------------------------------------------------------------------------
def test_increasing_volume():
    prior = [_bucket(f"P{i}", 5, 3) for i in range(3)]
    recent = [_bucket(f"R{i}", 10, 6) for i in range(3)]
    d = _post({"buckets": prior + recent, "trend_window": 3})
    assert d["volume_trend"] == "increasing"


# ---------------------------------------------------------------------------
# 4. Decreasing volume
# ---------------------------------------------------------------------------
def test_decreasing_volume():
    prior = [_bucket(f"P{i}", 10, 5) for i in range(3)]
    recent = [_bucket(f"R{i}", 4, 2) for i in range(3)]
    d = _post({"buckets": prior + recent, "trend_window": 3})
    assert d["volume_trend"] == "decreasing"


# ---------------------------------------------------------------------------
# 5. Improving win rate
# ---------------------------------------------------------------------------
def test_improving_win_rate():
    prior = [_bucket(f"P{i}", 10, 3) for i in range(3)]   # 30% wr
    recent = [_bucket(f"R{i}", 10, 8) for i in range(3)]  # 80% wr
    d = _post({"buckets": prior + recent, "trend_window": 3})
    assert d["win_rate_trend"] == "improving"


# ---------------------------------------------------------------------------
# 6. Declining win rate
# ---------------------------------------------------------------------------
def test_declining_win_rate():
    prior = [_bucket(f"P{i}", 10, 8) for i in range(3)]   # 80% wr
    recent = [_bucket(f"R{i}", 10, 2) for i in range(3)]  # 20% wr
    d = _post({"buckets": prior + recent, "trend_window": 3})
    assert d["win_rate_trend"] == "declining"


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    buckets = [_bucket(f"W{i}", 10, 5) for i in range(6)]
    d = _post({"buckets": buckets, "trend_window": 3})
    required = {
        "merchant_id", "total_buckets", "trend_window",
        "volume_trend", "win_rate_trend",
        "overall_dispute_count", "overall_win_rate",
        "buckets", "insights", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 110
