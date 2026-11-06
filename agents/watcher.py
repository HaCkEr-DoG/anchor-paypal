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
