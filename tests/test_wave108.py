"""
Wave 108 — Deadline Escalation Monitor  POST /api/oracle/deadline-escalation

Tests:
  1. No disputes → empty escalated
  2. Dispute with 1 day left → escalated
  3. Dispute with 4 days left → warning
  4. Dispute with 15 days left → ok
  5. Overdue dispute (past deadline) → escalated with overdue status
  6. Missing deadline → warning with missing_deadline issue
  7. Response shape complete
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/deadline-escalation", json=body).json()


def _days_from_now(n):
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# 1. No disputes → all empty
# ---------------------------------------------------------------------------
def test_no_disputes():
    d = _post({"disputes": []})
    assert d["escalated_count"] == 0
    assert d["total_checked"] == 0


# ---------------------------------------------------------------------------
# 2. 1 day left → escalated
# ---------------------------------------------------------------------------
def test_one_day_escalated():
    d = _post({"disputes": [{
        "dispute_id": "D1",
        "deadline_date": _days_from_now(1),
        "claim_type": "ITEM_NOT_RECEIVED",
        "amount_usd": 100,
        "status": "open",
    }]})
    assert d["escalated_count"] == 1
    assert d["escalated"][0]["status"] == "critical"


# ---------------------------------------------------------------------------
# 3. 4 days left → warning
# ---------------------------------------------------------------------------
def test_four_days_warning():
    d = _post({"disputes": [{
        "dispute_id": "D2",
        "deadline_date": _days_from_now(4),
        "claim_type": "OTHER",
        "amount_usd": 50,
        "status": "open",
    }]})
    assert d["warning_count"] >= 1


# ---------------------------------------------------------------------------
# 4. 15 days left → ok
# ---------------------------------------------------------------------------
def test_fifteen_days_ok():
    d = _post({"disputes": [{
        "dispute_id": "D3",
        "deadline_date": _days_from_now(15),
        "claim_type": "OTHER",
        "amount_usd": 50,
        "status": "open",
    }]})
    assert d["ok_count"] == 1
    assert d["escalated_count"] == 0


# ---------------------------------------------------------------------------
# 5. Overdue (past deadline) → escalated with overdue
# ---------------------------------------------------------------------------
def test_overdue_escalated():
    d = _post({"disputes": [{
        "dispute_id": "D4",
        "deadline_date": _days_from_now(-3),
        "claim_type": "OTHER",
        "amount_usd": 200,
        "status": "open",
    }]})
    assert d["escalated_count"] == 1
    assert d["escalated"][0]["status"] == "overdue"


# ---------------------------------------------------------------------------
# 6. Missing deadline → in warnings
# ---------------------------------------------------------------------------
def test_missing_deadline_warning():
    d = _post({"disputes": [{
        "dispute_id": "D5",
        "claim_type": "OTHER",
        "amount_usd": 100,
        "status": "open",
    }]})
    # Should land in warnings with missing_deadline issue
    all_issues = [w.get("issue") for w in d["warnings"]]
    assert "missing_deadline" in all_issues


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"disputes": []})
    required = {
        "checked_at_utc", "total_checked", "escalated_count",
        "warning_count", "ok_count", "escalated", "warnings", "ok", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 108
