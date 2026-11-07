"""
Wave 106 — Fraud Pattern Detector  POST /api/oracle/fraud-pattern

Tests:
  1. No disputes → low fraud risk
  2. Repeat buyer (3+ disputes same email) → pattern detected
  3. Rapid filing (< 3 days) → pattern detected
  4. High refund rate → pattern detected
  5. High fraud score → critical risk
  6. Cross-border concentration detected
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/fraud-pattern", json=body).json()


def _d(email="buyer@test.com", outcome="lost", amount=100, days=10, country="US"):
    return {"buyer_email": email, "outcome": outcome, "amount_usd": amount,
            "days_since_purchase": days, "ip_country": country}


# ---------------------------------------------------------------------------
# 1. No disputes → low risk
# ---------------------------------------------------------------------------
def test_no_disputes():
    d = _post({"disputes": []})
    assert d["fraud_risk"] == "low"
    assert d["fraud_score"] == 0
    assert d["total_analyzed"] == 0


# ---------------------------------------------------------------------------
# 2. Repeat buyer (3+ disputes)
# ---------------------------------------------------------------------------
def test_repeat_buyer_detected():
    disputes = [_d(email="fraud@test.com") for _ in range(4)]
    d = _post({"disputes": disputes})
    patterns = [p["pattern"] for p in d["patterns_detected"]]
    assert "repeat_dispute_buyer" in patterns
    assert d["repeat_buyer_count"] >= 1


# ---------------------------------------------------------------------------
# 3. Rapid filing detected
# ---------------------------------------------------------------------------
def test_rapid_filing_detected():
    disputes = [_d(days=1) for _ in range(5)] + [_d(days=20)]
    d = _post({"disputes": disputes})
    patterns = [p["pattern"] for p in d["patterns_detected"]]
    assert "rapid_dispute_filing" in patterns


# ---------------------------------------------------------------------------
# 4. High refund rate
# ---------------------------------------------------------------------------
def test_high_refund_rate():
    disputes = [_d(outcome="lost") for _ in range(5)] + [_d(outcome="won")]
    d = _post({"disputes": disputes, "threshold_refund_rate": 0.3})
    patterns = [p["pattern"] for p in d["patterns_detected"]]
    assert "high_refund_rate" in patterns


# ---------------------------------------------------------------------------
# 5. Multiple patterns → critical risk
# ---------------------------------------------------------------------------
def test_multiple_patterns_critical():
    email = "superfraud@evil.com"
    disputes = [_d(email=email, days=1, outcome="lost") for _ in range(5)]
    d = _post({"disputes": disputes})
    assert d["fraud_risk"] in ("critical", "high")
    assert d["fraud_score"] >= 35


# ---------------------------------------------------------------------------
# 6. Cross-border concentration
# ---------------------------------------------------------------------------
def test_cross_border_pattern():
    disputes = [_d(country="RU") for _ in range(5)] + [_d(country="US")]
    d = _post({"disputes": disputes})
    patterns = [p["pattern"] for p in d["patterns_detected"]]
    assert "cross_border_concentration" in patterns


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"disputes": [_d()]})
    required = {
        "total_analyzed", "patterns_detected", "fraud_score", "fraud_risk",
        "refund_rate", "recommendations", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 106
