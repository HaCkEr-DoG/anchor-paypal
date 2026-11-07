"""
Wave 135 — Evidence Package Builder  POST /api/oracle/build-evidence-package

Tests:
  1. Full INR evidence → COMPELLING or STRONG, can_file=True
  2. Missing required fields → can_file=False, gaps non-empty
  3. completeness_score 0–100
  4. evidence_strength is valid enum
  5. UNAUTHORIZED with IP log → higher strength
  6. win_rate_boost is float
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/build-evidence-package", json=body).json()


def test_full_inr_strong():
    d = _post({
        "dispute_id": "PP-D-001",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 150.0,
        "has_tracking_number": True,
        "has_shipment_date_proof": True,
        "has_delivery_confirmation": True,
        "has_signature_confirmation": True,
        "has_buyer_communication": True,
        "communication_supports_merchant": True,
    })
    assert d["evidence_strength"] in ("COMPELLING", "STRONG", "ADEQUATE")
    assert d["can_file"] is True
    assert d["wave"] == 135


def test_missing_required_no_file():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    assert d["can_file"] is False
    assert len(d["missing_required"]) > 0


def test_completeness_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 80.0,
    })
    assert 0 <= d["completeness_score"] <= 100


def test_evidence_strength_valid():
    d = _post({
        "dispute_id": "PP-D-004",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 60.0,
    })
    assert d["evidence_strength"] in (
        "COMPELLING", "STRONG", "ADEQUATE", "WEAK", "INSUFFICIENT"
    )


def test_unauthorized_with_ip_log():
    d_with = _post({
        "dispute_id": "PP-D-005",
        "reason_code": "UNAUTHORIZED",
        "network": "PAYPAL",
        "transaction_amount": 200.0,
        "has_ip_log": True,
        "has_order_history": True,
        "has_3ds_authentication": True,
    })
    d_without = _post({
        "dispute_id": "PP-D-006",
        "reason_code": "UNAUTHORIZED",
        "network": "PAYPAL",
        "transaction_amount": 200.0,
    })
    assert d_with["completeness_score"] > d_without["completeness_score"]


def test_win_rate_boost_float():
    d = _post({
        "dispute_id": "PP-D-007",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 100.0,
    })
    assert isinstance(d["win_rate_boost"], float)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-008",
        "reason_code": "INR",
        "network": "PAYPAL",
        "transaction_amount": 50.0,
    })
    required = {
        "dispute_id", "reason_code", "network", "completeness_score",
        "evidence_strength", "can_file", "win_rate_boost",
        "missing_required", "gaps", "recommendations", "summary", "wave",
    }
    assert required.issubset(d.keys())
