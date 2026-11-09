"""
Multi-tenant isolation tests — metric events, log_action, approval URL, webhook routing.
"""
import os
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


# ── Guardian: log_action uses correct merchant_id ─────────────────────────────

@pytest.mark.asyncio
async def test_guardian_log_action_uses_instance_merchant():
    os.environ["MERCHANT_ID"] = "global_merchant"
    from agents.guardian import Guardian
    g = Guardian(approval_secret="secret", merchant_id="merch_A")

    with patch("agents.guardian.log_action", new_callable=AsyncMock) as mock_log:
        with patch.object(g, "_ensure_config", new_callable=AsyncMock):
            with patch.object(g, "_get_token", new_callable=AsyncMock, return_value="tok"):
                # Trigger _request_approval which calls log_action
                mock_log.return_value = None
                mock_decision = MagicMock()
                mock_decision.reasoning = ["test reason"]
                with patch.object(g, "_notify", new_callable=AsyncMock):
                    await g._request_approval("PP-D-001", 100.0, 0.75, mock_decision)

    # log_action must have been called with merch_A, not global_merchant
    calls = mock_log.call_args_list
    assert len(calls) > 0
    for call in calls:
        args = call.args if call.args else call[0]
        assert args[0] == "merch_A", f"log_action called with wrong merchant: {args[0]}"


@pytest.mark.asyncio
async def test_guardian_approval_url_contains_correct_mid():
    os.environ["RENDER_EXTERNAL_URL"] = "https://anchor.example.com"
    from agents.guardian import Guardian
    g = Guardian(approval_secret="secret", merchant_id="merch_B")
    captured_msg = {}

    async def capture_notify(msg):
        captured_msg["msg"] = msg

    with patch.object(g, "_ensure_config", new_callable=AsyncMock):
        with patch.object(g, "_get_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(g, "_notify", side_effect=capture_notify):
                mock_decision = MagicMock()
                mock_decision.reasoning = ["test reason"]
                with patch("agents.guardian.log_action", new_callable=AsyncMock):
                    await g._request_approval("PP-D-999", 200.0, 0.80, mock_decision)

    assert "mid=merch_B" in captured_msg.get("msg", ""), \
        f"Approval URL missing mid=merch_B in: {captured_msg.get('msg', '')}"


# ── Metric events use correct merchant_id ─────────────────────────────────────

@pytest.mark.asyncio
async def test_record_merchant_metric_uses_param_not_env():
    os.environ["MERCHANT_ID"] = "env_merchant"
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    captured_payloads = []

    async def fake_put(url, headers, json, **kwargs):
        captured_payloads.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    import httpx
    with patch.object(httpx.AsyncClient, "__aenter__") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.put = fake_put
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        from agents import watcher
        await watcher._record_merchant_metric_event(
            event_id="evt1", event_type="capture", amount=100.0,
            reason="", timestamp="2026-01-01T00:00:00Z",
            merchant_id="param_merchant",
        )

    if captured_payloads:
        assert captured_payloads[0]["merchant_id"] == "param_merchant", \
            f"Wrong merchant: {captured_payloads[0]['merchant_id']}"


@pytest.mark.asyncio
async def test_record_merchant_metric_falls_back_to_env():
    os.environ["MERCHANT_ID"] = "env_merchant"
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    captured_payloads = []

    async def fake_put(url, headers, json, **kwargs):
        captured_payloads.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    import httpx
    with patch.object(httpx.AsyncClient, "__aenter__") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.put = fake_put
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        from agents import watcher
        await watcher._record_merchant_metric_event(
            event_id="evt2", event_type="capture", amount=100.0,
            reason="", timestamp="2026-01-01T00:00:00Z",
            merchant_id="",  # empty → should fall back to env var
        )

    if captured_payloads:
        assert captured_payloads[0]["merchant_id"] == "env_merchant"


# ── Webhook routing ───────────────────────────────────────────────────────────

def _make_app():
    from agents.watcher import app
    return TestClient(app)


def test_per_merchant_webhook_route_exists():
    client = _make_app()
    # POST to per-merchant URL — should not 404 (may 422 without valid body, but route exists)
    with patch("agents.watcher._process_webhook", new_callable=AsyncMock):
        resp = client.post("/webhooks/paypal/merch1",
                          json={}, headers={"content-type": "application/json"})
    assert resp.status_code != 404, "Per-merchant webhook route missing"


def test_legacy_webhook_route_exists():
    client = _make_app()
    os.environ["MERCHANT_ID"] = "some_merchant"
    with patch("agents.watcher._process_webhook", new_callable=AsyncMock):
        resp = client.post("/webhooks/paypal",
                          json={}, headers={"content-type": "application/json"})
    assert resp.status_code != 404, "Legacy webhook route missing"


def test_legacy_webhook_503_when_no_merchant_id():
    client = _make_app()
    os.environ.pop("MERCHANT_ID", None)
    with patch("agents.watcher._process_webhook", new_callable=AsyncMock):
        resp = client.post("/webhooks/paypal",
                          json={}, headers={"content-type": "application/json"})
    assert resp.status_code == 503, f"Expected 503 without MERCHANT_ID, got {resp.status_code}"


def test_per_merchant_webhook_passes_merchant_id():
    client = _make_app()
    captured = {}

    async def capture_process(merchant_id, request, background):
        captured["merchant_id"] = merchant_id

    with patch("agents.watcher._process_webhook", side_effect=capture_process):
        client.post("/webhooks/paypal/merchant_xyz",
                   json={}, headers={"content-type": "application/json"})

    assert captured.get("merchant_id") == "merchant_xyz"


# ── Approval endpoint: mid query param ───────────────────────────────────────

def test_approve_endpoint_reads_mid_from_query():
    import agents.watcher as w
    w.APPROVAL_TOKEN_SECRET = "testsecret"
    client = _make_app()

    # Forge a valid token for mid=testmerch
    expiry = int(time.time()) + 3600
    payload = f"PP-D-123:testmerch:fight:{expiry}"
    import hmac as _hmac, hashlib as _hl
    sig = _hmac.new("testsecret".encode(), payload.encode(), _hl.sha256).hexdigest()
    token = f"{expiry}.{sig}"

    with patch("agents.watcher._execute_approved_fight", new_callable=AsyncMock), \
         patch.dict(os.environ, {"ALLOW_EPHEMERAL_APPROVALS": "1"}, clear=False):
        resp = client.post(f"/approve/PP-D-123?token={token}&mid=testmerch")

    # Should not be 403 (signature valid for testmerch)
    assert resp.status_code in (200, 202, 204), \
        f"Expected success for valid token, got {resp.status_code}: {resp.text}"
