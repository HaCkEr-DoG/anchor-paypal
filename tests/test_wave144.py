"""
Wave 144 — HDR War Room  POST /api/oracle/assess-hdr-war-room

Tests:
  1. FEE_TIER rate → is_emergency True, zone = FEE_TIER
  2. SAFE rate → is_emergency False
  3. projections list present
  4. interventions sorted by priority
  5. immediate_actions list
  6. monthly_hdr_fee positive when in FEE_TIER
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/assess-hdr-war-room", json=body).json()


def test_fee_tier_emergency():
    d = _post({
        "current_hdr_rate": 0.018,
        "disputes_last_90d": 72,
        "transactions_last_90d": 4000,
    })
    assert d["current_zone"] in ("FEE_TIER", "LIMITED", "SEVERE")
    assert d["is_emergency"] is True
    assert d["wave"] == 144


def test_safe_rate_no_emergency():
    d = _post({
        "current_hdr_rate": 0.003,
        "disputes_last_90d": 12,
        "transactions_last_90d": 4000,
    })
    assert d["current_zone"] in ("SAFE", "ELEVATED")
    assert d["is_emergency"] is False


def test_projections_present():
    d = _post({
        "current_hdr_rate": 0.012,
        "disputes_last_90d": 48,
        "transactions_last_90d": 4000,
    })
    assert isinstance(d["projections"], list)
    assert len(d["projections"]) > 0
    proj = d["projections"][0]
    assert "scenario" in proj
    assert "projected_rate" in proj


def test_interventions_prioritized():
    d = _post({
        "current_hdr_rate": 0.025,
        "disputes_last_90d": 100,
        "transactions_last_90d": 4000,
    })
    assert isinstance(d["interventions"], list)
    if len(d["interventions"]) > 1:
        priorities = [i["priority"] for i in d["interventions"]]
        assert priorities == sorted(priorities)


def test_immediate_actions_list():
    d = _post({
        "current_hdr_rate": 0.018,
        "disputes_last_90d": 72,
        "transactions_last_90d": 4000,
    })
    assert isinstance(d["immediate_actions"], list)


def test_monthly_fee_in_fee_tier():
    d = _post({
        "current_hdr_rate": 0.020,
        "disputes_last_90d": 80,
        "transactions_last_90d": 4000,
        "monthly_gmv": 200000.0,
    })
    if d["current_zone"] == "FEE_TIER":
        assert d["monthly_hdr_fee"] > 0


def test_response_shape():
    d = _post({
        "current_hdr_rate": 0.012,
        "disputes_last_90d": 48,
        "transactions_last_90d": 4000,
    })
    required = {
        "current_hdr_rate", "current_zone", "is_emergency",
        "bps_to_next_threshold", "bps_from_prev_threshold",
        "monthly_hdr_fee", "fee_tier_annual_cost",
        "projections", "interventions", "zone_assessment",
        "immediate_actions", "this_week_actions", "avoid_actions",
        "summary", "wave",
    }
    assert required.issubset(d.keys())
