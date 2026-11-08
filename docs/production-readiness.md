# Anchor production readiness

**Last reviewed:** 2026-11-08
**Current operating mode:** PayPal Sandbox, shadow mode, autonomous actions disabled.

## Implemented in the current hardening pass

- PostgreSQL is the durable store for webhook events, approval jobs and used tokens, merchant policies, abandoned orders, and tracked dispute deadlines.
- Webhook and approval work is claimed with leases, retried with backoff, and moved to a dead-letter state after the retry limit. The Render blueprint now defines a dedicated worker for queue draining and scheduled tasks.
- Authenticated queue operations are available at `GET /api/admin/queues/dead-letters?queue=webhooks|approvals` and `POST /api/admin/queues/{queue}/dead-letters/{item_id}/retry`.
- Readiness checks database connectivity and schema, the safety state, and (when web scheduling is disabled) the worker heartbeat.
- Recovery requires explicit opt-in, complete PayPal order snapshots, matching configured seller identity, and matching amount/currency before it can capture. Recovery remains disabled by default.
- Deadline auto-accept is gated by the production safety report and `AUTONOMOUS_ACTIONS_ENABLED`; without both, the merchant receives a manual-action alert.
- Approval links use a shared PostgreSQL secret, expire according to `APPROVAL_LINK_MAX_AGE`, and are atomically consumed when queued.
- Dashboard data endpoints return actual stored rows or empty collections. Google Drive, Gmail, and Zapier status no longer claims connections based on hard-coded IDs.

## Verification

- `python3 -m compileall -q agents scripts oracle` — passed.
- `.venv/bin/python -m pytest tests/test_deadline_tracker.py tests/test_system_upgrades.py tests/test_production_hardening.py tests/test_recovery.py tests/test_deadline_safety.py -q` — 88 passed.
- Full `.venv/bin/python -m pytest -q` collection remains blocked by 15 existing import errors in older `test_wave*`, `test_strategic_layer.py`, and `test_undefeatable_v3.py` files. They import symbols missing from their corresponding `oracle` modules; these errors occur before test execution.
- No live PayPal credentials or autonomous money actions were enabled during this pass.

## Deployment and operations still required

1. Apply the updated `render.yaml` blueprint so both the web service and `anchor-worker` receive the same PostgreSQL database. Confirm the web service becomes ready and `worker_running` is true.
2. Verify the Render database plan and resulting monthly cost before confirming the blueprint change. The database and worker must stay in the same region.
3. Add the real PayPal merchant ID to `PAYPAL_MERCHANT_ID`, confirm Sandbox credentials and webhook registration, and test a signed Sandbox dispute and approved-order webhook end to end.
4. Load a real merchant policy into `merchant_policy` and verify `/health/ready` reports it. Do not use the environment flag as a substitute for a policy record.
5. Exercise webhook retry, the dead-letter inspection/replay endpoints, worker restart recovery, approval replay rejection, and concurrent recovery claims against a staging PostgreSQL instance.
6. Add alerting for failed readiness, stale worker heartbeat, and growing pending/dead-letter queues; document a human recovery/replay procedure.
7. Complete a privacy and retention review for buyer email, dispute evidence, and webhook payloads; define access control and deletion policy before serving merchants.
8. Keep `PAYPAL_ENV=sandbox`, `SHADOW_MODE=1`, `AUTONOMOUS_ACTIONS_ENABLED=0`, and `RECOVERY_MERCHANT_OPT_IN=0` until the above checks are complete and a merchant explicitly authorizes a controlled pilot.

## Release gate

Do not use Live PayPal credentials or enable automatic dispute/recovery actions until the deployment checks above pass in staging, a merchant policy is loaded and reviewed, queue operations are monitored, and the merchant has explicitly opted into the relevant action types.
