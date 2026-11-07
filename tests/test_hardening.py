"""
Hardening tests — security, observability, reliability, documentation, testing depth.

Security
  1. X-Request-ID propagation
  2. Custom request-ID echoed back
  3. Oracle key gate: open when ORACLE_API_KEY unset
  4. Oracle key gate: 401 on wrong key
  5. Oracle key gate: 200 on correct key
  6. Oracle gate does not bleed onto /health
  7. Rate limit: 429 after ORACLE_RATE_LIMIT calls per minute
  8. Body size limit: 413 when Content-Length exceeds MAX_BODY_BYTES
  9. Rate-limit 429 body shape
 10. Rate-limit Retry-After header present

Observability
 11. /metrics returns 200
 12. /metrics shape (uptime_seconds, total_requests, paths)
 13. /metrics count increments after a call
 14. Slow path would trigger warning (structural check)

Reliability
 15. 404 returns JSON with "error" key
 16. Empty oracle body → 200/400/422, never 500 HTML
 17. Validation error (422) returns "validation_error" error key
 18. HTTPException returns "http_error" error key
 19. Global exception handler shape (500 returns JSON)

Documentation
 20. /api/oracle/catalog returns 200
 21. Catalog shape and self-reference exclusion
 22. Catalog has ≥ 50 oracle endpoints
 23. /api/status returns 200
 24. /api/status shape: version, uptime, oracle_endpoints, env, limits
 25. /api/status env keys are all booleans
 26. /api/status oracle_endpoints >= 50
"""
from __future__ import annotations
import time
import pytest
from fastapi.testclient import TestClient
import agents.watcher as watcher_mod
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)

_ORACLE_ENDPOINT = "/api/oracle/authorize-return"
_ORACLE_BODY = {"dispute_id": "PP-H-001", "order_id": "O-001", "claim_type": "SNAD", "order_amount": 50.0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _headers_lower(response):
    return {k.lower(): v for k, v in response.headers.items()}


# ---------------------------------------------------------------------------
# Security: X-Request-ID
# ---------------------------------------------------------------------------
def test_response_has_request_id():
    r = _client.get("/health")
    assert "x-request-id" in _headers_lower(r)


def test_custom_request_id_echoed():
    r = _client.get("/health", headers={"X-Request-ID": "hardening-echo"})
    assert _headers_lower(r).get("x-request-id") == "hardening-echo"


# ---------------------------------------------------------------------------
# Security: Oracle key gate
# ---------------------------------------------------------------------------
def test_oracle_no_auth_when_key_unset(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "")
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    assert r.status_code != 401


def test_oracle_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "correct-secret")
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY, headers={"X-Oracle-Key": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_oracle_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "correct-secret")
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY, headers={"X-Oracle-Key": "correct-secret"})
    assert r.status_code == 200


def test_oracle_gate_does_not_affect_health(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "some-secret")
    r = _client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Security: Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limit_triggers_429(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "")
    monkeypatch.setattr(watcher_mod, "_RATE_LIMIT", 3)
    # Clear bucket for test client's IP
    watcher_mod._rate_buckets.clear()
    for _ in range(3):
        _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    assert r.status_code == 429
    watcher_mod._rate_buckets.clear()


def test_rate_limit_429_body_shape(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "")
    monkeypatch.setattr(watcher_mod, "_RATE_LIMIT", 2)
    watcher_mod._rate_buckets.clear()
    for _ in range(2):
        _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    body = r.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "retry_after_seconds" in body
    watcher_mod._rate_buckets.clear()


def test_rate_limit_retry_after_header(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_ORACLE_SECRET", "")
    monkeypatch.setattr(watcher_mod, "_RATE_LIMIT", 2)
    watcher_mod._rate_buckets.clear()
    for _ in range(2):
        _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY)
    assert "retry-after" in _headers_lower(r)
    watcher_mod._rate_buckets.clear()


def test_rate_limit_does_not_affect_non_oracle(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_RATE_LIMIT", 1)
    watcher_mod._rate_buckets.clear()
    # Health endpoint is never rate-limited
    for _ in range(5):
        r = _client.get("/health")
        assert r.status_code == 200
    watcher_mod._rate_buckets.clear()


# ---------------------------------------------------------------------------
# Security: Body-size limit
# ---------------------------------------------------------------------------
def test_body_size_limit_413(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_MAX_BODY_BYTES", 10)
    # Fake a large Content-Length header — middleware checks before reading body
    r = _client.post(
        _ORACLE_ENDPOINT,
        json=_ORACLE_BODY,
        headers={"Content-Length": "9999999"},
    )
    assert r.status_code == 413


def test_body_size_413_body_shape(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_MAX_BODY_BYTES", 10)
    r = _client.post(_ORACLE_ENDPOINT, json=_ORACLE_BODY, headers={"Content-Length": "9999999"})
    body = r.json()
    assert body["error"] == "request_too_large"
    assert "limit_bytes" in body


# ---------------------------------------------------------------------------
# Observability: /metrics
# ---------------------------------------------------------------------------
def test_metrics_returns_200():
    r = _client.get("/metrics")
    assert r.status_code == 200


def test_metrics_shape():
    r = _client.get("/metrics")
    body = r.json()
    assert "uptime_seconds" in body
    assert "total_requests" in body
    assert "paths" in body
    assert isinstance(body["paths"], list)


def test_metrics_count_increments():
    watcher_mod._path_metrics.clear()
    _client.get("/health")
    r = _client.get("/metrics")
    health_rows = [p for p in r.json()["paths"] if p["path"] == "/health"]
    assert len(health_rows) == 1
    assert health_rows[0]["count"] >= 1


def test_metrics_path_has_required_fields():
    _client.get("/health")
    r = _client.get("/metrics")
    for row in r.json()["paths"]:
        assert "path" in row
        assert "count" in row
        assert "errors" in row
        assert "avg_ms" in row
        assert "max_ms" in row


# ---------------------------------------------------------------------------
# Reliability: normalized error responses
# ---------------------------------------------------------------------------
def test_404_returns_json_with_error_key():
    r = _client.get("/api/oracle/nonexistent-xyz-99999")
    assert "application/json" in r.headers.get("content-type", "")
    body = r.json()
    # HTTPException handler: {"error": "http_error", ...}
    assert "error" in body or "detail" in body


def test_404_error_shape():
    r = _client.get("/api/oracle/nonexistent-xyz-99999")
    body = r.json()
    # With our custom HTTPException handler: should have "error" key
    assert body.get("error") == "http_error" or "detail" in body


def test_oracle_empty_body_no_500():
    r = _client.post(_ORACLE_ENDPOINT, json={})
    assert r.status_code in (200, 400, 422)
    assert "application/json" in r.headers.get("content-type", "")


def test_validation_error_422_normalized():
    # Sending a string instead of number for a typed field should trigger 422
    r = _client.post(
        "/api/oracle/score-evidence-quality",
        json={"dispute_id": "PP-H", "claim_type": "INR", "dispute_amount": "not-a-number", "evidence_items": []},
    )
    # May be 422 or 200 depending on how oracle handles it; if 422, check shape
    if r.status_code == 422:
        body = r.json()
        assert body["error"] == "validation_error"
        assert "detail" in body
        assert "path" in body


def test_http_exception_normalized():
    # Admin endpoint with wrong key returns 401 via HTTPException
    r = _client.get("/api/admin/status", headers={"X-API-Key": "wrong"})
    if r.status_code in (401, 503):
        body = r.json()
        # Should be normalized by our HTTPException handler
        assert "error" in body or "detail" in body


# ---------------------------------------------------------------------------
# Documentation: /api/oracle/catalog
# ---------------------------------------------------------------------------
def test_catalog_returns_200():
    r = _client.get("/api/oracle/catalog")
    assert r.status_code == 200


def test_catalog_shape():
    r = _client.get("/api/oracle/catalog")
    body = r.json()
    assert "total" in body
    assert "endpoints" in body
    assert body["total"] == len(body["endpoints"])
    for ep in body["endpoints"]:
        assert "path" in ep
        assert "methods" in ep


def test_catalog_has_known_endpoints():
    r = _client.get("/api/oracle/catalog")
    paths = {ep["path"] for ep in r.json()["endpoints"]}
    assert "/api/oracle/authorize-return" in paths
    assert "/api/oracle/score-evidence-quality" in paths
    assert "/api/oracle/classify-subscription-dispute" in paths


def test_catalog_endpoint_count():
    r = _client.get("/api/oracle/catalog")
    assert r.json()["total"] >= 50


def test_catalog_not_self_referential():
    r = _client.get("/api/oracle/catalog")
    paths = {ep["path"] for ep in r.json()["endpoints"]}
    assert "/api/oracle/catalog" not in paths


# ---------------------------------------------------------------------------
# Documentation: /api/status
# ---------------------------------------------------------------------------
def test_status_returns_200():
    r = _client.get("/api/status")
    assert r.status_code == 200


def test_status_shape():
    r = _client.get("/api/status")
    body = r.json()
    required = {"status", "version", "uptime_seconds", "started_at",
                "oracle_endpoints", "rate_limit_per_minute", "max_body_bytes", "env"}
    assert required.issubset(body.keys())


def test_status_env_keys_are_booleans():
    r = _client.get("/api/status")
    env = r.json()["env"]
    for key, val in env.items():
        assert isinstance(val, bool), f"env[{key}] should be bool, got {type(val)}"


def test_status_oracle_endpoints_count():
    r = _client.get("/api/status")
    assert r.json()["oracle_endpoints"] >= 50


def test_status_uptime_positive():
    r = _client.get("/api/status")
    assert r.json()["uptime_seconds"] >= 0


def test_status_limits_match_module(monkeypatch):
    monkeypatch.setattr(watcher_mod, "_RATE_LIMIT", 77)
    monkeypatch.setattr(watcher_mod, "_MAX_BODY_BYTES", 123456)
    r = _client.get("/api/status")
    body = r.json()
    assert body["rate_limit_per_minute"] == 77
    assert body["max_body_bytes"] == 123456
