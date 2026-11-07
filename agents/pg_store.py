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
