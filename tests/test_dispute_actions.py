"""
Tests for dispute action endpoints:
  POST /api/paypal/disputes/{id}/accept
  POST /api/paypal/disputes/{id}/provide-evidence
  POST /api/paypal/disputes/{id}/send-message
  POST /api/paypal/disputes/{id}/appeal

All tests run in demo mode (PayPal client patched to unconfigured).
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch
import agents.watcher as _watcher_mod
from fastapi.testclient import TestClient
from agents.watcher import app

_API_KEY = "test-action-key"
# Patch the module-level secret so _require_api_key passes
_watcher_mod._API_SECRET = _API_KEY

_client = TestClient(app, raise_server_exceptions=False)


def _headers():
    return {"X-API-Key": _API_KEY}


def _unconfigured_client():
    """Return a mock PayPalClient that reports as unconfigured."""
    mock = MagicMock()
    mock.configured.return_value = False
    return mock


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------
_PATCH = "agents.paypal_client.get_client"


def test_accept_no_body_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post("/api/paypal/disputes/PP-D-12345/accept", headers=_headers())
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d.get("status") == "accepted"
    assert d.get("dispute_id") == "PP-D-12345"
    assert d.get("demo") is True


def test_accept_with_note_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post(
            "/api/paypal/disputes/PP-D-12345/accept",
            headers=_headers(),
            json={"note": "We accept and apologize."},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_accept_requires_api_key():
    r = _client.post("/api/paypal/disputes/PP-D-12345/accept")
    assert r.status_code in (401, 403, 503)


# ---------------------------------------------------------------------------
# Provide Evidence
# ---------------------------------------------------------------------------
def test_provide_evidence_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post(
            "/api/paypal/disputes/PP-D-99999/provide-evidence",
            headers=_headers(),
            json={
                "evidence_type": "PROOF_OF_FULFILLMENT",
                "notes": "Package delivered per tracking 1Z999.",
            },
        )
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "submitted"
    assert d.get("dispute_id") == "PP-D-99999"


def test_provide_evidence_requires_api_key():
    r = _client.post("/api/paypal/disputes/PP-D-99999/provide-evidence", json={"notes": "x"})
    assert r.status_code in (401, 403, 503)


# ---------------------------------------------------------------------------
# Send Message
# ---------------------------------------------------------------------------
def test_send_message_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post(
            "/api/paypal/disputes/PP-D-11111/send-message",
            headers=_headers(),
            json={"message": "Hi buyer, we shipped your order on Nov 1."},
        )
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "sent"
    assert d.get("dispute_id") == "PP-D-11111"


def test_send_message_requires_api_key():
    r = _client.post("/api/paypal/disputes/PP-D-11111/send-message", json={"message": "x"})
    assert r.status_code in (401, 403, 503)


# ---------------------------------------------------------------------------
# Appeal
# ---------------------------------------------------------------------------
def test_appeal_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post(
            "/api/paypal/disputes/PP-D-22222/appeal",
            headers=_headers(),
            json={
                "evidences": [
                    {"evidence_type": "PROOF_OF_SHIPMENT", "notes": "Courier confirmed delivery."}
                ]
            },
        )
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "appealed"
    assert d.get("dispute_id") == "PP-D-22222"


def test_appeal_empty_evidences_demo():
    with patch(_PATCH, return_value=_unconfigured_client()):
        r = _client.post(
            "/api/paypal/disputes/PP-D-22222/appeal",
            headers=_headers(),
            json={},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "appealed"


def test_appeal_requires_api_key():
    r = _client.post("/api/paypal/disputes/PP-D-22222/appeal", json={"evidences": []})
    assert r.status_code in (401, 403, 503)
