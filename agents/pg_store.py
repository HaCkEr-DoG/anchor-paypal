"""
PostgreSQL durable storage for Anchor.

Replaces ephemeral JSONL + in-memory fallbacks on Render free plan.
Requires DATABASE_URL env var (Render PostgreSQL connection string).

Tables created on first call to init_db():
  webhook_outbox  — durable outbox for inbound PayPal webhook events
  audit_log       — persistent copy of oracle decision audit entries
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_conn_local = threading.local()


def _get_conn():
    import psycopg2

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")

    conn = getattr(_conn_local, "conn", None)
    try:
        if conn is None or conn.closed:
            raise psycopg2.OperationalError("connection missing or closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:
                pass
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        _conn_local.conn = conn
        return conn


_DDL = """
CREATE TABLE IF NOT EXISTS webhook_outbox (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    merchant_id     TEXT NOT NULL DEFAULT '',
    received_utc    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL DEFAULT 'pending',
    payload         JSONB NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner     TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error      TEXT,
    processed_utc   TIMESTAMPTZ
);

ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS merchant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE webhook_outbox ADD COLUMN IF NOT EXISTS processed_utc TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS webhook_outbox_pending_idx ON webhook_outbox(status, next_attempt_at, received_utc);

CREATE TABLE IF NOT EXISTS service_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dispute_velocity (
    bucket_utc      TIMESTAMPTZ NOT NULL,
    merchant_id     TEXT NOT NULL,
    dispute_count   INTEGER NOT NULL DEFAULT 1,
    amount_total    DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_utc, merchant_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id                TEXT PRIMARY KEY,
    dispute_id              TEXT NOT NULL,
    timestamp_utc           TIMESTAMPTZ NOT NULL,
    action                  TEXT NOT NULL,
    oracle_ev_fight         DOUBLE PRECISION,
    oracle_win_probability  DOUBLE PRECISION,
    oracle_recommendation   TEXT,
    guardian_reason         TEXT,
    amount_usd              DOUBLE PRECISION,
    claim_type              TEXT,
    paypal_api_calls        JSONB,
    outcome                 TEXT DEFAULT 'PENDING',
    outcome_updated_utc     TIMESTAMPTZ,
    notes                   TEXT DEFAULT '',
    is_sandbox              BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS audit_log_dispute_idx ON audit_log(dispute_id);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log(timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS abandoned_orders (
    order_id            TEXT PRIMARY KEY,
    merchant_id         TEXT NOT NULL,
    amount              DOUBLE PRECISION NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'USD',
    buyer_email         TEXT NOT NULL DEFAULT '',
    buyer_name          TEXT NOT NULL DEFAULT '',
    items               JSONB NOT NULL DEFAULT '[]',
    approved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recovery_attempts   INTEGER NOT NULL DEFAULT 0,
    recovered           BOOLEAN NOT NULL DEFAULT FALSE,
    recovery_status     TEXT NOT NULL DEFAULT 'pending_recovery',
    capture_id          TEXT NOT NULL DEFAULT '',
    recovery_note       TEXT NOT NULL DEFAULT '',
    last_recovery_at    TIMESTAMPTZ,
    lease_owner         TEXT,
    lease_expires_at    TIMESTAMPTZ,
    paypal_payee_id     TEXT NOT NULL DEFAULT '',
    created_utc         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE abandoned_orders ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE abandoned_orders ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE abandoned_orders ADD COLUMN IF NOT EXISTS paypal_payee_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS abandoned_orders_merchant_idx ON abandoned_orders(merchant_id);
CREATE INDEX IF NOT EXISTS abandoned_orders_status_idx ON abandoned_orders(recovery_status);
CREATE INDEX IF NOT EXISTS abandoned_orders_approved_idx ON abandoned_orders(approved_at);

CREATE TABLE IF NOT EXISTS approval_token_used (
    token_sig       TEXT PRIMARY KEY,
    dispute_id      TEXT NOT NULL,
    merchant_id     TEXT NOT NULL,
    action          TEXT NOT NULL,
    used_at_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS approval_token_dispute_idx ON approval_token_used(dispute_id);

CREATE TABLE IF NOT EXISTS approval_jobs (
    job_id          UUID PRIMARY KEY,
    dispute_id      TEXT NOT NULL,
    merchant_id     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner     TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error      TEXT,
    created_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_utc   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS approval_jobs_pending_idx ON approval_jobs(status, next_attempt_at, created_utc);

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_id       TEXT PRIMARY KEY,
    last_seen_utc   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merchant_policy (
    merchant_id     TEXT PRIMARY KEY,
    config          JSONB NOT NULL,
    updated_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tracked_disputes (
    dispute_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL DEFAULT '',
    opened_at TIMESTAMPTZ NOT NULL,
    response_deadline TIMESTAMPTZ NOT NULL,
    last_alert_level TEXT NOT NULL DEFAULT '',
    auto_acted BOOLEAN NOT NULL DEFAULT FALSE,
    win_probability DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS tracked_disputes_merchant_idx ON tracked_disputes(merchant_id, auto_acted, response_deadline);
"""


def init_db() -> bool:
    """Create tables if they don't exist. Returns True on success."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        logger.info("pg_store: tables initialised")
        return True
    except Exception as exc:
        logger.error("pg_store: init_db failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def health_check() -> bool:
    """Verify the database and required workflow tables without running DDL."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT to_regclass('public.webhook_outbox'),
                      to_regclass('public.abandoned_orders'),
                      to_regclass('public.approval_jobs'),
                      to_regclass('public.merchant_policy'),
                      to_regclass('public.tracked_disputes')"""
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row and all(row))


def save_webhook_event_atomic(
    event_id: str,
    event_type: str,
    payload: dict,
    merchant_id: str = "",
) -> bool:
    """
    INSERT ... ON CONFLICT DO NOTHING.

    Returns True  → event is new (saved successfully).
    Returns False → duplicate (rowcount == 0, already processed).

    This is the atomic save+dedup in one round-trip.
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_outbox (event_id, event_type, merchant_id, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, event_type, merchant_id, json.dumps(payload)),
            )
            is_new = cur.rowcount == 1
        conn.commit()
        return is_new
    except Exception as exc:
        logger.error("pg_store: save_webhook_event_atomic failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        raise


def mark_event_processed(event_id: str, worker_id: Optional[str] = None) -> bool:
    """Mark a webhook event as processed (status='processed')."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE webhook_outbox SET status='processed', processed_utc=NOW(), lease_owner=NULL,
                       lease_expires_at=NULL, last_error=NULL
                   WHERE event_id=%s AND (%s IS NULL OR lease_owner=%s)""",
                (event_id, worker_id, worker_id),
            )
            updated = cur.rowcount == 1
        conn.commit()
        return updated
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("pg_store: mark_event_processed failed: %s", exc)
        raise


def claim_pending_webhook_events(worker_id: str, limit: int = 20, lease_seconds: int = 120) -> List[Dict[str, Any]]:
    """Atomically lease due webhook rows, including rows whose worker lease expired."""
    import psycopg2.extras

    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH picked AS (
                SELECT event_id FROM webhook_outbox
                WHERE (status = 'pending' AND next_attempt_at <= NOW())
                   OR (status = 'processing' AND COALESCE(lease_expires_at, '-infinity'::timestamptz) < NOW())
                ORDER BY received_utc ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE webhook_outbox AS o
            SET status='processing', attempt_count=o.attempt_count + 1,
                lease_owner=%s, lease_expires_at=NOW() + (%s * INTERVAL '1 second')
            FROM picked
            WHERE o.event_id=picked.event_id
            RETURNING o.event_id, o.event_type, o.merchant_id, o.payload, o.attempt_count
            """,
            (limit, worker_id, lease_seconds),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def mark_webhook_failed(event_id: str, worker_id: str, error: str, max_attempts: int = 8) -> bool:
    """Schedule a bounded retry or move an exhausted event to dead_letter."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE webhook_outbox
            SET status=CASE WHEN attempt_count >= %s THEN 'dead_letter' ELSE 'pending' END,
                next_attempt_at=NOW() + (LEAST(3600, 15 * POWER(2, LEAST(attempt_count, 8))) * INTERVAL '1 second'),
                lease_owner=NULL, lease_expires_at=NULL, last_error=%s
            WHERE event_id=%s AND status='processing' AND lease_owner=%s
            """,
            (max_attempts, error[:2000], event_id, worker_id),
        )
        updated = cur.rowcount == 1
    conn.commit()
    return updated


def mark_webhook_dead_letter(event_id: str, worker_id: str, error: str) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE webhook_outbox SET status='dead_letter', lease_owner=NULL,
                lease_expires_at=NULL, last_error=%s
                WHERE event_id=%s AND status='processing' AND lease_owner=%s""",
            (error[:2000], event_id, worker_id),
        )
        updated = cur.rowcount == 1
    conn.commit()
    return updated


def list_dead_letters(queue: str, limit: int = 100) -> List[Dict[str, Any]]:
    table = {"webhooks": "webhook_outbox", "approvals": "approval_jobs"}.get(queue)
    if not table:
        raise ValueError("unsupported queue")
    import psycopg2.extras
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT job_id AS item_id, dispute_id, merchant_id, status, attempt_count, last_error, created_utc "
            f"FROM {table} WHERE status='dead_letter' ORDER BY created_utc DESC LIMIT %s"
            if queue == "approvals" else
            f"SELECT event_id AS item_id, event_type, merchant_id, status, attempt_count, last_error, received_utc AS created_utc "
            f"FROM {table} WHERE status='dead_letter' ORDER BY received_utc DESC LIMIT %s",
            (limit,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.commit()
    return rows


def requeue_dead_letter(queue: str, item_id: str) -> bool:
    table, key = {"webhooks": ("webhook_outbox", "event_id"), "approvals": ("approval_jobs", "job_id")}.get(queue, (None, None))
    if not table:
        raise ValueError("unsupported queue")
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET status='pending',attempt_count=0,next_attempt_at=NOW(),last_error=NULL "
            f"WHERE {key}=%s AND status='dead_letter'",
            (item_id,),
        )
        updated = cur.rowcount == 1
    conn.commit()
    return updated


def heartbeat_worker(worker_id: str) -> None:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO worker_heartbeat(worker_id,last_seen_utc) VALUES (%s,NOW())
               ON CONFLICT(worker_id) DO UPDATE SET last_seen_utc=NOW()""",
            (worker_id,),
        )
    conn.commit()


def get_worker_heartbeat_age(worker_id: str) -> Optional[float]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT EXTRACT(EPOCH FROM NOW()-last_seen_utc) FROM worker_heartbeat WHERE worker_id=%s", (worker_id,))
        row = cur.fetchone()
    conn.commit()
    return float(row[0]) if row and row[0] is not None else None


def save_policy(merchant_id: str, config: Dict[str, Any]) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO merchant_policy(merchant_id,config,updated_utc) VALUES (%s,%s,NOW())
               ON CONFLICT(merchant_id) DO UPDATE SET config=EXCLUDED.config,updated_utc=NOW()""",
            (merchant_id, json.dumps(config)),
        )
    conn.commit()
    return True


def get_policy(merchant_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT config FROM merchant_policy WHERE merchant_id=%s", (merchant_id,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def save_tracked_dispute(dispute: Dict[str, Any]) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO tracked_disputes
               (dispute_id,merchant_id,amount,reason_code,opened_at,response_deadline,last_alert_level,auto_acted,win_probability)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(dispute_id) DO NOTHING""",
            (dispute["dispute_id"], dispute["merchant_id"], dispute.get("amount", 0),
             dispute.get("reason_code", ""), dispute["opened_at"], dispute["response_deadline"],
             dispute.get("last_alert_level", ""), dispute.get("auto_acted", False),
             dispute.get("win_probability", 0)),
        )
        inserted = cur.rowcount == 1
    conn.commit()
    return inserted


def update_tracked_dispute(dispute_id: str, level: str, auto_acted: bool = False) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tracked_disputes SET last_alert_level=%s,auto_acted=%s,updated_utc=NOW() WHERE dispute_id=%s",
            (level, auto_acted, dispute_id),
        )
        updated = cur.rowcount == 1
    conn.commit()
    return updated


def delete_tracked_dispute(dispute_id: str) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tracked_disputes WHERE dispute_id=%s", (dispute_id,))
        deleted = cur.rowcount == 1
    conn.commit()
    return deleted


def get_tracked_disputes(merchant_id: str = "") -> List[Dict[str, Any]]:
    import psycopg2.extras
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if merchant_id:
            cur.execute("SELECT * FROM tracked_disputes WHERE merchant_id=%s AND auto_acted=FALSE", (merchant_id,))
        else:
            cur.execute("SELECT * FROM tracked_disputes WHERE auto_acted=FALSE")
        rows = [dict(row) for row in cur.fetchall()]
    conn.commit()
    return rows


def enqueue_approval_job(token_sig: str, dispute_id: str, merchant_id: str, action: str) -> Optional[str]:
    """Consume an approval token and enqueue its work atomically."""
    job_id = str(uuid.uuid4())
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO approval_token_used(token_sig,dispute_id,merchant_id,action)
               VALUES (%s,%s,%s,%s) ON CONFLICT(token_sig) DO NOTHING""",
            (token_sig, dispute_id, merchant_id, action),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        cur.execute(
            "INSERT INTO approval_jobs(job_id,dispute_id,merchant_id) VALUES (%s,%s,%s)",
            (job_id, dispute_id, merchant_id),
        )
    conn.commit()
    return job_id


def claim_pending_approval_jobs(worker_id: str, limit: int = 10, lease_seconds: int = 120) -> List[Dict[str, Any]]:
    import psycopg2.extras
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """WITH picked AS (
                   SELECT job_id FROM approval_jobs
                   WHERE (status='pending' AND next_attempt_at<=NOW())
                      OR (status='processing' AND COALESCE(lease_expires_at, '-infinity'::timestamptz)<NOW())
                   ORDER BY created_utc ASC LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE approval_jobs AS j SET status='processing',attempt_count=j.attempt_count+1,
                   lease_owner=%s,lease_expires_at=NOW()+(%s*INTERVAL '1 second')
               FROM picked WHERE j.job_id=picked.job_id
               RETURNING j.job_id,j.dispute_id,j.merchant_id,j.attempt_count""",
            (limit, worker_id, lease_seconds),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def complete_approval_job(job_id: str, worker_id: str) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("""UPDATE approval_jobs SET status='completed',completed_utc=NOW(),lease_owner=NULL,lease_expires_at=NULL
                       WHERE job_id=%s AND status='processing' AND lease_owner=%s""", (job_id, worker_id))
        ok = cur.rowcount == 1
    conn.commit()
    return ok


def fail_approval_job(job_id: str, worker_id: str, error: str, max_attempts: int = 8) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE approval_jobs SET status=CASE WHEN attempt_count >= %s THEN 'dead_letter' ELSE 'pending' END,
                   next_attempt_at=NOW()+(LEAST(3600,15*POWER(2,LEAST(attempt_count,8)))*INTERVAL '1 second'),
                   lease_owner=NULL,lease_expires_at=NULL,last_error=%s
               WHERE job_id=%s AND status='processing' AND lease_owner=%s""",
            (max_attempts, error[:2000], job_id, worker_id),
        )
        ok = cur.rowcount == 1
    conn.commit()
    return ok


def append_audit_entry(entry: dict) -> bool:
    """
    Insert an audit entry.  ON CONFLICT DO NOTHING (idempotent re-runs).
    Returns True on insert, False if already present or on error.
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (
                    entry_id, dispute_id, timestamp_utc, action,
                    oracle_ev_fight, oracle_win_probability,
                    oracle_recommendation, guardian_reason,
                    amount_usd, claim_type, paypal_api_calls,
                    outcome, outcome_updated_utc, notes, is_sandbox
                ) VALUES (
                    %(entry_id)s, %(dispute_id)s, %(timestamp_utc)s, %(action)s,
                    %(oracle_ev_fight)s, %(oracle_win_probability)s,
                    %(oracle_recommendation)s, %(guardian_reason)s,
                    %(amount_usd)s, %(claim_type)s, %(paypal_api_calls)s,
                    %(outcome)s, %(outcome_updated_utc)s, %(notes)s, %(is_sandbox)s
                )
                ON CONFLICT (entry_id) DO NOTHING
                """,
                {
                    "entry_id": entry.get("entry_id"),
                    "dispute_id": entry.get("dispute_id"),
                    "timestamp_utc": entry.get("timestamp_utc"),
                    "action": getattr(entry.get("action"), "value", entry.get("action")),
                    "oracle_ev_fight": entry.get("oracle_ev_fight"),
                    "oracle_win_probability": entry.get("oracle_win_probability"),
                    "oracle_recommendation": entry.get("oracle_recommendation"),
                    "guardian_reason": entry.get("guardian_reason"),
                    "amount_usd": entry.get("amount_usd"),
                    "claim_type": entry.get("claim_type"),
                    "paypal_api_calls": json.dumps(entry.get("paypal_api_calls", [])),
                    "outcome": getattr(entry.get("outcome", "PENDING"), "value", entry.get("outcome", "PENDING")),
                    "outcome_updated_utc": entry.get("outcome_updated_utc"),
                    "notes": entry.get("notes", ""),
                    "is_sandbox": entry.get("is_sandbox", True),
                },
            )
            inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    except Exception as exc:
        logger.error("pg_store: append_audit_entry failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def get_config_value(key: str) -> Optional[str]:
    """Read a service-level config value from service_config table."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE key=%s", (key,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.error("pg_store: get_config_value(%s) failed: %s", key, exc)
        return None


def get_or_create_secret(key: str, candidate: str) -> str:
    """Create a shared secret once; all web/worker processes read the same value."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO service_config(key,value,updated_utc) VALUES (%s,%s,NOW()) ON CONFLICT(key) DO NOTHING",
            (key, candidate),
        )
        cur.execute("SELECT value FROM service_config WHERE key=%s", (key,))
        row = cur.fetchone()
    conn.commit()
    if not row or not row[0]:
        raise RuntimeError(f"Could not initialize persistent secret {key}")
    return str(row[0])


def set_config_value(key: str, value: str) -> bool:
    """Upsert a service-level config value."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO service_config (key, value, updated_utc)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value=EXCLUDED.value, updated_utc=NOW()
                """,
                (key, value),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: set_config_value(%s) failed: %s", key, exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def record_dispute_velocity(merchant_id: str, amount: float = 0.0) -> bool:
    """
    Increment the per-hour dispute count for the given merchant.
    Uses date_trunc('hour', NOW()) as the bucket so queries are fast.
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dispute_velocity (bucket_utc, merchant_id, dispute_count, amount_total)
                VALUES (date_trunc('hour', NOW()), %s, 1, %s)
                ON CONFLICT (bucket_utc, merchant_id) DO UPDATE
                    SET dispute_count = dispute_velocity.dispute_count + 1,
                        amount_total  = dispute_velocity.amount_total + EXCLUDED.amount_total
                """,
                (merchant_id, amount),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: record_dispute_velocity failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def get_dispute_velocity(merchant_id: str, hours: int = 168) -> List[Dict[str, Any]]:
    """
    Return hourly dispute velocity for the last `hours` hours.
    Result is sorted ascending (oldest first) for time-series rendering.
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bucket_utc, dispute_count, amount_total
                FROM dispute_velocity
                WHERE merchant_id=%s
                  AND bucket_utc >= NOW() - (%s || ' hours')::INTERVAL
                ORDER BY bucket_utc ASC
                """,
                (merchant_id, str(hours)),
            )
            rows = cur.fetchall()
        return [
            {
                "bucket_utc": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "dispute_count": r[1],
                "amount_total": float(r[2]),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("pg_store: get_dispute_velocity failed: %s", exc)
        return []


def get_audit_entries(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent audit entries ordered by timestamp DESC."""
    try:
        import psycopg2.extras

        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT entry_id, dispute_id, timestamp_utc, action,
                       oracle_ev_fight, oracle_win_probability,
                       oracle_recommendation, guardian_reason,
                       amount_usd, claim_type, paypal_api_calls,
                       outcome, outcome_updated_utc, notes, is_sandbox
                FROM audit_log
                ORDER BY timestamp_utc DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("pg_store: get_audit_entries failed: %s", exc)
        return []


def save_abandoned_order(order: Dict[str, Any]) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING — idempotent for redelivered webhooks."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO abandoned_orders (
                    order_id, merchant_id, amount, currency,
                    buyer_email, buyer_name, items, approved_at, paypal_payee_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
                """,
                (
                    order["order_id"],
                    order["merchant_id"],
                    order["amount"],
                    order.get("currency", "USD"),
                    order.get("buyer_email", ""),
                    order.get("buyer_name", ""),
                    json.dumps(order.get("items", [])),
                    order.get("approved_at"),
                    order.get("paypal_payee_id", ""),
                ),
            )
            inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    except Exception as exc:
        logger.error("pg_store: save_abandoned_order failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        raise


def get_stale_abandoned_orders(merchant_id: str, cutoff_iso: str, max_attempts: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Return orders eligible for recovery: approved before cutoff, not yet recovered."""
    try:
        import psycopg2.extras
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM abandoned_orders
                WHERE merchant_id = %s
                  AND approved_at <= %s::TIMESTAMPTZ
                  AND recovered = FALSE
                  AND recovery_attempts < %s
                  AND recovery_status NOT IN ('captured', 'needs_review', 'not_eligible', 'completed_externally')
                  AND (recovery_status <> 'processing' OR lease_expires_at IS NULL OR lease_expires_at < NOW())
                ORDER BY approved_at ASC
                LIMIT %s
                """,
                (merchant_id, cutoff_iso, max_attempts, limit),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("pg_store: get_stale_abandoned_orders failed: %s", exc)
        raise


def get_abandoned_orders(merchant_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    import psycopg2.extras
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM abandoned_orders WHERE merchant_id=%s ORDER BY approved_at DESC LIMIT %s",
            (merchant_id, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.commit()
    return rows


def get_recovery_stats(merchant_id: str) -> Dict[str, Any]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE recovered),
                      COALESCE(SUM(amount) FILTER (WHERE recovered), 0),
                      COUNT(*) FILTER (WHERE NOT recovered AND recovery_status NOT IN
                          ('needs_review','completed_externally','not_eligible','captured'))
               FROM abandoned_orders WHERE merchant_id=%s""",
            (merchant_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return {"recovered_orders": int(row[0] or 0), "recovered_revenue": float(row[1] or 0), "awaiting_capture": int(row[2] or 0)}


def claim_abandoned_order(order_id: str, worker_id: str = "recovery", lease_seconds: int = 600) -> bool:
    """
    Atomically claim an order for recovery using SELECT FOR UPDATE SKIP LOCKED.
    Returns True if claim succeeded (order is ours to process).
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id FROM abandoned_orders
                WHERE order_id = %s
                  AND recovered = FALSE
                  AND recovery_status NOT IN ('captured', 'needs_review', 'completed_externally')
                  AND (recovery_status <> 'processing' OR lease_expires_at IS NULL OR lease_expires_at < NOW())
                FOR UPDATE SKIP LOCKED
                """,
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            cur.execute(
                """
                UPDATE abandoned_orders
                SET recovery_status = 'processing',
                    last_recovery_at = NOW(),
                    lease_owner = %s,
                    lease_expires_at = NOW() + (%s * INTERVAL '1 second')
                WHERE order_id = %s
                """,
                (worker_id, lease_seconds, order_id),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: claim_abandoned_order failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        raise


def update_abandoned_order_state(order_id: str, state: str, note: str = "", capture_id: str = "") -> bool:
    """Update recovery state after a capture attempt."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE abandoned_orders
                SET recovery_status = %s,
                    recovery_note = %s,
                    last_recovery_at = NOW(),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    recovery_attempts = recovery_attempts + 1,
                    recovered = (CASE WHEN %s = 'captured' THEN TRUE ELSE recovered END),
                    capture_id = (CASE WHEN %s != '' THEN %s ELSE capture_id END)
                WHERE order_id = %s
                """,
                (state, note, state, capture_id, capture_id, order_id),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: update_abandoned_order_state failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def mark_order_captured_externally(order_id: str) -> bool:
    """Mark order as completed externally (from PAYMENT.CAPTURE.COMPLETED webhook)."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE abandoned_orders
                SET recovery_status = CASE
                        WHEN recovery_status = 'pending_recovery' THEN 'completed_externally'
                        ELSE 'captured'
                    END,
                    recovered = CASE
                        WHEN recovery_status = 'pending_recovery' THEN FALSE
                        ELSE TRUE
                    END,
                    last_recovery_at = NOW(),
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE order_id = %s
                """,
                (order_id,),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: mark_order_captured_externally failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def consume_approval_token(token_sig: str, dispute_id: str, merchant_id: str, action: str) -> bool:
    """
    Atomically record a token as used. Returns True if this is the first use.
    INSERT ON CONFLICT DO NOTHING — returns False if token was already consumed.
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval_token_used (token_sig, dispute_id, merchant_id, action)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (token_sig) DO NOTHING
                """,
                (token_sig, dispute_id, merchant_id, action),
            )
            is_first_use = cur.rowcount == 1
        conn.commit()
        return is_first_use
    except Exception as exc:
        logger.error("pg_store: consume_approval_token failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


def get_audit_stats() -> Dict[str, Any]:
    """Return aggregate stats from the audit_log table."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                                        AS total_logged,
                    SUM(amount_usd)                                 AS total_amount,
                    AVG(oracle_win_probability)                     AS avg_win_probability,
                    SUM(CASE WHEN is_sandbox THEN 1 ELSE 0 END)    AS sandbox_count
                FROM audit_log
                """
            )
            row = cur.fetchone()
            if row is None or row[0] == 0:
                return {
                    "total_logged": 0,
                    "action_counts": {},
                    "action_amounts": {},
                    "outcome_counts": {},
                    "total_amount_at_risk_usd": 0.0,
                    "avg_win_probability": 0.0,
                    "sandbox_pct": 100.0,
                    "source": "postgresql",
                }
            total, total_amount, avg_wp, sandbox_count = row

            cur.execute(
                "SELECT action, COUNT(*) FROM audit_log GROUP BY action"
            )
            action_counts = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute("SELECT action, COALESCE(SUM(amount_usd),0) FROM audit_log GROUP BY action")
            action_amounts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

            cur.execute(
                "SELECT outcome, COUNT(*) FROM audit_log GROUP BY outcome"
            )
            outcome_counts = {r[0]: r[1] for r in cur.fetchall()}

        return {
            "total_logged": int(total),
            "action_counts": action_counts,
            "action_amounts": action_amounts,
            "outcome_counts": outcome_counts,
            "total_amount_at_risk_usd": round(float(total_amount or 0), 2),
            "avg_win_probability": round(float(avg_wp or 0), 4),
            "sandbox_pct": round(100.0 * int(sandbox_count) / int(total), 1),
            "source": "postgresql",
        }
    except Exception as exc:
        logger.error("pg_store: get_audit_stats failed: %s", exc)
        return {}


def purge_old_records(retain_days: int = 90) -> dict:
    """
    Delete audit_log and completed/failed webhook_outbox rows older than
    retain_days. Called nightly by the scheduler. Returns counts removed.

    Default retention: 90 days.  Override with ANCHOR_RETAIN_DAYS env var.
    """
    retain = int(os.environ.get("ANCHOR_RETAIN_DAYS", retain_days))
    removed: dict = {"audit_log": 0, "webhook_outbox": 0, "approval_jobs": 0}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_log WHERE timestamp_utc < NOW() - (%s * INTERVAL '1 day')",
                (retain,),
            )
            removed["audit_log"] = cur.rowcount
            cur.execute(
                """DELETE FROM webhook_outbox
                   WHERE status IN ('completed', 'failed')
                     AND received_utc < NOW() - (%s * INTERVAL '1 day')""",
                (retain,),
            )
            removed["webhook_outbox"] = cur.rowcount
            cur.execute(
                """DELETE FROM approval_jobs
                   WHERE status IN ('completed', 'failed')
                     AND created_utc < NOW() - (%s * INTERVAL '1 day')""",
                (retain,),
            )
            removed["approval_jobs"] = cur.rowcount
        conn.commit()
        logger.info("pg_store: purged records older than %d days: %s", retain, removed)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error("pg_store: purge_old_records failed: %s", exc)
    return removed


def get_oldest_pending_job_age_seconds() -> Optional[float]:
    """
    Return seconds the oldest due or expired-lease job has been waiting.

    Approval jobs are intentionally excluded while SHADOW_MODE is enabled,
    because the worker holds them without claiming them in that mode.

    Returns None if there are no pending jobs (queue is empty or DB unreachable).
    A large value means the drainer/worker is stalled.
    """
    try:
        conn = _get_conn()
        shadow_mode = os.environ.get("SHADOW_MODE", "0").strip().lower() in {"1", "true", "yes"}
        approval_union = ""
        if not shadow_mode:
            approval_union = """
                    UNION ALL
                    SELECT created_utc AS age_ts FROM approval_jobs
                    WHERE (status = 'pending' AND next_attempt_at <= NOW())
                       OR (status = 'processing' AND
                           (lease_expires_at IS NULL OR lease_expires_at < NOW()))
            """
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT EXTRACT(EPOCH FROM (NOW() - MIN(age_ts))) AS age_s
                FROM (
                    SELECT received_utc AS age_ts FROM webhook_outbox
                    WHERE (status = 'pending' AND next_attempt_at <= NOW())
                       OR (status = 'processing' AND
                           (lease_expires_at IS NULL OR lease_expires_at < NOW()))
                    {approval_union}
                ) AS q
                """
            )
            row = cur.fetchone()
        conn.commit()
        if row and row[0] is not None:
            return float(row[0])
        return None
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error("pg_store: get_oldest_pending_job_age_seconds failed: %s", exc)
        return None
