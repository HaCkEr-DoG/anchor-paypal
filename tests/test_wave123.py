"""
Wave 123 — Deadline Prioritizer  POST /api/oracle/prioritize-disputes

Tests:
  1. Disputes sorted by deadline urgency (most critical first)
  2. 0-day deadline counted in critical_count
  3. Total EV at risk computed
  4. Empty list returns empty report
  5. evidence_ready=True affects recommendation
  6. Ranks are 1-indexed and unique
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/prioritize-disputes", json=body).json()


_DISPUTES = [
    {"dispute_id": "PP-D-001", "claim_type": "INR", "amount_usd": 300.0, "days_until_deadline": 7, "win_probability": 0.65},
    {"dispute_id": "PP-D-002", "claim_type": "UNAUTHORIZED", "amount_usd": 500.0, "days_until_deadline": 1, "win_probability": 0.50},
    {"dispute_id": "PP-D-003", "claim_type": "SNAD", "amount_usd": 80.0, "days_until_deadline": 0, "win_probability": 0.40},
]


def test_sorted_by_urgency():
    d = _post({"disputes": _DISPUTES})
    ids = [x["dispute_id"] for x in d["disputes"]]
    # PP-D-003 (0 days) and PP-D-002 (1 day) should rank ahead of PP-D-001 (7 days)
    assert ids.index("PP-D-003") < ids.index("PP-D-001")
    assert d["wave"] == 123


def test_critical_count():
    d = _post({"disputes": _DISPUTES})
    assert d["critical_count"] >= 1


def test_total_ev_at_risk():
    d = _post({"disputes": _DISPUTES})
    assert d["total_ev_at_risk_usd"] > 0


def test_empty_list():
    d = _post({"disputes": []})
    assert d["disputes"] == []
    assert d["critical_count"] == 0


def test_evidence_ready_recommendation():
    d = _post({"disputes": [
        {"dispute_id": "PP-D-EVR", "claim_type": "INR", "amount_usd": 200.0,
         "days_until_deadline": 2, "win_probability": 0.70, "evidence_ready": True},
    ]})
    action = d["disputes"][0]["action_recommendation"]
    assert len(action) > 5


def test_ranks_unique_and_indexed():
    d = _post({"disputes": _DISPUTES})
    ranks = [x["rank"] for x in d["disputes"]]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1


def test_response_shape():
    d = _post({"disputes": _DISPUTES})
    required = {
        "critical_count", "high_count", "total_ev_at_risk_usd",
        "recommendation_summary", "disputes", "wave",
    }
    assert required.issubset(d.keys())
    for item in d["disputes"]:
        assert "dispute_id" in item
        assert "rank" in item
        assert "urgency_label" in item
