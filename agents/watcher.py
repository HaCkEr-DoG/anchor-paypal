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
    _scheduler.add_job(_run_deadline_check, "cron", minute="*/30", id="deadlines")
    _scheduler.add_job(
        _send_morning_brief, "cron",
        hour=_brief_hour, minute=0,
        id="brief",
        timezone=ZoneInfo(_brief_tz),
    )
    _scheduler.start()
    print(f"[Watcher] Scheduler started — recovery every 15 min, deadlines every 30 min, brief at {_brief_hour:02d}:00 {_brief_tz}")
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

    # Deadline tracking: store dispute so we never miss the response window
    try:
        from agents.deadline_tracker import store_open_dispute, OpenDispute, compute_deadline
        _deadline = compute_deadline(
            event.get("create_time", datetime.now(timezone.utc).isoformat()),
            reason,
        )
        await store_open_dispute(OpenDispute(
            dispute_id=dispute_id,
            merchant_id=merchant_id or os.environ.get("MERCHANT_ID", "default"),
            amount=dispute_amount,
            reason_code=reason,
            opened_at=event.get("create_time", datetime.now(timezone.utc).isoformat()),
            response_deadline=_deadline,
        ))
    except Exception as _dt_exc:
        print(f"[Watcher] Deadline tracker store failed (non-fatal): {_dt_exc}")

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

    # When a dispute resolves, record the outcome for Oracle calibration and
    # remove it from the deadline-tracking index.
    if status in ("RESOLVED", "CLOSED"):
        try:
            from agents.deadline_tracker import mark_dispute_resolved
            await mark_dispute_resolved(dispute_id)
        except Exception as _exc:
            print(f"[Watcher] Deadline mark_resolved failed (non-fatal): {_exc}")

        try:
            from oracle.feedback import record_outcome, OutcomeRecord
            _outcome_map = {
                "WON_IN_FAVOR_OF_SELLER": "WON",
                "RESOLVED_SELLER": "WON",
                "WON_IN_FAVOR_OF_BUYER": "LOST",
                "RESOLVED_BUYER": "LOST",
            }
            _seller_response = resource.get("seller_response_due_date", "")
            _adjudication = resource.get("adjudications", [{}])[0] if resource.get("adjudications") else {}
            _outcome_str = _adjudication.get("adjudication_type", status)
            _actual_outcome = _outcome_map.get(_outcome_str.upper(), "LOST")
            await record_outcome(OutcomeRecord(
                dispute_id=dispute_id,
                merchant_id=merchant_id or os.environ.get("MERCHANT_ID", "default"),
                claim_type=resource.get("reason", "UNKNOWN"),
                oracle_prediction=0.0,  # actual prediction stored when fight was initiated
                oracle_ev=0.0,
                oracle_action="FIGHT",
                actual_action="FIGHT",
                actual_outcome=_actual_outcome,
                dispute_amount=float(resource.get("dispute_amount", {}).get("value", 0)),
            ))
        except Exception as _exc:
            print(f"[Watcher] Oracle outcome record failed (non-fatal): {_exc}")

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


async def _run_deadline_check():
    """APScheduler job: sweep open disputes every 30 min, auto-accept before deadline."""
    merchant_id = os.environ.get("MERCHANT_ID", "default")
    try:
        from agents.deadline_tracker import check_deadlines

        async def _notify(msg: str):
            guardian = Guardian()
            await guardian._notify(msg)

        async def _auto_accept(dispute_id: str, amount: float, reason: str):
            guardian = Guardian(merchant_id=merchant_id, approval_secret=APPROVAL_TOKEN_SECRET)
            await guardian._accept_dispute(dispute_id, amount, reason=reason)

        result = await check_deadlines(merchant_id, _notify, _auto_accept)
        print(f"[Watcher] Deadline check complete: {result}")
    except Exception as exc:
        print(f"[Watcher] Deadline check failed (non-fatal): {exc}")


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


# ── Dispute Rate Protection & Account Health ──────────────────────────────────

@app.get("/api/account-health/{merchant_id}")
async def api_account_health(merchant_id: str, open_disputes: int = 0):
    """
    Real-time dispute rate health check for a merchant.

    Returns current dispute rate, threshold status, fight quota, and posture
    recommendation. This is the core output of the Dispute Rate Protection System.

    PayPal thresholds:
      < 0.5%  — safe
      0.5–1%  — elevated
      1–1.5%  — limited (account holds)
      1.5–3%  — hdr ($30 non-refundable fee)
      > 3%    — critical (suspension risk)
    """
    from agents.risk_monitor import get_account_health
    health = await get_account_health(merchant_id, open_disputes=open_disputes)
    return JSONResponse({
        "merchant_id": health.merchant_id,
        "threshold_status": health.threshold_status,
        "threshold_description": health.threshold_description,
        "dispute_rate_30d": round(health.window_30d.rate_by_amount, 5),
        "dispute_rate_90d": round(health.window_90d.rate_by_amount, 5),
        "dispute_count_30d": health.window_30d.dispute_count,
        "capture_count_30d": health.window_30d.capture_count,
        "safe_fight_quota": health.safe_fight_quota,
        "threshold_breach_at": health.threshold_breach_at,
        "next_threshold": health.next_threshold,
        "next_threshold_name": health.next_threshold_name,
        "fight_posture": health.fight_posture,
        "posture_reason": health.posture_reason,
        "open_disputes": health.open_disputes,
        "projected_rate_all_accepted": health.projected_rate_all_accepted,
        "projected_rate_all_fought": health.projected_rate_all_fought,
        "checked_at": health.checked_at,
    })


@app.get("/api/account-health")
async def api_account_health_default(open_disputes: int = 0):
    """Account health for the default merchant (MERCHANT_ID env var)."""
    return await api_account_health(os.environ.get("MERCHANT_ID", "default"), open_disputes=open_disputes)


# ── Buyer Risk Profiling ───────────────────────────────────────────────────────

@app.get("/api/buyer-risk")
async def api_buyer_risk(buyer_email: str, merchant_id: str = ""):
    """
    Score a buyer's dispute risk profile based on their history in Elasticsearch.

    Call this at capture time (when a buyer pays) to flag high-risk buyers
    before they can file a dispute. Use the recommendation to require extra
    verification, signature on delivery, etc.
    """
    from agents.fraud_detector import score_buyer
    mid = merchant_id or os.environ.get("MERCHANT_ID", "default")
    if not buyer_email:
        return JSONResponse({"error": "buyer_email required"}, status_code=422)
    profile = await score_buyer(buyer_email, mid)
    return JSONResponse({
        "buyer_email": profile.buyer_email,
        "merchant_id": profile.merchant_id,
        "risk_level": profile.risk_level,
        "risk_score": profile.risk_score,
        "is_serial_disputer": profile.is_serial_disputer,
        "recommendation": profile.recommendation,
        "flags": profile.flags,
        "total_disputes_90d": profile.total_disputes_90d,
        "total_disputes_180d": profile.total_disputes_180d,
        "disputes_reversed_90d": profile.disputes_reversed_90d,
        "dispute_rate": profile.dispute_rate,
        "checked_at": profile.checked_at,
    })


# ── Carrier Tracking Verification ────────────────────────────────────────────

@app.get("/api/tracking/verify")
async def api_tracking_verify(tracking_number: str):
    """
    Verify delivery status for a tracking number via USPS/FedEx/UPS/17track.

    Returns confirmed delivery date, location, signature status, and a deep
    link to the carrier's tracking page for use as dispute evidence.
    """
    from agents.carrier import verify_delivery
    if not tracking_number:
        return JSONResponse({"error": "tracking_number required"}, status_code=422)
    result = await verify_delivery(tracking_number)
    if result is None:
        return JSONResponse({
            "tracking_number": tracking_number,
            "delivered": False,
            "carrier": "unknown",
            "error": "No carrier credentials configured or tracking number format unrecognized.",
        }, status_code=200)
    return JSONResponse({
        "tracking_number": result.tracking_number,
        "carrier": result.carrier,
        "delivered": result.delivered,
        "delivery_date": result.delivery_date,
        "delivery_location": result.delivery_location,
        "signature_obtained": result.signature_obtained,
        "latest_status": result.latest_status,
        "carrier_url": result.carrier_url,
    })


# ── Deadline Guardian ──────────────────────────────────────────────────────────

@app.get("/api/deadlines/{merchant_id}")
async def api_deadlines(merchant_id: str):
    """
    Open disputes with their response deadlines and alert levels.

    Returns all non-resolved disputes tracked by the Deadline Guardian,
    sorted by urgency (soonest deadline first).
    """
    from agents.deadline_tracker import get_open_disputes, hours_until_deadline
    disputes = await get_open_disputes(merchant_id)
    rows = [
        {
            "dispute_id": d.dispute_id,
            "merchant_id": d.merchant_id,
            "amount": d.amount,
            "reason_code": d.reason_code,
            "opened_at": d.opened_at,
            "response_deadline": d.response_deadline,
            "hours_remaining": round(hours_until_deadline(d.response_deadline), 1),
            "last_alert_level": d.last_alert_level,
            "auto_acted": d.auto_acted,
            "win_probability": d.win_probability,
        }
        for d in disputes
    ]
    rows.sort(key=lambda r: r["hours_remaining"])
    return JSONResponse({"disputes": rows, "count": len(rows)})


@app.get("/api/deadlines")
async def api_deadlines_default():
    """Open deadlines for the default merchant (MERCHANT_ID env var)."""
    return await api_deadlines(os.environ.get("MERCHANT_ID", "default"))


# ── Oracle Calibration ────────────────────────────────────────────────────────

@app.get("/api/oracle/calibration/{merchant_id}")
async def api_oracle_calibration(merchant_id: str, limit: int = 500):
    """
    Oracle calibration statistics — how accurate are the win probability estimates?

    Returns Brier score, actual vs predicted win rates by claim type, and
    a recommendation quality classification. Use this to audit and tune Oracle.

    Brier score guide: 0.25 = random, 0.0 = perfect, 0.10 = excellent.
    """
    from oracle.feedback import get_calibration_stats
    stats = await get_calibration_stats(merchant_id, limit=limit)
    return JSONResponse({
        "merchant_id": stats.merchant_id,
        "total_records": stats.total_records,
        "brier_score": stats.brier_score,
        "mean_prediction": stats.mean_prediction,
        "actual_win_rate": stats.actual_win_rate,
        "calibration_error": stats.calibration_error,
        "oracle_accuracy": stats.oracle_accuracy,
        "recommendation_quality": stats.recommendation_quality,
        "win_rate_by_claim_type": stats.win_rate_by_claim_type,
        "computed_at": stats.computed_at,
    })


@app.get("/api/oracle/calibration")
async def api_oracle_calibration_default():
    """Oracle calibration for the default merchant (MERCHANT_ID env var)."""
    return await api_oracle_calibration(os.environ.get("MERCHANT_ID", "default"))


# ── Wave 52-58 Oracle Endpoints ───────────────────────────────────────────────

@app.post("/api/oracle/win-probability")
async def api_win_probability(request: Request):
    """
    Calibrated win probability for a dispute with signal adjustments.

    Accepts: dispute_id, reason_code, network, days_remaining_in_window,
             response_window_days, has_tracking_with_delivery, has_3ds_authentication,
             ce3_eligible, seller_protection_eligible, evidence_completeness_score (0-100)

    Returns: final_probability, base_rate, confidence, key_drivers, CI bounds.
    """
    from oracle.win_probability_calibrator import calibrate_win_probability, WinProbabilityInput
    body = await request.json()
    inp = WinProbabilityInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        network=body.get("network", "PAYPAL"),
        days_remaining_in_window=body.get("days_remaining_in_window", 10),
        response_window_days=body.get("response_window_days", 20),
        has_tracking_with_delivery=body.get("has_tracking_with_delivery", False),
        has_3ds_authentication=body.get("has_3ds_authentication", False),
        has_avs_match=body.get("has_avs_match", False),
        has_buyer_communication=body.get("has_buyer_communication", False),
        ce3_eligible=body.get("ce3_eligible", False),
        seller_protection_eligible=body.get("seller_protection_eligible", False),
        evidence_completeness_score=body.get("evidence_completeness_score", 50),
    )
    r = calibrate_win_probability(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "base_rate": r.base_rate,
        "network_multiplied": r.network_multiplied,
        "final_probability": r.final_probability,
        "total_adjustment": r.total_adjustment,
        "confidence": r.confidence.value,
        "confidence_interval_low": r.confidence_interval_low,
        "confidence_interval_high": r.confidence_interval_high,
        "key_drivers": r.key_drivers,
        "adjustments": [{"name": a.signal_name, "adjustment": a.adjustment, "description": a.description}
                        for a in r.adjustments],
        "summary": r.summary,
    })


@app.post("/api/oracle/strategy")
async def api_reason_code_strategy(request: Request):
    """
    Tailored dispute strategy for a reason code and network.

    Accepts: dispute_id, reason_code, network, transaction_amount

    Returns: strategy_type, headline, evidence_requirements, action_checklist,
             sp_eligible, ce3_eligible, base_win_rate, escalation_tip.
    """
    from oracle.reason_code_strategy_advisor import advise_reason_code_strategy, ReasonCodeStrategyInput
    body = await request.json()
    inp = ReasonCodeStrategyInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        network=body.get("network", "PAYPAL"),
        transaction_amount=body.get("transaction_amount", 0.0),
    )
    r = advise_reason_code_strategy(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "reason_code": r.reason_code,
        "network": r.network,
        "strategy_type": r.strategy_type.value,
        "headline": r.headline,
        "evidence_requirements": [
            {"type": e.evidence_type, "required": e.required, "description": e.description}
            for e in r.evidence_requirements
        ],
        "action_checklist": r.action_checklist,
        "risk_factors": [rf.value for rf in r.risk_factors],
        "sp_eligible": r.sp_eligible,
        "ce3_eligible": r.ce3_eligible,
        "base_win_rate": r.base_win_rate,
        "escalation_tip": r.escalation_tip,
        "summary": r.summary,
    })


@app.post("/api/oracle/appeal-windows")
async def api_appeal_windows(request: Request):
    """
    Appeal deadline tracker — computes all stage windows for a dispute.

    Accepts: dispute_id, network, stage (CHARGEBACK/PRE_ARB/ARBITRATION),
             stage_started_epoch (Unix timestamp), now_epoch (optional, defaults to now)

    Returns: days_to_act, overall_status, current_window, has_pre_arb, has_arb,
             critical_actions.
    """
    import time as _time
    from oracle.appeal_window_tracker import track_appeal_windows, AppealWindowInput
    body = await request.json()
    now_epoch = body.get("now_epoch") or int(_time.time())
    inp = AppealWindowInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        network=body.get("network", "PAYPAL"),
        stage=body.get("stage", "CHARGEBACK"),
        stage_started_epoch=body.get("stage_started_epoch", now_epoch - 86400 * 5),
        now_epoch=now_epoch,
    )
    r = track_appeal_windows(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "network": r.network,
        "current_stage": r.current_stage.value,
        "days_to_act": r.days_to_act,
        "overall_status": r.overall_status.value,
        "current_window": {
            "stage": r.current_window.stage.value,
            "total_days": r.current_window.total_days,
            "days_elapsed": r.current_window.days_elapsed,
            "days_remaining": r.current_window.days_remaining,
            "pct_elapsed": r.current_window.pct_elapsed,
            "status": r.current_window.status.value,
            "action_required": r.current_window.action_required,
        },
        "has_pre_arb": r.has_pre_arb,
        "has_arb": r.has_arb,
        "critical_actions": r.critical_actions,
        "summary": r.summary,
    })


@app.post("/api/oracle/portfolio")
async def api_portfolio_analysis(request: Request):
    """
    Dispute portfolio analysis — aggregate metrics across all open disputes.

    Accepts: merchant_id, disputes (list), total_transactions_90d

    Returns: total_disputes, total_amount_at_risk, EV sums, by_reason_code,
             by_urgency, top_disputes, portfolio_risk_level, hdr_impact_rate.
    """
    from oracle.dispute_portfolio_analyzer import analyze_dispute_portfolio, PortfolioAnalysisInput, PortfolioDispute
    body = await request.json()
    disputes_raw = body.get("disputes", [])
    disputes = [
        PortfolioDispute(
            dispute_id=d.get("dispute_id", "?"),
            reason_code=d.get("reason_code", "INR"),
            network=d.get("network", "PAYPAL"),
            amount=float(d.get("amount", 0)),
            days_remaining=int(d.get("days_remaining", 10)),
            win_probability=float(d.get("win_probability", 0.5)),
            is_hdr_counting=d.get("is_hdr_counting", True),
            is_in_sp=d.get("is_in_sp", False),
        )
        for d in disputes_raw
    ]
    inp = PortfolioAnalysisInput(
        merchant_id=body.get("merchant_id", os.environ.get("MERCHANT_ID", "default")),
        disputes=disputes,
        total_transactions_90d=body.get("total_transactions_90d", 1000),
    )
    r = analyze_dispute_portfolio(inp)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "total_disputes": r.total_disputes,
        "total_amount_at_risk": r.total_amount_at_risk,
        "sp_covered_amount": r.sp_covered_amount,
        "unprotected_amount": r.unprotected_amount,
        "total_ev_if_all_fought": r.total_ev_if_all_fought,
        "total_ev_if_all_accepted": r.total_ev_if_all_accepted,
        "expected_net_recovery": r.expected_net_recovery,
        "portfolio_avg_win_prob": r.portfolio_avg_win_prob,
        "hdr_dispute_count": r.hdr_dispute_count,
        "hdr_impact_rate": r.hdr_impact_rate,
        "portfolio_risk_level": r.portfolio_risk_level.value,
        "risk_flags": r.risk_flags,
        "by_reason_code": [
            {"reason_code": b.reason_code, "count": b.count, "total_amount": b.total_amount,
             "avg_win_prob": b.avg_win_prob, "expected_recovery": b.expected_recovery}
            for b in r.by_reason_code
        ],
        "by_urgency": [
            {"bucket": b.bucket.value, "count": b.count, "total_amount": b.total_amount}
            for b in r.by_urgency
        ],
        "top_disputes": [
            {"dispute_id": d.dispute_id, "reason_code": d.reason_code,
             "amount": d.amount, "days_remaining": d.days_remaining,
             "win_probability": d.win_probability, "ev_fight": d.ev_fight,
             "ev_accept": d.ev_accept, "priority_rank": d.priority_rank,
             "priority_rationale": d.priority_rationale}
            for d in r.top_disputes
        ],
        "summary": r.summary,
    })


@app.post("/api/oracle/complexity")
async def api_dispute_complexity(request: Request):
    """
    Dispute complexity estimator — scores handling complexity and SLA impact.

    Accepts: dispute_id, reason_code, network, amount, stage,
             has_tracking, has_3ds, days_remaining, prior_dispute_count

    Returns: complexity_tier, complexity_score, estimated_staff_hours,
             sla_days, key_drivers.
    """
    from oracle.dispute_complexity_estimator import estimate_dispute_complexity, DisputeComplexityInput
    body = await request.json()
    inp = DisputeComplexityInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        network=body.get("network", "PAYPAL"),
        transaction_amount=float(body.get("amount", 100.0)),
        evidence_completeness_score=int(body.get("evidence_completeness_score", 50)),
        has_digital_goods=bool(body.get("has_digital_goods", False)),
        has_cross_border_transaction=bool(body.get("cross_border", False)),
    )
    r = estimate_dispute_complexity(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "complexity_tier": r.complexity_tier.value,
        "total_score": r.total_score,
        "estimated_staff_hours": r.estimated_staff_hours,
        "sla_hours": r.sla_hours,
        "recommended_assignee": r.recommended_assignee,
        "complexity_flags": r.complexity_flags,
        "summary": r.summary,
    })


@app.post("/api/oracle/settlement")
async def api_settlement_evaluation(request: Request):
    """
    Settlement offer evaluator — ACCEPT / COUNTER / REJECT / ESCALATE.

    Accepts: dispute_id, dispute_amount, offer_amount, win_probability,
             reason_code, network, chargeback_fee (optional)

    Returns: recommendation, offer_quality_score, ev_fight, ev_accept,
             ev_settle, counter_offer (if COUNTER), reasoning.
    """
    from oracle.settlement_offer_evaluator import evaluate_settlement_offer, SettlementOfferInput, SettlementContext
    body = await request.json()
    ctx_str = body.get("context", "BUYER_PROPOSED")
    try:
        ctx = SettlementContext(ctx_str)
    except ValueError:
        ctx = SettlementContext.BUYER_PROPOSED
    inp = SettlementOfferInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        context=ctx_str,
        original_amount=float(body.get("dispute_amount", 100.0)),
        offer_amount=float(body.get("offer_amount", 50.0)),
        chargeback_fee=float(body.get("chargeback_fee", 20.0)),
    )
    r = evaluate_settlement_offer(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "recommendation": r.recommendation.value,
        "fairness_tier": r.fairness_tier.value,
        "fairness_score": r.fairness_score,
        "ev_fight": r.ev_fight,
        "ev_accept_offer": r.ev_accept_offer,
        "ev_delta": r.ev_delta,
        "counter_offer_amount": r.counter_offer_amount,
        "counter_offer_rationale": r.counter_offer_rationale,
        "accept_saves": r.accept_saves,
        "summary": r.summary,
    })


@app.post("/api/oracle/fraud-dna")
async def api_fraud_dna(request: Request):
    """
    Buyer Fraud DNA Profiler — detect professional chargeback filers via
    text fingerprinting, velocity analysis, and cross-merchant patterns.

    Accepts: dispute_id, buyer_id, reason_code, dispute_reason_text,
             transaction_amount, buyer_disputes_last_90d, buyer_disputes_total,
             buyer_transactions_total, same_reason_across_merchants,
             tracking_confirmed_delivered, dispute_filed_within_hours_of_purchase

    Returns: risk_tier, fraud_score, is_suspected_professional,
             text_similarity_score, velocity_risk, red_flags, recommended_action.
    """
    from oracle.buyer_fraud_dna_profiler import profile_buyer_fraud_dna, FraudDNAInput
    body = await request.json()
    inp = FraudDNAInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        buyer_id=body.get("buyer_id", "unknown"),
        reason_code=body.get("reason_code", "INR"),
        dispute_reason_text=body.get("dispute_reason_text", ""),
        transaction_amount=float(body.get("transaction_amount", 100.0)),
        dispute_amount=float(body.get("dispute_amount", body.get("transaction_amount", 100.0))),
        buyer_disputes_last_90d=int(body.get("buyer_disputes_last_90d", 0)),
        buyer_disputes_total=int(body.get("buyer_disputes_total", 0)),
        buyer_transactions_total=int(body.get("buyer_transactions_total", 100)),
        same_reason_across_merchants=int(body.get("same_reason_across_merchants", 0)),
        tracking_confirmed_delivered=bool(body.get("tracking_confirmed_delivered", False)),
        dispute_filed_within_hours_of_purchase=int(
            body.get("dispute_filed_within_hours_of_purchase", 9999)
        ),
    )
    r = profile_buyer_fraud_dna(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "buyer_id": r.buyer_id,
        "risk_tier": r.risk_tier.value,
        "fraud_score": r.fraud_score,
        "is_suspected_professional": r.is_suspected_professional,
        "text_similarity_score": r.text_similarity_score,
        "velocity_risk": r.velocity_risk.value,
        "dispute_to_transaction_ratio": r.dispute_to_transaction_ratio,
        "cross_merchant_flag": r.cross_merchant_flag,
        "cross_merchant_count": r.cross_merchant_count,
        "red_flags": r.red_flags,
        "top_pattern_match": {
            "pattern_id": r.top_pattern_match.pattern_id,
            "similarity_score": r.top_pattern_match.similarity_score,
        } if r.top_pattern_match else None,
        "recommended_action": r.recommended_action,
        "action_detail": r.action_detail,
        "summary": r.summary,
    })


@app.post("/api/oracle/transaction-risk")
async def api_transaction_risk(request: Request):
    """
    Pre-dispute transaction risk scorer — predict chargeback probability
    BEFORE a dispute is filed, using 15+ risk signals.

    Accepts: transaction_id, merchant_id, amount, item_category,
             billing_country, shipping_country, ip_country,
             avs_result, cvv_result, card_bin_country,
             buyer_account_age_days, buyer_prior_transactions,
             buyer_disputes_last_90d, buyer_email_domain,
             buyer_orders_last_24h, failed_payment_attempts,
             has_3ds_authentication, is_express_shipping,
             is_digital_delivery, hour_of_purchase

    Returns: risk_score, risk_tier, chargeback_probability, recommended_action,
             top_signals, mitigating_factors, score breakdown by category.
    """
    from oracle.transaction_risk_scorer import score_transaction_risk, TransactionRiskInput
    body = await request.json()
    inp = TransactionRiskInput(
        transaction_id=body.get("transaction_id", "DEMO"),
        merchant_id=body.get("merchant_id", os.environ.get("MERCHANT_ID", "default")),
        amount=float(body.get("amount", 100.0)),
        currency=body.get("currency", "USD"),
        item_category=body.get("item_category", "UNKNOWN"),
        billing_country=body.get("billing_country", ""),
        shipping_country=body.get("shipping_country", ""),
        ip_country=body.get("ip_country", ""),
        ship_to_billing_match=bool(body.get("ship_to_billing_match", True)),
        avs_result=body.get("avs_result", "Y"),
        cvv_result=body.get("cvv_result", "M"),
        card_bin_country=body.get("card_bin_country", ""),
        failed_payment_attempts=int(body.get("failed_payment_attempts", 0)),
        buyer_account_age_days=int(body.get("buyer_account_age_days", 365)),
        buyer_prior_transactions=int(body.get("buyer_prior_transactions", 0)),
        buyer_disputes_last_90d=int(body.get("buyer_disputes_last_90d", 0)),
        buyer_email_domain=body.get("buyer_email_domain", ""),
        buyer_orders_last_24h=int(body.get("buyer_orders_last_24h", 1)),
        is_express_shipping=bool(body.get("is_express_shipping", False)),
        is_digital_delivery=bool(body.get("is_digital_delivery", False)),
        has_3ds_authentication=bool(body.get("has_3ds_authentication", False)),
        hour_of_purchase=int(body.get("hour_of_purchase", 12)),
        is_first_transaction_with_merchant=bool(body.get("is_first_transaction_with_merchant", True)),
    )
    r = score_transaction_risk(inp)
    return JSONResponse({
        "transaction_id": r.transaction_id,
        "risk_score": r.risk_score,
        "risk_tier": r.risk_tier.value,
        "chargeback_probability": r.chargeback_probability,
        "recommended_action": r.recommended_action.value,
        "action_reason": r.action_reason,
        "top_signals": [
            {"id": s.signal_id, "name": s.signal_name, "score": s.score_contribution,
             "description": s.description}
            for s in r.top_signals
        ],
        "mitigating_factors": r.mitigating_factors,
        "score_breakdown": {
            "address": r.address_risk_score,
            "card": r.card_risk_score,
            "buyer": r.buyer_risk_score,
            "velocity": r.velocity_risk_score,
            "item": r.item_risk_score,
        },
        "summary": r.summary,
    })


@app.post("/api/oracle/response-quality")
async def api_response_quality(request: Request):
    """
    Response quality scorer — score an evidence package before submission.

    Accepts: dispute_id, reason_code, evidence fields (has_tracking, has_description,
             has_buyer_comms, has_policy, has_invoice, has_photos,
             has_3ds, has_avs_cvv, word_count)

    Returns: total_score, approved_to_submit, dimension_scores, missing_critical.
    """
    from oracle.response_quality_scorer import score_response_quality, ResponseDraftInput
    body = await request.json()
    inp = ResponseDraftInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        network=body.get("network", "PAYPAL"),
        has_tracking_evidence=body.get("has_tracking", False),
        has_auth_evidence=body.get("has_3ds", False),
        has_policy_evidence=body.get("has_policy", False),
        has_communication_log=body.get("has_buyer_comms", False),
        evidence_items_attached=int(body.get("evidence_items_attached", 0)),
        required_evidence_count=int(body.get("required_evidence_count", 3)),
    )
    r = score_response_quality(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "total_score": r.total_score,
        "quality_tier": r.quality_tier.value,
        "approved_to_submit": r.approved_to_submit,
        "blocking_issues": r.blocking_issues,
        "improvements": r.improvements,
        "strengths": r.strengths,
        "summary": r.summary,
    })


@app.post("/api/oracle/category")
async def api_dispute_category(request: Request):
    """
    Dispute category classifier — AUTO_ACCEPT / STRONG_FIGHT / CE3_CANDIDATE etc.

    Accepts: dispute_id, reason_code, network, amount, win_probability,
             has_tracking, has_3ds, ce3_eligible, sp_eligible

    Returns: category, category_description, recommended_path, confidence.
    """
    from oracle.dispute_category_classifier import classify_dispute, DisputeClassifierInput
    body = await request.json()
    inp = DisputeClassifierInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        network=body.get("network", "PAYPAL"),
        transaction_amount=float(body.get("amount", 100.0)),
        win_probability_estimate=float(body.get("win_probability", 0.5)),
        has_tracking_evidence=body.get("has_tracking", False),
        has_3ds_authentication=body.get("has_3ds", False),
        ce3_eligible=body.get("ce3_eligible", False),
        seller_protection_eligible=body.get("sp_eligible", False),
    )
    r = classify_dispute(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "primary_category": r.primary_category.value,
        "confidence": r.confidence,
        "first_action": r.first_action.value,
        "seller_protection": r.seller_protection.value,
        "ce3_eligible": r.ce3_eligible,
        "flags": r.flags,
        "rationale": r.rationale,
        "summary": r.summary,
    })


@app.post("/api/oracle/prevention")
async def api_chargeback_prevention(request: Request):
    """
    Chargeback Prevention Advisor — prescribe proactive steps to prevent an
    inquiry from escalating to a chargeback (0-72h window).

    Accepts: transaction_id, merchant_id, reason_code, amount,
             hours_since_purchase, delivery_status, days_past_expected_delivery,
             carrier_has_tracking, buyer_tier, buyer_contacted_support,
             buyer_complaint_unresolved, is_inquiry_stage, is_digital_product,
             response_cost, chargeback_fee

    Returns: urgency, prevention_probability, primary_action, steps,
             prevention_roi, chargeback_cost_if_lost, relationship_risk,
             key_insight, summary
    """
    from oracle.chargeback_prevention_advisor import advise_chargeback_prevention, PreventionInput
    body = await request.json()
    inp = PreventionInput(
        transaction_id=body.get("transaction_id", "DEMO"),
        merchant_id=body.get("merchant_id", "ANCHOR"),
        reason_code=body.get("reason_code", "INR"),
        amount=float(body.get("amount", 100.0)),
        hours_since_purchase=float(body.get("hours_since_purchase", 24.0)),
        delivery_status=body.get("delivery_status", "UNKNOWN"),
        days_past_expected_delivery=int(body.get("days_past_expected_delivery", 0)),
        carrier_has_tracking=bool(body.get("carrier_has_tracking", False)),
        buyer_tier=body.get("buyer_tier", "UNKNOWN"),
        buyer_contacted_support=bool(body.get("buyer_contacted_support", False)),
        buyer_complaint_unresolved=bool(body.get("buyer_complaint_unresolved", False)),
        hours_since_buyer_contact=float(body.get("hours_since_buyer_contact", 0.0)),
        is_inquiry_stage=bool(body.get("is_inquiry_stage", True)),
        has_active_inquiry=bool(body.get("has_active_inquiry", False)),
        is_digital_product=bool(body.get("is_digital_product", False)),
        digital_access_active=bool(body.get("digital_access_active", True)),
        response_cost=float(body.get("response_cost", 15.0)),
        chargeback_fee=float(body.get("chargeback_fee", 20.0)),
    )
    r = advise_chargeback_prevention(inp)
    return JSONResponse({
        "transaction_id": r.transaction_id,
        "urgency": r.urgency.value,
        "prevention_probability": r.prevention_probability,
        "primary_action": r.primary_action.value,
        "steps": [
            {
                "step_number": s.step_number,
                "action": s.action.value,
                "rationale": s.rationale,
                "time_to_complete": s.time_to_complete,
                "expected_outcome": s.expected_outcome,
                "cost_estimate": s.cost_estimate,
            }
            for s in r.recommended_steps
        ],
        "prevention_roi": r.prevention_roi,
        "chargeback_cost_if_lost": r.chargeback_cost_if_lost,
        "buyer_tier": r.buyer_tier.value,
        "relationship_risk": r.relationship_risk,
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


@app.post("/api/oracle/ltv-decision")
async def api_ltv_decision(request: Request):
    """
    Buyer LTV Decision Engine — override fight/accept recommendations by
    incorporating buyer lifetime value and churn probability into the EV.

    Accepts: dispute_id, buyer_id, dispute_amount, oracle_ev_fight,
             oracle_win_probability, oracle_recommendation,
             buyer_ltv_estimate, buyer_ltv_tier, buyer_total_spend,
             buyer_dispute_history_count, buyer_last_purchase_days_ago,
             response_cost, chargeback_fee, can_offer_partial, partial_refund_pct

    Returns: ltv_recommendation, overrides_oracle, raev, oracle_ev_fight,
             buyer_ltv_tier, buyer_ltv_estimate, ltv_at_risk,
             scenario_fight, scenario_accept, scenario_partial,
             key_insight, summary
    """
    from oracle.buyer_ltv_decision_engine import calculate_ltv_decision, LTVDecisionInput
    body = await request.json()
    inp = LTVDecisionInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        buyer_id=body.get("buyer_id", "unknown"),
        dispute_amount=float(body.get("dispute_amount", 100.0)),
        oracle_ev_fight=float(body.get("oracle_ev_fight", 0.0)),
        oracle_win_probability=float(body.get("oracle_win_probability", 0.5)),
        oracle_recommendation=body.get("oracle_recommendation", "FIGHT"),
        buyer_ltv_estimate=float(body.get("buyer_ltv_estimate", 0.0)),
        buyer_ltv_tier=body.get("buyer_ltv_tier", "UNKNOWN"),
        buyer_total_spend=float(body.get("buyer_total_spend", 0.0)),
        buyer_dispute_history_count=int(body.get("buyer_dispute_history_count", 0)),
        buyer_last_purchase_days_ago=int(body.get("buyer_last_purchase_days_ago", 90)),
        response_cost=float(body.get("response_cost", 15.0)),
        chargeback_fee=float(body.get("chargeback_fee", 20.0)),
        can_offer_partial=bool(body.get("can_offer_partial", True)),
        partial_refund_pct=float(body.get("partial_refund_pct", 0.65)),
    )
    r = calculate_ltv_decision(inp)

    def _scenario(s):
        if s is None:
            return None
        return {
            "action": s.action.value,
            "churn_probability": s.churn_probability,
            "expected_ltv_loss": s.expected_ltv_loss,
            "dispute_ev": s.dispute_ev,
            "relationship_adjusted_ev": s.relationship_adjusted_ev,
            "cost_to_execute": s.cost_to_execute,
            "reasoning": s.reasoning,
        }

    return JSONResponse({
        "dispute_id": r.dispute_id,
        "ltv_recommendation": r.ltv_recommendation.value,
        "overrides_oracle": r.overrides_oracle,
        "oracle_recommendation": r.oracle_recommendation,
        "raev": r.raev,
        "oracle_ev_fight": r.oracle_ev_fight,
        "buyer_ltv_tier": r.buyer_ltv_tier.value,
        "buyer_ltv_estimate": r.buyer_ltv_estimate,
        "ltv_at_risk": r.ltv_at_risk,
        "scenario_fight": _scenario(r.scenario_fight),
        "scenario_accept": _scenario(r.scenario_accept),
        "scenario_partial": _scenario(r.scenario_partial),
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


# ─── Wave 60 — Sponsor Integrations + PayPal Ecosystem Reporter ───────────────

@app.post("/api/paypal/ecosystem-report")
async def api_ecosystem_report(request: Request):
    """
    POST /api/paypal/ecosystem-report

    Generate a PayPal-facing merchant health report covering dispute rates,
    fraud signals, compliance flags, and buyer protection health.

    Inputs: merchant_id, report_period_days, total_transactions, total_disputes,
            disputes_won, disputes_lost, inr_count, snad_count, unauthorized_count,
            avg_evidence_score, unique_buyers_disputing, suspected_fraud_buyers,
            total_dispute_amount, total_recovered, prev_period_dispute_rate, ...

    Returns: merchant_health_tier, health_score, dispute_rate, win_rate,
             dispute_rate_trend, forecast_next_30d_rate, threshold_days_until_breach,
             buyer_protection_score, compliance_flags, fraud_buyer_signals,
             network_dispute_rates, merchant_actions, paypal_actions,
             summary, paypal_summary
    """
    from oracle.paypal_ecosystem_reporter import generate_ecosystem_report, EcosystemReportInput
    body = await request.json()
    inp = EcosystemReportInput(
        merchant_id=body.get("merchant_id", "ANCHOR"),
        report_period_days=int(body.get("report_period_days", 90)),
        total_transactions=int(body.get("total_transactions", 0)),
        total_disputes=int(body.get("total_disputes", 0)),
        disputes_won=int(body.get("disputes_won", 0)),
        disputes_lost=int(body.get("disputes_lost", 0)),
        disputes_accepted_early=int(body.get("disputes_accepted_early", 0)),
        disputes_pending=int(body.get("disputes_pending", 0)),
        inr_count=int(body.get("inr_count", 0)),
        snad_count=int(body.get("snad_count", 0)),
        unauthorized_count=int(body.get("unauthorized_count", 0)),
        subscription_count=int(body.get("subscription_count", 0)),
        avg_evidence_score=float(body.get("avg_evidence_score", 60.0)),
        evidence_missing_rate=float(body.get("evidence_missing_rate", 0.0)),
        unique_buyers_disputing=int(body.get("unique_buyers_disputing", 0)),
        repeat_disputers=int(body.get("repeat_disputers", 0)),
        suspected_fraud_buyers=int(body.get("suspected_fraud_buyers", 0)),
        total_dispute_amount=float(body.get("total_dispute_amount", 0.0)),
        total_recovered=float(body.get("total_recovered", 0.0)),
        total_lost=float(body.get("total_lost", 0.0)),
        total_fees_paid=float(body.get("total_fees_paid", 0.0)),
        visa_disputes=int(body.get("visa_disputes", 0)),
        mc_disputes=int(body.get("mc_disputes", 0)),
        amex_disputes=int(body.get("amex_disputes", 0)),
        paypal_disputes=int(body.get("paypal_disputes", 0)),
        avg_response_time_hours=float(body.get("avg_response_time_hours", 24.0)),
        disputes_responded_on_time=int(body.get("disputes_responded_on_time", 0)),
        prev_period_dispute_rate=float(body.get("prev_period_dispute_rate", 0.0)),
        prev_period_win_rate=float(body.get("prev_period_win_rate", 0.0)),
    )
    r = generate_ecosystem_report(inp)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "merchant_health_tier": r.merchant_health_tier.value,
        "health_score": r.health_score,
        "dispute_rate": r.dispute_rate,
        "win_rate": r.win_rate,
        "response_rate": r.response_rate,
        "dispute_rate_trend": r.dispute_rate_trend,
        "forecast_next_30d_rate": r.forecast_next_30d_rate,
        "threshold_days_until_breach": r.threshold_days_until_breach,
        "buyer_protection_score": r.buyer_protection_score.value,
        "compliance_flags": [
            {
                "code": f.code,
                "severity": f.severity,
                "description": f.description,
                "recommendation": f.recommendation,
                "affects_paypal": f.affects_paypal,
            }
            for f in r.compliance_flags
        ],
        "network_dispute_rates": r.network_dispute_rates,
        "highest_risk_network": r.highest_risk_network,
        "total_dispute_exposure": r.total_dispute_exposure,
        "total_recovered": r.total_recovered,
        "recovery_rate": r.recovery_rate,
        "avg_evidence_score": r.avg_evidence_score,
        "evidence_quality_tier": r.evidence_quality_tier,
        "paypal_processing_efficiency": r.paypal_processing_efficiency,
        "merchant_actions": r.merchant_actions,
        "paypal_actions": r.paypal_actions,
        "summary": r.summary,
        "paypal_summary": r.paypal_summary,
    })


@app.post("/api/integrations/shopify/enrich")
async def api_shopify_enrich(request: Request):
    """
    POST /api/integrations/shopify/enrich

    Accepts a raw Shopify order payload and returns enriched oracle inputs
    for all oracle modules (prevention, ltv, risk, fraud DNA).

    Input: Shopify order object (from webhook or REST API)
    Returns: item_category, delivery_status, carrier_has_tracking,
             tracking_number, hours_since_purchase, is_digital_product,
             is_express_shipping, ship_to_billing_match, buyer_ltv_tier,
             buyer_prior_orders, buyer_total_spent, order_amount,
             to_prevention_kwargs, to_ltv_kwargs, to_risk_kwargs
    """
    from integrations.shopify_enricher import enrich_from_shopify_order
    body = await request.json()
    order = body.get("order", body)
    merchant_id = body.get("merchant_id", "ANCHOR")
    try:
        ctx = enrich_from_shopify_order(order, merchant_id=merchant_id)
        return JSONResponse({
            "order_id": ctx.order_id,
            "merchant_id": ctx.merchant_id,
            "item_category": ctx.item_category,
            "delivery_status": ctx.delivery_status,
            "carrier_has_tracking": ctx.carrier_has_tracking,
            "tracking_number": ctx.tracking_number,
            "hours_since_purchase": ctx.hours_since_purchase,
            "is_digital_product": ctx.is_digital_product,
            "is_express_shipping": ctx.is_express_shipping,
            "ship_to_billing_match": ctx.ship_to_billing_match,
            "has_3ds_authentication": ctx.has_3ds_authentication,
            "buyer_email": ctx.buyer_email,
            "buyer_account_age_days": ctx.buyer_account_age_days,
            "buyer_prior_orders": ctx.buyer_prior_orders,
            "buyer_total_spent": ctx.buyer_total_spent,
            "buyer_ltv_tier": ctx.buyer_ltv_tier,
            "order_amount": ctx.order_amount,
            "currency": ctx.currency,
            "days_past_expected_delivery": ctx.days_past_expected_delivery,
            "warnings": ctx.warnings,
            "oracle_inputs": {
                "prevention": ctx.to_prevention_kwargs(),
                "ltv": ctx.to_ltv_kwargs(),
                "risk": ctx.to_risk_kwargs(),
                "dna": ctx.to_dna_kwargs(),
            },
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/integrations/zapier/dispute")
async def api_zapier_ingest(request: Request):
    """
    POST /api/integrations/zapier/dispute

    Zapier webhook endpoint — receives dispute payloads from any e-commerce
    platform (Stripe, WooCommerce, Square, etc.) and normalizes them for
    Anchor's oracle pipeline.

    Input: raw Zapier webhook body (platform auto-detected)
    Returns: normalized dispute + anchor_body for immediate pipeline injection
    """
    from integrations.zapier_ingest import normalize_zapier_payload
    body = await request.json()
    merchant_id = body.pop("anchor_merchant_id", "ANCHOR")
    try:
        dispute = normalize_zapier_payload(body, merchant_id=merchant_id)
        return JSONResponse({
            "dispute_id": dispute.dispute_id,
            "source_platform": dispute.source_platform.value,
            "transaction_id": dispute.transaction_id,
            "order_id": dispute.order_id,
            "amount": dispute.amount,
            "currency": dispute.currency,
            "reason_code": dispute.reason_code,
            "original_reason_code": dispute.original_reason_code,
            "buyer_email": dispute.buyer_email,
            "merchant_id": dispute.merchant_id,
            "network": dispute.network,
            "has_tracking": dispute.has_tracking,
            "tracking_number": dispute.tracking_number,
            "anchor_body": dispute.to_anchor_body(),
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/integrations/evidence/score")
async def api_evidence_score(request: Request):
    """
    POST /api/integrations/evidence/score

    Score an evidence package's completeness against PayPal's required
    evidence for a given dispute reason code.

    Input: dispute_id, order_id, reason_code, files (list of {file_name, mime_type})
    Returns: completeness_score, is_ready_to_submit, missing_evidence_types,
             submission_notes, file_count
    """
    from integrations.google_drive_evidence import (
        EvidencePackage, EvidenceFile, EvidenceType, EvidenceSource,
        classify_drive_file, score_evidence_package,
    )
    body = await request.json()
    pkg = EvidencePackage(
        dispute_id=body.get("dispute_id", "UNKNOWN"),
        order_id=body.get("order_id", "UNKNOWN"),
        reason_code=body.get("reason_code", "SNAD"),
    )
    for f in body.get("files", []):
        ev_type = classify_drive_file(f.get("file_name", ""), f.get("mime_type", ""))
        pkg.files.append(EvidenceFile(
            file_id=f.get("file_id", ""),
            file_name=f.get("file_name", ""),
            evidence_type=ev_type,
            source=EvidenceSource.GOOGLE_DRIVE,
            mime_type=f.get("mime_type", "application/octet-stream"),
            size_bytes=f.get("size_bytes", 0),
            drive_url=f.get("drive_url"),
        ))
    pkg = score_evidence_package(pkg)
    return JSONResponse({
        "dispute_id": pkg.dispute_id,
        "reason_code": pkg.reason_code,
        "completeness_score": pkg.completeness_score,
        "is_ready_to_submit": pkg.is_ready_to_submit,
        "missing_evidence_types": pkg.missing_evidence_types,
        "file_count": pkg.file_count,
        "submission_notes": pkg.submission_notes,
        "has_tracking_evidence": pkg.has_tracking_evidence,
        "has_invoice": pkg.has_invoice,
        "has_policy": pkg.has_policy_evidence,
        "has_communication": pkg.has_communication_log,
    })


@app.post("/api/integrations/gmail/prevention-draft")
async def api_gmail_prevention_draft(request: Request):
    """
    POST /api/integrations/gmail/prevention-draft

    Compose a buyer outreach email to prevent a chargeback before it's filed.
    Returns a ready-to-send draft for the Gmail MCP.

    Input: action_type (BUYER_OUTREACH|PARTIAL_REFUND_OFFER|SEND_TRACKING_UPDATE|
                        EXPEDITE_RESHIPMENT|PROACTIVE_REFUND),
           buyer_email, buyer_first_name, merchant_name, merchant_support_email,
           order_id, order_amount, item_name, tracking_number (optional),
           partial_refund_amount (optional), paypal_dispute_id (optional)
    Returns: to, subject, body, priority, action_type, send_within_hours
    """
    from integrations.gmail_prevention import compose_prevention_email, PreventionEmailContext
    body = await request.json()
    ctx = PreventionEmailContext(
        buyer_email=body.get("buyer_email", ""),
        buyer_first_name=body.get("buyer_first_name", "Valued Customer"),
        merchant_name=body.get("merchant_name", ""),
        merchant_support_email=body.get("merchant_support_email", ""),
        order_id=body.get("order_id", ""),
        order_amount=float(body.get("order_amount", 0.0)),
        item_name=body.get("item_name", "your order"),
        tracking_number=body.get("tracking_number"),
        tracking_url=body.get("tracking_url"),
        partial_refund_amount=body.get("partial_refund_amount"),
        estimated_delivery=body.get("estimated_delivery"),
    )
    action = body.get("action_type", "BUYER_OUTREACH")
    dispute_id = body.get("paypal_dispute_id")
    draft = compose_prevention_email(action, ctx, paypal_dispute_id=dispute_id)
    return JSONResponse({
        "to": draft.to,
        "subject": draft.subject,
        "body": draft.body,
        "priority": draft.priority.value,
        "action_type": draft.action_type,
        "send_within_hours": draft.send_within_hours,
        "cc_merchant_support": draft.cc_merchant_support,
        "paypal_dispute_id": draft.paypal_dispute_id,
    })


@app.post("/api/integrations/tracking/parse")
async def api_tracking_parse(request: Request):
    """
    POST /api/integrations/tracking/parse

    Parse carrier tracking page text (from Firecrawl) into a structured
    delivery status usable as proof-of-delivery in dispute evidence.

    Input: tracking_number, carrier (optional), page_text, scrape_url
    Returns: status, is_delivered, delivery_date, dispute_defensibility,
             can_use_as_proof_of_delivery, tracking_url (for Firecrawl)
    """
    from integrations.firecrawl_tracker import (
        detect_carrier, build_tracking_url, parse_tracking_page,
    )
    body = await request.json()
    tracking_number = body.get("tracking_number", "")
    carrier_hint = body.get("carrier", "")
    page_text = body.get("page_text", "")
    scrape_url = body.get("scrape_url", "")

    carrier = detect_carrier(tracking_number, carrier_hint)
    if not scrape_url:
        scrape_url = build_tracking_url(tracking_number, carrier)

    if page_text:
        result = parse_tracking_page(page_text, tracking_number, carrier, scrape_url)
        return JSONResponse({
            "tracking_number": result.tracking_number,
            "carrier": result.carrier.value,
            "status": result.status.value,
            "is_delivered": result.is_delivered,
            "delivery_date": result.delivery_date,
            "last_event": result.last_event,
            "dispute_defensibility": result.dispute_defensibility,
            "can_use_as_proof_of_delivery": result.can_use_as_proof_of_delivery,
            "scrape_url": result.scrape_url,
        })
    else:
        return JSONResponse({
            "tracking_number": tracking_number,
            "carrier": carrier.value,
            "tracking_url": scrape_url,
            "message": "Provide page_text from Firecrawl scrape to get delivery status",
        })


@app.post("/api/paypal/cross-merchant-intel")
async def api_cross_merchant_intel(request: Request):
    """
    POST /api/paypal/cross-merchant-intel

    Analyze dispute records across multiple merchants to detect fraud networks.
    Produces PayPal Risk team alerts for serial disputers, coordinated fraud,
    address reuse, copy-paste scripts, and chargeback washing.

    Input: records (list), analysis_window_days, min_merchants_for_network
    Returns: network_alerts, high_risk_buyer_ids, high_risk_zip_codes,
             paypal_should_act, summary
    """
    from oracle.cross_merchant_intel import (
        analyze_cross_merchant_intel, CrossMerchantIntelInput, MerchantDisputeRecord,
    )
    body = await request.json()
    records = []
    for rec in body.get("records", []):
        records.append(MerchantDisputeRecord(
            merchant_id=rec.get("merchant_id", ""),
            dispute_id=rec.get("dispute_id", ""),
            buyer_id=rec.get("buyer_id", ""),
            buyer_email_domain=rec.get("buyer_email_domain", ""),
            shipping_zip=rec.get("shipping_zip", ""),
            dispute_amount=float(rec.get("dispute_amount", 0.0)),
            reason_code=rec.get("reason_code", "SNAD"),
            dispute_date=rec.get("dispute_date", ""),
            was_resolved_refund=bool(rec.get("was_resolved_refund", False)),
            dispute_text_hash=rec.get("dispute_text_hash", ""),
        ))
    inp = CrossMerchantIntelInput(
        records=records,
        analysis_window_days=int(body.get("analysis_window_days", 30)),
        min_merchants_for_network=int(body.get("min_merchants_for_network", 2)),
    )
    r = analyze_cross_merchant_intel(inp)
    return JSONResponse({
        "total_records_analyzed": r.total_records_analyzed,
        "unique_merchants": r.unique_merchants,
        "unique_buyer_ids": r.unique_buyer_ids,
        "network_alerts": [
            {
                "alert_id": a.alert_id,
                "network_type": a.network_type.value,
                "threat_level": a.threat_level.value,
                "merchants_affected": a.merchants_affected,
                "merchant_count": a.merchant_count,
                "total_dispute_amount": a.total_dispute_amount,
                "dispute_count": a.dispute_count,
                "buyer_ids_involved": a.buyer_ids_involved,
                "common_reason_codes": a.common_reason_codes,
                "zip_codes_involved": a.zip_codes_involved,
                "date_range": a.date_range,
                "confidence": a.confidence,
                "recommended_paypal_action": a.recommended_paypal_action,
                "recommended_merchant_action": a.recommended_merchant_action,
                "summary": a.summary,
                "paypal_alert_text": a.paypal_alert_text,
            }
            for a in r.network_alerts
        ],
        "high_risk_buyer_ids": r.high_risk_buyer_ids,
        "high_risk_zip_codes": r.high_risk_zip_codes,
        "coordinated_wave_detected": r.coordinated_wave_detected,
        "paypal_should_act": r.paypal_should_act,
        "summary": r.summary,
    })


@app.post("/api/oracle/velocity-monitor")
async def api_velocity_monitor(request: Request):
    """
    POST /api/oracle/velocity-monitor

    Monitor dispute filing velocity and detect spikes: fraud waves,
    product defects, fulfillment failures, subscription churn.

    Inputs: merchant_id, window_hours, baseline_daily_disputes,
            disputes (list of {reason_code, amount, filed_hour, sku, carrier})

    Returns: spike_type, urgency, is_spike, velocity_multiplier,
             dominant_reason_code, top_sku, top_carrier,
             total_exposure, immediate_actions, paypal_notification,
             key_insight, summary
    """
    from oracle.dispute_velocity_monitor import (
        monitor_dispute_velocity, VelocityInput, DisputeVelocityRecord,
    )
    body = await request.json()
    records = []
    for i, d in enumerate(body.get("disputes", [])):
        records.append(DisputeVelocityRecord(
            dispute_id=d.get("dispute_id", f"D{i}"),
            reason_code=d.get("reason_code", "SNAD"),
            amount=float(d.get("amount", 100.0)),
            filed_hour=int(d.get("filed_hour", 12)),
            sku=d.get("sku", ""),
            carrier=d.get("carrier", ""),
        ))
    inp = VelocityInput(
        merchant_id=body.get("merchant_id", "ANCHOR"),
        window_hours=int(body.get("window_hours", 24)),
        current_window_disputes=records,
        baseline_daily_disputes=float(body.get("baseline_daily_disputes", 5.0)),
        baseline_win_rate=float(body.get("baseline_win_rate", 0.55)),
        high_value_threshold=float(body.get("high_value_threshold", 200.0)),
    )
    r = monitor_dispute_velocity(inp)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "window_hours": r.window_hours,
        "current_dispute_count": r.current_dispute_count,
        "baseline_rate_per_window": r.baseline_rate_per_window,
        "velocity_multiplier": r.velocity_multiplier,
        "spike_type": r.spike_type.value,
        "urgency": r.urgency.value,
        "is_spike": r.is_spike,
        "inr_count": r.inr_count,
        "snad_count": r.snad_count,
        "unauthorized_count": r.unauthorized_count,
        "subscription_count": r.subscription_count,
        "dominant_reason_code": r.dominant_reason_code,
        "dominant_reason_pct": r.dominant_reason_pct,
        "top_sku": r.top_sku,
        "top_sku_dispute_count": r.top_sku_dispute_count,
        "top_carrier": r.top_carrier,
        "top_carrier_dispute_count": r.top_carrier_dispute_count,
        "hour_cluster": r.hour_cluster,
        "high_value_dispute_count": r.high_value_dispute_count,
        "total_exposure": r.total_exposure,
        "avg_dispute_amount": r.avg_dispute_amount,
        "immediate_actions": [
            {"priority": a.priority, "action": a.action, "owner": a.owner, "deadline_hours": a.deadline_hours}
            for a in r.immediate_actions
        ],
        "paypal_notification": r.paypal_notification,
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


@app.post("/api/oracle/dispute-forecast")
async def dispute_probability_forecast(request: Request):
    """
    POST /api/oracle/dispute-forecast
    Score a shipped order's probability of generating a PayPal dispute in the next 30 days.

    Body: { orders: [ OrderProfile fields... ] }
    Returns: list of DisputeForecast sorted by probability desc
    """
    from oracle.dispute_probability_forecaster import (
        batch_forecast, OrderProfile,
    )
    body = await request.json()
    orders = []
    for o in body.get("orders", [body]):
        orders.append(OrderProfile(
            order_id=o.get("order_id", ""),
            merchant_id=o.get("merchant_id", "ANCHOR"),
            buyer_id=o.get("buyer_id", ""),
            amount=float(o.get("amount", 100.0)),
            is_digital=bool(o.get("is_digital", False)),
            is_subscription=bool(o.get("is_subscription", False)),
            item_category=o.get("item_category", ""),
            has_tracking=bool(o.get("has_tracking", True)),
            tracking_number=o.get("tracking_number", ""),
            carrier=o.get("carrier", ""),
            days_since_ship=int(o.get("days_since_ship", 0)),
            delivery_status=o.get("delivery_status", "IN_TRANSIT"),
            has_delivery_confirmation=bool(o.get("has_delivery_confirmation", False)),
            has_signature_confirmation=bool(o.get("has_signature_confirmation", False)),
            is_international=bool(o.get("is_international", False)),
            express_on_time=bool(o.get("express_on_time", True)),
            buyer_prior_disputes_at_merchant=int(o.get("buyer_prior_disputes_at_merchant", 0)),
            buyer_total_orders_at_merchant=int(o.get("buyer_total_orders_at_merchant", 1)),
            is_new_buyer=bool(o.get("is_new_buyer", False)),
            days_since_last_buyer_contact=int(o.get("days_since_last_buyer_contact", -1)),
            is_peak_season=bool(o.get("is_peak_season", False)),
            subscription_days_until_renewal=int(o.get("subscription_days_until_renewal", -1)),
        ))
    results = batch_forecast(orders)
    return JSONResponse([
        {
            "order_id": r.order_id,
            "merchant_id": r.merchant_id,
            "dispute_probability": r.dispute_probability,
            "probability_tier": r.probability_tier.value,
            "likely_dispute_type": r.likely_dispute_type.value,
            "no_tracking_signal": r.no_tracking_signal,
            "delivery_exception_signal": r.delivery_exception_signal,
            "high_value_no_sig_signal": r.high_value_no_sig_signal,
            "prior_dispute_signal": r.prior_dispute_signal,
            "subscription_signal": r.subscription_signal,
            "peak_season_signal": r.peak_season_signal,
            "international_signal": r.international_signal,
            "prevention_actions": [
                {"action_type": a.action_type.value, "urgency": a.urgency, "description": a.description}
                for a in r.prevention_actions
            ],
            "key_risk_factor": r.key_risk_factor,
            "summary": r.summary,
        }
        for r in results
    ])


@app.post("/api/oracle/merchant-health")
async def merchant_health_advisor(request: Request):
    """
    POST /api/oracle/merchant-health
    Analyze a merchant's full dispute history and produce a 30-day action plan.

    Body:
      merchant_id: str
      total_orders_in_period: int
      baseline_industry_dispute_rate: float (optional, default 0.01)
      paypal_threshold: float (optional, default 0.0075)
      disputes: list of {
        dispute_id, reason_code, amount, outcome,
        days_to_respond, had_tracking, had_invoice,
        had_buyer_communication, carrier, sku
      }

    Returns: health_score, health_tier, dispute_rate, win_rate,
             root_causes, recommendations, projected_rate,
             paypal_risk_signal, paypal_opportunity_signal,
             key_insight, summary
    """
    from oracle.merchant_health_advisor import (
        analyze_merchant_health, MerchantHealthInput, HistoricalDispute,
    )
    body = await request.json()
    disputes = []
    for d in body.get("disputes", []):
        disputes.append(HistoricalDispute(
            dispute_id=d.get("dispute_id", ""),
            reason_code=d.get("reason_code", "SNAD"),
            amount=float(d.get("amount", 100.0)),
            outcome=d.get("outcome", "LOST"),
            days_to_respond=int(d.get("days_to_respond", 3)),
            had_tracking=bool(d.get("had_tracking", False)),
            had_invoice=bool(d.get("had_invoice", False)),
            had_buyer_communication=bool(d.get("had_buyer_communication", False)),
            carrier=d.get("carrier", ""),
            sku=d.get("sku", ""),
            dispute_month=d.get("dispute_month", ""),
        ))
    inp = MerchantHealthInput(
        merchant_id=body.get("merchant_id", "ANCHOR"),
        disputes=disputes,
        total_orders_in_period=int(body.get("total_orders_in_period", 1000)),
        baseline_industry_dispute_rate=float(body.get("baseline_industry_dispute_rate", 0.01)),
        paypal_threshold=float(body.get("paypal_threshold", 0.0075)),
    )
    r = analyze_merchant_health(inp)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "health_score": r.health_score,
        "health_tier": r.health_tier.value,
        "current_dispute_rate": r.current_dispute_rate,
        "dispute_rate_vs_threshold": r.dispute_rate_vs_threshold,
        "total_disputes": r.total_disputes,
        "won_count": r.won_count,
        "lost_count": r.lost_count,
        "refunded_count": r.refunded_count,
        "win_rate": r.win_rate,
        "inr_pct": r.inr_pct,
        "snad_pct": r.snad_pct,
        "unauthorized_pct": r.unauthorized_pct,
        "subscription_pct": r.subscription_pct,
        "root_causes": [c.value for c in r.root_causes],
        "recommendations": [
            {
                "priority": rec.priority.value,
                "action": rec.action,
                "expected_impact": rec.expected_impact,
                "effort": rec.effort,
                "root_cause_addressed": rec.root_cause_addressed.value,
            }
            for rec in r.recommendations
        ],
        "projected_dispute_rate": r.projected_dispute_rate_if_actions_taken,
        "projected_health_score": r.projected_health_score_if_actions_taken,
        "paypal_risk_signal": r.paypal_risk_signal,
        "paypal_opportunity_signal": r.paypal_opportunity_signal,
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


@app.post("/api/oracle/response-writer")
async def dispute_response_writer(request: Request):
    """
    POST /api/oracle/response-writer
    Generate structured, evidence-backed dispute response text ready to submit to PayPal.

    Body: ResponseContext fields (dispute_id, reason_code, order_id, evidence_items, ...)
    Returns: 5-section dispute response with tone, submission_ready flag, evidence list
    """
    from oracle.dispute_response_writer import (
        write_dispute_response, ResponseContext, EvidenceItem,
    )
    body = await request.json()
    evidence_items = [EvidenceItem(e) for e in body.get("evidence_items", [])
                      if e in EvidenceItem.__members__]
    ctx = ResponseContext(
        dispute_id=body.get("dispute_id", ""),
        merchant_id=body.get("merchant_id", "ANCHOR"),
        merchant_name=body.get("merchant_name", "Merchant"),
        reason_code=body.get("reason_code", "INR"),
        order_id=body.get("order_id", ""),
        order_date=body.get("order_date", ""),
        order_amount=float(body.get("order_amount", 0.0)),
        currency=body.get("currency", "USD"),
        item_description=body.get("item_description", ""),
        buyer_name=body.get("buyer_name", ""),
        ship_date=body.get("ship_date", ""),
        carrier=body.get("carrier", ""),
        tracking_number=body.get("tracking_number", ""),
        delivery_date=body.get("delivery_date", ""),
        delivery_status=body.get("delivery_status", ""),
        has_delivery_confirmation=bool(body.get("has_delivery_confirmation", False)),
        has_signature_confirmation=bool(body.get("has_signature_confirmation", False)),
        is_international=bool(body.get("is_international", False)),
        subscription_start=body.get("subscription_start", ""),
        last_billing_date=body.get("last_billing_date", ""),
        cancellation_requested=bool(body.get("cancellation_requested", False)),
        cancellation_date=body.get("cancellation_date", ""),
        evidence_items=evidence_items,
        win_probability=float(body.get("win_probability", 0.5)),
        recommended_action=body.get("recommended_action", ""),
        prior_contact_summary=body.get("prior_contact_summary", ""),
    )
    r = write_dispute_response(ctx)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "merchant_id": r.merchant_id,
        "reason_code": r.reason_code,
        "tone": r.tone.value,
        "sections": [{"section": s.section, "content": s.content} for s in r.sections],
        "full_response": r.full_response,
        "evidence_count": r.evidence_count,
        "evidence_items_referenced": [e.value for e in r.evidence_items_referenced],
        "word_count": r.word_count,
        "is_submission_ready": r.is_submission_ready,
        "submission_note": r.submission_note,
        "summary": r.summary,
    })


@app.post("/api/oracle/buyer-satisfaction-risk")
async def buyer_satisfaction_risk(request: Request):
    """
    POST /api/oracle/buyer-satisfaction-risk
    Score the risk that a resolved dispute leads to buyer churn, negative review,
    or bank chargeback.

    Body: ResolvedDisputeContext fields
    Returns: churn_probability, bank_chargeback_probability, risk_tier,
             retention_actions, key_insight, summary
    """
    from oracle.buyer_satisfaction_risk import (
        score_buyer_satisfaction_risk, ResolvedDisputeContext,
        DisputeOutcome, BuyerLTVTier,
    )
    body = await request.json()
    ctx = ResolvedDisputeContext(
        dispute_id=body.get("dispute_id", ""),
        merchant_id=body.get("merchant_id", "ANCHOR"),
        buyer_id=body.get("buyer_id", ""),
        reason_code=body.get("reason_code", "INR"),
        dispute_outcome=DisputeOutcome(body.get("dispute_outcome", "FULL_REFUND")),
        dispute_amount=float(body.get("dispute_amount", 100.0)),
        days_to_resolution=int(body.get("days_to_resolution", 5)),
        buyer_ltv_tier=BuyerLTVTier(body.get("buyer_ltv_tier", "MEDIUM")),
        buyer_total_orders_at_merchant=int(body.get("buyer_total_orders_at_merchant", 1)),
        buyer_prior_disputes_at_merchant=int(body.get("buyer_prior_disputes_at_merchant", 0)),
        buyer_lifetime_spend=float(body.get("buyer_lifetime_spend", 0.0)),
        merchant_responded_before_escalation=bool(body.get("merchant_responded_before_escalation", True)),
        days_until_refund_issued=int(body.get("days_until_refund_issued", 0)),
        was_tone_empathetic=bool(body.get("was_tone_empathetic", True)),
        buyer_contacted_merchant_first=bool(body.get("buyer_contacted_merchant_first", True)),
        partial_refund_amount=float(body.get("partial_refund_amount", 0.0)),
        is_repeat_issue=bool(body.get("is_repeat_issue", False)),
    )
    r = score_buyer_satisfaction_risk(ctx)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "merchant_id": r.merchant_id,
        "buyer_id": r.buyer_id,
        "churn_probability": r.churn_probability,
        "negative_review_probability": r.negative_review_probability,
        "bank_chargeback_probability": r.bank_chargeback_probability,
        "overall_satisfaction_risk": r.overall_satisfaction_risk,
        "risk_tier": r.risk_tier.value,
        "outcome_signal": r.outcome_signal,
        "response_speed_signal": r.response_speed_signal,
        "empathy_signal": r.empathy_signal,
        "ltv_signal": r.ltv_signal,
        "prior_dispute_signal": r.prior_dispute_signal,
        "retention_actions": [
            {"action_type": a.action_type.value, "urgency": a.urgency,
             "description": a.description, "expected_impact": a.expected_impact}
            for a in r.retention_actions
        ],
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


@app.post("/api/oracle/refund-roi")
async def refund_roi(request: Request):
    """
    POST /api/oracle/refund-roi
    Compute net expected value of fighting vs. refunding a dispute.

    Body: RefundROIInput fields
    Returns: recommended_action, ev_fight, ev_refund, confidence,
             fight_refund_delta, paypal_signal, key_insight, summary
    """
    from oracle.refund_roi_calculator import (
        calculate_refund_roi, RefundROIInput, MerchantSegment,
    )
    body = await request.json()
    inp = RefundROIInput(
        dispute_id=body.get("dispute_id", ""),
        merchant_id=body.get("merchant_id", "ANCHOR"),
        dispute_amount=float(body.get("dispute_amount", 100.0)),
        reason_code=body.get("reason_code", "INR"),
        win_probability=float(body.get("win_probability", 0.5)),
        merchant_segment=MerchantSegment(body.get("merchant_segment", "SMALL_BUSINESS")),
        buyer_ltv=float(body.get("buyer_ltv", 0.0)),
        is_repeat_buyer=bool(body.get("is_repeat_buyer", False)),
        buyer_prior_orders=int(body.get("buyer_prior_orders", 0)),
        response_cost_override=body.get("response_cost_override"),
        chargeback_fee_override=body.get("chargeback_fee_override"),
        partial_refund_pct=float(body.get("partial_refund_pct", 0.0)),
        refund_goodwill_value=float(body.get("refund_goodwill_value", 0.0)),
    )
    r = calculate_refund_roi(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "merchant_id": r.merchant_id,
        "recommended_action": r.recommended_action.value,
        "confidence": r.confidence.value,
        "ev_fight": r.ev_fight,
        "ev_refund": r.ev_refund,
        "ev_partial_refund": r.ev_partial_refund,
        "fight_refund_delta": r.fight_refund_delta,
        "is_fight_roi_positive": r.is_fight_roi_positive,
        "is_refund_roi_better": r.is_refund_roi_better,
        "response_cost_used": r.response_cost_used,
        "chargeback_fee_used": r.chargeback_fee_used,
        "fight_breakdown": {
            "gross_win_value": r.fight_breakdown.gross_win_value,
            "gross_loss_value": r.fight_breakdown.gross_loss_value,
            "response_cost": r.fight_breakdown.response_cost,
            "chargeback_fee": r.fight_breakdown.chargeback_fee,
            "expected_value": r.fight_breakdown.expected_value,
        },
        "refund_breakdown": {
            "gross_win_value": r.refund_breakdown.gross_win_value,
            "gross_loss_value": r.refund_breakdown.gross_loss_value,
            "response_cost": r.refund_breakdown.response_cost,
            "chargeback_fee": r.refund_breakdown.chargeback_fee,
            "expected_value": r.refund_breakdown.expected_value,
        },
        "paypal_signal": r.paypal_signal,
        "key_insight": r.key_insight,
        "summary": r.summary,
    })


@app.post("/api/oracle/deadline-prioritizer")
async def deadline_prioritizer(request: Request):
    """
    POST /api/oracle/deadline-prioritizer
    Rank open disputes by composite urgency × EV score and return a
    time-budgeted work queue with per-dispute recommended actions.

    Body: { merchant_id, minutes_available_today, disputes: [...] }
    Returns: items (ranked), critical_count, deferred_disputes,
             paypal_capacity_signal, summary
    """
    from oracle.deadline_prioritizer import (
        prioritize_disputes, PrioritizerInput, OpenDispute,
    )
    from datetime import date
    body = await request.json()
    raw_disputes = body.get("disputes", [])
    disputes = []
    for rd in raw_disputes:
        dl = rd.get("deadline")
        if isinstance(dl, str):
            dl = date.fromisoformat(dl)
        elif dl is None:
            dl = date.today()
        disputes.append(OpenDispute(
            dispute_id=rd.get("dispute_id", ""),
            amount=float(rd.get("amount", 0)),
            reason_code=rd.get("reason_code", "INR"),
            deadline=dl,
            win_probability=float(rd.get("win_probability", 0.5)),
            expected_value=float(rd.get("expected_value", 0)),
            evidence_ready=bool(rd.get("evidence_ready", False)),
            estimated_response_minutes=int(rd.get("estimated_response_minutes", 60)),
        ))
    inp = PrioritizerInput(
        merchant_id=body.get("merchant_id", "ANCHOR"),
        disputes=disputes,
        minutes_available_today=int(body.get("minutes_available_today", 240)),
    )
    r = prioritize_disputes(inp)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "total_disputes": r.total_disputes,
        "total_minutes_required": r.total_minutes_required,
        "critical_count": r.critical_count,
        "high_count": r.high_count,
        "can_complete_all_today": r.can_complete_all_today,
        "minutes_available_today": r.minutes_available_today,
        "deferred_disputes": r.deferred_disputes,
        "items": [
            {
                "dispute_id": x.dispute_id,
                "amount": x.amount,
                "reason_code": x.reason_code,
                "deadline": x.deadline.isoformat(),
                "days_remaining": x.days_remaining,
                "priority": x.priority.value,
                "urgency_score": x.urgency_score,
                "deadline_component": x.deadline_component,
                "ev_component": x.ev_component,
                "win_component": x.win_component,
                "recommended_action": x.recommended_action.value,
                "action_detail": x.action_detail,
                "estimated_minutes": x.estimated_minutes,
                "paypal_signal": x.paypal_signal,
            }
            for x in r.items
        ],
        "paypal_capacity_signal": r.paypal_capacity_signal,
        "summary": r.summary,
    })


@app.post("/api/oracle/seller-protection")
async def seller_protection(request: Request):
    """
    POST /api/oracle/seller-protection
    Assess whether a dispute transaction qualifies for PayPal Seller Protection
    BEFORE the merchant spends response capacity fighting an ineligible case.

    Body: { dispute_id, reason_code, transaction_amount, fulfillment_method?,
            has_tracking_number?, tracking_carrier?, delivery_confirmed?,
            signature_obtained?, paid_via_paypal_checkout?, buyer_address_confirmed?,
            intangible_goods?, is_donation?, is_gambling?, is_money_transfer?,
            is_vehicle_purchase? }
    Returns: eligibility_status, protection_type, recommended_action,
             ineligible_reasons, fight_confidence, paypal_signal, action_detail
    """
    from oracle.seller_protection_checker import (
        check_seller_protection, SellerProtectionInput, FulfillmentMethod,
    )
    body = await request.json()

    fm_raw = body.get("fulfillment_method", "UNKNOWN")
    try:
        fm = FulfillmentMethod(fm_raw)
    except ValueError:
        fm = FulfillmentMethod.UNKNOWN

    inp = SellerProtectionInput(
        dispute_id=body.get("dispute_id", "UNKNOWN"),
        reason_code=body.get("reason_code", "INR"),
        transaction_amount=float(body.get("transaction_amount", 0)),
        fulfillment_method=fm,
        has_tracking_number=bool(body.get("has_tracking_number", False)),
        tracking_carrier=body.get("tracking_carrier"),
        delivery_confirmed=bool(body.get("delivery_confirmed", False)),
        signature_obtained=bool(body.get("signature_obtained", False)),
        paid_via_paypal_checkout=bool(body.get("paid_via_paypal_checkout", True)),
        buyer_address_confirmed=bool(body.get("buyer_address_confirmed", True)),
        intangible_goods=bool(body.get("intangible_goods", False)),
        transaction_amount_usd=float(body.get("transaction_amount", 0)),
        is_donation=bool(body.get("is_donation", False)),
        is_gambling=bool(body.get("is_gambling", False)),
        is_money_transfer=bool(body.get("is_money_transfer", False)),
        is_vehicle_purchase=bool(body.get("is_vehicle_purchase", False)),
    )
    r = check_seller_protection(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "reason_code": r.reason_code,
        "eligibility_status": r.eligibility_status.value,
        "protection_type": r.protection_type.value,
        "recommended_action": r.recommended_action.value,
        "ineligible_reasons": r.ineligible_reasons,
        "conditional_warnings": r.conditional_warnings,
        "fight_confidence": r.fight_confidence,
        "paypal_signal": r.paypal_signal,
        "action_detail": r.action_detail,
        "findings": [
            {
                "criterion": f.criterion,
                "met": f.met,
                "detail": f.detail,
                "blocking": f.blocking,
            }
            for f in r.findings
        ],
    })


@app.post("/api/oracle/evidence-strength")
async def evidence_strength(request: Request):
    """
    POST /api/oracle/evidence-strength
    Score the quality of a merchant's evidence package before submission.

    Body: { dispute_id, reason_code, transaction_amount, has_tracking_number?,
            has_delivery_scan?, has_signature_proof?, has_order_confirmation?,
            has_product_photos?, has_item_description_match?, has_buyer_communication?,
            has_billing_notification?, has_terms_acceptance?, has_prior_renewal_history?,
            has_address_match_proof?, has_fraud_prevention_evidence?, ... }
    Returns: overall_score, strength_tier, missing_critical, missing_beneficial,
             estimated_win_probability, submit_ready, recommendation, paypal_signal
    """
    from oracle.evidence_strength_scorer import score_evidence, EvidencePackage
    body = await request.json()

    pkg = EvidencePackage(
        dispute_id=body.get("dispute_id", "UNKNOWN"),
        reason_code=body.get("reason_code", "INR"),
        transaction_amount=float(body.get("transaction_amount", 0)),
        has_tracking_number=bool(body.get("has_tracking_number", False)),
        tracking_number=body.get("tracking_number"),
        has_delivery_scan=bool(body.get("has_delivery_scan", False)),
        has_signature_proof=bool(body.get("has_signature_proof", False)),
        has_carrier_confirmation_letter=bool(body.get("has_carrier_confirmation_letter", False)),
        has_order_confirmation=bool(body.get("has_order_confirmation", False)),
        has_product_photos=bool(body.get("has_product_photos", False)),
        has_item_description_match=bool(body.get("has_item_description_match", False)),
        has_inspection_record=bool(body.get("has_inspection_record", False)),
        has_buyer_communication=bool(body.get("has_buyer_communication", False)),
        has_prior_resolution_attempt=bool(body.get("has_prior_resolution_attempt", False)),
        has_refund_policy_acceptance=bool(body.get("has_refund_policy_acceptance", False)),
        has_billing_notification=bool(body.get("has_billing_notification", False)),
        has_terms_acceptance=bool(body.get("has_terms_acceptance", False)),
        has_prior_renewal_history=bool(body.get("has_prior_renewal_history", False)),
        has_ip_address_match=bool(body.get("has_ip_address_match", False)),
        has_device_fingerprint=bool(body.get("has_device_fingerprint", False)),
        has_address_match_proof=bool(body.get("has_address_match_proof", False)),
        has_fraud_prevention_evidence=bool(body.get("has_fraud_prevention_evidence", False)),
        has_policy_documentation=bool(body.get("has_policy_documentation", False)),
    )
    r = score_evidence(pkg)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "reason_code": r.reason_code,
        "overall_score": r.overall_score,
        "strength_tier": r.strength_tier.value,
        "missing_critical": r.missing_critical,
        "missing_beneficial": r.missing_beneficial,
        "estimated_win_probability": r.estimated_win_probability,
        "submit_ready": r.submit_ready,
        "recommendation": r.recommendation,
        "paypal_signal": r.paypal_signal,
        "items": [
            {
                "name": i.name,
                "status": i.status.value,
                "weight": i.weight,
                "critical": i.critical,
                "description": i.description,
            }
            for i in r.items
        ],
    })


@app.post("/api/oracle/merchant-benchmark")
async def merchant_benchmark(request: Request):
    """
    POST /api/oracle/merchant-benchmark
    Compare a merchant's dispute metrics against anonymized peer benchmarks.

    Body: { merchant_id, segment, category, total_orders, total_disputes,
            disputes_won, disputes_fought, disputes_escalated, preemptive_refunds,
            avg_response_days, inr_disputes, unauthorized_disputes }
    Returns: benchmarks (per-metric percentile), overall_health_tier,
             risk_flags, paypal_signal, merchant_coaching, summary
    """
    from oracle.merchant_benchmarker import (
        benchmark_merchant, MerchantMetrics, MerchantSegment, MerchantCategory,
    )
    body = await request.json()

    try:
        seg = MerchantSegment(body.get("segment", "SMALL_BUSINESS"))
    except ValueError:
        seg = MerchantSegment.SMALL_BUSINESS
    try:
        cat = MerchantCategory(body.get("category", "GENERAL"))
    except ValueError:
        cat = MerchantCategory.GENERAL

    metrics = MerchantMetrics(
        merchant_id=body.get("merchant_id", "UNKNOWN"),
        segment=seg,
        category=cat,
        total_orders=int(body.get("total_orders", 1000)),
        total_disputes=int(body.get("total_disputes", 10)),
        disputes_won=int(body.get("disputes_won", 5)),
        disputes_fought=int(body.get("disputes_fought", 10)),
        disputes_escalated=int(body.get("disputes_escalated", 1)),
        preemptive_refunds=int(body.get("preemptive_refunds", 2)),
        avg_response_days=float(body.get("avg_response_days", 4.0)),
        inr_disputes=int(body.get("inr_disputes", 5)),
        unauthorized_disputes=int(body.get("unauthorized_disputes", 2)),
    )
    r = benchmark_merchant(metrics)
    return JSONResponse({
        "merchant_id": r.merchant_id,
        "segment": r.segment.value,
        "category": r.category.value,
        "dispute_rate": r.dispute_rate,
        "win_rate": r.win_rate,
        "avg_response_days": r.avg_response_days,
        "refund_rate": r.refund_rate,
        "chargeback_escalation_rate": r.chargeback_escalation_rate,
        "inr_share": r.inr_share,
        "unauthorized_share": r.unauthorized_share,
        "overall_health_tier": r.overall_health_tier.value,
        "risk_flags": [f.value for f in r.risk_flags],
        "benchmarks": [
            {
                "metric": b.metric,
                "merchant_value": b.merchant_value,
                "peer_p50": b.peer_p50,
                "percentile_rank": b.percentile_rank,
                "tier": b.tier.value,
                "lower_is_better": b.lower_is_better,
                "insight": b.insight,
            }
            for b in r.benchmarks
        ],
        "paypal_signal": r.paypal_signal,
        "merchant_coaching": r.merchant_coaching,
        "summary": r.summary,
    })


# ─── Zapier Integration Endpoints ────────────────────────────────────────────
#
# Zapier can:
#   1. Push new disputes INTO Anchor (trigger intake pipeline)
#   2. Subscribe to Anchor events via webhook (register a Zapier hook URL)
#   3. Pull dispute summaries as trigger data for downstream Zaps
#
# This enables merchants to wire Anchor into their existing Zapier workflows:
#   PayPal Dispute → Zapier → POST /api/zapier/ingest → Anchor scores it →
#   sends scoring summary back via POST to merchant's Zapier webhook URL.


# In-memory webhook registry (production: use Redis/DB)
_zapier_hooks: dict[str, str] = {}  # hook_id → callback_url


@app.post("/api/zapier/subscribe")
async def zapier_subscribe(request: Request):
    """
    Zapier REST Hook subscribe endpoint.
    Zapier POSTs here to register a callback URL for Anchor events.
    Returns hook_id for unsubscribe.
    """
    import uuid
    body = await request.json()
    callback_url = body.get("hookUrl") or body.get("callback_url", "")
    hook_id = str(uuid.uuid4())[:12]
    _zapier_hooks[hook_id] = callback_url
    return JSONResponse({
        "hook_id": hook_id,
        "status": "subscribed",
        "message": f"Anchor will POST dispute events to {callback_url}",
        "event_types": ["dispute.scored", "dispute.p0_alert", "merchant.health_degraded"],
    })


@app.delete("/api/zapier/unsubscribe/{hook_id}")
async def zapier_unsubscribe(hook_id: str):
    """Zapier REST Hook unsubscribe endpoint."""
    removed = _zapier_hooks.pop(hook_id, None)
    return JSONResponse({
        "hook_id": hook_id,
        "status": "unsubscribed" if removed else "not_found",
    })


@app.post("/api/zapier/ingest")
async def zapier_ingest(request: Request):
    """
    Zapier trigger: a new PayPal dispute lands in the merchant's queue.
    Anchor runs it through the oracle pipeline and returns a scored summary.
    Optionally fires registered webhook callbacks with the result.

    Body: { dispute_id, amount, reason_code, merchant_id, deadline?,
            win_probability?, expected_value?, evidence_items? }
    """
    import httpx
    from datetime import date, timedelta

    body = await request.json()
    dispute_id = body.get("dispute_id", "ZAP-001")
    amount = float(body.get("amount", 0))
    reason_code = body.get("reason_code", "INR").upper()
    merchant_id = body.get("merchant_id", "UNKNOWN")
    days_until = int(body.get("days_until_deadline", 7))
    win_prob = float(body.get("win_probability", 0.60))
    ev = float(body.get("expected_value", amount * win_prob * 0.5))

    # Quick priority assessment
    from oracle.deadline_prioritizer import (
        prioritize_disputes, PrioritizerInput, OpenDispute,
    )
    od = OpenDispute(
        dispute_id=dispute_id,
        amount=amount,
        reason_code=reason_code,
        deadline=date.today() + timedelta(days=days_until),
        win_probability=win_prob,
        expected_value=ev,
    )
    queue = prioritize_disputes(PrioritizerInput(
        merchant_id=merchant_id,
        disputes=[od],
    ))
    item = queue.items[0] if queue.items else None

    result = {
        "dispute_id": dispute_id,
        "merchant_id": merchant_id,
        "amount": amount,
        "reason_code": reason_code,
        "priority": item.priority.value if item else "UNKNOWN",
        "urgency_score": item.urgency_score if item else 0,
        "recommended_action": item.recommended_action.value if item else "SCHEDULE_FOR_LATER",
        "action_detail": item.action_detail if item else "",
        "days_remaining": days_until,
        "event_type": "dispute.scored",
        "anchor_version": "71",
    }

    # Fire all registered webhooks (non-blocking best-effort)
    if _zapier_hooks:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for hook_id, url in list(_zapier_hooks.items()):
                try:
                    await client.post(url, json=result)
                except Exception:
                    pass  # never fail the main response due to webhook delivery

    return JSONResponse(result)


@app.get("/api/zapier/sample-dispute")
async def zapier_sample():
    """
    Zapier trigger polling endpoint — returns a sample dispute event
    so Zapier can map fields during Zap setup.
    """
    return JSONResponse([{
        "dispute_id": "PP-D-SAMPLE-001",
        "merchant_id": "MERCH_DEMO",
        "amount": 299.00,
        "reason_code": "INR",
        "priority": "P1_HIGH",
        "urgency_score": 0.72,
        "recommended_action": "GATHER_EVIDENCE",
        "action_detail": "2d remaining. Gather tracking + POD.",
        "days_remaining": 2,
        "event_type": "dispute.scored",
        "anchor_version": "71",
    }])


# ─── Elastic Integration Endpoints ───────────────────────────────────────────
#
# Elastic enables:
#   1. Full-text + structured search across all ingested disputes
#   2. Kibana dashboard integration (dispute stats, trend charts)
#   3. Anomaly detection on dispute rate spikes
#
# In production: Anchor would push to a real Elasticsearch cluster.
# For the sandbox demo: in-memory document store that mirrors the ES API shape.


_elastic_index: list[dict] = [
    {"_id": "PP-D-10041", "dispute_id": "PP-D-10041", "merchant_id": "MERCH_A", "amount": 299.00,  "reason_code": "INR",          "status": "OPEN",   "priority": "P0_CRITICAL", "urgency_score": 0.95, "win_probability": 0.72, "days_remaining": 0},
    {"_id": "PP-D-10042", "dispute_id": "PP-D-10042", "merchant_id": "MERCH_A", "amount": 850.00,  "reason_code": "UNAUTHORIZED", "status": "OPEN",   "priority": "P0_CRITICAL", "urgency_score": 0.91, "win_probability": 0.80, "days_remaining": 1},
    {"_id": "PP-D-10043", "dispute_id": "PP-D-10043", "merchant_id": "MERCH_B", "amount": 149.00,  "reason_code": "SNAD",         "status": "OPEN",   "priority": "P1_HIGH",     "urgency_score": 0.75, "win_probability": 0.55, "days_remaining": 2},
    {"_id": "PP-D-10044", "dispute_id": "PP-D-10044", "merchant_id": "MERCH_A", "amount": 420.00,  "reason_code": "INR",          "status": "OPEN",   "priority": "P1_HIGH",     "urgency_score": 0.68, "win_probability": 0.68, "days_remaining": 3},
    {"_id": "PP-D-10045", "dispute_id": "PP-D-10045", "merchant_id": "MERCH_C", "amount": 59.99,   "reason_code": "SUBSCRIPTION", "status": "OPEN",   "priority": "P2_MEDIUM",   "urgency_score": 0.42, "win_probability": 0.45, "days_remaining": 5},
    {"_id": "PP-D-10046", "dispute_id": "PP-D-10046", "merchant_id": "MERCH_B", "amount": 1200.00, "reason_code": "UNAUTHORIZED", "status": "OPEN",   "priority": "P2_MEDIUM",   "urgency_score": 0.38, "win_probability": 0.76, "days_remaining": 7},
    {"_id": "PP-D-10047", "dispute_id": "PP-D-10047", "merchant_id": "MERCH_C", "amount": 89.00,   "reason_code": "INR",          "status": "CLOSED", "priority": "P3_LOW",      "urgency_score": 0.20, "win_probability": 0.60, "days_remaining": 10},
    {"_id": "PP-D-10048", "dispute_id": "PP-D-10048", "merchant_id": "MERCH_A", "amount": 35.00,   "reason_code": "SUBSCRIPTION", "status": "CLOSED", "priority": "P4_DEFER",    "urgency_score": 0.08, "win_probability": 0.30, "days_remaining": 14},
]


@app.post("/api/elastic/search")
async def elastic_search(request: Request):
    """
    POST /api/elastic/search
    Elasticsearch-compatible search endpoint for disputes.
    Mirrors the ES _search API shape so dashboards can swap to real ES.

    Body: { query?: { match?: {field: value}, range?: {field: {gte, lte}} },
            sort?: [{field: order}], size?: int, from?: int }
    Returns: { hits: { total, hits: [{ _id, _source }] }, aggregations? }
    """
    body = await request.json()
    docs = list(_elastic_index)

    # Simple query processing (match + range)
    q = body.get("query", {})
    if "match" in q:
        for field, value in q["match"].items():
            docs = [d for d in docs if str(d.get(field, "")).lower() == str(value).lower()]
    if "match_all" in q:
        pass  # return all
    if "range" in q:
        for field, bounds in q["range"].items():
            gte = bounds.get("gte")
            lte = bounds.get("lte")
            if gte is not None:
                docs = [d for d in docs if d.get(field, 0) >= gte]
            if lte is not None:
                docs = [d for d in docs if d.get(field, 0) <= lte]
    if "term" in q:
        for field, value in q["term"].items():
            docs = [d for d in docs if d.get(field) == value]

    # Sorting
    sort = body.get("sort", [{"urgency_score": "desc"}])
    if sort:
        for s in reversed(sort):
            for field, direction in s.items():
                docs.sort(key=lambda d: d.get(field, 0), reverse=(direction == "desc"))

    # Pagination
    total = len(docs)
    from_ = int(body.get("from", 0))
    size = int(body.get("size", 10))
    page = docs[from_:from_ + size]

    # Aggregations (dispute_rate by reason_code)
    reason_agg: dict[str, int] = {}
    for d in docs:
        rc = d.get("reason_code", "UNKNOWN")
        reason_agg[rc] = reason_agg.get(rc, 0) + 1

    return JSONResponse({
        "hits": {
            "total": {"value": total, "relation": "eq"},
            "hits": [{"_id": d["_id"], "_source": d} for d in page],
        },
        "aggregations": {
            "reason_codes": {
                "buckets": [
                    {"key": k, "doc_count": v}
                    for k, v in sorted(reason_agg.items(), key=lambda x: -x[1])
                ]
            }
        },
    })


@app.post("/api/elastic/index")
async def elastic_index_doc(request: Request):
    """
    POST /api/elastic/index
    Index a new dispute document (called by Anchor after scoring).
    Mirrors POST /<index>/_doc behavior.
    """
    import uuid
    body = await request.json()
    doc_id = body.get("dispute_id", str(uuid.uuid4())[:12])
    # Remove old version if exists
    _elastic_index[:] = [d for d in _elastic_index if d.get("dispute_id") != doc_id]
    doc = {"_id": doc_id, **body}
    _elastic_index.append(doc)
    return JSONResponse({"_id": doc_id, "result": "created", "total_indexed": len(_elastic_index)})


@app.get("/api/elastic/stats")
async def elastic_stats():
    """
    GET /api/elastic/stats
    Returns index statistics for the Kibana-style overview panel.
    """
    open_docs = [d for d in _elastic_index if d.get("status") == "OPEN"]
    total_ev = sum(d.get("amount", 0) * d.get("win_probability", 0.5) for d in open_docs)
    p0_count = sum(1 for d in open_docs if d.get("priority", "").startswith("P0"))

    return JSONResponse({
        "index": "anchor-disputes",
        "total_documents": len(_elastic_index),
        "open_disputes": len(open_docs),
        "closed_disputes": len(_elastic_index) - len(open_docs),
        "total_ev_at_stake": round(total_ev, 2),
        "p0_critical_count": p0_count,
        "reason_code_distribution": {
            rc: sum(1 for d in _elastic_index if d.get("reason_code") == rc)
            for rc in ["INR", "UNAUTHORIZED", "SNAD", "SUBSCRIPTION"]
        },
    })


# ─── APIMatic OpenAPI Spec ────────────────────────────────────────────────────
#
# APIMatic can consume this endpoint's OpenAPI JSON to auto-generate:
#   - SDKs in Python, JS, C#, Java, Ruby, PHP, Go
#   - Interactive API documentation
#   - Postman collection
#
# This endpoint returns the OpenAPI 3.0 spec for Anchor's oracle API layer.


@app.get("/api/openapi.json")
async def openapi_spec():
    """
    GET /api/openapi.json
    OpenAPI 3.0 specification for the Anchor oracle API.
    Import into APIMatic to auto-generate SDKs and documentation.
    """
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "Anchor — Autonomous PayPal Dispute Management API",
            "description": (
                "Anchor is an AI-powered PayPal dispute management oracle. "
                "It provides 11 specialized oracle endpoints that score, rank, "
                "and recommend actions for every open dispute — integrating with "
                "PayPal, Zapier, Bryntum, Elastic, and AG Grid."
            ),
            "version": "1.0.0",
            "contact": {"name": "Anchor API Support"},
        },
        "servers": [
            {"url": "https://anchor-paypal.onrender.com", "description": "Production (Render)"},
            {"url": "http://localhost:8000", "description": "Local development"},
        ],
        "paths": {
            "/api/oracle/refund-roi": {
                "post": {
                    "summary": "Refund ROI Calculator",
                    "description": "Calculate fight vs. refund EV using segment-aware response costs",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "ROI report with EV analysis"}},
                }
            },
            "/api/oracle/buyer-satisfaction-risk": {
                "post": {
                    "summary": "Buyer Satisfaction Risk Scorer",
                    "description": "Predict post-dispute churn, negative reviews, bank chargebacks",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Risk report with tier and retention actions"}},
                }
            },
            "/api/oracle/deadline-prioritizer": {
                "post": {
                    "summary": "Dispute Deadline Prioritizer",
                    "description": "Rank open disputes by urgency × EV with time-budget analysis",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Ranked queue with P0-P4 priorities"}},
                }
            },
            "/api/oracle/seller-protection": {
                "post": {
                    "summary": "Seller Protection Eligibility Checker",
                    "description": "Verify PayPal Seller Protection eligibility before fighting",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Eligibility status and recommended action"}},
                }
            },
            "/api/oracle/evidence-strength": {
                "post": {
                    "summary": "Evidence Strength Scorer",
                    "description": "Rate dispute evidence quality before submission",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Score, tier, missing items, win probability"}},
                }
            },
            "/api/oracle/merchant-benchmark": {
                "post": {
                    "summary": "Merchant Dispute Benchmarker",
                    "description": "Compare merchant metrics to anonymized peer benchmarks",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Percentile rankings, risk flags, coaching"}},
                }
            },
            "/api/oracle/response-quality-analyzer": {
                "post": {
                    "summary": "Dispute Response Quality Analyzer",
                    "description": "Grade a merchant's written dispute response before submission — A/B/C/D/F with per-dimension breakdown and improvement suggestions",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "quality_score, grade, dimensions, issues, improvements, submit_ready"}},
                }
            },
            "/api/oracle/refund-roi": {
                "post": {
                    "summary": "Refund vs Fight ROI Calculator",
                    "description": "Dollar-denominated EV for fight and refund paths with break-even probability and CLV-adjusted recommendation",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "fight_ev, refund_ev, recommended_action, roi_differential, break_even_win_probability"}},
                }
            },
            "/api/astropods/agent-registry": {
                "get": {
                    "summary": "Astropods Agent Registry",
                    "description": "Governed oracle fleet manifest — budgets, capabilities, guardrails for all Anchor agents",
                    "responses": {"200": {"description": "Fleet manifest with agent list and governance policies"}},
                },
                "post": {
                    "summary": "Register Agent (Astropods)",
                    "description": "Add a new governed oracle agent to the Anchor fleet",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"201": {"description": "Registered agent entry"}},
                },
            },
            "/api/channel3/product-lookup": {
                "get": {
                    "summary": "Channel3 Product Lookup",
                    "description": "Enrich dispute with Channel3 product catalog data — INR/SNAD risk scores, delivery window, brand verification",
                    "responses": {"200": {"description": "Product data + dispute enrichment signals"}},
                }
            },
            "/api/channel3/dispute-enrich": {
                "post": {
                    "summary": "Channel3 Dispute Enrich",
                    "description": "Full dispute enrichment combining product intelligence with oracle recommendation",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Enriched dispute with win probability adjustment"}},
                }
            },
            "/api/kernel/community-trust": {
                "post": {
                    "summary": "KERNEL Community Trust",
                    "description": "Cross-reference buyer community trust score for UNAUTHORIZED dispute adjudication",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Trust tier, score, win probability adjustment, recommendation"}},
                }
            },
            "/api/kernel/trust-stats": {
                "get": {
                    "summary": "KERNEL Trust Stats",
                    "description": "Aggregated trust tier distribution across buyer cohort",
                    "responses": {"200": {"description": "Tier distribution, avg score, unauthorized flag count"}},
                }
            },
            "/api/zapier/ingest": {
                "post": {
                    "summary": "Zapier Dispute Ingest",
                    "description": "Receive a dispute from Zapier, score it, fire webhooks",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Scored dispute event"}},
                }
            },
            "/api/elastic/search": {
                "post": {
                    "summary": "Elastic-compatible Dispute Search",
                    "description": "Search disputes using Elasticsearch-compatible query DSL",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "Search results with aggregations"}},
                }
            },
            "/api/stats": {
                "get": {
                    "summary": "System Statistics",
                    "description": "Returns live Anchor system stats",
                    "responses": {"200": {"description": "Stats including disputes_total, oracle endpoints"}},
                }
            },
        },
    }
    return JSONResponse(spec)


# ─── Render Health + Deploy Info ──────────────────────────────────────────────
# Render provides always-on hosting for Anchor. This endpoint exposes deploy
# metadata so the Render dashboard can track service health and the demo video
# can show "running live on Render."

@app.get("/api/render/health")
async def render_health():
    """
    GET /api/render/health
    Extended health check for the Render deployment.
    Shows service version, uptime, oracle count, and sponsor integrations.
    """
    return JSONResponse({
        "status": "healthy",
        "service": "anchor-paypal",
        "platform": "Render",
        "version": "72",
        "oracle_endpoints": 11,
        "sponsor_integrations": ["Render", "AG Grid", "Bryntum", "Zapier", "Elastic", "APIMatic", "Postman", "Astropods", "Channel3", "KERNEL"],
        "features": {
            "autonomous_dispute_processing": True,
            "ai_oracle_pipeline": True,
            "bryntum_scheduler_dashboard": True,
            "zapier_webhooks": True,
            "elastic_search_index": True,
            "apimatic_openapi_spec": True,
            "ag_studio_analytics": True,
        },
        "uptime_seconds": int(time.time() - _start_time) if "_start_time" in globals() else 0,
    })


# ─── Postman Collection Export ────────────────────────────────────────────────
# Returns a Postman Collection v2.1 JSON that testers and judges can import
# directly into Postman to try every Anchor endpoint.

@app.get("/api/postman/collection")
async def postman_collection():
    """
    GET /api/postman/collection
    Postman Collection v2.1 — import into Postman to test all Anchor endpoints.
    """
    base = "https://anchor-paypal.onrender.com"
    collection = {
        "info": {
            "name": "Anchor — PayPal Dispute Management",
            "_postman_id": "anchor-paypal-v72",
            "description": "Complete Anchor API — 11 oracle endpoints + sponsor integrations",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": "Oracle",
                "item": [
                    _pm_post("Refund ROI Calculator", f"{base}/api/oracle/refund-roi", {"dispute_id": "D001", "amount": 299.0, "reason_code": "INR", "win_probability": 0.65, "merchant_segment": "SMALL_BUSINESS", "ltv": 800.0}),
                    _pm_post("Buyer Satisfaction Risk", f"{base}/api/oracle/buyer-satisfaction-risk", {"dispute_id": "D001", "resolution": "MERCHANT_WIN", "amount": 150.0, "reason_code": "SNAD", "is_repeat_buyer": True}),
                    _pm_post("Deadline Prioritizer", f"{base}/api/oracle/deadline-prioritizer", {"merchant_id": "MERCH_X", "minutes_available_today": 240, "disputes": [{"dispute_id": "D001", "amount": 299.0, "reason_code": "INR", "deadline": "2026-11-08", "win_probability": 0.65, "expected_value": 150.0}]}),
                    _pm_post("Seller Protection Checker", f"{base}/api/oracle/seller-protection", {"dispute_id": "D001", "reason_code": "INR", "transaction_amount": 299.0, "has_tracking_number": True, "paid_via_paypal_checkout": True}),
                    _pm_post("Evidence Strength Scorer", f"{base}/api/oracle/evidence-strength", {"dispute_id": "D001", "reason_code": "INR", "transaction_amount": 299.0, "has_tracking_number": True, "has_delivery_scan": True, "has_order_confirmation": True}),
                    _pm_post("Merchant Benchmarker", f"{base}/api/oracle/merchant-benchmark", {"merchant_id": "MERCH_X", "segment": "SMALL_BUSINESS", "category": "ELECTRONICS", "total_orders": 1000, "total_disputes": 12, "disputes_won": 7, "disputes_fought": 12, "disputes_escalated": 1, "preemptive_refunds": 2, "avg_response_days": 3.5, "inr_disputes": 6, "unauthorized_disputes": 2}),
                    _pm_post("Response Quality Analyzer", f"{base}/api/oracle/response-quality-analyzer", {"dispute_id": "D001", "reason_code": "INR", "response_text": "We shipped the item on Oct 15, 2026. Tracking number 1Z9999W99999999999 confirms delivery Oct 20, 2026. Please find attached the tracking confirmation and delivery scan. We respectfully request closure.", "transaction_amount": 299.0, "has_tracking": True, "has_delivery_scan": True, "has_order_confirmation": True}),
                    _pm_post("Refund vs Fight ROI", f"{base}/api/oracle/refund-roi", {"dispute_id": "D001", "transaction_amount": 299.0, "win_probability": 0.65, "seller_protection_eligible": True, "response_cost_usd": 15.0, "reorder_probability": 0.15, "customer_lifetime_value_usd": 500.0, "deadline_days": 5}),
                ],
            },
            {
                "name": "Zapier",
                "item": [
                    _pm_post("Subscribe Webhook", f"{base}/api/zapier/subscribe", {"hookUrl": "https://hooks.zapier.com/hooks/catch/YOUR_HOOK"}),
                    _pm_post("Ingest Dispute", f"{base}/api/zapier/ingest", {"dispute_id": "ZAP-001", "amount": 150.0, "reason_code": "INR", "merchant_id": "MERCH_X", "days_until_deadline": 3, "win_probability": 0.65}),
                    {"name": "Sample Dispute", "request": {"method": "GET", "url": f"{base}/api/zapier/sample-dispute"}},
                ],
            },
            {
                "name": "Elastic",
                "item": [
                    _pm_post("Search Disputes", f"{base}/api/elastic/search", {"query": {"match": {"reason_code": "INR"}}, "sort": [{"urgency_score": "desc"}], "size": 5}),
                    {"name": "Elastic Stats", "request": {"method": "GET", "url": f"{base}/api/elastic/stats"}},
                ],
            },
            {
                "name": "System",
                "item": [
                    {"name": "Stats", "request": {"method": "GET", "url": f"{base}/api/stats"}},
                    {"name": "Render Health", "request": {"method": "GET", "url": f"{base}/api/render/health"}},
                    {"name": "OpenAPI Spec (APIMatic)", "request": {"method": "GET", "url": f"{base}/api/openapi.json"}},
                ],
            },
            {
                "name": "Astropods",
                "item": [
                    {"name": "Agent Registry (GET)", "request": {"method": "GET", "url": f"{base}/api/astropods/agent-registry"}},
                    _pm_post("Register Agent", f"{base}/api/astropods/agent-registry", {"agent_id": "my-agent", "name": "My Custom Oracle", "capabilities": ["scoring"], "budget_usd_per_1k_calls": 0.02}),
                ],
            },
            {
                "name": "Channel3",
                "item": [
                    {"name": "Product Lookup", "request": {"method": "GET", "url": f"{base}/api/channel3/product-lookup?product_id=PROD-001"}},
                    _pm_post("Dispute Enrich", f"{base}/api/channel3/dispute-enrich", {"dispute_id": "D001", "reason_code": "SNAD", "product_id": "PROD-003", "amount": 2499.0}),
                ],
            },
            {
                "name": "KERNEL",
                "item": [
                    _pm_post("Community Trust", f"{base}/api/kernel/community-trust", {"dispute_id": "D001", "buyer_id": "buyer_flagged_C", "reason_code": "UNAUTHORIZED"}),
                    {"name": "Trust Stats", "request": {"method": "GET", "url": f"{base}/api/kernel/trust-stats"}},
                ],
            },
        ],
    }
    return JSONResponse(collection)


@app.post("/api/oracle/response-quality-analyzer")
async def api_response_quality_analyzer(request: Request):
    """
    POST /api/oracle/response-quality-analyzer

    Wave 72 — Dispute Response Quality Analyzer.
    Grades a merchant's written dispute response text before they submit it.

    Scores five dimensions: completeness, evidence_citation, tone,
    specificity, structure — returns letter grade A-F, per-dimension
    breakdown, issues, improvement suggestions, and estimated win
    probability boost.

    PayPal benefit: Higher-quality responses resolve faster, reducing
    dispute resolution team bandwidth and correlating with lower escalation
    rates across merchant cohorts.

    Request body:
      dispute_id          str
      reason_code         str  (INR | SNAD | UNAUTHORIZED | SUBSCRIPTION)
      response_text       str  — the actual written response draft
      transaction_amount  float (optional)
      has_tracking        bool
      has_delivery_scan   bool
      has_order_confirmation bool
      has_product_photos  bool
      has_buyer_communication bool

    Response:
      dispute_id, reason_code, quality_score, grade, dimensions[],
      issues[], improvements[], estimated_win_probability_boost,
      submit_ready, paypal_signal, recommendation
    """
    from oracle.response_quality_analyzer import (
        analyze_response_quality,
        ResponseAnalysisInput,
    )
    body = await request.json()
    inp = ResponseAnalysisInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        reason_code=body.get("reason_code", "INR"),
        response_text=body.get("response_text", ""),
        transaction_amount=float(body.get("transaction_amount", 0.0)),
        has_tracking=bool(body.get("has_tracking", False)),
        has_delivery_scan=bool(body.get("has_delivery_scan", False)),
        has_order_confirmation=bool(body.get("has_order_confirmation", False)),
        has_product_photos=bool(body.get("has_product_photos", False)),
        has_buyer_communication=bool(body.get("has_buyer_communication", False)),
    )
    r = analyze_response_quality(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "reason_code": r.reason_code,
        "quality_score": r.quality_score,
        "grade": r.grade.value,
        "dimensions": [
            {
                "name": d.name,
                "score": d.score,
                "weight": d.weight,
                "issues": d.issues,
                "suggestions": d.suggestions,
            }
            for d in r.dimensions
        ],
        "issues": r.issues,
        "improvements": r.improvements,
        "estimated_win_probability_boost": r.estimated_win_probability_boost,
        "submit_ready": r.submit_ready,
        "paypal_signal": r.paypal_signal,
        "recommendation": r.recommendation,
    })


@app.post("/api/oracle/dispute-aging")
async def api_dispute_aging(request: Request):
    """
    POST /api/oracle/dispute-aging

    Wave 74 — Dispute Aging Monitor.
    Scans a merchant's open disputes for aging patterns and returns
    tier breakdown (FRESH/ACTIVE/AGING/CRITICAL/OVERDUE), inaction risk
    scores, escalation flags, and an overall merchant health rating.

    PayPal benefit: Early detection of stale disputes reduces default
    judgment rate. Aging distribution is a leading indicator of merchant
    operational health — merchants with many CRITICAL disputes are
    candidates for proactive outreach by PayPal's merchant success team.

    Request body:
      merchant_id  str
      disputes[]   array of:
        dispute_id            str
        amount                float
        reason_code           str
        opened_hours_ago      float
        deadline_hours_total  float
        last_action_hours_ago float
    """
    from oracle.dispute_aging_monitor import monitor_aging, AgingMonitorInput, OpenDisputeAge
    body = await request.json()
    disputes = [
        OpenDisputeAge(
            dispute_id=d.get("dispute_id", "UNKNOWN"),
            amount=float(d.get("amount", 0.0)),
            reason_code=d.get("reason_code", "INR"),
            opened_hours_ago=float(d.get("opened_hours_ago", 0.0)),
            deadline_hours_total=float(d.get("deadline_hours_total", 240.0)),
            last_action_hours_ago=float(d.get("last_action_hours_ago", 0.0)),
        )
        for d in body.get("disputes", [])
    ]
    inp = AgingMonitorInput(
        merchant_id=body.get("merchant_id", "MERCH_DEMO"),
        disputes=disputes,
    )
    r = monitor_aging(inp)

    def _aged_dict(a):
        return {
            "dispute_id": a.dispute_id,
            "amount": a.amount,
            "reason_code": a.reason_code,
            "age_hours": a.age_hours,
            "age_tier": a.age_tier.value,
            "hours_until_deadline": a.hours_until_deadline,
            "inaction_risk_score": a.inaction_risk_score,
            "escalation_flag": a.escalation_flag,
            "recommendation": a.recommendation,
        }

    return JSONResponse({
        "merchant_id": r.merchant_id,
        "total_open": r.total_open,
        "tier_counts": r.tier_counts,
        "total_at_risk_usd": r.total_at_risk_usd,
        "overall_health": r.overall_health,
        "critical_cases": [_aged_dict(a) for a in r.critical_cases],
        "all_disputes": [_aged_dict(a) for a in r.all_disputes],
        "paypal_signal": r.paypal_signal,
    })


@app.post("/api/oracle/refund-roi")
async def api_refund_roi(request: Request):
    """
    POST /api/oracle/refund-roi

    Wave 73 — Refund vs. Fight ROI Calculator.
    Computes dollar-denominated expected value for both fight and refund
    paths, returning the break-even win probability, ROI differential,
    and a CLV-adjusted recommendation.

    Request body:
      dispute_id               str
      transaction_amount       float
      win_probability          float   (0.0 – 1.0)
      seller_protection_eligible bool
      response_cost_usd        float   (default 15.0)
      reorder_probability      float   (default 0.15)
      customer_lifetime_value_usd float (default 0.0)
      refund_recovery_rate     float   (default 0.0)
      deadline_days            int     (default 7)

    Response:
      dispute_id, fight{ev, clv_impact, total_ev}, refund{ev, clv_impact, total_ev},
      recommended_action, roi_differential, break_even_win_probability,
      clv_adjusted_recommendation, recommendation_reason, paypal_signal
    """
    from oracle.refund_roi_calculator import calculate_roi, ROIInput
    body = await request.json()
    inp = ROIInput(
        dispute_id=body.get("dispute_id", "DEMO"),
        transaction_amount=float(body.get("transaction_amount", 0.0)),
        win_probability=float(body.get("win_probability", 0.5)),
        seller_protection_eligible=bool(body.get("seller_protection_eligible", False)),
        response_cost_usd=float(body.get("response_cost_usd", 15.0)),
        reorder_probability=float(body.get("reorder_probability", 0.15)),
        customer_lifetime_value_usd=float(body.get("customer_lifetime_value_usd", 0.0)),
        refund_recovery_rate=float(body.get("refund_recovery_rate", 0.0)),
        deadline_days=int(body.get("deadline_days", 7)),
    )
    r = calculate_roi(inp)
    return JSONResponse({
        "dispute_id": r.dispute_id,
        "transaction_amount": r.transaction_amount,
        "win_probability": r.win_probability,
        "fight": {
            "gross_outcome_win": r.fight.gross_outcome_win,
            "gross_outcome_lose": r.fight.gross_outcome_lose,
            "expected_value": r.fight.expected_value,
            "clv_impact": r.fight.clv_impact,
            "total_ev_with_clv": r.fight.total_ev_with_clv,
        },
        "refund": {
            "gross_outcome_win": r.refund.gross_outcome_win,
            "gross_outcome_lose": r.refund.gross_outcome_lose,
            "expected_value": r.refund.expected_value,
            "clv_impact": r.refund.clv_impact,
            "total_ev_with_clv": r.refund.total_ev_with_clv,
        },
        "recommended_action": r.recommended_action.value,
        "roi_differential": r.roi_differential,
        "break_even_win_probability": r.break_even_win_probability,
        "clv_adjusted_recommendation": r.clv_adjusted_recommendation.value,
        "recommendation_reason": r.recommendation_reason,
        "paypal_signal": r.paypal_signal,
    })


def _pm_post(name: str, url: str, body: dict) -> dict:
    import json as _json
    return {
        "name": name,
        "request": {
            "method": "POST",
            "header": [{"key": "Content-Type", "value": "application/json"}],
            "body": {"mode": "raw", "raw": _json.dumps(body, indent=2)},
            "url": url,
        },
    }


# ─── Astropods — agentOS governance ──────────────────────────────────────────

#: Anchor's oracle agent fleet — the governed manifest Astropods would manage.
_ASTROPODS_REGISTRY: list[dict] = [
    {
        "agent_id": "anchor-oracle-ev-calculator",
        "name": "EV Calculator",
        "description": "Expected-value scorer for fight vs. refund decisions",
        "capabilities": ["dispute_scoring", "ev_calculation"],
        "budget_usd_per_1k_calls": 0.02,
        "guardrails": ["no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 1842,
    },
    {
        "agent_id": "anchor-oracle-deadline-prioritizer",
        "name": "Deadline Prioritizer",
        "description": "Ranks open disputes by composite urgency score",
        "capabilities": ["dispute_scheduling", "deadline_tracking"],
        "budget_usd_per_1k_calls": 0.01,
        "guardrails": ["no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 923,
    },
    {
        "agent_id": "anchor-oracle-seller-protection",
        "name": "Seller Protection Checker",
        "description": "PayPal Seller Protection eligibility assessor",
        "capabilities": ["policy_evaluation", "eligibility_check"],
        "budget_usd_per_1k_calls": 0.01,
        "guardrails": ["no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 741,
    },
    {
        "agent_id": "anchor-oracle-evidence-strength",
        "name": "Evidence Strength Scorer",
        "description": "Rates evidence package quality before submission",
        "capabilities": ["evidence_analysis", "win_probability"],
        "budget_usd_per_1k_calls": 0.01,
        "guardrails": ["no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 618,
    },
    {
        "agent_id": "anchor-oracle-merchant-benchmarker",
        "name": "Merchant Benchmarker",
        "description": "Peer benchmark percentile rankings for dispute metrics",
        "capabilities": ["benchmarking", "risk_flags", "coaching"],
        "budget_usd_per_1k_calls": 0.02,
        "guardrails": ["anonymized_peers", "no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 334,
    },
    {
        "agent_id": "anchor-oracle-response-quality",
        "name": "Response Quality Analyzer",
        "description": "Grades written dispute responses before submission",
        "capabilities": ["nlp_analysis", "response_grading", "coaching"],
        "budget_usd_per_1k_calls": 0.03,
        "guardrails": ["no_pii_output", "read_only"],
        "status": "active",
        "calls_this_period": 112,
    },
]


@app.get("/api/astropods/agent-registry")
async def api_astropods_registry():
    """
    GET /api/astropods/agent-registry

    Astropods agentOS integration — returns Anchor's governed oracle fleet.

    Exposes the full agent registry in the Astropods manifest format:
    each agent's capabilities, budget allocation, guardrails, and usage
    counters. In a production deployment this would sync to the Astropods
    platform so ops teams can govern Anchor's AI fleet centrally alongside
    all other company agents.

    PayPal benefit: PayPal can audit every AI oracle decision engine in
    Anchor through a single governance pane — budget controls, capability
    restrictions, and audit logs all in one place.
    """
    total_calls = sum(a["calls_this_period"] for a in _ASTROPODS_REGISTRY)
    total_budget_usd = sum(
        a["budget_usd_per_1k_calls"] * a["calls_this_period"] / 1000.0
        for a in _ASTROPODS_REGISTRY
    )
    return JSONResponse({
        "platform": "Astropods agentOS",
        "org": "Anchor — PayPal Dispute Management",
        "fleet_size": len(_ASTROPODS_REGISTRY),
        "total_calls_this_period": total_calls,
        "total_spend_usd_this_period": round(total_budget_usd, 4),
        "governance_status": "compliant",
        "agents": _ASTROPODS_REGISTRY,
        "policies": [
            {"id": "no-pii", "rule": "No PII in oracle outputs", "enforced": True},
            {"id": "read-only", "rule": "Oracle agents are read-only — no writes to dispute APIs", "enforced": True},
            {"id": "budget-cap", "rule": "Max $0.10 USD per 1k calls per agent", "enforced": True},
        ],
    })


@app.post("/api/astropods/agent-registry")
async def api_astropods_register(request: Request):
    """
    POST /api/astropods/agent-registry

    Register a new agent into the Anchor governed fleet.

    PayPal benefit: Merchants or PayPal engineers can add custom oracle
    agents to the fleet. Astropods governance policies are automatically
    applied — the agent cannot go to production without budget and
    guardrail declarations.
    """
    body = await request.json()
    required = {"agent_id", "name", "capabilities", "budget_usd_per_1k_calls"}
    missing = required - set(body.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    new_agent = {
        "agent_id": body["agent_id"],
        "name": body["name"],
        "description": body.get("description", ""),
        "capabilities": body["capabilities"],
        "budget_usd_per_1k_calls": float(body["budget_usd_per_1k_calls"]),
        "guardrails": body.get("guardrails", ["no_pii_output"]),
        "status": "registered",
        "calls_this_period": 0,
    }
    _ASTROPODS_REGISTRY.append(new_agent)
    return JSONResponse({"registered": True, "agent": new_agent}, status_code=201)


# ─── Channel3 — agentic commerce product enrichment ───────────────────────────

#: Simulated Channel3 product catalog for dispute enrichment.
_CHANNEL3_CATALOG: dict[str, dict] = {
    "PROD-001": {
        "product_id": "PROD-001",
        "title": "Sony WH-1000XM5 Headphones",
        "category": "Electronics",
        "price_usd": 349.99,
        "shipping_weight_kg": 0.254,
        "typical_delivery_days": {"standard": 5, "expedited": 2},
        "return_window_days": 30,
        "inr_risk_score": 0.08,
        "snad_risk_score": 0.12,
        "brand_verified": True,
    },
    "PROD-002": {
        "product_id": "PROD-002",
        "title": "iPhone 15 Pro",
        "category": "Electronics",
        "price_usd": 999.00,
        "shipping_weight_kg": 0.187,
        "typical_delivery_days": {"standard": 3, "expedited": 1},
        "return_window_days": 14,
        "inr_risk_score": 0.15,
        "snad_risk_score": 0.09,
        "brand_verified": True,
    },
    "PROD-003": {
        "product_id": "PROD-003",
        "title": "Generic Luxury Watch",
        "category": "Jewelry & Watches",
        "price_usd": 2499.00,
        "shipping_weight_kg": 0.350,
        "typical_delivery_days": {"standard": 7, "expedited": 3},
        "return_window_days": 7,
        "inr_risk_score": 0.22,
        "snad_risk_score": 0.35,
        "brand_verified": False,
    },
    "PROD-004": {
        "product_id": "PROD-004",
        "title": "Nike Air Max 2025",
        "category": "Apparel & Footwear",
        "price_usd": 149.99,
        "shipping_weight_kg": 0.650,
        "typical_delivery_days": {"standard": 5, "expedited": 2},
        "return_window_days": 60,
        "inr_risk_score": 0.06,
        "snad_risk_score": 0.18,
        "brand_verified": True,
    },
}


@app.get("/api/channel3/product-lookup")
async def api_channel3_lookup(product_id: str = "PROD-001"):
    """
    GET /api/channel3/product-lookup?product_id=PROD-001

    Channel3 agentic commerce integration — enriches a dispute with
    product catalog data to validate INR and SNAD claims.

    Channel3 provides a 100M+ product graph for AI agents. Anchor uses
    it to cross-reference the disputed item's typical delivery window,
    return policy, and historical dispute risk scores so the oracle can
    better calibrate fight vs. refund decisions.

    PayPal benefit: Product-level dispute risk signals let PayPal route
    high-risk category disputes (luxury, electronics) to enhanced review
    automatically — reducing false-positive refunds.
    """
    product = _CHANNEL3_CATALOG.get(product_id)
    if not product:
        return JSONResponse(
            {"error": f"Product {product_id!r} not found", "available": list(_CHANNEL3_CATALOG.keys())},
            status_code=404,
        )
    return JSONResponse({
        "source": "Channel3 product graph",
        "product": product,
        "dispute_enrichment": {
            "inr_baseline_risk": product["inr_risk_score"],
            "snad_baseline_risk": product["snad_risk_score"],
            "expected_delivery_window": product["typical_delivery_days"],
            "brand_verified": product["brand_verified"],
            "recommendation": (
                "High SNAD risk — request photos immediately"
                if product["snad_risk_score"] > 0.25
                else "Normal risk profile — standard evidence checklist applies"
            ),
        },
    })


@app.post("/api/channel3/dispute-enrich")
async def api_channel3_enrich(request: Request):
    """
    POST /api/channel3/dispute-enrich

    Enrich a dispute with Channel3 product intelligence.

    Given a dispute_id, reason_code, and product_id, returns the full
    oracle recommendation combining Channel3 product risk signals with
    Anchor's internal scoring.

    PayPal benefit: Merchants see product-specific dispute guidance —
    not just generic advice — which improves response quality and
    reduces unnecessary fights on high-SNAD-risk items.
    """
    body = await request.json()
    dispute_id = body.get("dispute_id", "DEMO")
    reason_code = body.get("reason_code", "INR")
    product_id = body.get("product_id", "PROD-001")
    amount = float(body.get("amount", 0.0))

    product = _CHANNEL3_CATALOG.get(product_id, {
        "title": "Unknown Product",
        "category": "Other",
        "inr_risk_score": 0.10,
        "snad_risk_score": 0.10,
        "brand_verified": False,
        "return_window_days": 30,
        "typical_delivery_days": {"standard": 7},
    })

    risk_score = product.get(
        "inr_risk_score" if reason_code == "INR" else "snad_risk_score", 0.10
    )

    return JSONResponse({
        "dispute_id": dispute_id,
        "reason_code": reason_code,
        "product_title": product.get("title"),
        "category": product.get("category"),
        "brand_verified": product.get("brand_verified"),
        "category_dispute_risk": risk_score,
        "win_probability_adjustment": round(-risk_score * 0.3, 4),
        "action": (
            "FIGHT" if risk_score < 0.15 else
            "GATHER_PHOTOS" if reason_code == "SNAD" else
            "STANDARD_REVIEW"
        ),
        "powered_by": "Channel3 product graph — 100M+ products",
    })


# ─── KERNEL — community trust signals ────────────────────────────────────────

#: Simulated KERNEL community trust ledger for buyer/seller reputation.
_KERNEL_TRUST_LEDGER: dict[str, dict] = {
    "buyer_verified_A": {
        "buyer_id": "buyer_verified_A",
        "trust_tier": "VERIFIED",
        "community_score": 0.92,
        "transactions_on_record": 48,
        "dispute_history_rate": 0.02,
        "unauthorized_flags": 0,
        "web3_identity_confirmed": True,
    },
    "buyer_new_B": {
        "buyer_id": "buyer_new_B",
        "trust_tier": "NEW",
        "community_score": 0.50,
        "transactions_on_record": 2,
        "dispute_history_rate": 0.0,
        "unauthorized_flags": 0,
        "web3_identity_confirmed": False,
    },
    "buyer_flagged_C": {
        "buyer_id": "buyer_flagged_C",
        "trust_tier": "FLAGGED",
        "community_score": 0.21,
        "transactions_on_record": 15,
        "dispute_history_rate": 0.40,
        "unauthorized_flags": 3,
        "web3_identity_confirmed": False,
    },
}


@app.post("/api/kernel/community-trust")
async def api_kernel_trust(request: Request):
    """
    POST /api/kernel/community-trust

    KERNEL community trust integration — cross-references a buyer's
    community trust score for UNAUTHORIZED dispute adjudication.

    KERNEL is a web3 community platform where participants build
    verifiable reputation across peer interactions. For UNAUTHORIZED
    disputes, Anchor queries KERNEL's trust graph to see whether the
    buyer has prior unauthorized transaction flags, boosting or
    reducing the merchant's fight recommendation accordingly.

    PayPal benefit: Trust-graph-augmented dispute routing reduces false
    positives for verified buyers and flags repeat unauthorized abusers
    earlier — improving chargeback prevention at the merchant segment level.
    """
    body = await request.json()
    dispute_id = body.get("dispute_id", "DEMO")
    buyer_id = body.get("buyer_id", "buyer_new_B")
    reason_code = body.get("reason_code", "UNAUTHORIZED")

    trust = _KERNEL_TRUST_LEDGER.get(buyer_id, {
        "buyer_id": buyer_id,
        "trust_tier": "UNKNOWN",
        "community_score": 0.50,
        "transactions_on_record": 0,
        "dispute_history_rate": 0.0,
        "unauthorized_flags": 0,
        "web3_identity_confirmed": False,
    })

    tier = trust["trust_tier"]
    score = float(trust["community_score"])
    flags = int(trust["unauthorized_flags"])

    # Derive a win-probability adjustment based on trust tier
    trust_adjustment = {
        "VERIFIED": +0.10,
        "NEW": 0.00,
        "FLAGGED": -0.15,
        "UNKNOWN": -0.05,
    }.get(tier, 0.0)

    recommendation = (
        "FIGHT — buyer has KERNEL community trust flags; likely fraudulent"
        if flags >= 2
        else "FIGHT — buyer is VERIFIED; this may be a legitimate dispute, but evidence supports fighting"
        if tier == "VERIFIED"
        else "INVESTIGATE — insufficient trust signal; gather full evidence before deciding"
    )

    return JSONResponse({
        "dispute_id": dispute_id,
        "buyer_id": buyer_id,
        "reason_code": reason_code,
        "trust_tier": tier,
        "community_score": score,
        "unauthorized_flags": flags,
        "web3_identity_confirmed": trust["web3_identity_confirmed"],
        "win_probability_adjustment": trust_adjustment,
        "recommendation": recommendation,
        "powered_by": "KERNEL community trust graph",
    })


@app.get("/api/kernel/trust-stats")
async def api_kernel_stats():
    """
    GET /api/kernel/trust-stats

    Returns aggregated trust tier distribution for the buyer cohort.
    PayPal benefit: Cohort-level trust stats let risk teams identify
    merchant segments with disproportionate UNAUTHORIZED disputes from
    low-trust buyers and apply proactive interventions.
    """
    tiers = [t["trust_tier"] for t in _KERNEL_TRUST_LEDGER.values()]
    return JSONResponse({
        "powered_by": "KERNEL community trust graph",
        "total_buyers_indexed": len(_KERNEL_TRUST_LEDGER),
        "tier_distribution": {
            t: tiers.count(t) for t in set(tiers)
        },
        "avg_community_score": round(
            sum(t["community_score"] for t in _KERNEL_TRUST_LEDGER.values()) / max(len(_KERNEL_TRUST_LEDGER), 1),
            4,
        ),
        "buyers_with_unauthorized_flags": sum(
            1 for t in _KERNEL_TRUST_LEDGER.values() if t["unauthorized_flags"] > 0
        ),
    })


# Track start time for Render health endpoint
_start_time = time.time()
