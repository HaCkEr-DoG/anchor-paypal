"""
PayPal REST API Client
https://developer.paypal.com/docs/api/overview/

Central PayPal client shared by Guardian, Classifier, and Recovery agents.
Replaces the duplicated _get_token() methods in guardian.py and classifier.py.

Covers:
  OAuth2    POST /v1/oauth2/token
  Disputes  GET  /v1/customer/disputes
            GET  /v1/customer/disputes/{id}
            POST /v1/customer/disputes/{id}/provide-evidence
            POST /v1/customer/disputes/{id}/accept-claim
            POST /v1/customer/disputes/{id}/send-message
            POST /v1/customer/disputes/{id}/appeal
  Orders    GET  /v2/checkout/orders/{id}
  Webhooks  GET  /v1/notifications/webhooks
            POST /v1/notifications/verify-webhook-signature

Env vars:
  PAYPAL_CLIENT_ID      sandbox or live client ID
  PAYPAL_CLIENT_SECRET  matching secret
  PAYPAL_WEBHOOK_ID     webhook registration ID (for signature verification)
  PAYPAL_ENV            "sandbox" (default) or "live"
"""

import base64
import os
import time
from typing import Optional

import httpx

PAYPAL_ENV        = os.environ.get("PAYPAL_ENV", "sandbox")
PAYPAL_CLIENT_ID  = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET     = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")

_BASE = {
    "sandbox": "https://api.sandbox.paypal.com",
    "live":    "https://api.paypal.com",
}


class PayPalClient:
    """
    Async PayPal REST client with automatic OAuth2 token refresh.

    Usage:
        client = PayPalClient()
        disputes = await client.list_disputes(page_size=10)
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        env: Optional[str] = None,
    ):
        self.client_id     = client_id     or PAYPAL_CLIENT_ID
        self.client_secret = client_secret or PAYPAL_SECRET
        self.env           = env           or PAYPAL_ENV
        self.base          = _BASE.get(self.env, _BASE["sandbox"])
        self._token: Optional[str]  = None
        self._token_expires_at: float = 0

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ── OAuth2 ───────────────────────────────────────────────────────────────

    async def get_token(self) -> str:
        """Return a valid Bearer token, refreshing if within 60s of expiry."""
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.base}/v1/oauth2/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            )
            r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expires_at = time.monotonic() + data.get("expires_in", 32400)
        return self._token

    async def _headers(self) -> dict:
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Disputes ─────────────────────────────────────────────────────────────

    async def list_disputes(
        self,
        dispute_state: Optional[str] = None,
        page_size: int = 20,
        start_time: Optional[str] = None,
    ) -> dict:
        """
        GET /v1/customer/disputes
        dispute_state: REQUIRED_ACTION | REQUIRED_OTHER_PARTY_ACTION | UNDER_REVIEW |
                       RESOLVED | OPEN_INQUIRIES | APPEALABLE
        Returns {items: [...], links: [...]}
        """
        params: dict = {"page_size": page_size}
        if dispute_state:
            params["dispute_state"] = dispute_state
        if start_time:
            params["start_time"] = start_time
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{self.base}/v1/customer/disputes",
                headers=await self._headers(),
                params=params,
            )
            r.raise_for_status()
        return r.json()

    async def get_dispute(self, dispute_id: str) -> dict:
        """GET /v1/customer/disputes/{id}"""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{self.base}/v1/customer/disputes/{dispute_id}",
                headers=await self._headers(),
            )
            r.raise_for_status()
        return r.json()

    async def provide_evidence(
        self,
        dispute_id: str,
        evidence_type: str,
        notes: str,
        documents: Optional[list[dict]] = None,
    ) -> dict:
        """
        POST /v1/customer/disputes/{id}/provide-evidence
        evidence_type: PROOF_OF_FULFILLMENT | PROOF_OF_REFUND | PROOF_OF_SHIPMENT |
                       AUTHORIZATION | CANCEL_NOTIFICATION | CHANGE_REASON |
                       COMPANY_INFORMATION | ITEM_DESCRIPTION | PROOF_OF_RETURN_SHIPMENT
        """
        payload: dict = {
            "evidences": [{
                "evidence_type": evidence_type,
                "notes": notes,
            }]
        }
        if documents:
            payload["evidences"][0]["documents"] = documents
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{self.base}/v1/customer/disputes/{dispute_id}/provide-evidence",
                headers=await self._headers(),
                json=payload,
            )
            r.raise_for_status()
        return r.json() if r.content else {}

    async def accept_claim(
        self,
        dispute_id: str,
        note: str = "Merchant accepts this claim.",
        refund_amount: Optional[dict] = None,
    ) -> dict:
        """POST /v1/customer/disputes/{id}/accept-claim"""
        payload: dict = {"note": note}
        if refund_amount:
            payload["refund_amount"] = refund_amount
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.base}/v1/customer/disputes/{dispute_id}/accept-claim",
                headers=await self._headers(),
                json=payload,
            )
            r.raise_for_status()
        return r.json() if r.content else {}

    async def send_message(self, dispute_id: str, message: str) -> dict:
        """POST /v1/customer/disputes/{id}/send-message"""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.base}/v1/customer/disputes/{dispute_id}/send-message",
                headers=await self._headers(),
                json={"message": message},
            )
            r.raise_for_status()
        return r.json() if r.content else {}

    async def appeal_dispute(self, dispute_id: str, evidences: list[dict]) -> dict:
        """POST /v1/customer/disputes/{id}/appeal"""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.base}/v1/customer/disputes/{dispute_id}/appeal",
                headers=await self._headers(),
                json={"evidences": evidences},
            )
            r.raise_for_status()
        return r.json() if r.content else {}

    # ── Orders ───────────────────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict:
        """GET /v2/checkout/orders/{id}"""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{self.base}/v2/checkout/orders/{order_id}",
                headers=await self._headers(),
            )
            r.raise_for_status()
        return r.json()

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def list_webhooks(self) -> dict:
        """GET /v1/notifications/webhooks"""
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{self.base}/v1/notifications/webhooks",
                headers=await self._headers(),
            )
            r.raise_for_status()
        return r.json()

    async def verify_webhook_signature(
        self,
        auth_algo: str,
        cert_url: str,
        transmission_id: str,
        transmission_sig: str,
        transmission_time: str,
        webhook_event: dict,
    ) -> bool:
        """
        POST /v1/notifications/verify-webhook-signature
        Returns True if PayPal confirms the signature is valid.
        """
        payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": PAYPAL_WEBHOOK_ID,
            "webhook_event": webhook_event,
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.base}/v1/notifications/verify-webhook-signature",
                headers=await self._headers(),
                json=payload,
            )
            r.raise_for_status()
        return r.json().get("verification_status") == "SUCCESS"

    # ── Status summary (for the dashboard) ───────────────────────────────────

    async def status_summary(self) -> dict:
        """
        Returns a dict the dashboard can render directly.
        Always returns something; uses demo data when keys are absent.
        """
        if not self.configured():
            return {
                "enabled": False,
                "note": "Set PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET in Render env vars",
                "demo": _demo_status(),
            }

        result: dict = {"enabled": True, "env": self.env, "base": self.base}

        try:
            token = await self.get_token()
            result["oauth_ok"] = True
        except Exception as e:
            result["oauth_ok"] = False
            result["oauth_error"] = str(e)
            result["demo"] = _demo_status()
            return result

        # Open disputes (REQUIRED_ACTION = needs our response urgently)
        try:
            d = await self.list_disputes(dispute_state="REQUIRED_ACTION", page_size=5)
            urgent = d.get("items", [])
            result["urgent_disputes"] = len(urgent)
            result["urgent_items"] = [_fmt_dispute(x) for x in urgent[:5]]
        except Exception as e:
            result["disputes_error"] = str(e)

        # All open
        try:
            d2 = await self.list_disputes(page_size=5)
            result["total_open"] = len(d2.get("items", []))
        except Exception:
            pass

        # Webhooks
        try:
            wh = await self.list_webhooks()
            result["webhooks"] = len(wh.get("webhooks", []))
        except Exception:
            pass

        return result


# ---------------------------------------------------------------------------
# Shared singleton (import once; token cached across requests)
# ---------------------------------------------------------------------------

_shared_client: Optional[PayPalClient] = None


def get_client() -> PayPalClient:
    """Return the module-level singleton PayPalClient."""
    global _shared_client
    if _shared_client is None:
        _shared_client = PayPalClient()
    return _shared_client


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_dispute(d: dict) -> dict:
    money = d.get("dispute_amount", {})
    amount = f"{money.get('currency_code','USD')} {money.get('value','0.00')}"
    return {
        "dispute_id": d.get("dispute_id", ""),
        "status":     d.get("status", ""),
        "reason":     d.get("reason", ""),
        "amount":     amount,
        "due_date":   d.get("seller_response_due_date", ""),
    }


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

def _demo_status() -> dict:
    return {
        "env": "sandbox",
        "base": "https://api.sandbox.paypal.com",
        "oauth_ok": True,
        "urgent_disputes": 2,
        "total_open": 5,
        "webhooks": 1,
        "urgent_items": [
            {"dispute_id": "PP-D-00112233", "status": "WAITING_FOR_SELLER_RESPONSE",
             "reason": "ITEM_NOT_RECEIVED",        "amount": "USD 245.00",
             "due_date": "2026-11-08T23:59:59Z"},
            {"dispute_id": "PP-D-00112185", "status": "WAITING_FOR_SELLER_RESPONSE",
             "reason": "SIGNIFICANTLY_NOT_AS_DESCRIBED", "amount": "USD 189.99",
             "due_date": "2026-11-09T23:59:59Z"},
        ],
        "api_endpoints": [
            "POST /v1/oauth2/token",
            "GET  /v1/customer/disputes",
            "GET  /v1/customer/disputes/{id}",
            "POST /v1/customer/disputes/{id}/provide-evidence",
            "POST /v1/customer/disputes/{id}/accept-claim",
            "POST /v1/customer/disputes/{id}/send-message",
            "GET  /v2/checkout/orders/{id}",
            "POST /v1/notifications/verify-webhook-signature",
        ],
    }
