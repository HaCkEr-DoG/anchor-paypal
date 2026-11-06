"""
Recovery agent tests — live/sandbox URL, min amount, buyer email, already recovered.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agents.recovery import RecoveryAgent, AbandonedOrder, RecoveryConfig


def _make_order(**kwargs) -> AbandonedOrder:
    defaults = dict(
        order_id="PP-ORDER-001",
        merchant_id="merch1",
        amount=50.0,
        currency="USD",
        buyer_email="buyer@example.com",
        buyer_name="Test Buyer",
    )
    defaults.update(kwargs)
    return AbandonedOrder(**defaults)


# ── Invoice URL: live vs sandbox ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invoice_url_is_sandbox_in_sandbox_mode():
    os.environ.pop("PAYPAL_ENV", None)  # defaults to sandbox
    os.environ["MERCHANT_EMAIL"] = "merchant@example.com"

    agent = RecoveryAgent()
    captured_urls = []

    async def fake_post(url, **kwargs):
        resp = MagicMock()
        if "/invoices" in url and "/send" not in url:
            resp.status_code = 201
            resp.json = MagicMock(return_value={"id": "INV-SANDBOX-001"})
            resp.headers = {}
        else:
            resp.status_code = 202
            resp.text = "ok"
        return resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = fake_post
        mock_cls.return_value = mock_client

        order = _make_order()
        url = await agent._create_recovery_invoice(order, "token")

    assert url is not None
    assert "sandbox.paypal.com" in url, f"Expected sandbox URL, got: {url}"
    assert "www.paypal.com" not in url.replace("sandbox.paypal.com", ""), \
        "URL should not contain live paypal.com"


@pytest.mark.asyncio
async def test_invoice_url_is_live_in_live_mode():
    os.environ["PAYPAL_ENV"] = "live"
    os.environ["MERCHANT_EMAIL"] = "merchant@example.com"

    agent = RecoveryAgent()

    async def fake_post(url, **kwargs):
        resp = MagicMock()
        if "/invoices" in url and "/send" not in url:
            resp.status_code = 201
            resp.json = MagicMock(return_value={"id": "INV-LIVE-001"})
            resp.headers = {}
        else:
            resp.status_code = 202
            resp.text = "ok"
        return resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = fake_post
        mock_cls.return_value = mock_client

        order = _make_order()
        url = await agent._create_recovery_invoice(order, "token")

    assert url is not None
    assert "sandbox" not in url, f"Expected live URL, got: {url}"
    assert "www.paypal.com" in url, f"Expected live paypal.com, got: {url}"
    os.environ.pop("PAYPAL_ENV", None)


# ── Skipping conditions ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_buyer_email_skips_store():
    """Order with no buyer email should not be indexed."""
    os.environ.pop("PAYPAL_ENV", None)
    agent = RecoveryAgent()

    indexed = []
    with patch.object(agent, "_index_abandoned_order", new_callable=AsyncMock) as mock_idx:
        event = {
            "resource": {
                "id": "PP-ORDER-001",
                "payer": {},  # no email
                "purchase_units": [{"amount": {"value": "100.00", "currency_code": "USD"}}],
            }
        }
        await agent.store_approved_order(event, "merch1")
        # Must NOT have been indexed since there's no email
        assert not mock_idx.called, "Should not index order without buyer email"


@pytest.mark.asyncio
async def test_amount_below_minimum_skips_store():
    """Order below min_amount should not be indexed."""
    agent = RecoveryAgent(config=RecoveryConfig(min_amount=25.0))

    with patch.object(agent, "_index_abandoned_order", new_callable=AsyncMock) as mock_idx:
        event = {
            "resource": {
                "id": "PP-ORDER-002",
                "payer": {"email_address": "buyer@example.com"},
                "purchase_units": [{"amount": {"value": "10.00", "currency_code": "USD"}}],
            }
        }
        await agent.store_approved_order(event, "merch1")
        assert not mock_idx.called, "Should not index order below min_amount"


@pytest.mark.asyncio
async def test_already_recovered_order_skipped():
    """Already-recovered orders must be skipped in scan_and_recover."""
    agent = RecoveryAgent()

    already_done = _make_order(recovered=True)

    with patch.object(agent, "_query_stale_orders", new_callable=AsyncMock,
                      return_value=[already_done]):
        with patch.object(agent, "_get_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(agent, "_create_recovery_invoice", new_callable=AsyncMock) as mock_inv:
                result = await agent.scan_and_recover("merch1")

    assert result["skipped"] == 1
    assert result["recovered"] == 0
    assert not mock_inv.called, "Should not create invoice for already-recovered order"


@pytest.mark.asyncio
async def test_max_attempts_exceeded_skipped():
    """Order that hit max_attempts must be skipped."""
    agent = RecoveryAgent(config=RecoveryConfig(max_attempts=2))
    exhausted = _make_order(recovery_attempts=2, recovered=False)

    with patch.object(agent, "_query_stale_orders", new_callable=AsyncMock,
                      return_value=[exhausted]):
        with patch.object(agent, "_get_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(agent, "_create_recovery_invoice", new_callable=AsyncMock) as mock_inv:
                result = await agent.scan_and_recover("merch1")

    assert result["skipped"] == 1
    assert not mock_inv.called


@pytest.mark.asyncio
async def test_no_merchant_email_skips_invoice():
    os.environ.pop("MERCHANT_EMAIL", None)
    agent = RecoveryAgent()
    order = _make_order()
    url = await agent._create_recovery_invoice(order, "token")
    assert url is None
