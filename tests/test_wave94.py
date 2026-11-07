"""
Wave 94 — Buyer Risk Profiler  POST /api/oracle/buyer-risk

Tests:
  1. Brand-new guest account, high dispute rate → critical
  2. Established account, zero disputes → low
  3. High unauthorized disputes → high risk
  4. Large transaction vs avg → flag raised
  5. dispute_rate explicitly provided overrides computation
  6. Response has all expected keys
  7. risk_score clamped 0–100
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/buyer-risk", json=body).json()


# ---------------------------------------------------------------------------
# 1. Brand-new guest, high dispute rate → critical
# ---------------------------------------------------------------------------

def test_new_guest_high_dispute_rate():
    # score: age=0(+20) + dispute_rate=0.4(+35) + guest(+10) + 10x avg tx(+15) = 80 → critical
    d = _post({
        "buyer_account_age_days": 0,
        "total_transactions": 5,
        "total_disputes": 2,
        "is_guest_checkout": True,
        "avg_transaction_usd": 10,
        "current_transaction_usd": 100,
    })
    assert d["risk_level"] == "critical"
    assert d["accept_transaction"] is False
    assert d["require_review"] is True
    assert d["risk_score"] >= 70


# ---------------------------------------------------------------------------
# 2. Established account, clean history → low
# ---------------------------------------------------------------------------

def test_established_clean_account():
    d = _post({
        "buyer_account_age_days": 500,
        "total_transactions": 30,
        "total_disputes": 0,
        "is_guest_checkout": False,
        "current_transaction_usd": 50,
        "avg_transaction_usd": 60,
    })
    assert d["risk_level"] == "low"
    assert d["accept_transaction"] is True
    assert d["risk_score"] < 20


# ---------------------------------------------------------------------------
# 3. Multiple unauthorized disputes → high risk
# ---------------------------------------------------------------------------

def test_unauthorized_disputes_high_risk():
    d = _post({
        "buyer_account_age_days": 200,
        "total_transactions": 20,
        "total_disputes": 4,
        "unauthorized_disputes": 3,
        "is_guest_checkout": False,
    })
    assert d["risk_level"] in ("high", "critical")
    # Flag about unauthorized should be present
    assert any("unauthorized" in f.lower() for f in d["flags"])


# ---------------------------------------------------------------------------
# 4. Large transaction vs avg → flag raised
# ---------------------------------------------------------------------------

def test_large_transaction_flagged():
    d = _post({
        "buyer_account_age_days": 100,
        "total_transactions": 10,
        "total_disputes": 0,
        "avg_transaction_usd": 50,
        "current_transaction_usd": 500,  # 10× avg
    })
    assert any("avg" in f.lower() or "x buyer" in f.lower() for f in d["flags"])


# ---------------------------------------------------------------------------
# 5. Explicit dispute_rate overrides auto-compute
# ---------------------------------------------------------------------------

def test_explicit_dispute_rate():
    # Explicit 0.0 rate should not add dispute-rate flags even with disputes
    d = _post({
        "buyer_account_age_days": 90,
        "total_transactions": 10,
        "total_disputes": 5,
        "dispute_rate": 0.0,
    })
    # No high-dispute-rate flag should appear
    assert not any("dispute rate" in f.lower() for f in d["flags"])


# ---------------------------------------------------------------------------
# 6. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({})
    required = {
        "buyer_email", "risk_score", "risk_level", "flags",
        "trust_signals", "dispute_rate_pct", "recommendation",
        "accept_transaction", "require_review",
    }
    assert required.issubset(d.keys())


# ---------------------------------------------------------------------------
# 7. risk_score clamped 0–100
# ---------------------------------------------------------------------------

def test_score_clamped():
    # Pile on every bad signal possible
    d = _post({
        "buyer_account_age_days": 0,
        "total_transactions": 5,
        "total_disputes": 5,
        "unauthorized_disputes": 5,
        "dispute_rate": 1.0,
        "avg_transaction_usd": 10,
        "current_transaction_usd": 10000,
        "is_guest_checkout": True,
    })
    assert 0 <= d["risk_score"] <= 100
