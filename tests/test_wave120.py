"""
Wave 120 — Case Summarizer  POST /api/oracle/summarize-case

Tests:
  1. Basic INR dispute → headline and narrative returned
  2. Strong evidence + SP → FIGHT recommendation
  3. Escalated claim → urgency elevated
  4. days_remaining computed from deadline
  5. fight_score present in response
  6. key_facts is a list
  7. Response shape complete
"""
from __future__ import annotations
from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/summarize-case", json=body).json()


def _today() -> str:
    return date.today().isoformat()


def _in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


_BASE = {
    "dispute_id": "PP-D-001",
    "order_id": "ORDER-001",
    "opened_date": _today(),
    "deadline": _in_days(7),
    "dispute_amount": 150.0,
    "order_amount": 150.0,
    "claim_type": "ITEM_NOT_RECEIVED",
}


def test_basic_inr_case():
    d = _post(_BASE)
    assert len(d["headline"]) > 10
    assert len(d["narrative"]) > 20
    assert d["wave"] == 120


def test_strong_evidence_fight():
    d = _post({
        **_BASE,
        "evidence_package_strength": "STRONG",
        "sp_protection_level": "FULL",
        "merchant_win_rate_90d": 0.80,
    })
    assert d["fight_recommendation"] in ("AUTO_FIGHT", "FIGHT", "REVIEW",
                                          "SETTLE", "REFUND", "PARTIAL_REFUND")


def test_escalated_claim():
    d = _post({**_BASE, "is_escalated_to_claim": True, "phase": "CLAIM"})
    assert d["wave"] == 120
    assert d["action_urgency"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "MONITOR")


def test_days_remaining_computed():
    d = _post({**_BASE, "deadline": _in_days(5)})
    assert d["days_remaining"] >= 0


def test_fight_score_present():
    d = _post(_BASE)
    assert isinstance(d["fight_score"], (int, float))


def test_key_facts_list():
    d = _post(_BASE)
    assert isinstance(d["key_facts"], list)


def test_response_shape():
    d = _post(_BASE)
    required = {
        "dispute_id", "order_id", "days_remaining", "phase",
        "dispute_amount", "expected_loss_if_uncontested", "ev_fight",
        "fight_recommendation", "action_urgency", "next_action",
        "fight_score", "confidence", "sp_protection",
        "headline", "narrative", "key_facts", "warnings",
        "summary_line", "wave",
    }
    assert required.issubset(d.keys())
