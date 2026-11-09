# Anchor production readiness

**Last reviewed:** 2026-11-09
**Current operating mode:** PayPal Sandbox, shadow mode, autonomous actions disabled.

## Implemented in the current hardening pass

- PostgreSQL is the durable store for webhook events, approval jobs and used tokens, merchant policies, abandoned orders, and tracked dispute deadlines.
- Webhook and approval work is claimed with leases, retried with backoff, and moved to a dead-letter state after the retry limit.
- Authenticated queue operations are available at `GET /api/admin/queues/dead-letters?queue=webhooks|approvals` and `POST /api/admin/queues/{queue}/dead-letters/{item_id}/retry`.
- Readiness checks database connectivity and schema, the safety state, scheduler liveness, PayPal credentials, and a recent queue-worker heartbeat whenever the webhook outbox is enabled.
- Recovery requires explicit opt-in, complete PayPal order snapshots, matching configured seller identity, and matching amount/currency before it can capture. Recovery remains disabled by default.
- Deadline auto-accept is gated by the production safety report and `AUTONOMOUS_ACTIONS_ENABLED`; without both, the merchant receives a manual-action alert.
- Approval links use a shared PostgreSQL secret, expire according to `APPROVAL_LINK_MAX_AGE`, and are atomically consumed when queued.
- Dashboard data endpoints return actual stored rows or empty collections.
- Google Drive, Gmail, and Zapier status no longer claims connections based on hard-coded IDs.
- `/api/admin/seed-policy` creates or refreshes only a placeholder. `PUT /api/admin/merchant-policy` stores a validated operator-reviewed policy and writes `_reviewed: true`; legacy records without that exact marker remain unreviewed.
- The web service owns scheduled jobs (`RUN_SCHEDULER: "1"`); `anchor-worker` drains durable queues with its scheduler disabled. Readiness checks the worker heartbeat regardless of which process owns scheduled jobs.

## Verification

- `python3 -m compileall -q agents scripts oracle` — passed.
- `.venv/bin/python -m pytest tests/test_deadline_tracker.py tests/test_system_upgrades.py tests/test_production_hardening.py tests/test_recovery.py tests/test_deadline_safety.py -q` — 95 passed.
- Full `.venv/bin/python -m pytest -q` collection remains blocked by 15 existing import errors in older `test_wave*`, `test_strategic_layer.py`, and `test_undefeatable_v3.py` files that pre-date this hardening pass.
- Live `/health/ready` has returned HTTP 200 with PostgreSQL connected and Shadow Mode active. The most recent response reported `policy_is_default: true`; the currently live worker heartbeat has not yet been confirmed by the updated readiness check.

## What is confirmed vs. what still needs operator action

**Confirmed in Sandbox/Shadow:**
- PostgreSQL connected; all schema tables initialized on every deploy.
- APScheduler running in-process (recovery every 15 min, deadlines every 30 min, brief at 08:00 UTC).
- Webhook outbox, dead-letter queue, and approval ledger tables operational.
- Safety flags conservative: `SHADOW_MODE=1`, `AUTONOMOUS_ACTIONS_ENABLED=0`, `RECOVERY_MERCHANT_OPT_IN=0`.

**Still required before production:**

1. **Operator-reviewed merchant policy** — Keep autonomous actions blocked until `policy_is_default: false`. Submit reviewed thresholds to `PUT /api/admin/merchant-policy` with the configured `X-API-Secret`; the endpoint validates values and marks the saved record reviewed.
2. **Queue worker** — Apply/sync the Blueprint and confirm `/health/ready` reports `worker_running: true`. The worker is required to drain durable webhook and approval queues even while the web scheduler is active.
3. **Signed Sandbox e2e test** — Add the real `PAYPAL_MERCHANT_ID`, confirm Sandbox credentials and webhook registration, send a signed Sandbox dispute event, verify the full outbox → worker → processed path end to end.
4. **Queue operations exercise** — Exercise webhook retry, dead-letter inspection/replay endpoints, worker restart recovery, approval replay rejection, and concurrent recovery claims against a staging PostgreSQL instance.
5. **Alerting** — Add monitoring for failed readiness, stale worker heartbeat, and growing pending/dead-letter queues; document a human recovery/replay procedure.
6. **Privacy/retention review** — Complete a review for buyer email, dispute evidence, and webhook payloads; define access control and deletion policy before serving merchants.

## Release gate

Do not use Live PayPal credentials or enable automatic dispute/recovery actions until the deployment checks above pass in staging, `policy_is_default` is `false` (a merchant has reviewed and stored a real policy), queue operations are monitored, and the merchant has explicitly opted into the relevant action types.
