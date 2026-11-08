# Anchor Production Readiness Audit

**Date:** 2026-11-08  
**Service:** `anchor-paypal` on Render (`anchor-paypal.onrender.com`)  
**PayPal environment:** Sandbox (must remain sandbox until all items below are GREEN)

---

## Summary

| Stage | Area | Status |
|-------|------|--------|
| 1 | Fail-closed config (`MANUAL_REVIEW_CONFIG`) | ✅ DONE |
| 2 | Autonomous actions kill switch | ✅ DONE |
| 3 | Webhook outbox mark-processed | ✅ DONE |
| 3 | Outbox schema migration (retry/lease/dead-letter) | ⚠️ PARTIAL — DDL extended, drainer not yet separate process |
| 4 | Recovery storage migrated ES → PG | ✅ DONE |
| 5 | Recovery `_claim_recovery_order` via PG SELECT FOR UPDATE | ✅ DONE |
| 6 | Approval token one-time consumption | ✅ DONE |
| 6 | Approval token expiry aligned to `APPROVAL_LINK_MAX_AGE` (1 h) | ✅ DONE |
| 7 | Honest dashboard labels (no fabricated "Connected") | ✅ DONE |
| 7 | `/health/ready` checks PG, safety gate, merchant policy | ✅ DONE |
| 8 | `render.yaml` missing env vars added | ✅ DONE |
| 9 | 15 production-hardening tests passing | ✅ DONE |

---

## Confirmed findings

### Safety gate (oracle/production_safety.py)

- `check_from_env()` returns `SafetyLevel.BLOCKED` when hard checks fail.
- `check_from_env()` checks `DATABASE_URL` (not ES alone) for `durable_storage`.
- `SHADOW_MODE=1` forces `SafetyLevel.SHADOW` — no PayPal API calls.
- `HUMAN_REVIEW` path in `watcher.py` has an explicit `return` before Guardian (commit `b34d2ea`).

### Kill switch (agents/guardian.py)

- `AUTONOMOUS_ACTIONS_ENABLED` env var checked at top of `handle_dispute()`.
- Default `0` — all disputes route to `_request_approval()` until operator sets `=1`.
- Set to `0` in `render.yaml`.

### Config fail-closed (agents/config.py)

- `MANUAL_REVIEW_CONFIG` has `auto_accept_below=0.0`, `auto_fight_above=1_000_000_000.0`, `auto_fight_min_prob=1.01`.
- `load_config()` returns `MANUAL_REVIEW_CONFIG` when ES is unavailable, returns non-200, or raises.

### Webhook outbox (agents/pg_store.py + agents/watcher.py)

- `save_webhook_event_atomic()` — atomic INSERT ON CONFLICT DO NOTHING.
- `_run_and_mark_processed()` calls `mark_event_processed()` after each handler.
- **Gap:** Outbox DDL does not yet include `retry_count`, `next_attempt_at`, `lease_expires_at`, `claimed_by`, `dead_letter` columns. The outbox is functional for dedup + mark-processed but not for a full retry/dead-letter drainer worker.

### Recovery storage (agents/recovery.py)

- Migrated from Elasticsearch to PostgreSQL (`abandoned_orders` table).
- `_save_abandoned_order()` — INSERT ON CONFLICT DO NOTHING.
- `_get_stale_orders()` — PG query with approved_at ≤ cutoff filter.
- `_claim_recovery_order()` — `SELECT FOR UPDATE SKIP LOCKED` via `pg_store.claim_abandoned_order()`.
- `_record_capture_result()`, `_mark_recovery_state()`, `mark_order_captured()`, `_mark_no_longer_needed()` — all use PG.
- **Gap:** `RECOVERY_MERCHANT_OPT_IN` env var is set to `0` in `render.yaml` but not yet enforced in `scan_and_recover()`. Recovery still runs for all merchants unless ES is absent (which it now is by default).

### Approval tokens (agents/guardian.py + agents/watcher.py)

- Token expiry: `APPROVAL_LINK_MAX_AGE` env var (default 3600 s = 1 h).
- One-time use: `consume_approval_token()` atomically inserts `token_sig` into `approval_token_used`; replay returns 403.
- Tokens are HMAC-SHA256 signed, bound to `dispute_id:merchant_id:action:expiry`.

### Dashboard honest labels (agents/watcher.py)

- `/api/astropods/status` — `demo: true` when `ASTRO_GATEWAY_URL` unset.
- `/api/kernel/status` — `demo: true` when `KERNEL_API_KEY` unset.
- `/api/status` — `env` block removed (no secret exposure).
- `/api/prod/overview` — security config stripped.
- `/api/zapier/status` — internal fields stripped.

### Readiness check (/health/ready)

- Checks: `scheduler_running`, `paypal_creds_configured`, `safety_gate`, `safety_level`, `merchant_policy_loaded`.
- If `DATABASE_URL` set: also checks `postgresql_connected`.
- Returns 503 when any critical check fails.

---

## Open gaps (must resolve before live credentials)

| # | Gap | Severity | File |
|---|-----|----------|------|
| G1 | Outbox missing retry/lease/dead-letter columns | MEDIUM | `agents/pg_store.py` |
| G2 | No separate drainer worker; APScheduler runs in web process (no distributed lock) | MEDIUM | `render.yaml` |
| G3 | `RECOVERY_MERCHANT_OPT_IN` not yet enforced in `scan_and_recover()` | LOW | `agents/recovery.py` |
| G4 | No test for concurrent recovery (two scanners, same order) | LOW | `tests/` |
| G5 | Merchant policy not stored in PG; `MERCHANT_POLICY_LOADED` must be set manually | MEDIUM | `agents/config.py` |

---

## Release checklist (operator actions before switching `PAYPAL_ENV=live`)

- [ ] Verify all 15+ production-hardening tests pass in CI
- [ ] Set `AUTONOMOUS_ACTIONS_ENABLED=1` only after reviewing shadow logs
- [ ] Set `MERCHANT_POLICY_LOADED=1` only after loading real merchant config from ES or PG
- [ ] Rotate `APPROVAL_TOKEN_SECRET` before first live use
- [ ] Review `APPROVAL_LINK_MAX_AGE` (default 3600 s — reduce if merchant turnaround allows)
- [ ] Confirm `PAYPAL_ENV=sandbox` in Render dashboard; do not change to `live` until all checks are GREEN
- [ ] Run `graphify update .` to keep the knowledge graph current
