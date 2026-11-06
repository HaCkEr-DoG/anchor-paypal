"""
PayPal client tests — OAuth, 429 backoff, 401 refresh, webhook_id_override.
"""
import os
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agents.paypal_client import PayPalClient


def _make_client(**kwargs) -> PayPalClient:
    return PayPalClient(
        client_id=kwargs.get("client_id", "test_id"),
        client_secret=kwargs.get("client_secret", "test_secret"),
        env=kwargs.get("env", "sandbox"),
    )


def _token_resp(token="fake_token", expires_in=32400):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": token, "expires_in": expires_in})
    return resp


# ── Token caching ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_cached_within_window():
    client = _make_client()
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _token_resp()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = fake_post
        mock_cls.return_value = mock_http

        t1 = await client.get_token()
        t2 = await client.get_token()  # should reuse cached

    assert call_count == 1, f"Expected 1 HTTP call, got {call_count}"
    assert t1 == t2


@pytest.mark.asyncio
async def test_config_key_change_invalidates_cache():
    client = PayPalClient(client_id="id1", client_secret="sec1", env="sandbox")
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _token_resp(token=f"token_{call_count}")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = fake_post
        mock_cls.return_value = mock_http

        await client.get_token()
        # Change config — cache must be invalidated
        client.client_id = "id2"
        await client.get_token()

    assert call_count == 2, f"Expected 2 HTTP calls after config change, got {call_count}"


# ── 401 refresh ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_401_triggers_token_refresh():
    client = _make_client()

    call_count = [0]
    token_call_count = [0]

    async def fake_post(url, **kwargs):
        token_call_count[0] += 1
        return _token_resp()

    async def fake_request(method, url, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        if call_count[0] == 1:
            resp.status_code = 401  # First call: unauthorized
            resp.raise_for_status = MagicMock(side_effect=Exception("401"))
        else:
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
        return resp

    # Pre-seed the token
    client._token = "old_token"
    client._token_expires_at = time.monotonic() + 3600
    client._token_config_key = ("test_id", "test_secret", "sandbox")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = fake_post
        mock_http.request = fake_request
        mock_cls.return_value = mock_http

        try:
            await client._request("GET", "https://api.sandbox.paypal.com/test")
        except Exception:
            pass

    # Token was cleared and re-fetched
    assert token_call_count[0] >= 1, "Token refresh not triggered after 401"


# ── 429 backoff ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_429_respects_retry_after():
    client = _make_client()
    client._token = "valid_token"
    client._token_expires_at = time.monotonic() + 3600
    client._token_config_key = ("test_id", "test_secret", "sandbox")

    call_count = [0]
    slept_for = [0.0]

    async def fake_request(method, url, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        if call_count[0] == 1:
            resp.status_code = 429
            resp.headers = {"Retry-After": "2"}
            resp.raise_for_status = MagicMock()
        else:
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={})
        return resp

    async def fake_sleep(seconds):
        slept_for[0] = seconds

    with patch("httpx.AsyncClient") as mock_cls:
        with patch("asyncio.sleep", side_effect=fake_sleep):
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.post = AsyncMock(return_value=_token_resp())
            mock_http.request = fake_request
            mock_cls.return_value = mock_http

            await client._request("GET", "https://api.sandbox.paypal.com/test")

    assert slept_for[0] == 2.0, f"Expected 2s backoff, got {slept_for[0]}"
    assert call_count[0] == 2, f"Expected 2 request attempts, got {call_count[0]}"


# ── webhook_id_override ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_id_override_takes_priority():
    client = _make_client()
    client._token = "tok"
    client._token_expires_at = time.monotonic() + 3600
    client._token_config_key = ("test_id", "test_secret", "sandbox")
    os.environ["PAYPAL_WEBHOOK_ID"] = "env_webhook_id"

    captured_body = {}

    async def fake_request(method, url, json=None, **kwargs):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"verification_status": "SUCCESS"})
        return resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=_token_resp())
        mock_http.request = fake_request
        mock_cls.return_value = mock_http

        await client.verify_webhook_signature(
            auth_algo="SHA256withRSA",
            cert_url="https://api.paypal.com/v1/notifications/certs/CERT-123",
            transmission_id="TID",
            transmission_sig="SIG",
            transmission_time="2026-01-01T00:00:00Z",
            webhook_event={"event_type": "PAYMENT.CAPTURE.COMPLETED"},
            webhook_id_override="override_webhook_id",
        )

    assert captured_body.get("webhook_id") == "override_webhook_id", \
        f"Expected override webhook_id, got: {captured_body.get('webhook_id')}"


@pytest.mark.asyncio
async def test_webhook_id_falls_back_to_env():
    client = _make_client()
    client._token = "tok"
    client._token_expires_at = time.monotonic() + 3600
    client._token_config_key = ("test_id", "test_secret", "sandbox")
    os.environ["PAYPAL_WEBHOOK_ID"] = "env_webhook_id_fallback"

    captured_body = {}

    async def fake_request(method, url, json=None, **kwargs):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"verification_status": "SUCCESS"})
        return resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.request = fake_request
        mock_cls.return_value = mock_http

        await client.verify_webhook_signature(
            auth_algo="SHA256withRSA",
            cert_url="https://api.paypal.com/v1/notifications/certs/CERT-123",
            transmission_id="TID",
            transmission_sig="SIG",
            transmission_time="2026-01-01T00:00:00Z",
            webhook_event={},
            # No override — should use env var
        )

    assert captured_body.get("webhook_id") == "env_webhook_id_fallback"
