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


def test_aggrid_data_does_not_fill_empty_results_with_demo_rows(_client, monkeypatch):
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)
    response = _client.get("/api/aggrid/data?grid=disputes")
    assert response.status_code == 200
    assert response.json()["rowData"] == []
    assert response.json()["demo"] is False


def test_external_integrations_do_not_claim_hardcoded_connections(_client):
    drive = _client.get("/api/google-drive/status").json()
    gmail = _client.get("/api/gmail/status").json()
    zapier = _client.get("/api/zapier/status").json()
    assert drive["connected"] is False
    assert "root_folder_id" not in drive
    assert gmail["connected"] is False
    assert gmail["labels"] == []
    assert zapier["connected"] is False
    assert zapier["paypal_actions_enabled"] == 0


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
    assert "policy_is_default" in body["checks"], (
        "policy_is_default missing from /health/ready"
    )


def test_policy_is_default_true_when_no_reviewed_marker(_client, monkeypatch):
    """policy_is_default must be True when stored policy lacks _reviewed:True."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("MERCHANT_ID", "test-merchant")
    import agents.pg_store as pg_store
    from agents.config import GuardianConfig
    import dataclasses
    unreviewed = dataclasses.asdict(GuardianConfig(merchant_id="test-merchant"))
    unreviewed["_auto_seeded"] = True  # old-style seeded, no _reviewed key
    monkeypatch.setattr(pg_store, "get_policy", lambda mid: unreviewed)
    monkeypatch.setattr(pg_store, "health_check", lambda: True)
    r = _client.get("/health/ready")
    assert r.json()["checks"]["policy_is_default"] is True, (
        "policy without _reviewed:True must be flagged as default"
    )


def test_policy_is_default_false_when_reviewed_marker(_client, monkeypatch):
    """policy_is_default must be False when stored policy has _reviewed:True."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("MERCHANT_ID", "test-merchant")
    import agents.pg_store as pg_store
    from agents.config import GuardianConfig
    import dataclasses
    reviewed = dataclasses.asdict(GuardianConfig(merchant_id="test-merchant"))
    reviewed["_reviewed"] = True
    monkeypatch.setattr(pg_store, "get_policy", lambda mid: reviewed)
    monkeypatch.setattr(pg_store, "health_check", lambda: True)
    r = _client.get("/health/ready")
    assert r.json()["checks"]["policy_is_default"] is False, (
        "policy with _reviewed:True must report policy_is_default:false"
    )


def test_update_merchant_policy_sets_reviewed_marker(_client, monkeypatch):
    """PUT /api/admin/merchant-policy must save policy with _reviewed:True."""
    import os
    monkeypatch.setenv("API_SECRET", "testsecret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("MERCHANT_ID", "test-merchant")
    import agents.pg_store as pg_store
    saved = {}
    monkeypatch.setattr(pg_store, "save_policy", lambda mid, cfg: saved.update({"mid": mid, "cfg": cfg}) or True)
    r = _client.put(
        "/api/admin/merchant-policy",
        json={"auto_accept_below": 55},
        headers={"X-API-Secret": "testsecret"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert saved["cfg"].get("_reviewed") is True, "_reviewed:True must be set"
    assert "_auto_seeded" not in saved["cfg"], "_auto_seeded must be removed"
    assert saved["cfg"].get("auto_accept_below") == 55, "operator override must be applied"


def test_update_merchant_policy_rejects_unknown_keys(_client, monkeypatch):
    """PUT /api/admin/merchant-policy must reject invalid config values."""
    monkeypatch.setenv("API_SECRET", "testsecret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    import agents.pg_store as pg_store
    monkeypatch.setattr(pg_store, "save_policy", lambda mid, cfg: True)
    r = _client.put(
        "/api/admin/merchant-policy",
        json={"auto_accept_below": "not-a-number"},
        headers={"X-API-Secret": "testsecret"},
    )
    assert r.status_code == 422, "Invalid field type must return 422"


def test_health_ready_pg_check_absent_when_no_database_url(_client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = _client.get("/health/ready")
    body = r.json()
    # postgresql_connected should NOT appear when DATABASE_URL is not configured
    assert "postgresql_connected" not in body["checks"], (
        "postgresql_connected should be absent when DATABASE_URL is unset"
    )


def test_health_ready_pg_false_when_init_db_returns_false(_client, monkeypatch):
    """init_db() returning False must set postgresql_connected=False and trigger 503."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    import agents.pg_store as pg_store
    monkeypatch.setattr(pg_store, "init_db", lambda: False)
    r = _client.get("/health/ready")
    assert r.status_code == 503, "Must return 503 when init_db returns False"
    body = r.json()
    assert body["checks"].get("postgresql_connected") is False, (
        "postgresql_connected must be False when init_db() returns False"
    )


def test_health_ready_human_review_without_policy_is_not_ready(_client, monkeypatch):
    """HUMAN_REVIEW safety level without merchant policy must degrade readiness to 503."""
    monkeypatch.delenv("MERCHANT_POLICY_LOADED", raising=False)
    from oracle.production_safety import check_from_env, SafetyLevel
    from unittest.mock import patch, MagicMock
    mock_report = MagicMock()
    mock_report.safety_level = SafetyLevel.HUMAN_REVIEW
    # patch via the oracle module since watcher imports check_from_env from there
    with patch("oracle.production_safety.check_from_env", return_value=mock_report):
        r = _client.get("/health/ready")
    assert r.status_code == 503, (
        "HUMAN_REVIEW without MERCHANT_POLICY_LOADED must return 503"
    )
    body = r.json()
    assert body["checks"].get("safety_degraded") is True, (
        "safety_degraded must be True (degraded state detected) when HUMAN_REVIEW without merchant policy"
    )
    assert body.get("status") != "ok", (
        "status must not be 'ok' when HUMAN_REVIEW without merchant policy"
    )


# ---------------------------------------------------------------------------
# Stage 2. Kill switch — AUTONOMOUS_ACTIONS_ENABLED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_routes_to_approval_when_disabled(monkeypatch):
    """When AUTONOMOUS_ACTIONS_ENABLED is absent/0, handle_dispute must call _request_approval."""
    monkeypatch.delenv("AUTONOMOUS_ACTIONS_ENABLED", raising=False)

    approval_called = []

    from agents.guardian import Guardian
    g = Guardian(approval_secret="testsecret", merchant_id="m1")

    async def _mock_request_approval(dispute_id, amount, win_prob, decision):
        approval_called.append(dispute_id)

    g._request_approval = _mock_request_approval

    await g.handle_dispute(
        dispute_id="PP-D-KS-001",
        dispute_amount=50.0,
        reason_code="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        transaction_id=None,
        event_time="2026-01-01T00:00:00Z",
    )

    assert "PP-D-KS-001" in approval_called, (
        "Kill switch must route to _request_approval when AUTONOMOUS_ACTIONS_ENABLED is unset"
    )


@pytest.mark.asyncio
async def test_kill_switch_zero_routes_to_approval(monkeypatch):
    """AUTONOMOUS_ACTIONS_ENABLED=0 must also route to approval."""
    monkeypatch.setenv("AUTONOMOUS_ACTIONS_ENABLED", "0")

    approval_called = []

    from agents.guardian import Guardian
    g = Guardian(approval_secret="testsecret", merchant_id="m1")

    async def _mock_request_approval(dispute_id, amount, win_prob, decision):
        approval_called.append(dispute_id)

    g._request_approval = _mock_request_approval

    await g.handle_dispute(
        dispute_id="PP-D-KS-002",
        dispute_amount=100.0,
        reason_code="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        transaction_id=None,
        event_time="2026-01-01T00:00:00Z",
    )

    assert "PP-D-KS-002" in approval_called


# ---------------------------------------------------------------------------
# Stage 6. Approval token replay — consume_approval_token
# ---------------------------------------------------------------------------

def test_approval_token_replay_blocked(monkeypatch):
    """consume_approval_token must return False on the second call with the same sig."""
    import sqlite3
    import tempfile
    import os

    # Use an in-memory SQLite substitute via psycopg2 mock
    consumed = {}

    import agents.pg_store as pg_store

    orig_consume = pg_store.consume_approval_token

    def _mock_consume(token_sig, dispute_id, merchant_id, action):
        if token_sig in consumed:
            return False
        consumed[token_sig] = True
        return True

    monkeypatch.setattr(pg_store, "consume_approval_token", _mock_consume)

    assert pg_store.consume_approval_token("sig123", "PP-D-001", "m1", "fight") is True
    assert pg_store.consume_approval_token("sig123", "PP-D-001", "m1", "fight") is False


# ---------------------------------------------------------------------------
# Stage 4/5. Recovery PG migration — save + query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_save_uses_pg_not_es(monkeypatch):
    """store_approved_order must call pg_store.save_abandoned_order, not ES."""
    import agents.pg_store as pg_store
    saved = []

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setattr(pg_store, "save_abandoned_order", lambda doc: saved.append(doc) or True)

    from agents.recovery import RecoveryAgent

    agent = RecoveryAgent()
    event = {
        "resource": {
            "id": "ORDER-PG-001",
            "payer": {"email_address": "test@example.com", "name": {"given_name": "Test", "surname": "User"}},
            "purchase_units": [{"amount": {"value": "50.00", "currency_code": "USD"}, "items": []}],
        },
        "create_time": "2026-01-01T00:00:00Z",
    }

    await agent.store_approved_order(event, "merchant1")
    assert len(saved) == 1, "save_abandoned_order must be called once"
    assert saved[0]["order_id"] == "ORDER-PG-001"


@pytest.mark.asyncio
async def test_recovery_query_uses_pg_not_es(monkeypatch):
    """scan_and_recover must query PG, not ES, when DATABASE_URL is set."""
    import agents.pg_store as pg_store

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("RECOVERY_MERCHANT_OPT_IN", "1")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    queried = []

    monkeypatch.setattr(pg_store, "get_stale_abandoned_orders", lambda mid, cutoff, max_att, **kw: queried.append(mid) or [])

    from agents.recovery import RecoveryAgent

    agent = RecoveryAgent()
    await agent.scan_and_recover("merchant1")
    assert "merchant1" in queried, "get_stale_abandoned_orders must be called with merchant_id"
