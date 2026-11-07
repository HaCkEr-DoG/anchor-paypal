"""
Wave 146 — Win/Loss Analyzer  POST /api/oracle/analyze-win-loss

Tests:
  1. 5 wins + 3 losses → overall_win_rate = 0.625
  2. All losses → win_rate = 0.0
  3. win_rate_by_claim_type is list
  4. insights list present
  5. calibration_updates is dict
  6. Empty outcomes → 0 total_outcomes
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-win-loss", json=body).json()


def _outcome(dispute_id, claim_type="INR", outcome="WON_INITIAL", tracking=True, quality="STRONG"):
    return {
        "dispute_id": dispute_id, "claim_type": claim_type,
        "dispute_amount": 100.0, "outcome": outcome,
        "had_tracking": tracking, "evidence_quality_tier": quality,
    }


def test_win_rate_625():
    outcomes = (
        [_outcome(f"PP-D-{i:03d}") for i in range(5)] +
        [_outcome(f"PP-D-{i:03d}", outcome="LOST_INITIAL", tracking=False, quality="WEAK")
         for i in range(5, 8)]
    )
    d = _post({"outcomes": outcomes})
    assert abs(d["overall_win_rate"] - 0.625) < 0.01
    assert d["wave"] == 146


def test_all_losses_zero_rate():
    outcomes = [_outcome(f"PP-D-{i:03d}", outcome="LOST_INITIAL") for i in range(4)]
    d = _post({"outcomes": outcomes})
    assert d["overall_win_rate"] == 0.0


def test_win_rate_by_claim_type_list():
    outcomes = [_outcome(f"PP-D-{i:03d}") for i in range(3)]
    d = _post({"outcomes": outcomes})
    assert isinstance(d["win_rate_by_claim_type"], list)
    if d["win_rate_by_claim_type"]:
        entry = d["win_rate_by_claim_type"][0]
        assert "segment_name" in entry
        assert "win_rate" in entry


def test_insights_list():
    outcomes = (
        [_outcome(f"PP-D-{i:03d}", quality="STRONG") for i in range(5)] +
        [_outcome(f"PP-D-{i:03d}", quality="WEAK", outcome="LOST_INITIAL") for i in range(5, 10)]
    )
    d = _post({"outcomes": outcomes})
    assert isinstance(d["insights"], list)


def test_calibration_updates_dict():
    outcomes = [_outcome(f"PP-D-{i:03d}") for i in range(5)]
    d = _post({"outcomes": outcomes})
    assert isinstance(d["calibration_updates"], dict)


def test_empty_outcomes():
    d = _post({"outcomes": []})
    assert d["total_outcomes"] == 0


def test_response_shape():
    outcomes = [_outcome("PP-D-001")]
    d = _post({"outcomes": outcomes})
    required = {
        "generated_date", "total_outcomes", "total_wins", "total_losses",
        "total_settled", "overall_win_rate",
        "win_rate_by_claim_type", "win_rate_by_evidence_quality",
        "insights", "calibration_updates",
        "settled_vs_fought_comparison", "summary", "wave",
    }
    assert required.issubset(d.keys())
