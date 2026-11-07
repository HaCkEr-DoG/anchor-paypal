"""
Wave 103 — Evidence Gap Analyzer  POST /api/oracle/evidence-gap

Tests:
  1. All evidence present → readiness=ready
  2. Missing critical evidence → readiness=not_ready
  3. INR missing tracking → critical gap
  4. SNAD missing photos → critical gap
  5. UNAUTHORIZED missing IP log → critical gap
  6. Response shape complete
  7. Mostly ready when only important gaps
"""

from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/evidence-gap", json=body).json()


# ---------------------------------------------------------------------------
# 1. All evidence for INR → ready
# ---------------------------------------------------------------------------
def test_all_evidence_inr_ready():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": True,
        "has_communication_log": True,
        "has_invoice": True,
    })
    assert d["readiness"] == "ready"
    assert d["critical_gap_count"] == 0


# ---------------------------------------------------------------------------
# 2. Missing two critical gaps → not_ready
# ---------------------------------------------------------------------------
def test_missing_critical_not_ready():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery_confirmation": False,
    })
    assert d["readiness"] == "not_ready"
    assert d["critical_gap_count"] >= 2


# ---------------------------------------------------------------------------
# 3. INR missing tracking → critical gap listed
# ---------------------------------------------------------------------------
def test_inr_missing_tracking_critical():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery_confirmation": True,
    })
    critical = [g for g in d["gaps"] if g["priority"] == "critical"]
    labels = [g["evidence"].lower() for g in critical]
    assert any("tracking" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# 4. SNAD missing photos → critical gap
# ---------------------------------------------------------------------------
def test_snad_missing_photos_critical():
    d = _post({
        "claim_type": "SIGNIFICANTLY_NOT_AS_DESCRIBED",
        "has_photos": False,
        "has_refund_policy": True,
    })
    critical = [g for g in d["gaps"] if g["priority"] == "critical"]
    labels = [g["evidence"].lower() for g in critical]
    assert any("photo" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# 5. UNAUTHORIZED missing IP log → critical gap
# ---------------------------------------------------------------------------
def test_unauth_missing_ip_log_critical():
    d = _post({
        "claim_type": "UNAUTHORIZED",
        "has_ip_log": False,
        "has_login_record": True,
    })
    critical = [g for g in d["gaps"] if g["priority"] == "critical"]
    labels = [g["evidence"].lower() for g in critical]
    assert any("ip" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# 6. Response shape
# ---------------------------------------------------------------------------
def test_response_shape():
    d = _post({"claim_type": "OTHER"})
    required = {
        "claim_type", "readiness", "readiness_score", "evidence_present",
        "gaps", "critical_gap_count", "important_gap_count",
        "urgent_actions", "wave",
    }
    assert required.issubset(d.keys())


# ---------------------------------------------------------------------------
# 7. Missing only important gap → mostly_ready
# ---------------------------------------------------------------------------
def test_mostly_ready_when_important_only():
    d = _post({
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": True,
        "has_communication_log": False,
        "has_invoice": True,
    })
    assert d["readiness"] in ("ready", "mostly_ready")
    assert d["critical_gap_count"] == 0
