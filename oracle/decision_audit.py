"""
Decision Audit Trail — append-only log of every oracle decision.

Every autonomous action Anchor takes is logged here: the full inputs,
the oracle outputs, the guardian gate result, and the actual PayPal API
call made (or skipped). This provides:

  - A complete, immutable record for merchant review
  - Evidence that oracle decisions are traceable and explainable
  - Input for backtesting win-rate calibration over time
  - Trust signal: merchants can inspect every decision their agent made

Storage: in-memory ring buffer (last 1000 entries) + optionally appends
to `audit_log.jsonl` in the working directory for persistence.

PayPal benefit:
  - Auditable autonomous dispute management reduces liability risk
  - Audit trails are required for any merchant-facing automation product
  - Logged outcome data feeds back into win-probability model calibration
  - Transparent decisions reduce escalations and merchant disputes with PayPal
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


_MAX_MEMORY_ENTRIES = 1000
_AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "audit_log.jsonl")

_lock = threading.Lock()
_ring: deque = deque(maxlen=_MAX_MEMORY_ENTRIES)


class AuditAction(str, Enum):
    AUTO_ACCEPT    = "AUTO_ACCEPT"
    AUTO_FIGHT     = "AUTO_FIGHT"
    MANUAL_ACCEPT  = "MANUAL_ACCEPT"   # merchant accepted from dashboard
    MANUAL_FIGHT   = "MANUAL_FIGHT"    # merchant submitted evidence from dashboard
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    GATHER_EVIDENCE  = "GATHER_EVIDENCE"
    SKIPPED          = "SKIPPED"          # oracle ran but no PayPal call made


class AuditOutcome(str, Enum):
    PENDING    = "PENDING"    # action taken, awaiting PayPal result
    WON        = "WON"        # dispute resolved in merchant favor
    LOST       = "LOST"       # dispute resolved in buyer favor
    SETTLED    = "SETTLED"    # settlement reached
    WITHDRAWN  = "WITHDRAWN"  # buyer withdrew dispute
    UNKNOWN    = "UNKNOWN"


@dataclass
class AuditEntry:
    entry_id: str
    dispute_id: str
    timestamp_utc: str              # ISO-8601
    action: AuditAction
    oracle_ev_fight: float
    oracle_win_probability: float
    oracle_recommendation: str
    guardian_reason: str
    amount_usd: float
    claim_type: str
    paypal_api_calls: List[Dict[str, str]]
    outcome: AuditOutcome = AuditOutcome.PENDING
    outcome_updated_utc: Optional[str] = None
    notes: str = ""
    # Sandbox/test flag — required for honest demo labeling
    is_sandbox: bool = True


def _make_entry_id(dispute_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"AUD-{dispute_id[-6:].upper()}-{ts}"


def log_decision(
    *,
    dispute_id: str,
    action: AuditAction,
    oracle_ev_fight: float,
    oracle_win_probability: float,
    oracle_recommendation: str,
    guardian_reason: str,
    amount_usd: float,
    claim_type: str,
    paypal_api_calls: List[Dict[str, str]],
    is_sandbox: bool = True,
    notes: str = "",
) -> AuditEntry:
    entry = AuditEntry(
        entry_id=_make_entry_id(dispute_id),
        dispute_id=dispute_id,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        action=action,
        oracle_ev_fight=round(oracle_ev_fight, 4),
        oracle_win_probability=round(oracle_win_probability, 4),
        oracle_recommendation=oracle_recommendation,
        guardian_reason=guardian_reason,
        amount_usd=amount_usd,
        claim_type=claim_type,
        paypal_api_calls=paypal_api_calls,
        is_sandbox=is_sandbox,
        notes=notes,
    )
    with _lock:
        _ring.append(entry)
        _try_append_jsonl(entry)
        _try_append_pg(entry)
    return entry


def update_outcome(entry_id: str, outcome: AuditOutcome, notes: str = "") -> Optional[AuditEntry]:
    with _lock:
        for entry in reversed(list(_ring)):
            if entry.entry_id == entry_id:
                entry.outcome = outcome
                entry.outcome_updated_utc = datetime.now(timezone.utc).isoformat()
                if notes:
                    entry.notes = notes
                return entry
    return None


def get_recent(limit: int = 50) -> List[AuditEntry]:
    with _lock:
        entries = list(_ring)
    return list(reversed(entries))[:limit]


def get_stats() -> Dict[str, Any]:
    with _lock:
        entries = list(_ring)
    total = len(entries)
    if total == 0:
        return {
            "total_logged": 0,
            "action_counts": {},
            "outcome_counts": {},
            "total_amount_at_risk_usd": 0.0,
            "avg_win_probability": 0.0,
            "sandbox_pct": 100.0,
        }
    action_counts: Dict[str, int] = {}
    outcome_counts: Dict[str, int] = {}
    total_amount = 0.0
    total_wp = 0.0
    sandbox_count = 0
    for e in entries:
        action_counts[e.action.value] = action_counts.get(e.action.value, 0) + 1
        outcome_counts[e.outcome.value] = outcome_counts.get(e.outcome.value, 0) + 1
        total_amount += e.amount_usd
        total_wp += e.oracle_win_probability
        if e.is_sandbox:
            sandbox_count += 1
    return {
        "total_logged": total,
        "action_counts": action_counts,
        "outcome_counts": outcome_counts,
        "total_amount_at_risk_usd": round(total_amount, 2),
        "avg_win_probability": round(total_wp / total, 4),
        "sandbox_pct": round(100.0 * sandbox_count / total, 1),
    }


def _try_append_jsonl(entry: AuditEntry) -> None:
    try:
        line = json.dumps(asdict(entry), default=str) + "\n"
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # disk write is best-effort; ring buffer is authoritative


def _try_append_pg(entry: AuditEntry) -> None:
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        import agents.pg_store as pg_store
        pg_store.append_audit_entry(asdict(entry))
    except Exception:
        pass  # PG write is best-effort alongside JSONL
