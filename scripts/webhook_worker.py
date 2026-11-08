"""Durable Render worker for webhook, approval, recovery, and deadline jobs."""

from __future__ import annotations

import asyncio
import logging
import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anchor.worker")
WORKER_ID = "anchor-worker"


async def _heartbeat(pg_store) -> None:
    await asyncio.to_thread(pg_store.heartbeat_worker, WORKER_ID)


async def _drain_webhooks(pg_store, watcher) -> None:
    rows = await asyncio.to_thread(pg_store.claim_pending_webhook_events, WORKER_ID, 20, 120)
    for row in rows:
        event_id = row["event_id"]
        try:
            await watcher.process_persisted_webhook_event(
                row["payload"], row.get("merchant_id") or os.environ.get("MERCHANT_ID", "default")
            )
        except watcher.UnsupportedWebhookEvent as exc:
            await asyncio.to_thread(pg_store.mark_webhook_dead_letter, event_id, WORKER_ID, str(exc))
        except Exception as exc:
            log.exception("Webhook event %s failed; scheduling retry", event_id)
            await asyncio.to_thread(pg_store.mark_webhook_failed, event_id, WORKER_ID, repr(exc))
        else:
            await asyncio.to_thread(pg_store.mark_event_processed, event_id, WORKER_ID)


async def _drain_approval_jobs(pg_store, watcher) -> None:
    if os.environ.get("SHADOW_MODE", "0").strip().lower() in {"1", "true", "yes"}:
        return
    rows = await asyncio.to_thread(pg_store.claim_pending_approval_jobs, WORKER_ID, 10, 180)
    for row in rows:
        try:
            await watcher._execute_approved_fight(row["dispute_id"])
        except Exception as exc:
            log.exception("Approval job %s failed; scheduling retry", row["job_id"])
            await asyncio.to_thread(
                pg_store.fail_approval_job, str(row["job_id"]), WORKER_ID, repr(exc)
            )
        else:
            await asyncio.to_thread(
                pg_store.complete_approval_job, str(row["job_id"]), WORKER_ID
            )


async def run_worker() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required by the Anchor worker")
    import agents.pg_store as pg_store
    import agents.watcher as watcher
    from agents.config import load_config

    if not await asyncio.to_thread(pg_store.init_db):
        raise RuntimeError("Could not initialize PostgreSQL schema")
    watcher.APPROVAL_TOKEN_SECRET = await watcher._load_or_create_approval_secret()

    merchant_id = os.environ.get("MERCHANT_ID", "default")
    config = await load_config(merchant_id)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(watcher._run_recovery_check, "cron", minute="*/15", id="recovery")
    scheduler.add_job(watcher._run_deadline_check, "cron", minute="*/30", id="deadlines")
    scheduler.add_job(
        watcher._send_morning_brief,
        "cron",
        hour=config.morning_brief_hour,
        minute=0,
        id="brief",
        timezone=ZoneInfo(config.morning_brief_timezone),
    )
    scheduler.start()
    log.info("Anchor worker started: scheduler and durable queues active")

    try:
        while True:
            try:
                await _heartbeat(pg_store)
                await _drain_webhooks(pg_store, watcher)
                await _drain_approval_jobs(pg_store, watcher)
            except Exception:
                log.exception("Worker loop failed; leased work will become available after expiry")
            await asyncio.sleep(5)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(run_worker())
