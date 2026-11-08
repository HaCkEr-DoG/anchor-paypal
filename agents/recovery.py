"""
Anchor Recovery — abandoned checkout recovery agent.

Monitors PayPal orders that reached APPROVED status (buyer authorized payment)
but were never captured by the merchant. These are highest-intent abandoned carts:
the buyer completed the full PayPal auth flow — the merchant's page likely errored
before calling /capture.

Flow:
  CHECKOUT.ORDER.APPROVED webhook  → store the approved order in Elasticsearch
  recovery scan after delay         → re-fetch PayPal order state
  status APPROVED                   → retry capture on the same order, idempotently
  capture webhook                   → mark recovered and update the merchant history

The buyer already approved this PayPal order. Resuming its intended capture avoids
creating a second invoice/payment request for the same purchase. Unknown or
unexpected PayPal states fail closed and require merchant review.
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
    recovery_status: str = "pending_recovery"
    capture_id: str = ""


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
        Saves the order to PostgreSQL so the recovery cron can find it.
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

        if amount < self.config.min_amount:
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
        await self._save_abandoned_order(order)
        print(f"[Recovery] Tracked approved order {order_id}: ${amount:.2f}")

    # ── Cron entry point ────────────────────────────────────────────────────

    async def scan_and_recover(self, merchant_id: str) -> dict:
        """
        Called every 15 minutes by the /recovery-check cron endpoint.
        Finds stale approved orders and safely resumes their existing capture.
        Returns a summary of actions taken.
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self.config.delay_minutes)
        ).isoformat()

        stale_orders = await self._get_stale_orders(merchant_id, cutoff)
        print(f"[Recovery] Found {len(stale_orders)} stale approved orders for recovery")

        recovered = 0
        skipped = 0
        pending = 0
        for order in stale_orders:
            if order.recovery_attempts >= self.config.max_attempts:
                skipped += 1
                continue
            if order.recovered:
                skipped += 1
                continue

            # Re-fetch source-of-truth state. Only an explicitly APPROVED order
            # is eligible; UNKNOWN and other states never trigger a payment.
            try:
                token = await self._get_token()
                paypal_order = await self._get_order_details(order.order_id, token)
                status = str((paypal_order or {}).get("status", "UNKNOWN")).upper()
            except Exception as exc:
                print(f"[Recovery] State check failed for {order.order_id}: {exc}")
                await self._mark_recovery_state(order.order_id, "needs_review", "PayPal state lookup failed")
                skipped += 1
                continue
            if status == "COMPLETED":
                if order.recovery_status == "capture_pending" or order.capture_id:
                    await self._record_capture_result(
                        order, merchant_id, "captured", {"capture_id": order.capture_id}
                    )
                    recovered += 1
                else:
                    await self._mark_no_longer_needed(order.order_id)
                    skipped += 1
                continue
            if order.recovery_status == "capture_pending":
                await self._mark_recovery_state(
                    order.order_id,
                    "needs_review",
                    "PayPal capture did not reach COMPLETED; verify its status before taking payment action",
                )
                skipped += 1
                continue
            intent = str((paypal_order or {}).get("intent", "")).upper()
            if status != "APPROVED" or intent != "CAPTURE":
                await self._mark_recovery_state(
                    order.order_id,
                    "needs_review",
                    f"PayPal order state/intent {status}/{intent or 'UNKNOWN'} is not eligible for automatic capture",
                )
                skipped += 1
                continue

            if not await self._claim_recovery_order(order.order_id):
                skipped += 1
                continue

            capture = await self._capture_approved_order(order.order_id, token)
            if not capture:
                # Retry is safe because the same PayPal-Request-Id is reused.
                state = "needs_review" if order.recovery_attempts + 1 >= self.config.max_attempts else "retry_safe"
                await self._record_capture_result(order, merchant_id, state, None)
                pending += 1
                continue

            capture_status = str(capture.get("status", "UNKNOWN")).upper()
            if capture_status == "COMPLETED":
                await self._record_capture_result(order, merchant_id, "captured", capture)
                recovered += 1
            elif capture_status in {"PENDING", "PENDING_REVIEW"}:
                await self._record_capture_result(order, merchant_id, "capture_pending", capture)
                pending += 1
            else:
                await self._record_capture_result(order, merchant_id, "needs_review", capture)
                skipped += 1

        return {"scanned": len(stale_orders), "recovered": recovered, "pending": pending, "skipped": skipped}

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
        """Compatibility helper returning only the current order status."""
        return str((await self._get_order_details(order_id, token) or {}).get("status", "UNKNOWN"))

    async def _get_order_details(self, order_id: str, token: str) -> Optional[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.paypal_base}/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        if r.status_code != 200:
            return None
        return r.json()

    async def _capture_approved_order(self, order_id: str, token: str) -> Optional[dict]:
        """Capture the buyer-approved order with a stable PayPal idempotency key."""
        import hashlib

        request_id = "an-" + hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:32]
        try:
            async with httpx.AsyncClient() as c:
                response = await c.post(
                    f"{self.paypal_base}/v2/checkout/orders/{order_id}/capture",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": request_id,
                    },
                    json={},
                    timeout=10.0,
                )
            if response.status_code not in (200, 201):
                print(f"[Recovery] Capture request failed for {order_id}: {response.status_code} {response.text}")
                return None
            payload = response.json()
            purchase_units = payload.get("purchase_units", [])
            captures = [
                capture
                for unit in purchase_units
                for capture in unit.get("payments", {}).get("captures", [])
            ]
            status = captures[0].get("status", payload.get("status", "UNKNOWN")) if captures else payload.get("status", "UNKNOWN")
            return {"status": status, "capture_id": captures[0].get("id") if captures else None}
        except Exception as exc:
            print(f"[Recovery] Capture request uncertain for {order_id}: {exc}")
            return None

    async def _claim_recovery_order(self, order_id: str) -> bool:
        """Atomically claim an order using SELECT FOR UPDATE SKIP LOCKED in PostgreSQL."""
        if not os.environ.get("DATABASE_URL"):
            return False
        import agents.pg_store as pg_store
        try:
            return await asyncio.to_thread(pg_store.claim_abandoned_order, order_id)
        except Exception as exc:
            print(f"[Recovery] Could not claim order {order_id}: {exc}")
            return False

    async def _create_recovery_invoice(
        self, order: AbandonedOrder, token: str
    ) -> Optional[str]:
        """
        Legacy invoice helper retained for existing callers.

        The automated recovery scan does not use invoices; it resumes the
        buyer-approved order capture to avoid creating a second payment request.
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

    # ── PostgreSQL storage ──────────────────────────────────────────────────

    async def _save_abandoned_order(self, order: AbandonedOrder) -> None:
        if not os.environ.get("DATABASE_URL"):
            print(f"[Recovery] DATABASE_URL not set — cannot persist order {order.order_id}")
            return
        import agents.pg_store as pg_store
        doc = {
            "order_id": order.order_id,
            "merchant_id": order.merchant_id,
            "amount": order.amount,
            "currency": order.currency,
            "buyer_email": order.buyer_email,
            "buyer_name": order.buyer_name,
            "items": order.items,
            "approved_at": order.approved_at,
        }
        try:
            await asyncio.to_thread(pg_store.save_abandoned_order, doc)
        except Exception as e:
            print(f"[Recovery] PG save failed for {order.order_id}: {e}")

    async def _get_stale_orders(
        self, merchant_id: str, cutoff_iso: str
    ) -> list[AbandonedOrder]:
        if not os.environ.get("DATABASE_URL"):
            return []
        import agents.pg_store as pg_store
        try:
            rows = await asyncio.to_thread(
                pg_store.get_stale_abandoned_orders,
                merchant_id,
                cutoff_iso,
                self.config.max_attempts,
            )
            orders = []
            for src in rows:
                orders.append(AbandonedOrder(
                    order_id=src["order_id"],
                    merchant_id=src["merchant_id"],
                    amount=float(src["amount"]),
                    currency=src.get("currency", "USD"),
                    buyer_email=src.get("buyer_email", ""),
                    buyer_name=src.get("buyer_name", ""),
                    items=src.get("items", []),
                    approved_at=str(src.get("approved_at", "")),
                    recovery_attempts=src.get("recovery_attempts", 0),
                    recovered=src.get("recovered", False),
                    recovery_status=src.get("recovery_status", "pending_recovery"),
                    capture_id=src.get("capture_id", ""),
                ))
            return orders
        except Exception as e:
            print(f"[Recovery] PG query failed: {e}")
            return []

    async def _record_capture_result(
        self,
        order: AbandonedOrder,
        merchant_id: str,
        state: str,
        capture: Optional[dict],
    ) -> None:
        """Persist capture state to PostgreSQL."""
        if os.environ.get("DATABASE_URL"):
            import agents.pg_store as pg_store
            capture_id = (capture or {}).get("capture_id", "") or ""
            try:
                await asyncio.to_thread(
                    pg_store.update_abandoned_order_state,
                    order.order_id, state,
                    f"capture_id={capture_id or 'pending'}" if capture else "",
                    capture_id,
                )
            except Exception as exc:
                print(f"[Recovery] Could not persist capture state for {order.order_id}: {exc}")

        from agents.briefing import ActionRecord, log_action
        outcome = "captured" if state == "captured" else state
        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="recovery_capture",
            dispute_id=None,
            amount=order.amount,
            outcome=outcome,
            reasoning=(
                f"Approved PayPal order {order.order_id}: {state}; "
                f"capture_id={(capture or {}).get('capture_id') or 'pending'}"
            ),
            requires_attention=state in {"needs_review", "retry_safe"},
        )
        await log_action(merchant_id, action)

    async def _mark_recovery_state(self, order_id: str, state: str, reason: str) -> None:
        if not os.environ.get("DATABASE_URL"):
            return
        import agents.pg_store as pg_store
        try:
            await asyncio.to_thread(
                pg_store.update_abandoned_order_state, order_id, state, reason, ""
            )
        except Exception as exc:
            print(f"[Recovery] Could not update state for {order_id}: {exc}")

    async def mark_order_captured(self, order_id: str) -> None:
        """Update an approved-order record from the PayPal capture webhook."""
        if not order_id or not os.environ.get("DATABASE_URL"):
            return
        import agents.pg_store as pg_store
        try:
            await asyncio.to_thread(pg_store.mark_order_captured_externally, order_id)
        except Exception as exc:
            print(f"[Recovery] Could not mark captured order {order_id}: {exc}")

    async def _mark_no_longer_needed(self, order_id: str) -> None:
        if not os.environ.get("DATABASE_URL"):
            return
        import agents.pg_store as pg_store
        try:
            await asyncio.to_thread(
                pg_store.update_abandoned_order_state, order_id, "completed_externally", "", ""
            )
        except Exception:
            pass
