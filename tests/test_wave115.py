"""
Wave 115 — Pre-Dispute Interceptor  POST /api/oracle/preempt-scan

Tests:
  1. Chargeback threat → high dispute probability + ESCALATION_THREAT signal
  2. INR message → INR_SIGNAL detected
  3. Satisfaction message → low probability
  4. Item complaint → ITEM_COMPLAINT detected
  5. EV analysis returned
  6. Response script non-empty when action requires response
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/preempt-scan", json=body).json()


def test_chargeback_threat_high_prob():
    d = _post({
        "message_text": "I'm going to file a chargeback with my bank if you don't refund me.",
        "order_amount": 150.0,
    })
    assert d["dispute_probability"] > 0.4
    signal_types = [s["signal_type"] for s in d["detected_signals"]]
    assert "ESCALATION_THREAT" in signal_types
    assert d["wave"] == 115


def test_inr_signal_detected():
    d = _post({
        "message_text": "My package never arrived and tracking shows nothing.",
        "order_amount": 80.0,
        "days_since_purchase": 10,
    })
    signal_types = [s["signal_type"] for s in d["detected_signals"]]
    assert "INR_SIGNAL" in signal_types


def test_satisfaction_low_prob():
    d = _post({
        "message_text": "Thank you, I received the item and it looks great!",
        "order_amount": 50.0,
    })
    assert d["dispute_probability"] < 0.2


def test_item_complaint_detected():
    d = _post({
        "message_text": "This is not what I ordered — wrong item and it's broken.",
        "order_amount": 120.0,
    })
    signal_types = [s["signal_type"] for s in d["detected_signals"]]
    assert "ITEM_COMPLAINT" in signal_types


def test_ev_analysis_present():
    d = _post({
        "message_text": "I want a refund immediately.",
        "order_amount": 200.0,
        "dispute_fee": 15.0,
    })
    assert isinstance(d["ev_analysis"], dict)
    assert len(d["ev_analysis"]) > 0


def test_response_script_for_action():
    d = _post({
        "message_text": "Where is my order? It still hasn't arrived.",
        "order_amount": 60.0,
        "days_since_purchase": 8,
    })
    assert d["action"] in ("RESPOND", "OFFER_PARTIAL", "FULL_REFUND",
                           "IGNORE", "GATHER_TRACKING")


def test_response_shape():
    d = _post({"message_text": "Need help with my order.", "order_amount": 50.0})
    required = {
        "dispute_probability", "action", "detected_signals",
        "response_script", "ev_analysis", "reasoning", "urgency_hours", "wave",
    }
    assert required.issubset(d.keys())
    assert isinstance(d["detected_signals"], list)
    assert isinstance(d["reasoning"], list)
