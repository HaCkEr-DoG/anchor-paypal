"""
Render Workflow: Batch Dispute Enrichment Pipeline

Orchestrates parallel evidence-gathering tasks for open PayPal disputes:
  1. Channel3 product catalog lookup (item validation)
  2. Kernel browser screenshot (delivery confirmation)
  3. Oracle score refresh for disputes with stale / null win_probability
  4. Audit log flush (compact old entries, enforce ANCHOR_RETAIN_DAYS)

Dispatched by Render Workflows across concurrent instances. Each task is a
standard Python function — no framework lock-in. The workflow exits cleanly
after completing one pass; Render's cron triggers it on schedule.

Usage (Render Workflow definition in render.yaml or Render dashboard):
  startCommand: python -m scripts.dispute_workflow
  schedule: "0 */6 * * *"   # every 6 hours
"""

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
_log = logging.getLogger("dispute-workflow")


async def _enrich_with_channel3(dispute: dict) -> dict:
    """Fetch Channel3 product data for the disputed item."""
    try:
        from agents.channel3 import Channel3Client
        client = Channel3Client()
        if not client.enabled():
            return {"dispute_id": dispute.get("dispute_id", ""), "channel3": "not_configured"}
        item_desc = dispute.get("item_description") or dispute.get("reason", "")
        if not item_desc:
            return {"dispute_id": dispute.get("dispute_id", ""), "channel3": "no_description"}
        result = await client.enrich_dispute(
            dispute.get("dispute_id", ""),
            item_desc,
            float(dispute.get("amount", 0)),
        )
        return {
            "dispute_id": dispute.get("dispute_id", ""),
            "channel3": "ok" if result.succeeded else "not_found",
            "product_title": result.product.title if result.product else None,
        }
    except Exception as exc:
        _log.warning("Channel3 enrich failed for %s: %s", dispute.get("dispute_id"), exc)
        return {"dispute_id": dispute.get("dispute_id", ""), "channel3": "error", "error": str(exc)}


async def _capture_kernel_screenshot(dispute: dict) -> dict:
    """Use Kernel browser to screenshot carrier tracking page for INR disputes."""
    try:
        from agents.kernel_browser import KernelBrowser
        kb = KernelBrowser()
        if not kb.enabled():
            return {"dispute_id": dispute.get("dispute_id", ""), "kernel": "not_configured"}
        reason = dispute.get("reason", "").upper()
        if "NOT_RECEIVED" not in reason and "INR" not in reason:
            return {"dispute_id": dispute.get("dispute_id", ""), "kernel": "skipped_not_inr"}
        tracking = dispute.get("tracking_number") or dispute.get("tracking_info", {}).get("tracking_number")
        if not tracking:
            return {"dispute_id": dispute.get("dispute_id", ""), "kernel": "no_tracking_number"}
        result = await kb.screenshot_tracking(tracking)
        return {
            "dispute_id": dispute.get("dispute_id", ""),
            "kernel": "ok" if result.get("success") else "failed",
            "screenshot_url": result.get("screenshot_url"),
        }
    except Exception as exc:
        _log.warning("Kernel screenshot failed for %s: %s", dispute.get("dispute_id"), exc)
        return {"dispute_id": dispute.get("dispute_id", ""), "kernel": "error", "error": str(exc)}


async def _refresh_oracle_score(dispute: dict) -> dict:
    """Re-run oracle for disputes with null/zero win_probability and persist the result."""
    dispute_id = dispute.get("dispute_id", "")
    existing_wp = dispute.get("oracle_win_probability") or dispute.get("win_probability")
    if existing_wp is not None and float(existing_wp) > 0.001:
        return {"dispute_id": dispute_id, "oracle": "skipped_already_scored"}
    try:
        from oracle.decision import calculate as oracle_calculate, ClaimType, Evidence
        amount = float(dispute.get("amount", 0) or dispute.get("amount_usd", 0))
        reason = (dispute.get("claim_type") or dispute.get("reason") or "ITEM_NOT_RECEIVED").upper()
        valid_types = {e.value: e for e in ClaimType}
        ct = valid_types.get(reason, ClaimType.ITEM_NOT_RECEIVED)
        evidence = Evidence()
        result = oracle_calculate(dispute_amount=amount, claim_type=ct, evidence=evidence)
        # Persist the score back to the audit log so the dashboard can show it
        if os.environ.get("DATABASE_URL"):
            import agents.pg_store as pg_store
            await asyncio.to_thread(
                pg_store.update_audit_oracle_score,
                dispute_id,
                result.win_probability,
                result.ev_fight,
                result.recommendation.value if hasattr(result.recommendation, "value") else str(result.recommendation),
            )
        return {
            "dispute_id": dispute_id,
            "oracle": "ok",
            "win_probability": result.win_probability,
            "ev_fight": result.ev_fight,
            "recommendation": result.recommendation.value if hasattr(result.recommendation, "value") else str(result.recommendation),
        }
    except Exception as exc:
        _log.warning("Oracle refresh failed for %s: %s", dispute_id, exc)
        return {"dispute_id": dispute_id, "oracle": "error", "error": str(exc)}


async def run_workflow():
    """Main workflow entry point."""
    _log.info("Dispute enrichment workflow starting")

    # Load open disputes from DB
    disputes: list[dict] = []
    try:
        import agents.pg_store as pg_store
        import asyncio as _asyncio
        rows = await _asyncio.to_thread(pg_store.get_audit_entries, 100)
        disputes = [r for r in (rows or []) if r.get("outcome") in (None, "", "OPEN", "PENDING")]
        _log.info("Loaded %d open disputes for enrichment", len(disputes))
    except Exception as exc:
        _log.error("Failed to load disputes: %s", exc)
        return

    if not disputes:
        _log.info("No open disputes — workflow complete")
        return

    # Fan out: run all three enrichment tasks per dispute in parallel
    tasks = []
    for d in disputes[:20]:  # cap at 20 to stay within Render free tier
        tasks.append(_enrich_with_channel3(d))
        tasks.append(_capture_kernel_screenshot(d))
        tasks.append(_refresh_oracle_score(d))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(
        1 for r in results
        if isinstance(r, dict) and (r.get("channel3") == "ok" or r.get("oracle") == "ok" or r.get("kernel") == "ok")
    )
    _log.info("Workflow complete — %d/%d tasks succeeded", ok, len(tasks))


if __name__ == "__main__":
    asyncio.run(run_workflow())
