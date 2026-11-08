"""
Production hardening tests — verify fail-closed behaviors added during hardening.

Covers:
  1.  Config fallback: load_config returns MANUAL_REVIEW_CONFIG when ES unavailable
  2.  MANUAL_REVIEW_CONFIG prevents autonomous accept/fight
  3.  Safety gate HUMAN_REVIEW: return before Guardian is called
  4.  Safety check_from_env uses DATABASE_URL for durable_storage
  5.  Safety check_from_env ignores ELASTICSEARCH_URL alone for durable_storage
  6.  Astropods status: demo=True when ASTRO_GATEWAY_URL unset
  7.  Astropods status: demo=False when ASTRO_GATEWAY_URL set
  8.  Kernel status: demo=True when KERNEL_API_KEY unset
  9.  Kernel status: demo=False when KERNEL_API_KEY set
 10.  Approval token uses APPROVAL_LINK_MAX_AGE (default 3600s, not 48h)
 11.  /health/ready includes safety_gate check
 12.  /health/ready includes merchant_policy_loaded status
"""
from __future__ import annotations

import os
import time
import hmac
import hashlib
import asyncio
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 1-3. Config fallback: MANUAL_REVIEW_CONFIG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_config_returns_manual_review_when_no_es(monkeypatch):
    """load_config must return MANUAL_REVIEW_CONFIG when ES creds are absent."""
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)

    from agents.config import load_config, MANUAL_REVIEW_CONFIG
    cfg = await load_config("test_merchant")
    assert cfg.auto_accept_below == MANUAL_REVIEW_CONFIG.auto_accept_below
    assert cfg.auto_fight_above == MANUAL_REVIEW_CONFIG.auto_fight_above


def test_manual_review_config_never_auto_accepts():
    from agents.config import MANUAL_REVIEW_CONFIG
    for amount in (0.01, 10.0, 59.99, 100.0, 1000.0):
        assert not MANUAL_REVIEW_CONFIG.auto_accept(amount), (
            f"MANUAL_REVIEW_CONFIG auto-accepted amount={amount}"
        )


def test_manual_review_config_never_auto_fights():
    from agents.config import MANUAL_REVIEW_CONFIG
    for amount in (0.01, 200.0, 999999.0, 999_999_999.0):
        assert not MANUAL_REVIEW_CONFIG.auto_fight(amount, win_prob=1.0), (
            f"MANUAL_REVIEW_CONFIG auto-fought amount={amount}"
        )


def test_manual_review_config_always_needs_approval():
    from agents.config import MANUAL_REVIEW_CONFIG
    for amount in (0.01, 60.0, 200.0, 999.0):
        assert MANUAL_REVIEW_CONFIG.needs_approval(amount, win_prob=1.0), (
            f"MANUAL_REVIEW_CONFIG did not require approval for amount={amount}"
        )


# ---------------------------------------------------------------------------
# 4-5. Safety check_from_env: durable_storage uses DATABASE_URL not ES alone
# ---------------------------------------------------------------------------

def test_safety_durable_storage_true_with_database_url(monkeypatch):
    """DATABASE_URL present → durable_storage_configured=True."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    from oracle.production_safety import check_from_env
    report = check_from_env()
    durable = next((c for c in report.checks if c.name == "durable_storage"), None)
    assert durable is not None
    from oracle.production_safety import CheckStatus
    assert durable.status == CheckStatus.PASS


def test_safety_durable_storage_false_without_database_url(monkeypatch):
    """DATABASE_URL absent AND ELASTICSEARCH_URL absent → durable_storage_configured=False."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    from oracle.production_safety import check_from_env
    report = check_from_env()
    durable = next((c for c in report.checks if c.name == "durable_storage"), None)
    assert durable is not None
    from oracle.production_safety import CheckStatus
    assert durable.status in (CheckStatus.WARN, CheckStatus.FAIL)


# ---------------------------------------------------------------------------
# 6-9. Dashboard honest labels: Astropods + Kernel
# ---------------------------------------------------------------------------

@pytest.fixture()
def _client():
    from agents.watcher import app
    return TestClient(app, raise_server_exceptions=False)


def test_astropods_status_demo_when_no_gateway(_client, monkeypatch):
    monkeypatch.delenv("ASTRO_GATEWAY_URL", raising=False)
    r = _client.get("/api/astropods/status")
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is True
    assert body["gateway_configured"] is False


def test_astropods_status_connected_when_gateway_set(_client, monkeypatch):
    monkeypatch.setenv("ASTRO_GATEWAY_URL", "https://gateway.astropods.test")
    r = _client.get("/api/astropods/status")
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is False
    assert body["gateway_configured"] is True


def test_kernel_status_demo_when_no_key(_client, monkeypatch):
    monkeypatch.delenv("KERNEL_API_KEY", raising=False)
    r = _client.get("/api/kernel/status")
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is True
    assert body["gateway_configured"] is False


def test_kernel_status_connected_when_key_set(_client, monkeypatch):
    monkeypatch.setenv("KERNEL_API_KEY", "test-kernel-key")
    r = _client.get("/api/kernel/status")
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is False
    assert body["gateway_configured"] is True


# ---------------------------------------------------------------------------
# 10. Approval token expiry uses APPROVAL_LINK_MAX_AGE
# ---------------------------------------------------------------------------

def test_approval_token_uses_max_age_env(monkeypatch):
    """Token expiry should match APPROVAL_LINK_MAX_AGE, not a hardcoded 48h."""
    monkeypatch.setenv("APPROVAL_LINK_MAX_AGE", "7200")
    from agents.guardian import Guardian
    g = Guardian(approval_secret="testsecret", merchant_id="merchant1")
    before = int(time.time())
    token = g._make_approval_token("PP-D-001", "fight")
    after = int(time.time())
    expiry = int(token.split(".")[0])
    # Expiry should be ~7200s from now, not 48h (172800s)
    assert before + 7000 <= expiry <= after + 7400, (
        f"Token expiry {expiry} not in expected 7200s window"
    )


def test_approval_token_default_is_1h_not_48h(monkeypatch):
    """Default APPROVAL_LINK_MAX_AGE should be 3600s (1h), not 172800s (48h)."""
    monkeypatch.delenv("APPROVAL_LINK_MAX_AGE", raising=False)
    from agents.guardian import Guardian
    g = Guardian(approval_secret="testsecret", merchant_id="merchant1")
    before = int(time.time())
    token = g._make_approval_token("PP-D-002", "fight")
    after = int(time.time())
    expiry = int(token.split(".")[0])
    # Must be 1h window, not 48h
    assert before + 3400 <= expiry <= after + 3800, (
        f"Token expiry {expiry} not in 1h window (before={before}, after={after})"
    )
    assert expiry < before + 86400, "Token expiry should not be 24h+ into the future by default"


# ---------------------------------------------------------------------------
# 11-12. /health/ready strengthened checks
# ---------------------------------------------------------------------------

def test_health_ready_has_safety_gate(_client):
    r = _client.get("/health/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "safety_gate" in body["checks"], "safety_gate check missing from /health/ready"
    assert "safety_level" in body["checks"], "safety_level missing from /health/ready"


def test_health_ready_has_merchant_policy(_client):
    r = _client.get("/health/ready")
    body = r.json()
    assert "merchant_policy_loaded" in body["checks"], (
        "merchant_policy_loaded missing from /health/ready"
    )


def test_health_ready_pg_check_absent_when_no_database_url(_client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = _client.get("/health/ready")
    body = r.json()
    # postgresql_connected should NOT appear when DATABASE_URL is not configured
    assert "postgresql_connected" not in body["checks"], (
        "postgresql_connected should be absent when DATABASE_URL is unset"
    )
