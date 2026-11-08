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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_conn_pool = None  # module-level psycopg2 connection (re-created on failure)


def _get_conn():
    global _conn_pool
    import psycopg2
    import psycopg2.extras

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")

    try:
        if _conn_pool is None or _conn_pool.closed:
            _conn_pool = psycopg2.connect(db_url)
            _conn_pool.autocommit = False
        # Cheap liveness check
        _conn_pool.cursor().execute("SELECT 1")
        return _conn_pool
    except Exception:
        _conn_pool = psycopg2.connect(db_url)
        _conn_pool.autocommit = False
        return _conn_pool


_DDL = """
CREATE TABLE IF NOT EXISTS webhook_outbox (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    received_utc    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL DEFAULT 'pending',
    payload         JSONB NOT NULL
);

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
    created_utc         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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


def save_webhook_event_atomic(
    event_id: str,
    event_type: str,
    payload: dict,
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
                INSERT INTO webhook_outbox (event_id, event_type, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, event_type, json.dumps(payload)),
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


def mark_event_processed(event_id: str) -> None:
    """Mark a webhook event as processed (status='processed')."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE webhook_outbox SET status='processed' WHERE event_id=%s",
                (event_id,),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("pg_store: mark_event_processed failed: %s", exc)


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
                    "action": entry.get("action"),
                    "oracle_ev_fight": entry.get("oracle_ev_fight"),
                    "oracle_win_probability": entry.get("oracle_win_probability"),
                    "oracle_recommendation": entry.get("oracle_recommendation"),
                    "guardian_reason": entry.get("guardian_reason"),
                    "amount_usd": entry.get("amount_usd"),
                    "claim_type": entry.get("claim_type"),
                    "paypal_api_calls": json.dumps(entry.get("paypal_api_calls", [])),
                    "outcome": entry.get("outcome", "PENDING"),
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
                    buyer_email, buyer_name, items, approved_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
        return False


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
                ORDER BY approved_at ASC
                LIMIT %s
                """,
                (merchant_id, cutoff_iso, max_attempts, limit),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("pg_store: get_stale_abandoned_orders failed: %s", exc)
        return []


def claim_abandoned_order(order_id: str, lease_seconds: int = 600) -> bool:
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
                  AND recovery_status NOT IN ('captured', 'needs_review', 'completed_externally', 'processing')
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
                    last_recovery_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("pg_store: claim_abandoned_order failed: %s", exc)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return False


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
                    last_recovery_at = NOW()
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

            cur.execute(
                "SELECT outcome, COUNT(*) FROM audit_log GROUP BY outcome"
            )
            outcome_counts = {r[0]: r[1] for r in cur.fetchall()}

        return {
            "total_logged": int(total),
            "action_counts": action_counts,
            "outcome_counts": outcome_counts,
            "total_amount_at_risk_usd": round(float(total_amount or 0), 2),
            "avg_win_probability": round(float(avg_wp or 0), 4),
            "sandbox_pct": round(100.0 * int(sandbox_count) / int(total), 1),
            "source": "postgresql",
        }
    except Exception as exc:
        logger.error("pg_store: get_audit_stats failed: %s", exc)
        return {}
