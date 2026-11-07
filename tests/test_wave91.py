"""
Wave 91 — Auto-webhook registration + PG service_config + dispute velocity.

Tests:
  1. get_config_value / set_config_value round-trip (mocked PG)
  2. record_dispute_velocity and get_dispute_velocity (mocked PG)
  3. _auto_register_webhook sets PAYPAL_WEBHOOK_ID from stored PG value
  4. _auto_register_webhook registers via PayPal when not stored
  5. /api/webhook/health with no webhook configured
  6. /api/webhook/health with webhook configured
  7. /api/velocity/disputes without DATABASE_URL
  8. /api/velocity/disputes with data
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 1. get_config_value / set_config_value round-trip
# ---------------------------------------------------------------------------

def test_pg_config_set_and_get():
    """set_config_value persists; get_config_value reads it back."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # fetchone returns the value we set
    mock_cursor.fetchone.return_value = ("rnd-webhook-99",)

    with patch("agents.pg_store._get_conn", return_value=mock_conn):
        import agents.pg_store as pg_store
        ok = pg_store.set_config_value("paypal_webhook_id", "rnd-webhook-99")
        assert ok is True

        val = pg_store.get_config_value("paypal_webhook_id")
        assert val == "rnd-webhook-99"


# ---------------------------------------------------------------------------
# 2. record_dispute_velocity / get_dispute_velocity
# ---------------------------------------------------------------------------

def test_dispute_velocity_record_and_read():
    """record_dispute_velocity upserts; get_dispute_velocity returns rows."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    dt = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)
    mock_cursor.fetchall.return_value = [(dt, 3, 450.0)]

    with patch("agents.pg_store._get_conn", return_value=mock_conn):
        import agents.pg_store as pg_store
        ok = pg_store.record_dispute_velocity("anchor-main", 150.0)
        assert ok is True

        rows = pg_store.get_dispute_velocity("anchor-main", hours=24)
        assert len(rows) == 1
        assert rows[0]["dispute_count"] == 3
        assert rows[0]["amount_total"] == 450.0


# ---------------------------------------------------------------------------
# 3. _auto_register_webhook: loads from PG when stored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_register_loads_from_pg():
    """When PG has a stored webhook_id, it's loaded without calling PayPal."""
    env = {
        "DATABASE_URL": "postgresql://x:y@host/db",
        "PAYPAL_CLIENT_ID": "clientid",
        "PAYPAL_CLIENT_SECRET": "secret",
        "RENDER_EXTERNAL_URL": "https://anchor-paypal.onrender.com",
        "MERCHANT_ID": "anchor-main",
    }
    with patch.dict(os.environ, env, clear=False):
        # Remove PAYPAL_WEBHOOK_ID so auto-register runs
        os.environ.pop("PAYPAL_WEBHOOK_ID", None)

        with patch("agents.pg_store.get_config_value", return_value="WH-FROM-PG-123"):
            from agents.watcher import _auto_register_webhook
            await _auto_register_webhook()

        assert os.environ.get("PAYPAL_WEBHOOK_ID") == "WH-FROM-PG-123"
    # Cleanup
    os.environ.pop("PAYPAL_WEBHOOK_ID", None)


# ---------------------------------------------------------------------------
# 4. _auto_register_webhook: calls PayPal when not in PG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_register_calls_paypal():
    """When no stored webhook_id, calls PayPal and stores the result."""
    env = {
        "DATABASE_URL": "postgresql://x:y@host/db",
        "PAYPAL_CLIENT_ID": "clientid",
        "PAYPAL_CLIENT_SECRET": "secret",
        "RENDER_EXTERNAL_URL": "https://anchor-paypal.onrender.com",
        "MERCHANT_ID": "anchor-main",
        "PAYPAL_ENV": "sandbox",
    }
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": "WH-NEW-456"}

    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("PAYPAL_WEBHOOK_ID", None)
        with patch("agents.pg_store.get_config_value", return_value=None), \
             patch("agents.pg_store.set_config_value", return_value=True), \
             patch("agents.paypal_client.get_client") as mock_client_cls, \
             patch("httpx.AsyncClient") as mock_http:

            mock_pp_client = AsyncMock()
            mock_pp_client.get_token.return_value = "access-token"
            mock_client_cls.return_value = mock_pp_client

            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post = AsyncMock(return_value=mock_response)
            mock_http.return_value = mock_http_inst

            from agents.watcher import _auto_register_webhook
            await _auto_register_webhook()

        assert os.environ.get("PAYPAL_WEBHOOK_ID") == "WH-NEW-456"
    os.environ.pop("PAYPAL_WEBHOOK_ID", None)


# ---------------------------------------------------------------------------
# 5. GET /api/webhook/health — no webhook configured
# ---------------------------------------------------------------------------

def test_webhook_health_not_configured():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PAYPAL_WEBHOOK_ID", None)
        os.environ.pop("DATABASE_URL", None)

        from agents.watcher import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/webhook/health")
        assert r.status_code == 200
        data = r.json()
        assert data["webhook_registered"] is False
        assert data["webhook_id"] is None


# ---------------------------------------------------------------------------
# 6. GET /api/webhook/health — webhook configured via env var
# ---------------------------------------------------------------------------

def test_webhook_health_configured():
    with patch.dict(os.environ, {"PAYPAL_WEBHOOK_ID": "WH-ENV-TEST"}):
        os.environ.pop("DATABASE_URL", None)

        from agents.watcher import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/webhook/health")
        assert r.status_code == 200
        data = r.json()
        assert data["webhook_registered"] is True
        assert data["webhook_id"] == "WH-ENV-TEST"
        assert data["source"] == "env_var"


# ---------------------------------------------------------------------------
# 7. GET /api/velocity/disputes — no DATABASE_URL
# ---------------------------------------------------------------------------

def test_dispute_velocity_no_db():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)

        from agents.watcher import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/velocity/disputes")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is False


# ---------------------------------------------------------------------------
# 8. GET /api/velocity/disputes — returns velocity data
# ---------------------------------------------------------------------------

def test_dispute_velocity_with_data():
    # avg = (1+1+1+20)/4 = 5.75 → threshold = max(2*5.75, 2) = 11.5
    # hour with 20 disputes IS a spike; hours with 1 are not
    dt = datetime(2026, 11, 7, 6, 0, 0, tzinfo=timezone.utc)
    mock_buckets = [
        {"bucket_utc": datetime(2026, 11, 7, h, 0, 0, tzinfo=timezone.utc).isoformat(),
         "dispute_count": cnt, "amount_total": float(cnt * 100)}
        for h, cnt in [(6, 1), (7, 1), (8, 1), (9, 20)]
    ]
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db", "MERCHANT_ID": "m1"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=mock_buckets):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/api/velocity/disputes?hours=24")
            assert r.status_code == 200
            data = r.json()
            assert data["available"] is True
            assert data["total_disputes"] == 23
            assert len(data["buckets"]) == 4
            # hour with 20 disputes is > 2× avg (avg=5.75) → spike
            assert len(data["spike_hours"]) == 1
