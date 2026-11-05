"""
Watcher Agent — 24/7 webhook listener.

Receives PayPal webhooks and routes to the correct downstream agent.
This is what makes Anchor *autonomous* — it doesn't wait for a user
to open a dashboard. It reacts within seconds of a dispute being filed.

PayPal webhook events we handle:
  CUSTOMER.DISPUTE.CREATED        → Guardian dispute pipeline
  CUSTOMER.DISPUTE.UPDATED        → deadline changes, status updates
  CHECKOUT.ORDER.APPROVED         → buyer authorized; Recovery Agent tracks for capture
  INVOICING.INVOICE.PAYMENT.PAID  → mark invoice resolved
  INVOICING.INVOICE.UPDATED       → track overdue
  PAYMENT.CAPTURE.COMPLETED       → baseline for dispute risk scoring
  BILLING.SUBSCRIPTION.PAYMENT.FAILED → flag for invoice follow-up
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from agents.classifier import DisputeClassifier
from agents.invoice import InvoiceCollector
from agents.guardian import Guardian
from agents.recovery import RecoveryAgent
import agents.onboarding as onboarding_module

app = FastAPI()
onboarding_module.register_routes(app)

PAYPAL_WEBHOOK_ID = os.environ["PAYPAL_WEBHOOK_ID"]
PAYPAL_CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]
PAYPAL_CLIENT_SECRET = os.environ["PAYPAL_CLIENT_SECRET"]

# Secret for signing approval tokens — set via APPROVAL_TOKEN_SECRET env var.
# Render auto-generates this; for local dev set it in .env.
APPROVAL_TOKEN_SECRET = os.environ.get("APPROVAL_TOKEN_SECRET", secrets.token_hex(32))

# ---------------------------------------------------------------------------
# Idempotency store — prevents duplicate processing on PayPal's 25 retries.
# In-memory for single-instance; swap for Redis/ES on multi-instance deploy.
# Stores event_id → processed_at timestamp; entries expire after 4 days.
# ---------------------------------------------------------------------------
_processed_events: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 4 * 24 * 3600  # 4 days > PayPal's 3-day retry window


def _is_duplicate(event_id: str) -> bool:
    now = time.monotonic()
    # Evict expired entries
    expired = [k for k, v in _processed_events.items() if now - v > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _processed_events[k]
    if event_id in _processed_events:
        return True
    _processed_events[event_id] = now
    return False


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhooks/paypal")
async def handle_webhook(request: Request, background: BackgroundTasks):
    """
    PayPal sends events here. We verify the signature immediately
    (synchronous), then deduplicate and process in background so PayPal
    gets a fast 200. PayPal retries 25× over 3 days — dedup is mandatory.
    """
    body = await request.body()
    headers = dict(request.headers)

    # Verify webhook authenticity before processing anything
    await _verify_paypal_signature(body, headers)

    event = json.loads(body)
    event_id = event.get("id", "")
    event_type = event.get("event_type", "")

    # Deduplicate — return 200 immediately on repeat so PayPal stops retrying
    if event_id and _is_duplicate(event_id):
        print(f"[Watcher] Duplicate event {event_id} ({event_type}) — ignored")
        return JSONResponse({"status": "duplicate"}, status_code=200)

    if event_type == "CUSTOMER.DISPUTE.CREATED":
        background.add_task(_handle_dispute_created, event)

    elif event_type == "CUSTOMER.DISPUTE.UPDATED":
        background.add_task(_handle_dispute_updated, event)

    elif event_type in (
        "INVOICING.INVOICE.UPDATED",
        "INVOICING.INVOICE.PAYMENT.PAID",
    ):
        background.add_task(_handle_invoice_event, event)

    elif event_type == "CHECKOUT.ORDER.APPROVED":
        background.add_task(_handle_order_approved, event)

    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        background.add_task(_handle_capture, event)

    # Always return 200 immediately — PayPal retries on anything else
    return JSONResponse({"status": "received"}, status_code=200)


async def _verify_paypal_signature(body: bytes, headers: dict):
    """
    PayPal WEBHOOK-SIG verification.
    PayPal sends: PayPal-Transmission-Id, PayPal-Cert-Url, PayPal-Transmission-Time,
                  PayPal-Transmission-Sig, PayPal-Auth-Algo
    We must verify before trusting any payload — this prevents replay attacks.
    """
    import httpx

    transmission_id = headers.get("paypal-transmission-id")
    timestamp = headers.get("paypal-transmission-time")
    cert_url = headers.get("paypal-cert-url")
    actual_sig = headers.get("paypal-transmission-sig")
    auth_algo = headers.get("paypal-auth-algo", "SHA256withRSA")

    if not all([transmission_id, timestamp, cert_url, actual_sig]):
        raise HTTPException(400, "Missing PayPal signature headers")

    # Verify against PayPal's verification endpoint
    # In production: use PayPal SDK verify_webhook_signature
    # This is the correct approach — don't roll your own crypto
    async with httpx.AsyncClient() as client:
        access_token = await _get_access_token()
        resp = await client.post(
            "https://api.sandbox.paypal.com/v1/notifications/verify-webhook-signature",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "transmission_id": transmission_id,
                "transmission_time": timestamp,
                "cert_url": cert_url,
                "auth_algo": auth_algo,
                "transmission_sig": actual_sig,
                "webhook_id": PAYPAL_WEBHOOK_ID,
                "webhook_event": json.loads(body),
            },
        )

    result = resp.json()
    if result.get("verification_status") != "SUCCESS":
        raise HTTPException(403, f"Webhook signature verification failed: {result}")


async def _get_access_token() -> str:
    import httpx, base64
    creds = base64.b64encode(
        f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()
    ).decode()
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.sandbox.paypal.com/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        )
        return r.json()["access_token"]


async def _handle_dispute_created(event: dict):
    """
    Main dispute pipeline entry point.
    Runs entirely without human intervention:
      1. Classify the dispute
      2. Score available evidence
      3. Run Settlement Oracle
      4. If FIGHT → assemble + submit evidence
      5. If ACCEPT → accept claim
      6. If GATHER → alert seller with specific evidence checklist
    """
    resource = event.get("resource", {})
    dispute_id = resource.get("dispute_id")
    dispute_amount = float(
        resource.get("dispute_amount", {}).get("value", 0)
    )
    reason = resource.get("reason", "ITEM_NOT_RECEIVED")
    seller_transaction_id = resource.get("disputed_transactions", [{}])[0].get(
        "seller_transaction_id"
    )

    print(f"[Watcher] New dispute {dispute_id}: ${dispute_amount} ({reason})")

    # Route through Guardian — applies pre-auth thresholds before acting
    guardian = Guardian()
    await guardian.handle_dispute(
        dispute_id=dispute_id,
        dispute_amount=dispute_amount,
        reason_code=reason,
        transaction_id=seller_transaction_id,
        event_time=event.get("create_time", datetime.now(timezone.utc).isoformat()),
    )


async def _handle_dispute_updated(event: dict):
    resource = event.get("resource", {})
    dispute_id = resource.get("dispute_id")
    status = resource.get("status")
    print(f"[Watcher] Dispute {dispute_id} updated → {status}")
    # Update Bryntum board state, notify if resolved in seller's favor


async def _handle_invoice_event(event: dict):
    event_type = event.get("event_type")
    resource = event.get("resource", {})
    invoice_id = resource.get("id")

    collector = InvoiceCollector()
    if event_type == "INVOICING.INVOICE.PAYMENT.PAID":
        await collector.mark_paid(invoice_id)
    else:
        await collector.check_overdue(invoice_id, resource)


async def _handle_order_approved(event: dict):
    """
    Buyer authorized the PayPal payment but merchant hasn't captured yet.
    Store in Elasticsearch; Recovery Agent will follow up if capture never arrives.
    """
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    recovery = RecoveryAgent()
    await recovery.store_approved_order(event, merchant_id)


async def _handle_capture(event: dict):
    """New payment — baseline data for future dispute risk scoring."""
    resource = event.get("resource", {})
    # Index transaction in Elasticsearch for dispute pattern matching
    # Used by Settlement Oracle's historical win rate lookup
    print(f"[Watcher] Capture {resource.get('id')} — indexing for dispute baseline")


# ---------------------------------------------------------------------------
# Merchant approval endpoints
# ---------------------------------------------------------------------------

def _make_approval_token(dispute_id: str, merchant_id: str, action: str) -> str:
    """
    HMAC-SHA256 token binding approval to a specific dispute + merchant + action.
    Expires after 48 hours. Format: <expiry_unix>.<hmac_hex>
    """
    expiry = int(time.time()) + 48 * 3600
    payload = f"{dispute_id}:{merchant_id}:{action}:{expiry}"
    sig = hmac.new(
        APPROVAL_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{expiry}.{sig}"


def _verify_approval_token(token: str, dispute_id: str, merchant_id: str, action: str) -> bool:
    """Verify token is valid, unexpired, and bound to this exact dispute."""
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False
    if time.time() > expiry:
        return False
    payload = f"{dispute_id}:{merchant_id}:{action}:{expiry}"
    expected = hmac.new(
        APPROVAL_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.post("/approve/{dispute_id}")
async def approve_dispute(
    dispute_id: str,
    background: BackgroundTasks,
    request: Request,
):
    """
    Merchant approved a pending dispute fight.

    Requires a signed token in the Authorization header or `token` query param:
      Authorization: Bearer <token>   OR   ?token=<token>

    The token is generated by Guardian when it sends the Slack alert and
    is bound to: dispute_id + merchant_id + action="fight". It expires in 48h.
    This prevents:
      - Approving the wrong dispute (token is dispute-specific)
      - Replay attacks (token expires)
      - Unauthorized approvals (HMAC requires the server secret)
    """
    merchant_id = os.environ.get("MERCHANT_ID", "default")

    token = request.query_params.get("token")
    if not token:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()

    if not token or not _verify_approval_token(token, dispute_id, merchant_id, "fight"):
        raise HTTPException(403, "Invalid or expired approval token")

    background.add_task(_execute_approved_fight, dispute_id)
    return JSONResponse({"status": "approved", "dispute_id": dispute_id})


async def _execute_approved_fight(dispute_id: str):
    from agents.classifier import DisputeClassifier
    from agents.evidence import EvidenceAssembler, EvidencePackage
    from agents.briefing import ActionRecord, log_action
    from pathlib import Path
    import tempfile

    token_getter = Guardian()
    token = await token_getter._get_token()

    classifier = DisputeClassifier()
    dispute_detail = await classifier._fetch_dispute(dispute_id, token)
    dispute_amount = float(
        dispute_detail.get("dispute_amount", {}).get("value", 0)
    )
    transaction = dispute_detail.get("disputed_transactions", [{}])[0]

    assembler = EvidenceAssembler(access_token=token)
    tmp_dir = Path(tempfile.mkdtemp())
    package = EvidencePackage(
        dispute_id=dispute_id,
        notes="Merchant approved — Anchor executing evidence submission.",
    )

    tracking = transaction.get("items", [{}])[0].get("tracking_info", {}) if transaction.get("items") else {}
    if tracking.get("tracking_number"):
        f = await assembler.gather_tracking_evidence(
            tracking["tracking_number"],
            tracking.get("carrier_name", "USPS"),
            tmp_dir,
        )
        if f:
            package.add(f)

    buyer_email = transaction.get("buyer", {}).get("email", "")
    if buyer_email:
        for f in await assembler.gather_communication_evidence(buyer_email, tmp_dir):
            package.add(f)

    result = await assembler.submit(package) if package.is_sufficient() else None

    action = ActionRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        action_type="dispute_fought",
        dispute_id=dispute_id,
        amount=dispute_amount,
        outcome="evidence_submitted" if result else "failed",
        reasoning="Merchant approved via Slack reply",
        requires_attention=False,
    )
    await log_action(os.environ.get("MERCHANT_ID", "default"), action)
    print(f"[Watcher] Approved fight executed for {dispute_id}: {result}")


@app.post("/recovery-check")
async def recovery_check(background: BackgroundTasks):
    """
    Triggered every 15 minutes by Render cron.
    Scans for approved-but-uncaptured orders and sends buyer recovery invoices.
    """
    background.add_task(_run_recovery_check)
    return JSONResponse({"status": "recovery_check_queued"})


async def _run_recovery_check():
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    recovery = RecoveryAgent()
    result = await recovery.scan_and_recover(merchant_id)
    print(f"[Watcher] Recovery check complete: {result}")


@app.post("/morning-brief")
async def morning_brief(background: BackgroundTasks):
    """
    Trigger the morning brief manually, or from Render cron at 8 AM.
    Also callable from Zapier on a schedule.
    """
    background.add_task(_send_morning_brief)
    return JSONResponse({"status": "brief_queued"})


async def _send_morning_brief():
    guardian = Guardian()
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    await guardian.send_morning_brief(merchant_id)
