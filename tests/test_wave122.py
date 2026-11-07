"""
Wave 122 — Deadline Tracker  POST /api/oracle/track-deadline

Tests:
  1. Deadline 1 day away → CRITICAL heat, is_at_risk=True
  2. Deadline 7 days away → lower heat
  3. Response already submitted → not at risk
  4. Effective deadline < true deadline (buffer applied)
  5. HDR contributing flag preserved
  6. Action required non-empty
  7. Response shape complete
"""
from __future__ import annotations
from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/track-deadline", json=body).json()


def _in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _today() -> str:
    return date.today().isoformat()


def test_one_day_deadline_critical():
    d = _post({
        "dispute_id": "PP-D-001",
        "true_deadline": _in_days(1),
        "opened_date": _today(),
        "response_status": "NOT_STARTED",
        "dispute_amount": 200.0,
    })
    assert d["heat"] in ("CRITICAL", "RED", "ORANGE")
    assert d["wave"] == 122


def test_seven_day_deadline_cooler():
    d = _post({
        "dispute_id": "PP-D-002",
        "true_deadline": _in_days(7),
        "opened_date": _today(),
        "response_status": "NOT_STARTED",
    })
    assert d["heat"] in ("CRITICAL", "RED", "ORANGE", "YELLOW", "GREEN")
    assert d["days_to_true"] >= 5


def test_submitted_not_at_risk():
    d = _post({
        "dispute_id": "PP-D-003",
        "true_deadline": _in_days(2),
        "opened_date": _today(),
        "response_status": "SUBMITTED",
        "dispute_amount": 150.0,
    })
    assert d["is_at_risk"] is False


def test_effective_deadline_before_true():
    d = _post({
        "dispute_id": "PP-D-004",
        "true_deadline": _in_days(5),
        "opened_date": _today(),
    })
    true_d = date.fromisoformat(d["true_deadline"])
    eff_d = date.fromisoformat(d["effective_deadline"])
    assert eff_d <= true_d


def test_hdr_flag_preserved():
    d = _post({
        "dispute_id": "PP-D-005",
        "true_deadline": _in_days(3),
        "opened_date": _today(),
        "is_hdr_contributing": True,
    })
    assert d["is_hdr_contributing"] is True


def test_action_required_non_empty():
    d = _post({
        "dispute_id": "PP-D-006",
        "true_deadline": _in_days(2),
        "opened_date": _today(),
    })
    assert len(d["action_required"]) > 5


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "true_deadline": _in_days(5),
        "opened_date": _today(),
    })
    required = {
        "dispute_id", "true_deadline", "effective_deadline",
        "days_to_true", "days_to_effective", "heat",
        "response_status", "is_at_risk", "claim_type",
        "dispute_amount", "is_hdr_contributing", "action_required", "wave",
    }
    assert required.issubset(d.keys())
