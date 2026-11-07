"""
Wave 148 — Chargeback Rate Forecaster  POST /api/oracle/forecast-chargeback-rate

Tests:
  1. Rising trend → alert_level not LOW
  2. Flat/low data → alert_level LOW or ELEVATED
  3. forecast_rate is float
  4. confidence band: confidence_low <= forecast_rate <= confidence_high
  5. contributing_factors is list
  6. days_to_threshold is int or None
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/forecast-chargeback-rate", json=body).json()


def _record(month, transactions=1000, disputes=5, unauth=1, inr=2, snad=2):
    return {
        "month_label": month,
        "total_transactions": transactions,
        "total_disputes": disputes,
        "unauth_disputes": unauth,
        "inr_disputes": inr,
        "snad_disputes": snad,
    }


def test_rising_trend_alert():
    records = [
        _record("2026-05", disputes=5),
        _record("2026-06", disputes=10),
        _record("2026-07", disputes=18),
        _record("2026-08", disputes=28),
    ]
    d = _post({"merchant_id": "M-001", "monthly_records": records})
    assert d["alert_level"] in ("ELEVATED", "WARNING", "CRITICAL")
    assert d["wave"] == 148


def test_stable_low_no_alert():
    records = [_record(f"2026-0{i}", disputes=3) for i in range(1, 5)]
    d = _post({"merchant_id": "M-002", "monthly_records": records})
    assert d["alert_level"] in ("LOW", "NORMAL", "ELEVATED")


def test_forecast_rate_is_float():
    records = [_record("2026-10")]
    d = _post({"merchant_id": "M-003", "monthly_records": records})
    assert isinstance(d["forecast_rate"], float)


def test_confidence_band():
    records = [_record(f"2026-0{i}") for i in range(1, 5)]
    d = _post({"merchant_id": "M-004", "monthly_records": records})
    assert d["confidence_low"] <= d["forecast_rate"] <= d["confidence_high"]


def test_contributing_factors_list():
    records = [_record("2026-10", disputes=20)]
    d = _post({"merchant_id": "M-005", "monthly_records": records})
    assert isinstance(d["contributing_factors"], list)


def test_days_to_threshold():
    records = [_record("2026-10")]
    d = _post({"merchant_id": "M-006", "monthly_records": records})
    assert d["days_to_threshold"] is None or isinstance(d["days_to_threshold"], int)


def test_response_shape():
    records = [_record("2026-10")]
    d = _post({"merchant_id": "M-007", "monthly_records": records})
    required = {
        "merchant_id", "base_rate", "trend", "trend_magnitude",
        "forecast_rate", "confidence_low", "confidence_high",
        "alert_level", "contributing_factors", "recommendation",
        "days_to_threshold", "wave",
    }
    assert required.issubset(d.keys())
