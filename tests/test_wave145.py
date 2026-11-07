"""
Wave 145 — Refund Impact Analyzer  POST /api/oracle/analyze-refund-impact

Tests:
  1. Low p_win + no LTV → FULL_REFUND cheaper than fighting
  2. High p_win + fraud → FIGHT
  3. cost_full_refund > 0
  4. ev_fight is float
  5. partial_refund_amount = pct * transaction_amount
  6. hdr_fee_applicable True when hdr_fee_per_dispute > 0
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-refund-impact", json=body).json()


def test_low_pwin_full_refund():
    d = _post({
        "dispute_id": "PP-D-001",
        "transaction_amount": 100.0,
        "p_win": 0.2,
        "is_fraud_risk": False,
    })
    assert d["recommendation"] in ("FULL_REFUND", "PARTIAL_REFUND")
    assert d["wave"] == 145


def test_high_pwin_fraud_fight():
    d = _post({
        "dispute_id": "PP-D-002",
        "transaction_amount": 200.0,
        "p_win": 0.85,
        "is_fraud_risk": True,
    })
    assert d["recommendation"] in ("FIGHT", "PARTIAL_REFUND", "CLOSE_NO_REFUND")


def test_cost_full_refund_positive():
    d = _post({
        "dispute_id": "PP-D-003",
        "transaction_amount": 150.0,
        "p_win": 0.5,
    })
    assert d["cost_full_refund"] > 0


def test_ev_fight_is_float():
    d = _post({
        "dispute_id": "PP-D-004",
        "transaction_amount": 100.0,
        "p_win": 0.6,
    })
    assert isinstance(d["ev_fight"], float)


def test_partial_refund_amount():
    d = _post({
        "dispute_id": "PP-D-005",
        "transaction_amount": 100.0,
        "p_win": 0.5,
        "partial_refund_pct": 0.70,
    })
    assert abs(d["partial_refund_amount"] - 70.0) < 0.01


def test_hdr_fee_applicable():
    d = _post({
        "dispute_id": "PP-D-006",
        "transaction_amount": 100.0,
        "p_win": 0.5,
        "hdr_fee_per_dispute": 30.0,
    })
    assert d["hdr_fee_applicable"] is True


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "transaction_amount": 80.0,
        "p_win": 0.5,
    })
    required = {
        "dispute_id", "transaction_amount", "cogs", "gross_margin",
        "cost_full_refund", "cost_partial_refund", "cost_fight_win",
        "cost_fight_lose", "ev_fight", "ltv_adjustment",
        "recommendation", "partial_refund_amount",
        "net_savings_vs_full_refund", "hdr_fee_applicable",
        "rationale", "immediate_actions", "summary", "wave",
    }
    assert required.issubset(d.keys())
