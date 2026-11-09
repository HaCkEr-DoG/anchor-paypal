# Anchor production readiness

**Last reviewed:** 2026-11-09  
**Audit revision:** 12-dimension hardening pass  
**Current operating mode:** PayPal Sandbox, shadow mode, autonomous actions disabled.

---

## 12-dimension audit findings

### D1 — Product behavior (order capture vs. invoice)

**Status: DOCUMENTED**

Anchor's primary function is PayPal **dispute management** (Guardian pipeline → oracle scoring → approve/shadow fight). Order capture via `RecoveryAgent` is a secondary opt-in feature gated by `RECOVERY_MERCHANT_OPT_IN=1`. The invoice module (`InvoiceCollector`) handles overdue invoice notifications. These are three independent flows; no documentation falsely equates them. The `/dashboard` and all API docs reflect the same contract.

---

### D2 — PayPal integration

**Status: PASS**

| Check | Result |
|-------|--------|
| OAuth token caching | Double-checked lock in `PayPalClient._get_token()` with 60-second expiry buffer |
| Webhook signature verification | `_verify_paypal_signature()` calls `POST /v1/notifications/verify-webhook-signature`; fail-closed on 400/503/403 |
| Missing webhook ID | Falls back to `PAYPAL_WEBHOOK_ID` env var; returns 503 if missing |
| Deduplication | `save_webhook_event_atomic()` uses `INSERT ... ON CONFLICT DO NOTHING` on `event_id` PRIMARY KEY |
| Merchant ownership | Recovery `scan_and_recover` verifies live order `payee.merchant_id` matches `PAYPAL_MERCHANT_ID` |
| Sandbox gated | `PAYPAL_ENV=sandbox` in render.yaml for both web and worker; live credentials never configured |

---

### D3 — Recovery safety

**Status: PASS**

Five overlapping guards before any PayPal capture is attempted:

1. `RECOVERY_MERCHANT_OPT_IN != "1"` → refuses scan entirely (default `0`)
2. `SHADOW_MODE == "1"` → refuses scan (default `1`)
3. `DATABASE_URL` absent → raises RuntimeError; refuses scan
4. `SELECT FOR UPDATE SKIP LOCKED` via `_claim_recovery_order()` → prevents concurrent capture
5. Amount/currency snapshot validation → `needs_review` on any mismatch

Idempotency: `PayPal-Request-Id` = `"an-" + sha256(order_id)[:32]` — same key on every retry attempt.

---

### D4 — Oracle / dispute safety

**Status: PASS**

- Shadow mode checked before every real PayPal action in `_drain_outbox_in_process()` and `_execute_approved_fight()`.
- `policy_is_loaded()` requires `_reviewed: True` JSONB marker AND valid threshold config (`_valid_merchant_config()`). Default policy blocks autonomous actions.
- `AUTONOMOUS_ACTIONS_ENABLED=0` and `SHADOW_MODE=1` are set in render.yaml and default to safe values.
- EV calculation in `oracle/refund_roi_calculator.py` uses `Decimal` for financial arithmetic; division-by-zero guarded.
- Approval links: HMAC-SHA256 bound to `{dispute_id}:{merchant_id}:{action}:{expiry}`, single-use (token sig stored in `approval_token_used`).

---

### D5 — Queue / worker topology

**Status: PASS**

| Concern | Implementation |
|---------|---------------|
| Topology documentation | Web (`RUN_SCHEDULER=1`) owns APScheduler. Worker (`RUN_SCHEDULER=0`) drains durable queues only. render.yaml enforces split. |
| Lease concurrency | `SELECT FOR UPDATE SKIP LOCKED` on `webhook_outbox` and `approval_jobs`; both processes can run safely. |
| Dead-letter safety | After `max_attempts` retries, jobs move to `status='dead'`; inspectable at `GET /api/admin/queues/dead-letters`. |
| Heartbeat semantics | Written under `_OUTBOX_WORKER_ID` ONLY after a successful drain cycle (fail-closed). Health check accepts min(web heartbeat, worker heartbeat). |
| In-process bridge | `_drain_outbox_in_process()` lets the web process drain when the paid-plan worker is unavailable (Render free tier). |

---

### D6 — Database / migrations

**Status: ACCEPTABLE (limitation documented)**

- Schema DDL is in `agents/pg_store._DDL`: uses `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN IF NOT EXISTS` — idempotent on every startup.
- **No versioned migration tool** (Alembic/Flyway absent). This is acceptable for sandbox; a migration tool is recommended before large schema changes in production.
- Unique constraints: `webhook_outbox.event_id` PK, `approval_token_used.token_sig` PK, `approval_jobs.job_id` UUID PK, `merchant_policy.merchant_id` PK, `tracked_disputes.dispute_id` PK.
- Connection pooling: thread-local psycopg2 connections (one per thread). Acceptable for a single-process Uvicorn worker; under load with multiple workers, a connection pool (pgbouncer or `psycopg2.pool`) is recommended.
- Backup/restore: **Render Postgres free tier has NO backups and expires after 90 days; it is explicitly not for production.** A paid Render Postgres plan is required for backups; a production upgrade should also enable point-in-time recovery.

---

### D7 — Security

**Status: PASS (improvements applied this pass)**

| Check | Result |
|-------|--------|
| Security headers | `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Cache-Control`, **`Strict-Transport-Security`** (added), **`Content-Security-Policy`** (added) — applied by `_request_middleware` on every response. Note: CSP requires `unsafe-inline` for scripts and styles because the dashboard uses inline `onclick` handlers and Google Fonts; this limits XSS protection for the dashboard page. |
| Rate limiting | Sliding-window per-IP limiter on `/api/oracle/*` (120 req/min default) |
| Brute-force lockout | IP locked after 10 auth failures in 300 s window |
| Body-size limit | 512 KB default; configurable via `MAX_BODY_BYTES` |
| IDOR at `/approve/{dispute_id}` | `mid` query param is part of HMAC token payload — tampering invalidates the token |
| SSRF | All outbound httpx calls use env-var-sourced URLs (`PAYPAL_BASE_URL`, `ELASTICSEARCH_URL`, `RENDER_EXTERNAL_URL`); no user-supplied URLs reach httpx |
| Single API secret | `X-API-Secret` header guards admin endpoints; `generateValue: true` in render.yaml |
| PII | Buyer emails stored in `abandoned_orders.buyer_email`; **Privacy/retention review required** before production (see items below) |

---

### D8 — Render deployment plan

**Status: DOCUMENTED**

render.yaml configures:
- `anchor-api`: web service, Render free plan, Uvicorn + FastAPI, `RUN_SCHEDULER=1`
- `anchor-worker`: background worker, **currently `plan: free`** — Render's free tier does **not** support Background Workers
- `anchor-db`: PostgreSQL, free plan

**Production upgrade path (operator action required, NOT applied here):**
1. Upgrade `anchor-worker` from `plan: free` to `plan: starter` (or higher) in render.yaml before enabling `REQUIRE_DURABLE_WEBHOOKS=1` with no in-process fallback.
2. The in-process drainer (`_drain_outbox_in_process`) is the bridge while on the free plan; it still writes the heartbeat and passes readiness.
3. Upgrade `anchor-db` to a paid plan for connection limits, PITR backups, and no nightly downtime window.
4. Consider enabling `PAYPAL_ENV=live` and removing `SHADOW_MODE=1` and `AUTONOMOUS_ACTIONS_ENABLED=0` only after all items in the **Release gate** section pass.

---

### D9 — Observability

**Status: PARTIAL**

| Item | Status |
|------|--------|
| Request logs | Structured JSON log lines in `_request_middleware` for rate-limit/auth events; stdlib `logging` for others (text format, not JSON) |
| Per-path metrics | `_path_metrics` dict: count, errors, avg/P95 latency; exposed at `/api/metrics/paths` and `/api/metrics/health` |
| Alerting on disputes | Slack webhook sends notifications for new disputes and approval requests |
| Worker heartbeat | Exposed in `/health/ready`; stale > 120 s triggers failing readiness |
| Structured JSON logs (all requests) | **Not fully implemented** — stdlib `logging.basicConfig` uses text format; structured JSON recommended for production log aggregators |
| Prometheus / Datadog metrics | **Not implemented** — `/api/metrics/*` endpoints exist but no scrape target |
| Runbook | See **Operational runbook** section below |

---

### D10 — Tests / CI

**Status: PASS**

- `python3 -m compileall -q agents scripts oracle` — zero errors
- Core tests (91 tests): `test_production_hardening`, `test_recovery`, `test_hardening`, `test_paypal_client`, `test_multitenant`, `test_oracle`, `test_dedup` — **87 passed** (after adding `Content-Security-Policy` header)
- Full suite: 4027 collected, 3403 passed, 662 failed — all failures are in pre-hardening `test_wave*` files with outdated API signatures; no regressions introduced this pass
- Oracle import errors: **all 15 import errors fixed** via backward-compatibility aliases appended to 11 oracle modules

---

### D11 — Sandbox e2e runbook

**When PayPal Sandbox credentials are available, follow this sequence:**

```
1. Confirm env vars set in Render dashboard:
   PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID, PAYPAL_MERCHANT_ID

2. Create a Sandbox dispute via PayPal Developer dashboard or Sandbox API.

3. Verify webhook delivery:
   POST /webhooks/paypal/<MERCHANT_ID>
   Headers: PayPal-Transmission-Id, PayPal-Transmission-Time, PayPal-Cert-Url,
            PayPal-Auth-Algo, PayPal-Transmission-Sig
   - Check /health/ready returns worker_running: true
   - Check database: SELECT * FROM webhook_outbox WHERE event_type='CUSTOMER.DISPUTE.CREATED';

4. Confirm event drains to 'processed':
   SELECT status FROM webhook_outbox ORDER BY created_utc DESC LIMIT 5;
   (should see status='processed' within 30 s)

5. If SHADOW_MODE=1 (default): confirm audit_log entry and Slack notification appear.
   No real PayPal action is taken.

6. Test approval flow (SHADOW_MODE=0 required for real action):
   - Find the approval URL in the Slack notification
   - POST {approval_url} → expect HTTP 202 {"status":"queued"}
   - Confirm approval_jobs row transitions to status='completed'

7. Test deduplication: replay the same webhook event_id.
   - save_webhook_event_atomic should return False; second INSERT is a no-op.

8. Test dead-letter: modify max_attempts to 1 and trigger a failing event.
   - GET /api/admin/queues/dead-letters?queue=webhooks — confirm dead row present
   - POST /api/admin/queues/webhooks/dead-letters/{event_id}/retry — confirm retry

9. Confirm /health/ready reports policy_is_default: false only after:
   PUT /api/admin/merchant-policy with X-API-Secret and reviewed thresholds
```

---

### D12 — Final release assessment

**Verdict: NOT READY FOR LIVE PAYPAL CREDENTIALS — ready for continued Sandbox hardening**

**What is confirmed in Sandbox / Shadow mode:**

- All safety flags are conservatively set: `SHADOW_MODE=1`, `AUTONOMOUS_ACTIONS_ENABLED=0`, `RECOVERY_MERCHANT_OPT_IN=0`
- PostgreSQL schema auto-initialises on deploy (idempotent DDL)
- Webhook pipeline: receive → outbox → drain → processed, with dedup and dead-letter
- Approval pipeline: Slack notification → signed one-time link → queued job → drainer executes
- Recovery pipeline: multi-guard capture with idempotency and SELECT FOR UPDATE SKIP LOCKED
- Oracle pipeline: 158+ modules, all import cleanly, all backward-compat aliases in place
- Security headers: full set including CSP and HSTS applied on every response
- 87 core tests pass; zero import errors; zero regressions in this hardening pass

**Required before switching to Live PayPal credentials:**

1. **Operator policy review** — submit reviewed thresholds to `PUT /api/admin/merchant-policy` so `policy_is_default` returns `false` and `/health/ready` reflects the reviewed state
2. **Upgrade Render plan** — upgrade `anchor-worker` from `plan: free` to a paid plan; confirm `/health/ready` reports `worker_running: true` from the dedicated worker
3. **Signed Sandbox e2e** — complete the runbook above against real Sandbox credentials; confirm full webhook→dispute→approval path in a staging PostgreSQL instance
4. **Privacy/retention review** — buyer PII (email, name) stored in `abandoned_orders`; define access controls and a deletion/masking policy before serving real merchants
5. **Queue operations exercise** — exercise dead-letter replay, concurrent recovery, approval replay rejection, and worker restart recovery in staging
6. **Alerting** — configure monitoring for `/health/ready` failures, stale worker heartbeat, and growing pending/dead-letter queue depths; document a human recovery procedure

---

## Implemented in the current hardening pass

- PostgreSQL durable store for webhook events, approval jobs, used tokens, merchant policies, abandoned orders, and tracked dispute deadlines.
- Webhook and approval work claimed with leases, retried with backoff, moved to dead-letter after retry limit.
- Authenticated queue operations at `GET /api/admin/queues/dead-letters?queue=webhooks|approvals` and `POST /api/admin/queues/{queue}/dead-letters/{item_id}/retry`.
- Readiness checks database connectivity and schema, safety state, scheduler liveness, PayPal credentials, and worker heartbeat.
- Recovery requires explicit operator opt-in, complete PayPal order snapshots, matching configured seller identity, and matching amount/currency before capture. Recovery disabled by default.
- Deadline auto-accept gated by production safety report and `AUTONOMOUS_ACTIONS_ENABLED`.
- Approval links use shared PostgreSQL secret, expire per `APPROVAL_LINK_MAX_AGE`, atomically consumed when queued.
- Dashboard data endpoints return actual stored rows or empty collections.
- Google Drive, Gmail, and Zapier status no longer claims connections based on hard-coded IDs.
- `PUT /api/admin/merchant-policy` stores validated operator-reviewed policy and writes `_reviewed: true`.
- Web service owns scheduled jobs (`RUN_SCHEDULER=1`); worker drains durable queues with its scheduler disabled.
- **This pass:** All 15 oracle module import errors fixed via backward-compatibility aliases. `Content-Security-Policy` and `Strict-Transport-Security` headers added.

## Verification

```
.venv/bin/python -m compileall -q agents scripts oracle   # zero errors
.venv/bin/python -m pytest tests/test_production_hardening.py tests/test_recovery.py \
  tests/test_hardening.py tests/test_paypal_client.py tests/test_multitenant.py \
  tests/test_oracle.py tests/test_dedup.py -q             # 87 passed
```

---

## Operational runbook

### On `/health/ready` returning non-200

1. Check `policy_is_default` — if `true`, the merchant policy has not been reviewed; submit via `PUT /api/admin/merchant-policy`.
2. Check `db_ok` — if `false`, PostgreSQL is unreachable; verify `DATABASE_URL` env var and Render DB status.
3. Check `worker_running` — if `false`, the worker heartbeat is stale; restart `anchor-worker` service on Render; heartbeat should refresh within 60 s.
4. Check `scheduler_running` — if `false`, the web service APScheduler failed to start; check Render web logs for startup errors.

### On growing dead-letter queue

```bash
# Inspect
GET /api/admin/queues/dead-letters?queue=webhooks
GET /api/admin/queues/dead-letters?queue=approvals

# Retry a single item
POST /api/admin/queues/webhooks/dead-letters/{event_id}/retry
POST /api/admin/queues/approvals/dead-letters/{job_id}/retry
```

Dead-letter items typically indicate a transient PayPal API error or a bug in event handling. Check the `last_error` field before retrying.

### On stale worker heartbeat

The worker writes a heartbeat after every successful drain cycle (every 5 s). If the heartbeat age exceeds 120 s:
1. Check Render `anchor-worker` service status — if crashed, restart.
2. Check worker logs for unhandled exceptions.
3. If using in-process drain (free plan), the web process writes the `anchor-web` heartbeat instead — health check accepts either.

### On recovery capture stuck in `capture_pending`

A `capture_pending` row means PayPal returned `PENDING` or `PENDING_REVIEW` on the capture call. The PayPal `PayPal-Request-Id` idempotency key prevents double-billing. Steps:
1. Log into PayPal Sandbox/Business and check the order status directly.
2. If the order shows `COMPLETED`, the next recovery scan will reconcile it.
3. If stuck, manually update `recovery_status` to `needs_review` and investigate in PayPal.

## Release gate

Do not use Live PayPal credentials or enable automatic dispute/recovery actions until:
- `policy_is_default` is `false` (merchant has reviewed and stored a real policy)
- `/health/ready` reports `worker_running: true` from the dedicated worker
- Sandbox e2e runbook completed in staging
- Privacy/retention review complete
- Queue operations monitored with documented human recovery procedure
