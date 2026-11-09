#!/usr/bin/env python3
"""
Anchor Sandbox Smoke Test
=========================
Verifies that Anchor can talk to the PayPal Sandbox API end-to-end.

Run:
    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python scripts/sandbox_smoke_test.py

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
"""

import asyncio
import os
import sys
import json

# Allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m–\033[0m"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


async def run_checks():
    from agents.paypal_client import PayPalClient

    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    env = os.environ.get("PAYPAL_ENV", "sandbox")

    print(f"\nAnchor Sandbox Smoke Test  [{env.upper()}]\n")

    # ── 1. Credentials present ────────────────────────────────────────────────
    check("PAYPAL_CLIENT_ID set", bool(client_id))
    check("PAYPAL_CLIENT_SECRET set", bool(client_secret))

    if not (client_id and client_secret):
        print("\n  Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to continue.\n")
        return

    client = PayPalClient(client_id=client_id, client_secret=client_secret, env=env)

    # ── 2. OAuth2 token ───────────────────────────────────────────────────────
    try:
        token = await client.get_token()
        check("OAuth2 token obtained", bool(token), f"{token[:8]}…" if token else "")
    except Exception as exc:
        check("OAuth2 token obtained", False, str(exc))
        print("\n  Auth failed — remaining checks skipped.\n")
        return

    # ── 3. List disputes ──────────────────────────────────────────────────────
    try:
        data = await client.list_disputes(page_size=5)
        items = data.get("items", [])
        total = data.get("total_items", len(items))
        check("list_disputes API call", True, f"{total} total disputes")
    except Exception as exc:
        check("list_disputes API call", False, str(exc))

    # ── 4. List webhooks ──────────────────────────────────────────────────────
    try:
        wh = await client.list_webhooks()
        hooks = wh.get("webhooks", [])
        check("list_webhooks API call", True, f"{len(hooks)} registered")
    except Exception as exc:
        check("list_webhooks API call", False, str(exc))

    # ── 5. PAYPAL_WEBHOOK_ID set (not critical for smoke but flags missing setup) ─
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "")
    check("PAYPAL_WEBHOOK_ID configured", bool(webhook_id),
          "run POST /api/admin/register-webhook to set" if not webhook_id else "")

    # ── 6. SHADOW_MODE check ──────────────────────────────────────────────────
    shadow = os.environ.get("SHADOW_MODE", "1")
    check("SHADOW_MODE=1 (safe)", shadow in {"1", "true", "yes"},
          "set SHADOW_MODE=1 before going live")

    # ── 7. DATABASE_URL set ───────────────────────────────────────────────────
    db_url = os.environ.get("DATABASE_URL", "")
    check("DATABASE_URL configured", bool(db_url),
          "SQLite fallback active" if not db_url else "")

    # ── 8. Postgres connectivity (optional) ───────────────────────────────────
    if db_url:
        try:
            import agents.pg_store as pg_store
            await asyncio.to_thread(pg_store.init_db)
            check("PostgreSQL connection", True)
        except Exception as exc:
            check("PostgreSQL connection", False, str(exc))
    else:
        results.append(("PostgreSQL connection", None, "skipped — no DATABASE_URL"))
        print(f"  {SKIP}  PostgreSQL connection  (skipped — no DATABASE_URL)")


async def main():
    await run_checks()

    passed = sum(1 for _, ok, _ in results if ok is True)
    failed = sum(1 for _, ok, _ in results if ok is False)
    skipped = sum(1 for _, ok, _ in results if ok is None)

    print(f"\n  {passed} passed  {failed} failed  {skipped} skipped\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
