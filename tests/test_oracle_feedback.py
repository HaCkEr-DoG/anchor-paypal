"""
Oracle Calibration Feedback Loop tests — Brier score, calibration, ES persistence.
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


# ── OutcomeRecord ─────────────────────────────────────────────────────────────

def test_as_binary_win_won():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-1", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.7, oracle_ev=50.0, oracle_action="FIGHT",
        actual_action="FIGHT", actual_outcome="WON", dispute_amount=100.0,
    )
    assert r.as_binary_win() == 1


def test_as_binary_win_lost():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-2", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.7, oracle_ev=50.0, oracle_action="FIGHT",
        actual_action="FIGHT", actual_outcome="LOST", dispute_amount=100.0,
    )
    assert r.as_binary_win() == 0


def test_as_binary_win_settled_is_none():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-3", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.5, oracle_ev=20.0, oracle_action="NEGOTIATE",
        actual_action="NEGOTIATE", actual_outcome="SETTLED", dispute_amount=100.0,
    )
    assert r.as_binary_win() is None


def test_prediction_error_correct():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-4", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.7, oracle_ev=50.0, oracle_action="FIGHT",
        actual_action="FIGHT", actual_outcome="WON", dispute_amount=100.0,
    )
    # prediction(0.7) - actual(1) = -0.3
    assert abs(r.prediction_error() - (-0.3)) < 1e-9


def test_brier_score_contribution():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-5", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.7, oracle_ev=50.0, oracle_action="FIGHT",
        actual_action="FIGHT", actual_outcome="WON", dispute_amount=100.0,
    )
    # (0.7 - 1)^2 = 0.09
    assert abs(r.brier_score_contribution() - 0.09) < 1e-9


def test_brier_contribution_none_for_settled():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-6", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.5, oracle_ev=10.0, oracle_action="NEGOTIATE",
        actual_action="NEGOTIATE", actual_outcome="SETTLED", dispute_amount=100.0,
    )
    assert r.brier_score_contribution() is None


def test_recorded_at_auto_set():
    from oracle.feedback import OutcomeRecord
    r = OutcomeRecord(
        dispute_id="PP-D-7", merchant_id="m1", claim_type="INR",
        oracle_prediction=0.5, oracle_ev=0.0, oracle_action="ACCEPT",
        actual_action="ACCEPT", actual_outcome="ACCEPTED", dispute_amount=50.0,
    )
    assert r.recorded_at != ""
    assert "2026" in r.recorded_at or "202" in r.recorded_at


# ── _compute_stats ────────────────────────────────────────────────────────────

def test_compute_stats_empty():
    from oracle.feedback import _compute_stats
    stats = _compute_stats("m1", [])
    assert stats.total_records == 0
    assert stats.brier_score == 0.25  # random baseline
    assert stats.recommendation_quality == "needs_improvement"


def test_compute_stats_perfect_predictions():
    from oracle.feedback import _compute_stats, OutcomeRecord
    # Perfect: predict 1.0 and win, predict 0.0 and lose
    records = [
        OutcomeRecord("d1", "m1", "INR", 1.0, 100.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d2", "m1", "INR", 0.0, -50.0, "ACCEPT", "ACCEPT", "LOST", 100.0),
    ]
    stats = _compute_stats("m1", records)
    assert stats.brier_score == 0.0
    assert stats.recommendation_quality in ("excellent",)


def test_compute_stats_brier_random_baseline():
    from oracle.feedback import _compute_stats, OutcomeRecord
    # Predict 0.5 for everything — Brier score should be 0.25
    records = [
        OutcomeRecord("d1", "m1", "INR", 0.5, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d2", "m1", "INR", 0.5, 0.0, "FIGHT", "FIGHT", "LOST", 100.0),
    ]
    stats = _compute_stats("m1", records)
    # (0.5-1)^2 = 0.25, (0.5-0)^2 = 0.25 → avg = 0.25
    assert abs(stats.brier_score - 0.25) < 1e-9


def test_compute_stats_calibration_error():
    from oracle.feedback import _compute_stats, OutcomeRecord
    # Predict 0.8 but only win 0.6 → overconfident by +0.2
    records = [
        OutcomeRecord("d1", "m1", "INR", 0.8, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d2", "m1", "INR", 0.8, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d3", "m1", "INR", 0.8, 0.0, "FIGHT", "FIGHT", "LOST", 100.0),
        OutcomeRecord("d4", "m1", "INR", 0.8, 0.0, "FIGHT", "FIGHT", "LOST", 100.0),
        OutcomeRecord("d5", "m1", "INR", 0.8, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
    ]
    stats = _compute_stats("m1", records)
    # mean_prediction = 0.8, actual_win_rate = 3/5 = 0.6, error = +0.20
    assert abs(stats.calibration_error - 0.20) < 0.01


def test_compute_stats_win_rate_by_claim_type():
    from oracle.feedback import _compute_stats, OutcomeRecord
    records = [
        OutcomeRecord("d1", "m1", "INR", 0.7, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d2", "m1", "INR", 0.7, 0.0, "FIGHT", "FIGHT", "LOST", 100.0),
        OutcomeRecord("d3", "m1", "SNAD", 0.6, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
    ]
    stats = _compute_stats("m1", records)
    assert "INR" in stats.win_rate_by_claim_type
    assert "SNAD" in stats.win_rate_by_claim_type
    assert stats.win_rate_by_claim_type["INR"]["actual_win_rate"] == 0.5
    assert stats.win_rate_by_claim_type["SNAD"]["actual_win_rate"] == 1.0


def test_compute_stats_quality_excellent():
    from oracle.feedback import _compute_stats, OutcomeRecord
    # Very accurate predictions → excellent
    records = [
        OutcomeRecord("d1", "m1", "INR", 0.95, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d2", "m1", "INR", 0.95, 0.0, "FIGHT", "FIGHT", "WON", 100.0),
        OutcomeRecord("d3", "m1", "INR", 0.05, 0.0, "ACCEPT", "ACCEPT", "LOST", 100.0),
    ]
    stats = _compute_stats("m1", records)
    assert stats.recommendation_quality in ("excellent", "good")


# ── record_outcome (no ES) ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_no_es_returns_false():
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from oracle.feedback import record_outcome, OutcomeRecord
    r = OutcomeRecord(
        "PP-D-X", "m1", "INR", 0.5, 0.0, "FIGHT", "FIGHT", "WON", 100.0
    )
    result = await record_outcome(r)
    assert result is False


@pytest.mark.asyncio
async def test_get_calibration_stats_no_es_returns_empty():
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from oracle.feedback import get_calibration_stats
    stats = await get_calibration_stats("m1")
    assert stats.total_records == 0
    assert stats.brier_score == 0.25


# ── API endpoint ──────────────────────────────────────────────────────────────

def test_oracle_calibration_api_endpoint_exists():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    from oracle.feedback import CalibrationStats
    from datetime import datetime, timezone
    client = TestClient(app)
    mock_stats = CalibrationStats(
        merchant_id="merchant1",
        total_records=10,
        brier_score=0.15,
        mean_prediction=0.65,
        actual_win_rate=0.60,
        calibration_error=0.05,
        win_rate_by_claim_type={},
        oracle_accuracy=0.70,
        recommendation_quality="good",
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
    with patch("oracle.feedback.get_calibration_stats", new_callable=AsyncMock, return_value=mock_stats):
        resp = client.get("/api/oracle/calibration/merchant1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["brier_score"] == 0.15
    assert data["recommendation_quality"] == "good"


def test_oracle_calibration_default_endpoint():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    from oracle.feedback import CalibrationStats
    from datetime import datetime, timezone
    client = TestClient(app)
    mock_stats = CalibrationStats(
        merchant_id="default",
        total_records=0,
        brier_score=0.25,
        mean_prediction=0.0,
        actual_win_rate=0.0,
        calibration_error=0.0,
        win_rate_by_claim_type={},
        oracle_accuracy=0.0,
        recommendation_quality="needs_improvement",
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
    with patch("oracle.feedback.get_calibration_stats", new_callable=AsyncMock, return_value=mock_stats):
        resp = client.get("/api/oracle/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_records"] == 0
