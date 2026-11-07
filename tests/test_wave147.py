"""
Wave 147 — Dispute Response Writer  POST /api/oracle/write-dispute-response

Tests:
  1. INR dispute with tracking → is_submission_ready True
  2. UNAUTHORIZED dispute → tone in valid set
  3. full_response is non-empty string
  4. sections is list with entries
  5. evidence_count matches evidence_items length
  6. word_count > 0
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/write-dispute-response", json=body).json()


def _inr_body(**kwargs):
    base = {
        "dispute_id": "PP-D-001",
        "merchant_id": "M-001",
        "merchant_name": "Test Merchant",
        "reason_code": "INR",
        "order_id": "ORD-001",
        "order_date": "2026-10-01",
        "order_amount": 150.0,
        "ship_date": "2026-10-02",
        "carrier": "UPS",
        "tracking_number": "1Z999AA10123456784",
        "delivery_date": "2026-10-05",
        "delivery_status": "DELIVERED",
        "evidence_items": ["TRACKING_NUMBER", "PROOF_OF_DELIVERY"],
        "win_probability": 0.85,
    }
    base.update(kwargs)
    return base


def test_inr_tracking_submission_ready():
    d = _post(_inr_body())
    assert d["is_submission_ready"] is True
    assert d["wave"] == 147


def test_unauthorized_tone_valid():
    d = _post({
        "dispute_id": "PP-D-002",
        "reason_code": "UNAUTHORIZED",
        "order_id": "ORD-002",
        "order_date": "2026-10-01",
        "order_amount": 200.0,
        "evidence_items": ["INVOICE", "PROOF_OF_DELIVERY"],
        "win_probability": 0.7,
    })
    assert d["tone"] in ("ASSERTIVE", "COOPERATIVE", "CONCILIATORY")


def test_full_response_nonempty():
    d = _post(_inr_body())
    assert isinstance(d["full_response"], str)
    assert len(d["full_response"]) > 50


def test_sections_list():
    d = _post(_inr_body())
    assert isinstance(d["sections"], list)
    assert len(d["sections"]) > 0
    if d["sections"]:
        s = d["sections"][0]
        assert "section" in s
        assert "content" in s


def test_evidence_count():
    d = _post(_inr_body())
    assert d["evidence_count"] == len(d["evidence_items_referenced"])


def test_word_count_positive():
    d = _post(_inr_body())
    assert d["word_count"] > 0


def test_response_shape():
    d = _post(_inr_body())
    required = {
        "dispute_id", "merchant_id", "reason_code", "tone",
        "sections", "full_response", "evidence_count",
        "evidence_items_referenced", "word_count",
        "is_submission_ready", "submission_note", "summary", "wave",
    }
    assert required.issubset(d.keys())
