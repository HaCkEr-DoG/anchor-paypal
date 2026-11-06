"""
Anchor Recovery — abandoned checkout recovery agent.

Monitors PayPal orders that reached APPROVED status (buyer authorized payment)
but were never captured by the merchant. These are highest-intent abandoned carts:
the buyer completed the full PayPal auth flow — the merchant's page likely errored
before calling /capture.

Flow:
  CHECKOUT.ORDER.APPROVED webhook  → store order in Elasticsearch
  POST /recovery-check (cron 15m)  → scan approved+uncaptured orders >DELAY_MINUTES
                                   → generate PayPal invoice for same amount
                                   → send buyer a one-click payment link via Zapier
                                   → log recovery_attempted to anchor-actions
  Morning brief                    → "3 abandoned orders recovered ($312)"

Why invoices over re-capture:
  Capturing an abandoned order hours later without re-engaging the buyer risks
  confusion and chargebacks. A PayPal invoice gives the buyer a clear payment
  prompt from PayPal itself, with full context, that they can complete or ignore.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

_PAYPAL_BASE = {
    "sandbox": "https://api.sandbox.paypal.com",
    "live":    "https://api.paypal.com",
}


@dataclass
class RecoveryConfig:
    delay_minutes: int = int(os.environ.get("RECOVERY_DELAY_MINUTES", "30"))
    min_amount: float = float(os.environ.get("RECOVERY_MIN_AMOUNT", "10"))
    max_attempts: int = int(os.environ.get("RECOVERY_MAX_ATTEMPTS", "2"))


@dataclass
class AbandonedOrder:
    order_id: str
    merchant_id: str
    amount: float
    currency: str
    buyer_email: str
    buyer_name: str
    items: list[dict] = field(default_factory=list)
    approved_at: str = ""
    recovery_attempts: int = 0
    recovered: bool = False


class RecoveryAgent:
    def __init__(self, config: Optional[RecoveryConfig] = None):
        self.config = config or RecoveryConfig()
        env = os.environ.get("PAYPAL_ENV", "sandbox")
        self.paypal_base = _PAYPAL_BASE.get(env, _PAYPAL_BASE["sandbox"])
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._token_lock = asyncio.Lock()

    # ── Webhook entry point ─────────────────────────────────────────────────

    async def store_approved_order(self, event: dict, merchant_id: str) -> None:
        """
        Called from watcher.py on CHECKOUT.ORDER.APPROVED.
        Indexes the order into Elasticsearch so the recovery cron can find it.
        """
        resource = event.get("resource", {})
        order_id = resource.get("id")
        if not order_id:
            return

        payer = resource.get("payer", {})
        buyer_email = payer.get("email_address", "")
        buyer_name_parts = payer.get("name", {})
        buyer_name = f"{buyer_name_parts.get('given_name', '')} {buyer_name_parts.get('surname', '')}".strip()

        # Sum across all purchase units
        purchase_units = resource.get("purchase_units", [])
        amount = sum(
            float(pu.get("amount", {}).get("value", 0))
            for pu in purchase_units
        )
        currency = (purchase_units[0].get("amount", {}).get("currency_code", "USD")
                    if purchase_units else "USD")

        items = []
        for pu in purchase_units:
            for item in pu.get("items", []):
                items.append({
                    "name": item.get("name", "Item"),
                    "quantity": item.get("quantity", "1"),
                    "unit_price": item.get("unit_amount", {}).get("value", "0"),
                    "currency": item.get("unit_amount", {}).get("currency_code", currency),
                })

        if not buyer_email or amount < self.config.min_amount:
            return

        order = AbandonedOrder(
            order_id=order_id,
            merchant_id=merchant_id,
            amount=amount,
            currency=currency,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            items=items,
            approved_at=event.get("create_time", datetime.now(timezone.utc).isoformat()),
        )
        await self._index_abandoned_order(order)
        print(f"[Recovery] Tracked approved order {order_id}: ${amount:.2f} for {buyer_email}")

    # ── Cron entry point ────────────────────────────────────────────────────

    async def scan_and_recover(self, merchant_id: str) -> dict:
        """
        Called every 15 minutes by the /recovery-check cron endpoint.
        Finds stale approved orders, generates invoices, and sends recovery emails.
        Returns a summary of actions taken.
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self.config.delay_minutes)
        ).isoformat()

        stale_orders = await self._query_stale_orders(merchant_id, cutoff)
        print(f"[Recovery] Found {len(stale_orders)} stale approved orders for recovery")

        recovered = 0
        skipped = 0
        for order in stale_orders:
            if order.recovery_attempts >= self.config.max_attempts:
                skipped += 1
                continue
            if order.recovered:
                skipped += 1
                continue

            # Verify order is still uncaptured before bothering buyer
            token = await self._get_token()
            status = await self._get_order_status(order.order_id, token)
            if status in ("COMPLETED", "VOIDED"):
                await self._mark_no_longer_needed(order.order_id)
                skipped += 1
                continue

            invoice_link = await self._create_recovery_invoice(order, token)
            if invoice_link:
                await self._send_recovery_email(order, invoice_link)
                await self._log_recovery_attempt(order, invoice_link, merchant_id)
                recovered += 1
                print(
                    f"[Recovery] Sent recovery for {order.order_id} "
                    f"${order.amount:.2f} → {order.buyer_email}"
                )

        return {"scanned": len(stale_orders), "recovered": recovered, "skipped": skipped}

    # ── PayPal API calls ────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token
            import base64
            creds = base64.b64encode(
                f"{os.environ['PAYPAL_CLIENT_ID']}:{os.environ['PAYPAL_CLIENT_SECRET']}".encode()
            ).decode()
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{self.paypal_base}/v1/oauth2/token",
                    headers={"Authorization": f"Basic {creds}"},
                    data={"grant_type": "client_credentials"},
                )
                r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._token_expires_at = time.monotonic() + data.get("expires_in", 32400)
            return self._token

    async def _get_order_status(self, order_id: str, token: str) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.paypal_base}/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        if r.status_code != 200:
            return "UNKNOWN"
        return r.json().get("status", "UNKNOWN")

    async def _create_recovery_invoice(
        self, order: AbandonedOrder, token: str
    ) -> Optional[str]:
        """
        Creates a PayPal invoice for the abandoned order amount.
        Returns the invoice payment link the buyer can click to complete purchase.
        """
        merchant_email = os.environ.get("MERCHANT_EMAIL", "")
        if not merchant_email:
            print("[Recovery] MERCHANT_EMAIL not set — skipping invoice creation")
            return None

        line_items = []
        if order.items:
            for item in order.items:
                line_items.append({
                    "name": item["name"],
                    "quantity": item.get("quantity", "1"),
                    "unit_amount": {
                        "currency_code": item.get("currency", order.currency),
                        "value": item.get("unit_price", "0"),
                    },
                })
        else:
            line_items.append({
                "name": "Order",
                "quantity": "1",
                "unit_amount": {
                    "currency_code": order.currency,
                    "value": f"{order.amount:.2f}",
                },
            })

        invoice_payload = {
            "detail": {
                "currency_code": order.currency,
                "note": (
                    "Your order is almost complete! You authorized this payment but we "
                    "didn't receive confirmation. Click below to complete your purchase."
                ),
                "memo": f"Recovery for order {order.order_id}",
            },
            "invoicer": {"email_address": merchant_email},
            "primary_recipients": [
                {
                    "billing_info": {
                        "email_address": order.buyer_email,
                        "name": {"full_name": order.buyer_name or order.buyer_email},
                    }
                }
            ],
            "items": line_items,
            "amount": {
                "breakdown": {
                    "item_total": {
                        "currency_code": order.currency,
                        "value": f"{order.amount:.2f}",
                    }
                }
            },
        }

        async with httpx.AsyncClient() as c:
            # Create draft invoice
            r = await c.post(
                f"{self.paypal_base}/v2/invoicing/invoices",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=invoice_payload,
                timeout=10.0,
            )
            if r.status_code not in (200, 201):
                print(f"[Recovery] Invoice creation failed: {r.status_code} {r.text}")
                return None

            invoice_id = r.json().get("id") or r.headers.get("location", "").split("/")[-1]

            # Send the invoice
            send_r = await c.post(
                f"{self.paypal_base}/v2/invoicing/invoices/{invoice_id}/send",
                headers={"Authorization": f"Bearer {token}"},
                json={"send_to_recipient": True},
                timeout=10.0,
            )
            if send_r.status_code not in (200, 202):
                print(f"[Recovery] Invoice send failed: {send_r.status_code}")

        _inv_host = (
            "www.paypal.com"
            if os.environ.get("PAYPAL_ENV", "sandbox") == "live"
            else "www.sandbox.paypal.com"
        )
        return f"https://{_inv_host}/invoice/payerView/details/{invoice_id}"

    # ── Notification ────────────────────────────────────────────────────────

    async def _send_recovery_email(self, order: AbandonedOrder, invoice_link: str) -> None:
        """
        Notifies the merchant via Slack so they know a recovery was triggered.
        The buyer gets the email directly from PayPal's invoice system.
        """
        slack_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not slack_url:
            return

        msg = (
            f"*Anchor Recovery* — abandoned order recovered\n"
            f"Order `{order.order_id}`: ${order.amount:.2f} for {order.buyer_email}\n"
            f"Invoice sent. Buyer can complete payment here: {invoice_link}"
        )
        try:
            async with httpx.AsyncClient() as c:
                await c.post(slack_url, json={"text": msg}, timeout=5.0)
        except Exception:
            pass

    # ── Elasticsearch ───────────────────────────────────────────────────────

    async def _index_abandoned_order(self, order: AbandonedOrder) -> None:
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return
        doc = {
            "order_id": order.order_id,
            "merchant_id": order.merchant_id,
            "amount": order.amount,
            "currency": order.currency,
            "buyer_email": order.buyer_email,
            "buyer_name": order.buyer_name,
            "items": order.items,
            "approved_at": order.approved_at,
            "recovery_attempts": 0,
            "recovered": False,
            "status": "pending_recovery",
        }
        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"{es_url}/anchor-abandoned-orders/_doc/{order.order_id}",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=doc,
                    timeout=3.0,
                )
        except Exception as e:
            print(f"[Recovery] ES index failed: {e}")

    async def _query_stale_orders(
        self, merchant_id: str, cutoff_iso: str
    ) -> list[AbandonedOrder]:
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return []

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"merchant_id": merchant_id}},
                        {"term": {"recovered": False}},
                        {"range": {"approved_at": {"lte": cutoff_iso}}},
                        {"range": {"recovery_attempts": {"lt": self.config.max_attempts}}},
                    ]
                }
            },
            "size": 50,
        }
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{es_url}/anchor-abandoned-orders/_search",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=query,
                    timeout=5.0,
                )
            hits = r.json().get("hits", {}).get("hits", [])
            orders = []
            for hit in hits:
                src = hit["_source"]
                orders.append(AbandonedOrder(
                    order_id=src["order_id"],
                    merchant_id=src["merchant_id"],
                    amount=src["amount"],
                    currency=src.get("currency", "USD"),
                    buyer_email=src["buyer_email"],
                    buyer_name=src.get("buyer_name", ""),
                    items=src.get("items", []),
                    approved_at=src.get("approved_at", ""),
                    recovery_attempts=src.get("recovery_attempts", 0),
                    recovered=src.get("recovered", False),
                ))
            return orders
        except Exception as e:
            print(f"[Recovery] ES query failed: {e}")
            return []

    async def _log_recovery_attempt(
        self, order: AbandonedOrder, invoice_link: str, merchant_id: str
    ) -> None:
        """Update ES doc + write to anchor-actions for morning brief."""
        from agents.briefing import ActionRecord, log_action

        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if es_url and es_key:
            update = {
                "script": {
                    "source": "ctx._source.recovery_attempts += 1; ctx._source.last_recovery_at = params.ts",
                    "params": {"ts": datetime.now(timezone.utc).isoformat()},
                }
            }
            try:
                async with httpx.AsyncClient() as c:
                    await c.post(
                        f"{es_url}/anchor-abandoned-orders/_update/{order.order_id}",
                        headers={
                            "Authorization": f"ApiKey {es_key}",
                            "Content-Type": "application/json",
                        },
                        json=update,
                        timeout=3.0,
                    )
            except Exception:
                pass

        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="recovery_attempted",
            amount=order.amount,
            outcome="invoice_sent",
            reasoning=f"Approved order {order.order_id} uncaptured >{self.config.delay_minutes}m — invoice sent to buyer",
            requires_attention=False,
        )
        await log_action(merchant_id, action)

    async def _mark_no_longer_needed(self, order_id: str) -> None:
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return
        update = {"doc": {"recovered": True, "status": "completed_externally"}}
        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"{es_url}/anchor-abandoned-orders/_update/{order_id}",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=update,
                    timeout=3.0,
                )
        except Exception:
            pass
