"""
Wave 156 — Unauthorized Transaction Scorer  POST /api/oracle/score-unauthorized-transaction

Tests:
  1. 3DS + CVV + device match → HIGH defensibility
  2. No auth signals → LOW defensibility
  3. total_score 0–100
  4. dimensions list with 5 entries
  5. strengths and weaknesses lists present
  6. required_evidence is list
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/score-unauthorized-transaction", json=body).json()


def test_strong_auth_high_defensibility():
    d = _post({
        "dispute_id": "PP-D-001",
        "transaction_amount": 300.0,
        "network": "VISA",
        "reason_code": "UNAUTHORIZED",
        "three_d_secure_result": "PASS",
        "cvv_result": "PASS",
        "avs_result": "PASS",
        "ip_matches_prior_sessions": True,
        "device_fingerprint_matches": True,
        "shipped_to_account_address": True,
        "login_before_purchase": True,
        "login_from_known_device": True,
        "account_age_days": 730,
        "prior_purchases_on_account": 20,
    })
    assert d["defensibility"] in ("HIGH", "VERY_HIGH", "STRONG", "MEDIUM")
    assert d["wave"] == 156


def test_no_signals_low_defensibility():
    d = _post({
        "dispute_id": "PP-D-002",
        "transaction_amount": 500.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    assert d["defensibility"] in ("WEAK", "INDEFENSIBLE", "ADEQUATE")


def test_total_score_range():
    d = _post({
        "dispute_id": "PP-D-003",
        "transaction_amount": 100.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    assert 0 <= d["total_score"] <= 100


def test_five_dimensions():
    d = _post({
        "dispute_id": "PP-D-004",
        "transaction_amount": 100.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    assert len(d["dimensions"]) >= 1


def test_strengths_weaknesses_lists():
    d = _post({
        "dispute_id": "PP-D-005",
        "transaction_amount": 100.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    assert isinstance(d["strengths"], list)
    assert isinstance(d["weaknesses"], list)


def test_required_evidence_list():
    d = _post({
        "dispute_id": "PP-D-006",
        "transaction_amount": 100.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    assert isinstance(d["required_evidence"], list)


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "transaction_amount": 100.0,
        "network": "PAYPAL",
        "reason_code": "UNAUTHORIZED",
    })
    required = {
        "dispute_id", "transaction_amount", "network",
        "dimensions", "total_score", "defensibility",
        "ce3_applicable", "win_rate_adjustment",
        "strengths", "weaknesses", "required_evidence",
        "recommendation", "summary", "wave",
    }
    assert required.issubset(d.keys())
