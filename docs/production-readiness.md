# Anchor production readiness

**Last reviewed:** 2026-11-09
**Current operating mode:** PayPal Sandbox, shadow mode, autonomous actions disabled.

## Implemented in the current hardening pass

- PostgreSQL is the durable store for webhook events, approval jobs and used tokens, merchant policies, abandoned orders, and tracked dispute deadlines.
- Webhook and approval work is claimed with leases, retried with backoff, and moved to a dead-letter state after the retry limit.
- Authenticated queue operations are available at `GET /api/admin/queues/dead-letters?queue=webhooks|approvals` and `POST /api/admin/queues/{queue}/dead-letters/{item_id}/retry`.
- Readiness checks database connectivity and schema, the safety state, scheduler liveness, and PayPal credentials.
- Recovery requires explicit opt-in, complete PayPal order snapshots, matching configured seller identity, and matching amount/currency before it can capture. Recovery remains disabled by default.
- Deadline auto-accept is gated by the production safety report and `AUTONOMOUS_ACTIONS_ENABLED`; without both, the merchant receives a manual-action alert.
- Approval links use a shared PostgreSQL secret, expire according to `APPROVAL_LINK_MAX_AGE`, and are atomically consumed when queued.
- Dashboard data endpoints return actual stored rows or empty collections.
- Google Drive, Gmail, and Zapier status no longer claims connections based on hard-coded IDs.
- `/api/admin/seed-policy` seeds a placeholder policy on first boot and marks it `_auto_seeded: true`. `/health/ready` reports `policy_is_default: true` for such records so operators can distinguish a placeholder from a reviewed policy.
- `render.yaml` now reflects the live deployment: `RUN_SCHEDULER: "1"` on the web service (scheduler runs in-process; the Blueprint's anchor-worker definition remains for future paid-plan use).

## Verification

- `python3 -m compileall -q agents scripts oracle` — passed.
- `.venv/bin/python -m pytest tests/test_deadline_tracker.py tests/test_system_upgrades.py tests/test_production_hardening.py tests/test_recovery.py tests/test_deadline_safety.py -q` — 88 passed.
- Full `.venv/bin/python -m pytest -q` collection remains blocked by 15 existing import errors in older `test_wave*`, `test_strategic_layer.py`, and `test_undefeatable_v3.py` files that pre-date this hardening pass.
- `/health/ready` returns HTTP 200 with `scheduler_running: true`, `paypal_creds_configured: true`, `safety_gate: true`, `postgresql_connected: true`, `merchant_policy_loaded: true`.
- `policy_is_default: true` will appear while only the auto-seeded placeholder policy exists.

## What is confirmed vs. what still needs operator action

**Confirmed in Sandbox/Shadow:**
- PostgreSQL connected; all schema tables initialized on every deploy.
- APScheduler running in-process (recovery every 15 min, deadlines every 30 min, brief at 08:00 UTC).
- Webhook outbox, dead-letter queue, and approval ledger tables operational.
- Safety flags conservative: `SHADOW_MODE=1`, `AUTONOMOUS_ACTIONS_ENABLED=0`, `RECOVERY_MERCHANT_OPT_IN=0`.

**Still required before production:**

1. **Operator-reviewed merchant policy** — `policy_is_default: true` in `/health/ready` signals that only the auto-seeded placeholder exists. Load a real, merchant-reviewed policy via `POST /api/admin/seed-policy` after updating the config, or directly upsert into `merchant_policy`. Once loaded, `policy_is_default` will become `false`.
2. **render.yaml sync** — Blueprint now matches live env (`RUN_SCHEDULER: "1"` on web). Apply the Blueprint in Render to pick up the change and confirm the web service deploys cleanly.
3. **Signed Sandbox e2e test** — Add the real `PAYPAL_MERCHANT_ID`, confirm Sandbox credentials and webhook registration, send a signed Sandbox dispute event, verify the full outbox → scheduler → processed path end to end.
4. **Queue operations exercise** — Exercise webhook retry, dead-letter inspection/replay endpoints, worker restart recovery, approval replay rejection, and concurrent recovery claims against a staging PostgreSQL instance.
5. **Alerting** — Add monitoring for failed readiness, stale worker heartbeat, and growing pending/dead-letter queues; document a human recovery/replay procedure.
6. **Privacy/retention review** — Complete a review for buyer email, dispute evidence, and webhook payloads; define access control and deletion policy before serving merchants.
7. **Background Worker (future)** — `anchor-worker` in `render.yaml` requires a paid Render plan. When ready to separate the scheduler into a dedicated process, upgrade the plan and deploy the worker; the web service can then revert to `RUN_SCHEDULER: "0"`.

## Release gate

Do not use Live PayPal credentials or enable automatic dispute/recovery actions until the deployment checks above pass in staging, `policy_is_default` is `false` (a merchant has reviewed and stored a real policy), queue operations are monitored, and the merchant has explicitly opted into the relevant action types.
