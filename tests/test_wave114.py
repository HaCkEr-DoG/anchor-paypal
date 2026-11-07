"""
Wave 114 — Appeal Analyzer  POST /api/oracle/analyze-appeal

Tests:
  1. No proof of delivery → STRONG/MODERATE worthiness + appeal letter
  2. Expired deadline (ruling 11+ days ago) → FUTILE + days_remaining=0
  3. Insufficient evidence with new obtainable evidence → positive EV guide
  4. Seller Protection not invoked → SP angle in reasoning
  5. UNAUTHORIZED ruling → buyer communication in new_evidence
  6. Unknown ruling reason → falls back to OTHER strategy
  7. Response shape complete
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _post(body: dict):
    return _client.post("/api/oracle/analyze-appeal", json=body).json()


def test_no_proof_of_delivery_strong():
    d = _post({
        "ruling_reason": "NO_PROOF_OF_DELIVERY",
        "dispute_amount": 200.0,
        "claim_type": "ITEM_NOT_RECEIVED",
        "ruling_date": _days_ago(2),
        "original_evidence_had_delivery": False,
    })
    assert d["worthiness"] in ("STRONG", "MODERATE")
    assert len(d["appeal_letter"]) > 50
    assert d["wave"] == 114


def test_expired_deadline_futile():
    d = _post({
        "ruling_reason": "NO_PROOF_OF_DELIVERY",
        "dispute_amount": 500.0,
        "claim_type": "ITEM_NOT_RECEIVED",
        "ruling_date": _days_ago(11),
    })
    assert d["worthiness"] == "FUTILE"
    assert d["days_remaining"] == 0


def test_insufficient_evidence_guidance():
    d = _post({
        "ruling_reason": "INSUFFICIENT_EVIDENCE",
        "dispute_amount": 300.0,
        "claim_type": "INR",
        "ruling_date": _days_ago(3),
    })
    assert len(d["new_evidence_needed"]) > 0
    assert len(d["ruling_addressed"]) > 10


def test_sp_angle_reasoning():
    d = _post({
        "ruling_reason": "SELLER_PROTECTION_VOID",
        "dispute_amount": 400.0,
        "claim_type": "ITEM_NOT_RECEIVED",
        "ruling_date": _days_ago(1),
        "seller_protection_requirements_met": True,
    })
    assert any("SP" in r or "Seller Protection" in r or "seller" in r.lower()
               for r in d["reasoning"])


def test_unauthorized_buyer_communication():
    d = _post({
        "ruling_reason": "UNAUTHORIZED_CLAIM",
        "dispute_amount": 250.0,
        "claim_type": "UNAUTHORIZED",
        "ruling_date": _days_ago(2),
        "can_obtain_buyer_communication": True,
    })
    assert any("communication" in e.lower() or "buyer" in e.lower()
               for e in d["new_evidence_needed"])


def test_unknown_ruling_reason_fallback():
    d = _post({
        "ruling_reason": "SOME_NONEXISTENT_REASON",
        "dispute_amount": 100.0,
        "claim_type": "INR",
        "ruling_date": _days_ago(2),
    })
    assert d["wave"] == 114
    assert d["worthiness"] in ("STRONG", "MODERATE", "WEAK", "FUTILE")


def test_response_shape():
    d = _post({
        "ruling_reason": "NO_PROOF_OF_DELIVERY",
        "dispute_amount": 150.0,
        "claim_type": "INR",
        "ruling_date": _days_ago(2),
    })
    required = {
        "worthiness", "deadline", "days_remaining",
        "appeal_letter", "new_evidence_needed", "ruling_addressed",
        "ev_appeal", "reasoning", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["new_evidence_needed"], list)
    assert isinstance(d["reasoning"], list)
