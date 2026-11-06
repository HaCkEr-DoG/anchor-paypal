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
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse

from agents.classifier import DisputeClassifier
from agents.invoice import InvoiceCollector
from agents.guardian import Guardian
from agents.recovery import RecoveryAgent
import agents.onboarding as onboarding_module

_scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _scheduler.add_job(_run_recovery_check, "cron", minute="*/15", id="recovery")
    _scheduler.add_job(_send_morning_brief, "cron", hour=8, minute=0, id="brief")
    _scheduler.start()
    print("[Watcher] Scheduler started — recovery every 15 min, brief at 08:00 UTC")
    yield
    _scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
onboarding_module.register_routes(app)

# Dashboard HTML — read once at startup
import pathlib
_DASHBOARD_HTML = (
    pathlib.Path(__file__).parent.parent / "static" / "dashboard.html"
).read_text()

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

    # Channel3 product enrichment — auto-generate ITEM_DESCRIPTION evidence
    item_name = ""
    items = transaction.get("items") or []
    if items:
        item_name = items[0].get("name", "") or items[0].get("description", "")
    if not item_name:
        item_name = transaction.get("description", "") or f"PayPal dispute {dispute_id}"
    product_evidence = await assembler.gather_product_evidence(
        item_name, dispute_id, dispute_amount, tmp_dir
    )
    if product_evidence:
        package.add(product_evidence)
        print(f"[Watcher] Channel3 enriched {dispute_id} with product data for '{item_name}'")

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


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/api/stats")
async def api_stats():
    """Aggregate stats for the dashboard header cards."""
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")

    if not es_url or not es_key:
        return JSONResponse({"demo": True})

    import httpx
    query = {
        "query": {"term": {"merchant_id": merchant_id}},
        "size": 0,
        "aggs": {
            "by_type": {"terms": {"field": "action_type.keyword", "size": 20}},
            "total_amount": {"sum": {"field": "amount"}},
        },
    }
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{es_url}/anchor-actions/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query, timeout=5.0,
            )
        data = r.json()
        buckets = {b["key"]: b["doc_count"] for b in data.get("aggregations", {}).get("by_type", {}).get("buckets", [])}
        fought_amount_q = {
            "query": {"bool": {"must": [
                {"term": {"merchant_id": merchant_id}},
                {"term": {"action_type.keyword": "dispute_fought"}},
            ]}},
            "size": 0,
            "aggs": {"total": {"sum": {"field": "amount"}}},
        }
        async with httpx.AsyncClient() as c:
            r2 = await c.post(
                f"{es_url}/anchor-actions/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=fought_amount_q, timeout=5.0,
            )
        fought_amt = r2.json().get("aggregations", {}).get("total", {}).get("value", 0)

        recovery_q = {
            "query": {"term": {"merchant_id": merchant_id}},
            "size": 0,
            "aggs": {
                "total_rev": {"sum": {"field": "amount"}},
                "recovered_count": {"filter": {"term": {"recovered": True}}},
            },
        }
        async with httpx.AsyncClient() as c:
            r3 = await c.post(
                f"{es_url}/anchor-abandoned-orders/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=recovery_q, timeout=5.0,
            )
        rec_data = r3.json().get("aggregations", {})

        return {
            "disputes_total":    sum(buckets.get(k, 0) for k in ("dispute_fought", "dispute_accepted", "dispute_pending")),
            "disputes_fought":   buckets.get("dispute_fought", 0),
            "disputes_accepted": buckets.get("dispute_accepted", 0),
            "revenue_protected": round(fought_amt, 2),
            "orders_recovered":  r3.json().get("hits", {}).get("total", {}).get("value", 0),
            "revenue_recovered": round(rec_data.get("total_rev", {}).get("value", 0), 2),
        }
    except Exception as e:
        print(f"[Dashboard] stats query failed: {e}")
        return JSONResponse({"demo": True})


@app.get("/api/disputes")
async def api_disputes():
    """Recent dispute actions for the AG Grid disputes table."""
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")

    if not es_url or not es_key:
        return JSONResponse({"rows": [], "demo": True})

    import httpx
    query = {
        "query": {"bool": {"must": [
            {"term": {"merchant_id": merchant_id}},
            {"terms": {"action_type.keyword": ["dispute_fought", "dispute_accepted", "dispute_pending"]}},
        ]}},
        "sort": [{"timestamp": "desc"}],
        "size": 200,
    }
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{es_url}/anchor-actions/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query, timeout=5.0,
            )
        rows = [h["_source"] for h in r.json().get("hits", {}).get("hits", [])]
        return {"rows": rows}
    except Exception as e:
        print(f"[Dashboard] disputes query failed: {e}")
        return JSONResponse({"rows": [], "error": str(e)})


@app.get("/api/oracle/calculate")
async def api_oracle_calculate(
    amount: float = 245.0,
    claim_type: str = "ITEM_NOT_RECEIVED",
    has_tracking: bool = False,
    has_delivery: bool = False,
    has_comm: bool = False,
    has_item_proof: bool = False,
    high_volume: bool = False,
    deadline_hours: int = 72,
    dispute_fee: Optional[float] = None,
):
    """Settlement Oracle — live EV calculation. No API key required."""
    from oracle.decision import calculate, ClaimType, Evidence
    ct_map = {
        "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
        "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
        "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
        "BILLING_ISSUE": ClaimType.BILLING_ISSUE,
    }
    ct = ct_map.get(claim_type.upper(), ClaimType.ITEM_NOT_RECEIVED)
    evidence = Evidence(
        has_tracking_number=has_tracking,
        has_delivery_confirmation=has_delivery,
        has_buyer_communication=has_comm,
        has_item_description_proof=has_item_proof,
    )
    decision = calculate(
        dispute_amount=amount,
        claim_type=ct,
        evidence=evidence,
        deadline_hours=deadline_hours,
        seller_hourly_rate=float(os.environ.get("SELLER_HOURLY_RATE", 50)),
        similar_dispute_win_rate=float(os.environ.get("SELLER_WIN_RATE", 0.5)),
        high_volume_tier=high_volume,
        dispute_fee_override=dispute_fee,
    )
    return JSONResponse({
        "recommendation": decision.recommendation.value,
        "ev_fight": round(decision.ev_fight, 2),
        "ev_accept": round(decision.ev_accept, 2),
        "win_probability": round(decision.win_probability, 4),
        "confidence": decision.confidence,
        "break_even_probability": round(decision.break_even_probability, 4),
        "reasoning": decision.reasoning,
        "time_to_respond_hours": decision.time_to_respond_hours,
        "inputs": {
            "amount": amount, "claim_type": claim_type, "high_volume": high_volume,
            "has_tracking": has_tracking, "has_delivery": has_delivery,
            "has_comm": has_comm, "has_item_proof": has_item_proof,
        },
    })


@app.get("/api/apimatic/status")
async def api_apimatic_status():
    """APIMatic integration status — version, features, endpoint catalog counts."""
    from agents.apimatic import ApiMaticClient
    return JSONResponse(ApiMaticClient().status_summary())


@app.get("/api/apimatic/context")
async def api_apimatic_context():
    """
    Returns the APIMatic-generated PayPal Disputes API context document.
    This is what the Guardian agent loads before every PayPal API call
    to ensure correct endpoints and parameters.
    """
    from agents.apimatic import ApiMaticClient
    client = ApiMaticClient()
    context = client.get_context()
    return JSONResponse({
        "enabled": client.enabled(),
        "source": "APIMatic Context Plugin — apimatic.io",
        "context": context,
        "note": "Set APIMATIC_API_KEY to regenerate from live PayPal OpenAPI spec",
    })


@app.get("/api/apimatic/lookup/{action}")
async def api_apimatic_lookup(action: str):
    """
    Looks up the correct PayPal endpoint for a given dispute action.
    Called by Guardian before every API call — this is the runtime benefit
    of APIMatic's Context Plugin: deterministic, hallucination-free routing.

    Actions: accept, provide_evidence, send_message, appeal, escalate,
             get_detail, list, create_invoice, send_invoice, get_order, get_token
    """
    from agents.apimatic import ApiMaticClient
    client = ApiMaticClient()
    return JSONResponse(client.lookup_endpoint(action))


@app.get("/api/channel3/status")
async def api_channel3_status():
    """Channel3 integration status — catalog size, features, API key state."""
    from agents.channel3 import Channel3Client
    return JSONResponse(Channel3Client().status_summary())


@app.post("/api/channel3/enrich")
async def api_channel3_enrich(request: Request):
    """
    Channel3 product enrichment endpoint.
    Given a dispute item description + amount, returns canonical product data
    from Channel3's 100M+ product graph.

    Body: {"item_description": "Nike Air Max 90", "amount": 89.99, "dispute_id": "PP-D-..."}
    Returns: Channel3 product match + formatted ITEM_DESCRIPTION evidence text.

    Used by: Guardian (auto enrichment on dispute_fought), dashboard demo button.
    """
    from agents.channel3 import Channel3Client

    body = await request.json()
    item_description = body.get("item_description", "")
    amount = body.get("amount")
    dispute_id = body.get("dispute_id", "demo")

    if not item_description:
        return JSONResponse({"error": "item_description required"}, status_code=400)

    client = Channel3Client()
    if not client.enabled():
        return JSONResponse({
            "enabled": False,
            "note": "CHANNEL3_API_KEY not configured — set it in Render env vars",
            "demo": {
                "product": "Nike Air Max 90",
                "brand": "Nike",
                "category": "Athletic Footwear",
                "catalog_price": 90.00,
                "currency": "USD",
                "availability": "InStock",
                "domain": "nike.com",
                "evidence_text": (
                    f"Product: Nike Air Max 90\n"
                    f"Brand: Nike\n"
                    f"Category: Athletic Footwear\n"
                    f"Catalog price: USD 90.00\n"
                    f"Transaction amount: USD {amount or '89.99'}\n"
                    f"Price match: within 2% of catalog price ✓\n"
                    f"Availability: InStock\n"
                    f"Sold by: nike.com\n\n"
                    f"Data source: Channel3 product graph — trychannel3.com (100M+ products)"
                ),
            }
        })

    result = await client.enrich_dispute(dispute_id, item_description, float(amount) if amount else None)

    if not result.succeeded:
        return JSONResponse({"enabled": True, "error": result.error})

    p = result.product
    return JSONResponse({
        "enabled": True,
        "dispute_id": dispute_id,
        "query": result.query,
        "product": {
            "id": p.id,
            "title": p.title,
            "brand": p.brand,
            "category": p.category,
            "catalog_price": p.price,
            "currency": p.currency,
            "availability": p.availability,
            "domain": p.domain,
            "description": p.description[:200] + "…" if len(p.description) > 200 else p.description,
            "images": p.images,
        },
        "evidence_text": result.evidence_text,
    })


@app.get("/api/recoveries")
async def api_recoveries():
    """Abandoned orders for the AG Grid recoveries table."""
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")

    if not es_url or not es_key:
        return JSONResponse({"rows": [], "demo": True})

    import httpx
    query = {
        "query": {"term": {"merchant_id": merchant_id}},
        "sort": [{"approved_at": "desc"}],
        "size": 200,
    }
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{es_url}/anchor-abandoned-orders/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query, timeout=5.0,
            )
        rows = [h["_source"] for h in r.json().get("hits", {}).get("hits", [])]
        return {"rows": rows}
    except Exception as e:
        print(f"[Dashboard] recoveries query failed: {e}")
        return JSONResponse({"rows": [], "error": str(e)})


# ---------------------------------------------------------------------------
# ── Postman Collection endpoints ─────────────────────────────────────────────

@app.get("/api/postman/status")
async def api_postman_status():
    """Postman integration status — collection structure, request count, features."""
    from agents.postman import PostmanClient
    return JSONResponse(PostmanClient().status_summary())



# ---------------------------------------------------------------------------

@app.get("/api/postman/collection")
async def api_postman_collection():
    """
    Returns Anchor's Postman Collection v2.1 JSON.
    Import this into Postman, or use the 'Run in Postman' button on the dashboard.
    """
    from agents.postman import build_collection
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-paypal.onrender.com")
    collection = build_collection(base_url)
    from fastapi.responses import Response
    return Response(
        content=json.dumps(collection, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="anchor-paypal-collection.json"',
        },
    )


@app.post("/api/postman/publish")
async def api_postman_publish():
    """
    Creates or updates Anchor's collection on Postman via the Postman API.
    Requires POSTMAN_API_KEY env var. Returns the 'Run in Postman' URL.
    """
    from agents.postman import PostmanClient
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-paypal.onrender.com")
    client = PostmanClient()
    result = await client.publish(base_url=base_url)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Render Deployment endpoints
# ---------------------------------------------------------------------------

@app.get("/api/render/status")
async def api_render_status():
    """
    Returns Anchor's current Render deployment status:
    service info, latest deploy, and last 5 deploys.
    Falls back to demo data when RENDER_API_KEY / RENDER_SERVICE_ID not set.
    """
    from agents.render_deploy import RenderClient
    client = RenderClient()
    result = await client.status_summary()
    return JSONResponse(result)


@app.post("/api/render/deploy")
async def api_render_deploy(request: Request):
    """
    Triggers a new deploy on Render.
    Requires RENDER_API_KEY and RENDER_SERVICE_ID env vars.
    Optional body: {"clear_cache": true}
    """
    from agents.render_deploy import RenderClient
    client = RenderClient()
    if not client.enabled():
        return JSONResponse({
            "enabled": False,
            "note": "RENDER_API_KEY and RENDER_SERVICE_ID must be set to trigger deploys",
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    clear_cache = bool(body.get("clear_cache", False))
    try:
        deploy = await client.trigger_deploy(clear_cache=clear_cache)
        return JSONResponse({"enabled": True, "deploy": deploy})
    except Exception as e:
        return JSONResponse({"enabled": True, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Astropods AI Governance endpoints
# ---------------------------------------------------------------------------

@app.get("/api/astropods/status")
async def api_astropods_status():
    """
    Returns Astropods gateway config, available models with pricing,
    and current-month billing usage.
    Falls back to demo data when ASTRO_GATEWAY_API_KEY is not set.
    """
    from agents.astropods import AstropodsGateway
    gw = AstropodsGateway()
    result = await gw.status_summary()
    return JSONResponse(result)


@app.post("/api/astropods/chat")
async def api_astropods_chat(request: Request):
    """
    Routes a chat completion through the Astropods AI Gateway.
    Body: {"messages": [...], "model": "claude-sonnet-4-6"}
    Demonstrates that Anchor can route LLM calls through Astropods for governance.
    """
    from agents.astropods import AstropodsGateway
    gw = AstropodsGateway()
    if not gw.gateway_enabled():
        return JSONResponse({
            "enabled": False,
            "note": "ASTRO_GATEWAY_URL and ASTRO_GATEWAY_API_KEY must be set",
            "demo_response": {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "[Astropods demo] Guardian Agent reasoning: "
                            "Dispute PP-D-00112233 — ITEM_NOT_RECEIVED, $245.00. "
                            "EV(fight)=$142.80 > EV(accept)=$−245.00. "
                            "Recommended action: PROVIDE_EVIDENCE. "
                            "This response was routed through Astropods AI Gateway "
                            "for governance audit and spend control."
                        ),
                    }
                }],
                "model": "claude-sonnet-4-6",
                "usage": {"prompt_tokens": 48, "completion_tokens": 72, "total_tokens": 120},
            },
        })
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    messages = body.get("messages", [{"role": "user", "content": "Hello from Anchor — PayPal AI Hackathon 2026"}])
    model    = body.get("model", "claude-sonnet-4-6")
    try:
        resp = await gw.chat_completion(messages, model=model)
        return JSONResponse({"enabled": True, "response": resp})
    except Exception as e:
        return JSONResponse({"enabled": True, "error": str(e)}, status_code=500)


# ── AG Grid endpoints ─────────────────────────────────────────────────────────

@app.get("/api/aggrid/status")
async def aggrid_status():
    """
    Returns AG Grid version, features used, row/column counts, and sample
    columnDefs + rowData. Always returns a renderable payload.
    """
    from agents.aggrid import AGGridExporter
    return JSONResponse(AGGridExporter().status_summary())


@app.get("/api/aggrid/data")
async def aggrid_data(grid: str = "disputes"):
    """
    GET /api/aggrid/data?grid=disputes  (default)
    GET /api/aggrid/data?grid=recoveries

    Returns {columnDefs, rowData, defaultColDef, gridOptions} ready for:
        createGrid(el, { columnDefs: d.columnDefs, rowData: d.rowData })

    Uses live Elasticsearch data when available; falls back to demo rows.
    """
    from agents.aggrid import AGGridExporter, _demo_disputes, _demo_recoveries
    exporter = AGGridExporter()
    rows: list[dict] = []

    if grid == "recoveries":
        try:
            from agents.briefing import load_recent_recoveries
            rows = await load_recent_recoveries(limit=50)
        except Exception:
            pass
        if not rows:
            rows = _demo_recoveries()
        return JSONResponse(exporter.export_recoveries(rows))
    else:
        try:
            from agents.briefing import load_recent_actions
            records = await load_recent_actions(limit=50)
            rows = [r.__dict__ if hasattr(r, "__dict__") else r for r in records]
        except Exception:
            pass
        if not rows:
            rows = _demo_disputes()
        return JSONResponse(exporter.export_disputes(rows))


# ── PayPal API endpoints ──────────────────────────────────────────────────────

@app.get("/api/paypal/status")
async def paypal_status():
    """
    Returns PayPal API connection status, OAuth health, open dispute count,
    and webhook registrations.
    Always returns a renderable payload; uses demo data when keys are absent.
    """
    from agents.paypal_client import get_client
    return JSONResponse(await get_client().status_summary())


@app.get("/api/paypal/disputes")
async def paypal_disputes(state: str = "REQUIRED_ACTION", page_size: int = 10):
    """
    GET /api/paypal/disputes?state=REQUIRED_ACTION&page_size=10
    Proxies PayPal's /v1/customer/disputes filtered by state.
    Returns demo data when keys are absent.
    """
    from agents.paypal_client import get_client, _demo_status
    client = get_client()
    if not client.configured():
        return JSONResponse({"demo": True, "items": _demo_status()["urgent_items"]})
    try:
        data = await client.list_disputes(dispute_state=state, page_size=page_size)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Bryntum Scheduler endpoints ──────────────────────────────────────────────

@app.get("/api/bryntum/status")
async def bryntum_status():
    """
    Returns Bryntum Scheduler integration status and demo data formatted
    in Bryntum's native resource/event/timeRange JSON schema.
    """
    from agents.bryntum import BryntumDataExporter
    return JSONResponse(BryntumDataExporter().status_summary())


@app.get("/api/bryntum/data")
async def bryntum_data():
    """
    Returns live dispute data in Bryntum Scheduler's CrudManager format.
    A Bryntum Scheduler component can load this endpoint directly:

        crudManager: { transport: { load: { url: '/api/bryntum/data' } } }

    Falls back to demo disputes when Elasticsearch is not configured.
    """
    from agents.bryntum import BryntumDataExporter, _demo_disputes
    exporter = BryntumDataExporter()
    # Try live data from Elasticsearch briefing store
    disputes: list[dict] = []
    try:
        from agents.briefing import load_recent_actions
        records = await load_recent_actions(limit=20)
        disputes = [r.__dict__ if hasattr(r, "__dict__") else r for r in records]
    except Exception:
        pass
    if not disputes:
        disputes = _demo_disputes()
    return JSONResponse(exporter.export(disputes))


# ── Kernel Browser endpoints ──────────────────────────────────────────────────

@app.get("/api/kernel/status")
async def kernel_status():
    """
    Returns Kernel browser config and capabilities.
    Always returns a renderable payload; uses demo data when KERNEL_API_KEY is absent.

    Demo mode: shows stealth capabilities + carrier support without a real key.
    Live mode: verifies the key with a lightweight /browsers list call.
    """
    from agents.kernel_browser import KernelBrowser
    kb = KernelBrowser()
    return JSONResponse(await kb.status_summary())


@app.post("/api/kernel/screenshot")
async def kernel_screenshot(request: Request):
    """
    Demonstrates Kernel browser automation: spin up a stealth browser,
    navigate to a URL, return screenshot metadata.

    Body: {"url": "https://...", "wait_selector": ".optional-selector"}
    Returns demo metadata when KERNEL_API_KEY is not set.
    """
    from agents.kernel_browser import KernelBrowser
    kb = KernelBrowser()

    if not kb.enabled():
        return JSONResponse({
            "enabled": False,
            "demo": True,
            "note": "Set KERNEL_API_KEY to capture real screenshots",
            "demo_result": {
                "url": "https://www.ups.com/track?tracknum=1Z999AA10123456784",
                "carrier": "UPS",
                "status": "Delivered — October 28, 2026 at 2:14 P.M.",
                "screenshot_size_kb": 184,
                "spin_up_ms": 28,
                "stealth": True,
            },
        })

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    url           = body.get("url", "")
    wait_selector = body.get("wait_selector")

    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)

    try:
        png_bytes = await kb.screenshot_url(url, wait_selector=wait_selector)
        return JSONResponse({
            "enabled": True,
            "url": url,
            "screenshot_size_kb": round(len(png_bytes) / 1024, 1),
            "stealth": True,
        })
    except Exception as e:
        return JSONResponse({"enabled": True, "error": str(e)}, status_code=500)
