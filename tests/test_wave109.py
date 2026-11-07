"""
Wave 109 — Dispute Portfolio Snapshot  POST /api/oracle/portfolio-snapshot

Tests:
  1. Empty portfolio → safe defaults
  2. High exposure → risk_rating critical or high
  3. Win rate calculated from resolved
  4. Critical deadline disputes counted
  5. Claim type distribution correct
  6. Low exposure/count → low risk
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/portfolio-snapshot", json=body).json()


def _d(status="open", outcome=None, amount=100, claim="ITEM_NOT_RECEIVED", deadline_days=10):
    r = {"status": status, "amount_usd": amount, "claim_type": claim,
         "days_until_deadline": deadline_days}
    if outcome:
        r["outcome"] = outcome
        r["status"] = "resolved"
    return r


# ---------------------------------------------------------------------------
# 1. Empty portfolio
# ---------------------------------------------------------------------------
def test_empty_portfolio():
    d = _post({"disputes": []})
    assert d["total_disputes"] == 0
    assert d["total_open_exposure_usd"] == 0.0
    assert d["win_rate"] is None


# ---------------------------------------------------------------------------
# 2. High exposure → high or critical risk
# ---------------------------------------------------------------------------
def test_high_exposure_risk():
    disputes = [_d(amount=5000) for _ in range(3)]
    d = _post({"disputes": disputes})
    assert d["risk_rating"] in ("high", "critical")


# ---------------------------------------------------------------------------
# 3. Win rate from resolved
# ---------------------------------------------------------------------------
def test_win_rate_from_resolved():
    disputes = [_d(outcome="won") for _ in range(4)] + [_d(outcome="lost") for _ in range(1)]
    d = _post({"disputes": disputes})
    assert d["win_rate"] == pytest.approx(0.8, abs=0.01)


# ---------------------------------------------------------------------------
# 4. Critical deadline counted
# ---------------------------------------------------------------------------
def test_critical_deadline_count():
    disputes = [_d(deadline_days=1), _d(deadline_days=1), _d(deadline_days=15)]
    d = _post({"disputes": disputes})
    assert d["critical_deadline_count"] == 2


# ---------------------------------------------------------------------------
# 5. Claim type distribution
# ---------------------------------------------------------------------------
def test_claim_type_distribution():
    disputes = [_d(claim="ITEM_NOT_RECEIVED") for _ in range(3)] + [_d(claim="UNAUTHORIZED")]
    d = _post({"disputes": disputes})
    assert d["claim_type_distribution"]["ITEM_NOT_RECEIVED"] == 3
    assert d["claim_type_distribution"]["UNAUTHORIZED"] == 1


# ---------------------------------------------------------------------------
# 6. Low exposure → low risk
# ---------------------------------------------------------------------------
def test_low_exposure_low_risk():
    disputes = [_d(amount=50, deadline_days=20) for _ in range(2)]
    d = _post({"disputes": disputes})
    assert d["risk_rating"] == "low"


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"disputes": [_d()]})
    required = {
        "merchant_id", "total_disputes", "open_disputes", "resolved_disputes",
        "total_open_exposure_usd", "risk_rating", "claim_type_distribution", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 109
