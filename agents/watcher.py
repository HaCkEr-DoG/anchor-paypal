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
    global APPROVAL_TOKEN_SECRET
    from zoneinfo import ZoneInfo
    from agents.config import load_config as _load_config
    APPROVAL_TOKEN_SECRET = await _load_or_create_approval_secret()
    _merchant_id = os.environ.get("MERCHANT_ID", "default")
    _brief_hour = 8
    _brief_tz = "UTC"
    try:
        _cfg = await _load_config(_merchant_id)
        _brief_hour = _cfg.morning_brief_hour
        _brief_tz = _cfg.morning_brief_timezone
    except Exception:
        pass
    _scheduler.add_job(_run_recovery_check, "cron", minute="*/15", id="recovery")
    _scheduler.add_job(
        _send_morning_brief, "cron",
        hour=_brief_hour, minute=0,
        id="brief",
        timezone=ZoneInfo(_brief_tz),
    )
    _scheduler.start()
    print(f"[Watcher] Scheduler started — recovery every 15 min, brief at {_brief_hour:02d}:00 {_brief_tz}")
    yield
    _scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
onboarding_module.register_routes(app)

# Dashboard HTML — read once at startup
import pathlib
_DASHBOARD_HTML = (
    pathlib.Path(__file__).parent.parent / "static" / "dashboard.html"
).read_text()

# Approval token secret — loaded/persisted from Elasticsearch in lifespan so it
# survives Render restarts. APPROVAL_TOKEN_SECRET env var always wins.
APPROVAL_TOKEN_SECRET: str = ""  # Populated during lifespan startup


async def _load_or_create_approval_secret() -> str:
    """Load from ES so approval links survive restarts; fall back to ephemeral if ES absent."""
    env_secret = os.environ.get("APPROVAL_TOKEN_SECRET")
    if env_secret:
        return env_secret
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        print("[Watcher] No ES — approval secret is ephemeral (links expire on restart)")
        return secrets.token_hex(32)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(
                f"{es_url}/anchor-config/_doc/_approval_secret",
                headers={"Authorization": f"ApiKey {es_key}"},
            )
            if r.status_code == 200:
                stored = r.json().get("_source", {}).get("secret", "")
                if stored:
                    print("[Watcher] Loaded persistent approval secret from Elasticsearch")
                    return stored
            new_secret = secrets.token_hex(32)
            await c.put(
                f"{es_url}/anchor-config/_doc/_approval_secret",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json={"secret": new_secret, "created_at": datetime.now(timezone.utc).isoformat()},
            )
            print("[Watcher] Generated and stored persistent approval secret in Elasticsearch")
            return new_secret
    except Exception as exc:
        print(f"[Watcher] Could not persist approval secret ({exc}) — using ephemeral key")
        return secrets.token_hex(32)

# API key for internal cron/admin endpoints. Set API_SECRET in Render env vars.
# If unset in dev the check is skipped; warn loudly so it isn't forgotten.
_API_SECRET = os.environ.get("API_SECRET", "")
if not _API_SECRET:
    print("[Watcher] WARNING: API_SECRET not set — /recovery-check, /morning-brief and dashboard API endpoints are unauthenticated")


def _require_api_key(request: Request) -> None:
    """Raise 401 unless the caller supplies the correct X-API-Key header or ?api_key= param."""
    if not _API_SECRET:
        return
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key", "")
    if not hmac.compare_digest(key, _API_SECRET):
        raise HTTPException(401, "Unauthorized")


# ---------------------------------------------------------------------------
# Idempotency store — prevents duplicate processing on PayPal's 25 retries.
# Primary: ES op_type=create (atomic, survives restarts, multi-instance safe).
# Fallback: in-memory dict when ES is unavailable.
# ---------------------------------------------------------------------------
_processed_events: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 4 * 24 * 3600  # 4 days > PayPal's 3-day retry window


async def _is_duplicate(event_id: str) -> bool:
    """ES-backed atomic dedup; falls back to memory when ES is unavailable."""
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if es_url and es_key:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.put(
                    f"{es_url}/anchor-dedup/_doc/{event_id}?op_type=create",
                    headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                    json={"event_id": event_id, "processed_at": datetime.now(timezone.utc).isoformat()},
                )
            if r.status_code == 409:
                return True   # Document already exists — duplicate
            if r.status_code in (200, 201):
                return False  # Created atomically — first time we've seen it
            print(f"[Watcher] ES dedup unexpected {r.status_code} — falling back to memory")
        except Exception as exc:
            print(f"[Watcher] ES dedup unavailable ({exc}) — falling back to memory")
    # Memory fallback
    now = time.monotonic()
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


@app.post("/webhooks/paypal/{merchant_id}")
async def handle_webhook_for_merchant(
    merchant_id: str, request: Request, background: BackgroundTasks
):
    """Per-merchant webhook URL — register this in PayPal: POST /webhooks/paypal/{your-merchant-id}"""
    await _process_webhook(merchant_id, request, background)
    return JSONResponse({"status": "received"}, status_code=200)


@app.post("/webhooks/paypal")
async def handle_webhook(request: Request, background: BackgroundTasks):
    """
    Legacy single-tenant webhook URL — backward-compatible; requires MERCHANT_ID env var.
    PayPal sends events here, verifies signature immediately, deduplicates, then
    dispatches to background tasks. PayPal retries 25× over 3 days — dedup is mandatory.
    """
    merchant_id = os.environ.get("MERCHANT_ID", "")
    if not merchant_id:
        raise HTTPException(
            503,
            "Set MERCHANT_ID env var or use the per-merchant URL: POST /webhooks/paypal/{merchant_id}",
        )
    await _process_webhook(merchant_id, request, background)
    return JSONResponse({"status": "received"}, status_code=200)


async def _process_webhook(merchant_id: str, request: Request, background: BackgroundTasks):
    """Core webhook processing shared by both endpoint variants."""
    body = await request.body()
    headers = dict(request.headers)

    await _verify_paypal_signature(body, headers, merchant_id=merchant_id)

    event = json.loads(body)
    event_id = event.get("id", "")
    event_type = event.get("event_type", "")

    if event_id and await _is_duplicate(event_id):
        print(f"[Watcher] Duplicate event {event_id} ({event_type}) — ignored")
        return

    if event_type == "CUSTOMER.DISPUTE.CREATED":
        background.add_task(_handle_dispute_created, event, merchant_id)

    elif event_type == "CUSTOMER.DISPUTE.UPDATED":
        background.add_task(_handle_dispute_updated, event, merchant_id)

    elif event_type in (
        "INVOICING.INVOICE.UPDATED",
        "INVOICING.INVOICE.PAYMENT.PAID",
    ):
        background.add_task(_handle_invoice_event, event)

    elif event_type == "CHECKOUT.ORDER.APPROVED":
        background.add_task(_handle_order_approved, event, merchant_id)

    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        background.add_task(_handle_capture, event, merchant_id)


async def _verify_paypal_signature(body: bytes, headers: dict, merchant_id: str = ""):
    """
    PayPal WEBHOOK-SIG verification.
    PayPal sends: PayPal-Transmission-Id, PayPal-Cert-Url, PayPal-Transmission-Time,
                  PayPal-Transmission-Sig, PayPal-Auth-Algo
    We must verify before trusting any payload — this prevents replay attacks.
    Uses per-merchant paypal_webhook_id from config when available, falls back
    to the PAYPAL_WEBHOOK_ID env var.
    """
    import httpx

    transmission_id = headers.get("paypal-transmission-id")
    timestamp = headers.get("paypal-transmission-time")
    cert_url = headers.get("paypal-cert-url")
    actual_sig = headers.get("paypal-transmission-sig")
    auth_algo = headers.get("paypal-auth-algo", "SHA256withRSA")

    if not all([transmission_id, timestamp, cert_url, actual_sig]):
        raise HTTPException(400, "Missing PayPal signature headers")

    # Resolve per-merchant webhook ID: config takes priority over global env var
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "")
    if merchant_id:
        try:
            from agents.config import load_config as _load_config
            _cfg = await _load_config(merchant_id)
            if _cfg.paypal_webhook_id:
                webhook_id = _cfg.paypal_webhook_id
        except Exception:
            pass
    if not webhook_id:
        # Fail closed — missing webhook ID is a config error, never accept unverified events
        raise HTTPException(503, "PAYPAL_WEBHOOK_ID is not configured")

    from agents.paypal_client import get_client
    try:
        verified = await get_client().verify_webhook_signature(
            auth_algo=auth_algo,
            cert_url=cert_url,
            transmission_id=transmission_id,
            transmission_sig=actual_sig,
            transmission_time=timestamp,
            webhook_event=json.loads(body),
            webhook_id_override=webhook_id,
        )
    except Exception as e:
        raise HTTPException(503, f"PayPal signature verification unavailable: {e}") from e
    if not verified:
        raise HTTPException(403, "Webhook signature verification failed")


async def _get_access_token() -> str:
    from agents.paypal_client import get_client
    return await get_client().get_token()


async def _handle_dispute_created(event: dict, merchant_id: str = ""):
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
    lifecycle_stage = resource.get("dispute_life_cycle_stage", "").upper()
    eligible_claim = lifecycle_stage in {"CHARGEBACK", "CLAIM"} and reason.upper() in {
        "ITEM_NOT_RECEIVED", "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    }
    await _record_merchant_metric_event(
        # Stable claim key means CREATED and UPDATED webhooks upsert the same
        # eligible claim instead of inflating the fee-tier ratio.
        event_id=(f"claim-{dispute_id}" if eligible_claim else event.get("id") or f"dispute-{dispute_id}"),
        event_type="eligible_dispute" if eligible_claim else "dispute",
        amount=dispute_amount,
        reason=reason,
        timestamp=event.get("create_time", datetime.now(timezone.utc).isoformat()),
        merchant_id=merchant_id,
    )
    seller_transaction_id = resource.get("disputed_transactions", [{}])[0].get(
        "seller_transaction_id"
    )

    print(f"[Watcher] New dispute {dispute_id}: ${dispute_amount} ({reason})")

    # Route through Guardian — applies pre-auth thresholds before acting
    guardian = Guardian(
        merchant_id=merchant_id or os.environ.get("MERCHANT_ID", "default"),
        approval_secret=APPROVAL_TOKEN_SECRET,
    )
    await guardian.handle_dispute(
        dispute_id=dispute_id,
        dispute_amount=dispute_amount,
        reason_code=reason,
        transaction_id=seller_transaction_id,
        event_time=event.get("create_time", datetime.now(timezone.utc).isoformat()),
    )


async def _handle_dispute_updated(event: dict, merchant_id: str = ""):
    resource = event.get("resource", {})
    dispute_id = resource.get("dispute_id")
    status = resource.get("status")
    stage = resource.get("dispute_life_cycle_stage", "").upper()
    if stage in {"CHARGEBACK", "CLAIM"} and resource.get("reason", "").upper() in {
        "ITEM_NOT_RECEIVED", "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    }:
        try:
            amount = float(resource.get("dispute_amount", {}).get("value", 0))
        except (TypeError, ValueError):
            amount = 0.0
        await _record_merchant_metric_event(
            event_id=f"claim-{dispute_id}", event_type="eligible_dispute", amount=amount,
            reason=resource.get("reason", ""),
            timestamp=event.get("create_time", datetime.now(timezone.utc).isoformat()),
            merchant_id=merchant_id,
        )
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


async def _handle_order_approved(event: dict, merchant_id: str = ""):
    """
    Buyer authorized the PayPal payment but merchant hasn't captured yet.
    Store in Elasticsearch; Recovery Agent will follow up if capture never arrives.
    """
    mid = merchant_id or os.environ.get("MERCHANT_ID", "default")
    recovery = RecoveryAgent()
    await recovery.store_approved_order(event, mid)


async def _handle_capture(event: dict, merchant_id: str = ""):
    """New payment — baseline data for future dispute risk scoring."""
    resource = event.get("resource", {})
    amount = resource.get("amount", {})
    if not amount:
        amount = resource.get("seller_receivable_breakdown", {}).get("gross_amount", {})
    try:
        value = float(amount.get("value", 0))
    except (TypeError, ValueError):
        value = 0.0
    await _record_merchant_metric_event(
        event_id=event.get("id") or f"capture-{resource.get('id', secrets.token_hex(8))}",
        event_type="capture",
        amount=value,
        reason="",
        timestamp=event.get("create_time", datetime.now(timezone.utc).isoformat()),
        merchant_id=merchant_id,
    )
    print(f"[Watcher] Capture {resource.get('id')} — indexing for dispute baseline")


async def _record_merchant_metric_event(
    event_id: str, event_type: str, amount: float, reason: str, timestamp: str,
    merchant_id: str = "",
) -> None:
    """Persist the minimal merchant-scoped aggregates used by the fee-tier estimate."""
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return
    import httpx
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "merchant_id": merchant_id or os.environ.get("MERCHANT_ID", "default"),
        "amount": amount,
        "reason": reason,
        "timestamp": timestamp,
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.put(
                f"{es_url}/anchor-events/_doc/{event_id}",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
    except Exception as e:
        print(f"[Watcher] Could not persist merchant metric event {event_id}: {e}")


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
    # mid is embedded in the approval URL by Guardian; fall back to env var for
    # single-tenant deployments where the URL was generated before this change.
    merchant_id = request.query_params.get("mid") or os.environ.get("MERCHANT_ID", "default")

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
    if tracking.get("tracking_number") and str(tracking.get("status", "")).upper() == "DELIVERED":
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

    # Channel3 catalog data is context only. It is not proof of what this merchant
    # listed or shipped, so it must not be submitted without merchant verification.
    item_name = ""
    items = transaction.get("items") or []
    if items:
        item_name = items[0].get("name", "") or items[0].get("description", "")
    if not item_name:
        item_name = transaction.get("description", "") or f"PayPal dispute {dispute_id}"
    if item_name:
        print(f"[Watcher] Product context available for {dispute_id}; merchant verification required before submission")

    result = await assembler.submit(package) if package.is_sufficient() else None

    action = ActionRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        action_type="dispute_fought" if result else "dispute_pending",
        dispute_id=dispute_id,
        amount=dispute_amount,
        outcome="evidence_submitted" if result else "insufficient_evidence",
        reasoning="Merchant approved via Slack reply",
        requires_attention=result is None,
    )
    await log_action(os.environ.get("MERCHANT_ID", "default"), action)
    print(f"[Watcher] Approved fight executed for {dispute_id}: {result}")


@app.post("/recovery-check")
async def recovery_check(background: BackgroundTasks, request: Request):
    """
    Triggered every 15 minutes by Render cron.
    Requires X-API-Key header matching API_SECRET env var.
    """
    _require_api_key(request)
    background.add_task(_run_recovery_check)
    return JSONResponse({"status": "recovery_check_queued"})


async def _run_recovery_check():
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    recovery = RecoveryAgent()
    result = await recovery.scan_and_recover(merchant_id)
    print(f"[Watcher] Recovery check complete: {result}")


@app.post("/morning-brief")
async def morning_brief(background: BackgroundTasks, request: Request):
    """
    Trigger the morning brief manually, or from Render cron at 8 AM.
    Requires X-API-Key header matching API_SECRET env var.
    """
    _require_api_key(request)
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
async def api_stats(request: Request):
    """Aggregate stats for the dashboard header cards. Requires X-API-Key."""
    _require_api_key(request)
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
async def api_disputes(request: Request):
    """Recent dispute actions for the AG Grid disputes table. Requires X-API-Key."""
    _require_api_key(request)
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
    currency: str = "USD",
):
    """Settlement Oracle — live EV calculation. No API key required."""
    from oracle.decision import calculate, ClaimType, Evidence
    ct_map = {
        "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
        "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
        "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
        "BILLING_ISSUE": ClaimType.BILLING_ISSUE,
    }
    if claim_type.upper() not in ct_map:
        return JSONResponse({"error": "Unsupported claim_type"}, status_code=400)
    ct = ct_map[claim_type.upper()]
    evidence = Evidence(
        has_tracking_number=has_tracking,
        has_delivery_confirmation=has_delivery,
        has_buyer_communication=has_comm,
        has_item_description_proof=has_item_proof,
    )
    try:
        decision = calculate(
            dispute_amount=amount,
            claim_type=ct,
            evidence=evidence,
            deadline_hours=deadline_hours,
            seller_hourly_rate=float(os.environ.get("SELLER_HOURLY_RATE", 50)),
            high_volume_tier=high_volume,
            dispute_fee_override=dispute_fee,
            currency_code=currency,
        )
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
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


# ---------------------------------------------------------------------------
# Live Dispute Simulation — judges can fire a dispute and see Anchor decide
# ---------------------------------------------------------------------------

DEMO_SCENARIOS = {
    "inr_fight": {
        "label": "Item Not Received — AUTO FIGHT",
        "dispute_id": "PP-D-DEMO-001",
        "amount": 245.0,
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery": True,
        "has_comm": False,
        "has_item_proof": False,
        "high_volume": False,
        "deadline_hours": 72,
        "narrative": (
            "Buyer claims item wasn't received. Seller has tracking + delivery confirmation. "
            "The simulator marks delivery proof as present; live actions require PayPal's current HATEOAS links."
        ),
    },
    "micro_accept": {
        "label": "Micro Dispute — AUTO ACCEPT ($18.50)",
        "dispute_id": "PP-D-DEMO-002",
        "amount": 18.50,
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery": False,
        "has_comm": False,
        "has_item_proof": False,
        "high_volume": False,
        "deadline_hours": 72,
        "narrative": (
            "Amount is $18.50 — below the $60 auto-accept threshold. "
            "EV(fight) is negative: costs more to fight than to lose. Anchor auto-accepts immediately."
        ),
    },
    "unauthorized_accept": {
        "label": "Unauthorized Claim — ACCEPT (10% base rate)",
        "dispute_id": "PP-D-DEMO-003",
        "amount": 180.0,
        "claim_type": "UNAUTHORIZED",
        "has_tracking": False,
        "has_delivery": False,
        "has_comm": False,
        "has_item_proof": False,
        "high_volume": False,
        "deadline_hours": 48,
        "narrative": (
            "Unauthorized claims have only a 10% base win rate. Without buyer communication "
            "evidence, EV(fight) = -$155. Anchor accepts; competitors would fight and lose."
        ),
    },
    "high_volume_accept": {
        "label": "High-Volume Tier — $30 Fee Changes the Math",
        "dispute_id": "PP-D-DEMO-004",
        "amount": 95.0,
        "claim_type": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery": False,
        "has_comm": False,
        "has_item_proof": False,
        "high_volume": True,
        "deadline_hours": 72,
        "narrative": (
            "High-volume tier: $30 non-refundable fee changes break-even. "
            "With $15 fee this would FIGHT; with $30 non-refundable it's negative EV. "
            "Anchor knows your tier — competitors don't."
        ),
    },
    "snad_fight": {
        "label": "Significantly Not As Described — FIGHT (with proof)",
        "dispute_id": "PP-D-DEMO-005",
        "amount": 320.0,
        "claim_type": "SIGNIFICANTLY_NOT_AS_DESCRIBED",
        "has_tracking": False,
        "has_delivery": False,
        "has_comm": True,
        "has_item_proof": True,
        "high_volume": False,
        "deadline_hours": 36,
        "narrative": (
            "Buyer claims item not as described. Seller has item listing proof + "
            "buyer communication confirming receipt. Strong evidence → FIGHT. "
            "⚠️ Urgent: 36h deadline."
        ),
    },
}


@app.post("/api/demo/simulate-dispute")
async def demo_simulate_dispute(request: Request):
    """
    Live dispute simulation — no PayPal credentials required.

    POST /api/demo/simulate-dispute
    Body: {"scenario": "inr_fight"} or {"custom": {...dispute fields...}}

    Returns the full Guardian + Oracle decision pipeline output:
    - Oracle EV calculation with reasoning
    - Guardian pre-auth threshold check
    - Recommended autonomous action
    - Simulated PayPal API calls that would be made
    - Morning brief entry that would be logged

    Used by the dashboard "Fire Live Dispute" panel.
    """
    from oracle.decision import calculate, score_evidence, ClaimType, Evidence, Recommendation

    try:
        body = await request.json()
    except Exception:
        body = {}

    scenario_key = body.get("scenario", "inr_fight")

    if scenario_key == "custom":
        s = body.get("custom", {})
        try:
            scenario = {
                "label": "Custom Scenario",
                "dispute_id": str(s.get("dispute_id", "PP-D-CUSTOM-001")),
                "amount": float(s.get("amount", 100.0)),
                "claim_type": str(s.get("claim_type", "ITEM_NOT_RECEIVED")),
                "has_tracking": bool(s.get("has_tracking", False)),
                "has_delivery": bool(s.get("has_delivery", False)),
                "has_comm": bool(s.get("has_comm", False)),
                "has_item_proof": bool(s.get("has_item_proof", False)),
                "high_volume": bool(s.get("high_volume", False)),
                "deadline_hours": int(s.get("deadline_hours", 72)),
                "narrative": "Custom scenario",
            }
        except (TypeError, ValueError):
            return JSONResponse({"error": "Custom amount and deadline_hours must be numeric"}, status_code=400)
        if scenario["claim_type"].upper() not in {
            "ITEM_NOT_RECEIVED", "SIGNIFICANTLY_NOT_AS_DESCRIBED", "UNAUTHORIZED", "BILLING_ISSUE"
        }:
            return JSONResponse({"error": "Unsupported claim_type"}, status_code=400)
    else:
        scenario = DEMO_SCENARIOS.get(scenario_key)
        if scenario is None:
            return JSONResponse(
                {"error": f"Unknown scenario '{scenario_key}'", "available": list(DEMO_SCENARIOS)},
                status_code=400,
            )

    ct_map = {
        "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
        "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
        "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
        "BILLING_ISSUE": ClaimType.BILLING_ISSUE,
    }
    ct = ct_map.get(scenario["claim_type"].upper(), ClaimType.ITEM_NOT_RECEIVED)
    evidence = Evidence(
        has_tracking_number=scenario["has_tracking"],
        has_delivery_confirmation=scenario["has_delivery"],
        has_buyer_communication=scenario["has_comm"],
        has_item_description_proof=scenario["has_item_proof"],
        # Deterministic demo assumptions keep the displayed 91.8% tied to
        # the INR base rate × delivery evidence multiplier, without merchant priors.
        seller_account_age_days=365,
        seller_dispute_win_rate=0.5,
    )
    oracle = calculate(
        dispute_amount=scenario["amount"],
        claim_type=ct,
        evidence=evidence,
        deadline_hours=scenario["deadline_hours"],
        seller_hourly_rate=float(os.environ.get("SELLER_HOURLY_RATE", 50)),
        high_volume_tier=scenario["high_volume"],
    )

    # Guardian pre-auth threshold simulation
    auto_accept_below = float(os.environ.get("AUTO_ACCEPT_BELOW", 60))
    auto_fight_above  = float(os.environ.get("AUTO_FIGHT_ABOVE", 200))
    auto_fight_min_p  = float(os.environ.get("AUTO_FIGHT_MIN_PROB", 0.60))

    if scenario["amount"] <= auto_accept_below:
        guardian_action = "AUTO_ACCEPT"
        guardian_reason = f"Amount ${scenario['amount']:.2f} ≤ auto-accept threshold ${auto_accept_below:.2f}"
    elif (scenario["amount"] >= auto_fight_above and
          oracle.win_probability >= auto_fight_min_p and
          oracle.recommendation == Recommendation.FIGHT):
        guardian_action = "AUTO_FIGHT"
        guardian_reason = (
            f"Amount ${scenario['amount']:.2f} ≥ ${auto_fight_above:.2f} AND "
            f"win probability {oracle.win_probability:.0%} ≥ {auto_fight_min_p:.0%} minimum"
        )
    elif oracle.recommendation == Recommendation.FIGHT:
        guardian_action = "REQUEST_APPROVAL"
        guardian_reason = "Oracle says FIGHT but amount is in the ask-me zone — sending merchant a one-tap approval link"
    elif oracle.recommendation.value == "GATHER_EVIDENCE_FIRST":
        guardian_action = "GATHER_EVIDENCE"
        guardian_reason = "Oracle says gather evidence first — sending merchant a checklist"
    else:
        guardian_action = "AUTO_ACCEPT"
        guardian_reason = "Oracle EV(fight) ≤ 0 — auto-accepting claim"

    # Simulated PayPal API calls
    simulated_calls = []
    if guardian_action == "AUTO_ACCEPT":
        simulated_calls = [
            {"method": "GET",  "path": f"/v1/customer/disputes/{scenario['dispute_id']}", "purpose": "Fetch HATEOAS links to verify accept-claim is permitted"},
            {"method": "POST", "path": f"/v1/customer/disputes/{scenario['dispute_id']}/accept-claim", "purpose": "Accept the claim — Oracle confirmed negative EV to fight"},
        ]
    elif guardian_action == "AUTO_FIGHT":
        simulated_calls = [
            {"method": "GET",  "path": f"/v1/customer/disputes/{scenario['dispute_id']}", "purpose": "Fetch HATEOAS links to verify provide-evidence is permitted"},
            {"method": "POST", "path": f"/v1/customer/disputes/{scenario['dispute_id']}/provide-evidence", "purpose": "Submit tracking + delivery evidence"},
        ]
    elif guardian_action == "REQUEST_APPROVAL":
        simulated_calls = [
            {"method": "GET",  "path": f"/v1/customer/disputes/{scenario['dispute_id']}", "purpose": "Fetch HATEOAS links"},
            {"method": "POST", "path": "/slack-webhook", "purpose": "Send merchant a one-tap approve/reject link (48h HMAC token, dispute-bound)"},
        ]

    # What would appear in the morning brief
    brief_entry = {
        "action": guardian_action,
        "dispute_id": scenario["dispute_id"],
        "amount": f"${scenario['amount']:.2f}",
        "claim_type": scenario["claim_type"],
        "win_probability": f"{oracle.win_probability:.0%}",
        "ev_fight": f"${oracle.ev_fight:.2f}",
        "brief_line": (
            f"{'⚔️ Fought' if guardian_action == 'AUTO_FIGHT' else '✅ Accepted' if guardian_action == 'AUTO_ACCEPT' else '🔔 Pending'} "
            f"{scenario['dispute_id']} ${scenario['amount']:.2f} "
            f"({oracle.win_probability:.0%} win probability, EV={oracle.ev_fight:+.2f})"
        ),
    }

    return JSONResponse({
        "scenario": {
            "key": scenario_key,
            "label": scenario["label"],
            "dispute_id": scenario["dispute_id"],
            "amount": scenario["amount"],
            "claim_type": scenario["claim_type"],
            "narrative": scenario.get("narrative", ""),
            "inputs": {
                "has_tracking": scenario["has_tracking"],
                "has_delivery": scenario["has_delivery"],
                "has_buyer_comm": scenario["has_comm"],
                "has_item_proof": scenario["has_item_proof"],
                "high_volume_tier": scenario["high_volume"],
                "deadline_hours": scenario["deadline_hours"],
            },
        },
        "oracle": {
            "recommendation": oracle.recommendation.value,
            "ev_fight": round(oracle.ev_fight, 2),
            "ev_accept": round(oracle.ev_accept, 2),
            "win_probability": round(oracle.win_probability, 4),
            "break_even": round(oracle.break_even_probability, 4),
            "confidence": round(oracle.confidence, 4),
            "evidence_multiplier": round(score_evidence(evidence, ct), 4),
            "reasoning": oracle.reasoning,
        },
        "guardian": {
            "action": guardian_action,
            "reason": guardian_reason,
            "thresholds": {
                "auto_accept_below": auto_accept_below,
                "auto_fight_above": auto_fight_above,
                "auto_fight_min_prob": auto_fight_min_p,
            },
        },
        "simulated_paypal_calls": simulated_calls,
        "morning_brief_entry": brief_entry,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/demo/scenarios")
async def demo_scenarios():
    """List of available demo scenarios for the dashboard simulator."""
    return JSONResponse({
        "scenarios": [
            {"key": k, "label": v["label"], "amount": v["amount"],
             "claim_type": v["claim_type"], "narrative": v.get("narrative", "")}
            for k, v in DEMO_SCENARIOS.items()
        ]
    })
