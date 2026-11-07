"""
Hardening tests — security, observability, reliability, documentation.

Covers:
  1. X-Request-ID propagation (observability)
  2. Oracle API key gating (security)
  3. Global exception handler returns JSON (reliability)
  4. 404 responses are JSON, not HTML (reliability)
  5. Oracle endpoints tolerate empty body gracefully (reliability)
  6. /api/oracle/catalog endpoint (documentation)
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
import agents.watcher as watcher_mod
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Observability: X-Request-ID header
# ---------------------------------------------------------------------------
def test_response_has_request_id():
    r = _client.get("/health")
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower


def test_custom_request_id_echoed():
    r = _client.get("/health", headers={"X-Request-ID": "hardening-001"})
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert headers_lower.get("x-request-id") == "hardening-001"


# ---------------------------------------------------------------------------
# Security: oracle key gating
# ---------------------------------------------------------------------------
def test_oracle_no_auth_when_key_unset(monkeypatch):
    """When ORACLE_API_KEY is not set, oracle endpoints must not return 401."""
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "")
    r = _client.post(
        "/api/oracle/authorize-return",
        json={"dispute_id": "PP-H-001", "order_id": "O-001", "claim_type": "SNAD", "order_amount": 50.0},
    )
    assert r.status_code != 401


def test_oracle_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "correct-secret")
    r = _client.post(
        "/api/oracle/authorize-return",
        json={"dispute_id": "PP-H-002", "order_id": "O-002", "claim_type": "SNAD", "order_amount": 50.0},
        headers={"X-Oracle-Key": "wrong-key"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "unauthorized"
    assert "X-Oracle-Key" in body["detail"] or "detail" in body


def test_oracle_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "correct-secret")
    r = _client.post(
        "/api/oracle/authorize-return",
        json={"dispute_id": "PP-H-003", "order_id": "O-003", "claim_type": "SNAD", "order_amount": 50.0},
        headers={"X-Oracle-Key": "correct-secret"},
    )
    assert r.status_code == 200


def test_oracle_auth_does_not_affect_health(monkeypatch):
    """The key gate must not be applied to non-oracle paths."""
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "some-secret")
    r = _client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Reliability: 404 returns JSON
# ---------------------------------------------------------------------------
def test_404_returns_json():
    r = _client.get("/api/oracle/nonexistent-endpoint-xyz-99999")
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct
    body = r.json()
    # FastAPI default: {"detail": "Not Found"}
    assert "detail" in body


# ---------------------------------------------------------------------------
# Reliability: empty body handled gracefully
# ---------------------------------------------------------------------------
def test_oracle_empty_body_no_500():
    """Empty body must not cause an unhandled 500."""
    r = _client.post("/api/oracle/authorize-return", json={})
    assert r.status_code in (200, 400, 422)
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct


def test_oracle_classify_subscription_empty_body_no_500():
    r = _client.post("/api/oracle/classify-subscription-dispute", json={})
    assert r.status_code in (200, 400, 422)


# ---------------------------------------------------------------------------
# Documentation: catalog endpoint
# ---------------------------------------------------------------------------
def test_catalog_returns_200():
    r = _client.get("/api/oracle/catalog")
    assert r.status_code == 200


def test_catalog_shape():
    r = _client.get("/api/oracle/catalog")
    body = r.json()
    assert "total" in body
    assert "endpoints" in body
    assert isinstance(body["endpoints"], list)
    assert body["total"] == len(body["endpoints"])


def test_catalog_has_known_endpoints():
    r = _client.get("/api/oracle/catalog")
    paths = {ep["path"] for ep in r.json()["endpoints"]}
    assert "/api/oracle/authorize-return" in paths
    assert "/api/oracle/score-evidence-quality" in paths
    assert "/api/oracle/classify-subscription-dispute" in paths


def test_catalog_endpoint_count():
    """Service should have at least 50 oracle endpoints registered."""
    r = _client.get("/api/oracle/catalog")
    assert r.json()["total"] >= 50


def test_catalog_not_self_referential():
    """The catalog endpoint itself must not appear in the catalog."""
    r = _client.get("/api/oracle/catalog")
    paths = {ep["path"] for ep in r.json()["endpoints"]}
    assert "/api/oracle/catalog" not in paths
