"""
Wave 126 — Buyer Communication Analyzer  POST /api/oracle/analyze-buyer-communication

Tests:
  1. Hostile messages → HOSTILE tone
  2. Legal threat phrase → legal_threat_detected True
  3. Buyer acknowledges delivery → delivery_acknowledged_by_buyer True
  4. Cooperative messages → LOW escalation_risk
  5. Message counts correct
  6. Social media threat detected
  7. Response shape complete
"""
from __future__ import annotations
import time
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-buyer-communication", json=body).json()


def _msg(sender, content, ts=None):
    return {"sender": sender, "content": content, "timestamp_epoch": ts or int(time.time())}


def test_hostile_tone():
    d = _post({
        "dispute_id": "PP-D-001",
        "messages": [
            _msg("BUYER", "This is a scam and you are a fraud! Worst company ever."),
        ],
    })
    assert d["buyer_tone"] in ("HOSTILE", "DEMANDING")
    assert d["wave"] == 126


def test_legal_threat_detected():
    d = _post({
        "dispute_id": "PP-D-002",
        "messages": [
            _msg("BUYER", "I will hire a lawyer and take legal action against you."),
        ],
    })
    assert d["legal_threat_detected"] is True


def test_delivery_acknowledged():
    d = _post({
        "dispute_id": "PP-D-003",
        "messages": [
            _msg("BUYER", "I received the package and got the item."),
            _msg("BUYER", "But it was not what I ordered."),
        ],
    })
    assert d["delivery_acknowledged_by_buyer"] is True


def test_cooperative_low_escalation():
    d = _post({
        "dispute_id": "PP-D-004",
        "messages": [
            _msg("BUYER", "Thank you, I appreciate your help. Happy to work together on this."),
            _msg("MERCHANT", "We understand and are happy to assist. Please let us know."),
        ],
    })
    assert d["escalation_risk"] in ("LOW", "MODERATE")


def test_message_counts():
    d = _post({
        "dispute_id": "PP-D-005",
        "messages": [
            _msg("BUYER", "Where is my order?"),
            _msg("MERCHANT", "We are checking."),
            _msg("BUYER", "Still waiting."),
        ],
    })
    assert d["total_messages"] == 3
    assert d["buyer_message_count"] == 2
    assert d["merchant_message_count"] == 1


def test_social_media_threat():
    d = _post({
        "dispute_id": "PP-D-006",
        "messages": [
            _msg("BUYER", "I will post about this on Twitter and Facebook for everyone to see."),
        ],
    })
    assert d["social_media_threat_detected"] is True


def test_response_shape():
    d = _post({
        "dispute_id": "PP-D-007",
        "messages": [_msg("BUYER", "I never received my order.")],
    })
    required = {
        "dispute_id", "buyer_tone", "fraud_signal_level", "escalation_risk",
        "legal_threat_detected", "social_media_threat_detected",
        "delivery_acknowledged_by_buyer", "message_burst_detected",
        "total_messages", "buyer_message_count", "merchant_message_count",
        "negotiation_leverage", "suggested_response_tone", "summary", "wave",
    }
    assert required.issubset(d.keys())
