"""
Wave 107 — Recovery Probability Scorer  POST /api/oracle/recovery-score

Tests:
  1. All positive factors → high probability → fight
  2. No positive factors → very_low → settle
  3. Tracking boosts probability
  4. Low response quality reduces probability
  5. Repeat buyer boosts probability
  6. Expected recovery USD calculated correctly
  7. Response shape complete
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/recovery-score", json=body).json()


# ---------------------------------------------------------------------------
# 1. All positive factors → fight
# ---------------------------------------------------------------------------
def test_all_positive_factors():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "amount_usd": 200,
        "has_tracking": True,
        "has_delivery_confirmation": True,
        "has_communication_log": True,
        "has_photos": True,
        "has_refund_policy": True,
        "buyer_dispute_history": 5,
        "response_quality_score": 90,
    })
    assert d["recovery_probability"] >= 0.7
    assert d["recommended_action"] == "fight"
    assert d["confidence_band"] == "high"


# ---------------------------------------------------------------------------
# 2. No positive factors → settle
# ---------------------------------------------------------------------------
def test_no_positive_factors():
    d = _post({
        "claim_type": "UNAUTHORIZED",
        "amount_usd": 100,
        "response_quality_score": 20,
    })
    assert d["recovery_probability"] <= 0.5
    assert d["recommended_action"] in ("settle", "negotiate")


# ---------------------------------------------------------------------------
# 3. Tracking boosts probability
# ---------------------------------------------------------------------------
def test_tracking_boosts():
    base = _post({"claim_type": "ITEM_NOT_RECEIVED", "has_tracking": False})
    with_tracking = _post({"claim_type": "ITEM_NOT_RECEIVED", "has_tracking": True})
    assert with_tracking["recovery_probability"] > base["recovery_probability"]


# ---------------------------------------------------------------------------
# 4. Low quality response penalizes
# ---------------------------------------------------------------------------
def test_low_quality_penalizes():
    good = _post({"claim_type": "OTHER", "response_quality_score": 85})
    poor = _post({"claim_type": "OTHER", "response_quality_score": 20})
    assert good["recovery_probability"] > poor["recovery_probability"]


# ---------------------------------------------------------------------------
# 5. Repeat buyer boosts
# ---------------------------------------------------------------------------
def test_repeat_buyer_boosts():
    base = _post({"claim_type": "OTHER", "buyer_dispute_history": 0})
    repeat = _post({"claim_type": "OTHER", "buyer_dispute_history": 5})
    assert repeat["recovery_probability"] > base["recovery_probability"]


# ---------------------------------------------------------------------------
# 6. Expected recovery calculated
# ---------------------------------------------------------------------------
def test_expected_recovery_usd():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "amount_usd": 100,
        "has_tracking": True,
    })
    assert d["expected_recovery_usd"] == pytest.approx(
        d["recovery_probability"] * 100, abs=0.5
    )


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"claim_type": "OTHER", "amount_usd": 50})
    required = {
        "claim_type", "recovery_probability", "confidence_band",
        "recommended_action", "base_probability", "adjustments",
        "expected_recovery_usd", "wave",
    }
    assert required.issubset(d.keys())
    assert d["wave"] == 107
