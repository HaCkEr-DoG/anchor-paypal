"""
Wave 112 — Response Timing Advisor  POST /api/oracle/response-timing

Tests:
  1. Fresh dispute → OPTIMAL urgency
  2. 8-day-old dispute → URGENT
  3. 10-day-old dispute → CRITICAL, should_respond_now=True
  4. 11-day-old dispute → EXPIRED
  5. High evidence completeness → should_respond_now=True
  6. Low evidence + still gathering → may wait
  7. Response shape complete
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _post(body: dict):
    return _client.post("/api/oracle/response-timing", json=body).json()


def test_optimal_timing():
    d = _post({"dispute_filed_at": _days_ago(2), "evidence_completeness": 0.8})
    assert d["urgency"] == "OPTIMAL"
    assert d["wave"] == 112


def test_urgent_timing():
    d = _post({"dispute_filed_at": _days_ago(6), "evidence_completeness": 0.6})
    assert d["urgency"] == "URGENT"


def test_critical_timing():
    d = _post({"dispute_filed_at": _days_ago(9.5), "evidence_completeness": 0.3})
    assert d["urgency"] == "CRITICAL"
    assert d["should_respond_now"] is True


def test_expired_timing():
    d = _post({"dispute_filed_at": _days_ago(11), "evidence_completeness": 0.9})
    assert d["urgency"] == "EXPIRED"
    assert d["hours_remaining"] == 0.0


def test_high_evidence_respond_now():
    d = _post({"dispute_filed_at": _days_ago(3), "evidence_completeness": 0.90})
    assert d["should_respond_now"] is True


def test_low_evidence_still_gathering():
    d = _post({
        "dispute_filed_at": _days_ago(1),
        "evidence_completeness": 0.3,
        "still_gathering_evidence": True,
        "hours_to_complete_evidence": 24.0,
    })
    # Has lots of time — can wait
    assert d["should_respond_now"] is False


def test_response_shape():
    d = _post({"dispute_filed_at": _days_ago(3)})
    required = {
        "urgency", "hours_remaining", "respond_by", "should_respond_now",
        "timing_penalty", "win_rate_at_current_timing",
        "evidence_completeness_threshold", "advice", "wave",
    }
    assert required.issubset(d.keys())
    assert len(d["advice"]) > 20
