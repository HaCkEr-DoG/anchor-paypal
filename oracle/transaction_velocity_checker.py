"""
Transaction Velocity Checker — flag unusual transaction velocity patterns
that may indicate fraud, account takeover, or card testing.

Velocity checks:
  1. Amount velocity — multiple high-value charges in a short window
  2. Frequency velocity — too many transactions per hour/day
  3. Address velocity — same card, multiple shipping addresses
  4. Card BIN velocity — multiple cards from same BIN tested in window
  5. Account velocity — new account with unusual purchase pattern

High velocity disputes typically involve:
  - Card testing (many small charges to verify a stolen card)
  - Account takeover (sudden pattern change)
  - Friendly fraud precursor (buying out an account)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VelocityRisk(str, Enum):
    CRITICAL  = "CRITICAL"   # Strongly suggests fraud — block immediately
    HIGH      = "HIGH"       # Likely fraud — manual review required
    MODERATE  = "MODERATE"   # Unusual — flag for monitoring
    LOW       = "LOW"        # Normal variation
    NORMAL    = "NORMAL"     # Within expected bounds


class VelocityTrigger(str, Enum):
    AMOUNT_SPIKE      = "AMOUNT_SPIKE"
    FREQUENCY_SPIKE   = "FREQUENCY_SPIKE"
    ADDRESS_CHANGE    = "ADDRESS_CHANGE"
    BIN_TESTING       = "BIN_TESTING"
    NEW_ACCOUNT       = "NEW_ACCOUNT"
    PATTERN_CHANGE    = "PATTERN_CHANGE"


@dataclass
class VelocityTransaction:
    """A single transaction in the velocity window."""
    transaction_id: str
    amount: float
    timestamp_epoch: int
    shipping_address_hash: str = ""   # Hashed address for comparison
    card_bin: str = ""                 # First 6 digits


@dataclass
class VelocityInput:
    """Input for velocity checking."""
    dispute_id: str
    card_last4: str
    account_id: str
    account_age_days: int

    # Transactions in the velocity window (last 24–72h)
    recent_transactions: list[VelocityTransaction] = field(default_factory=list)

    # Baselines (historical norms for this account)
    avg_daily_transactions: float = 1.0
    avg_transaction_amount: float = 50.0
    typical_shipping_address_hash: str = ""

    # Window settings
    velocity_window_hours: int = 24


def _time_diff_hours(t1: int, t2: int) -> float:
    return abs(t1 - t2) / 3600.0


def _check_amount_spike(
        transactions: list[VelocityTransaction],
        avg_amount: float) -> tuple[VelocityRisk, Optional[VelocityTrigger], str]:
    if not transactions:
        return VelocityRisk.NORMAL, None, ""
    total = sum(t.amount for t in transactions)
    max_single = max(t.amount for t in transactions)
    ratio = max_single / max(avg_amount, 1.0)
    if ratio >= 10:
        return VelocityRisk.CRITICAL, VelocityTrigger.AMOUNT_SPIKE, \
               f"Single charge ${max_single:.2f} is {ratio:.0f}x avg (${avg_amount:.2f})"
    if ratio >= 5:
        return VelocityRisk.HIGH, VelocityTrigger.AMOUNT_SPIKE, \
               f"Single charge ${max_single:.2f} is {ratio:.0f}x avg"
    if ratio >= 3:
        return VelocityRisk.MODERATE, VelocityTrigger.AMOUNT_SPIKE, \
               f"Amount {ratio:.0f}x above avg"
    return VelocityRisk.NORMAL, None, ""


def _check_frequency(
        transactions: list[VelocityTransaction],
        avg_daily: float,
        window_hours: int) -> tuple[VelocityRisk, Optional[VelocityTrigger], str]:
    n = len(transactions)
    expected = avg_daily * (window_hours / 24)
    ratio = n / max(expected, 0.1)
    if ratio >= 10:
        return VelocityRisk.CRITICAL, VelocityTrigger.FREQUENCY_SPIKE, \
               f"{n} txns in {window_hours}h vs expected {expected:.1f}"
    if ratio >= 5:
        return VelocityRisk.HIGH, VelocityTrigger.FREQUENCY_SPIKE, \
               f"{n} txns is {ratio:.0f}x normal frequency"
    if ratio >= 3:
        return VelocityRisk.MODERATE, VelocityTrigger.FREQUENCY_SPIKE, \
               f"{n} txns is elevated vs avg {expected:.1f}"
    return VelocityRisk.NORMAL, None, ""


def _check_address_changes(
        transactions: list[VelocityTransaction],
        typical_addr: str) -> tuple[VelocityRisk, Optional[VelocityTrigger], str]:
    if not transactions:
        return VelocityRisk.NORMAL, None, ""
    unique_addrs = {t.shipping_address_hash for t in transactions
                    if t.shipping_address_hash}
    if len(unique_addrs) >= 4:
        return VelocityRisk.CRITICAL, VelocityTrigger.ADDRESS_CHANGE, \
               f"{len(unique_addrs)} different shipping addresses in window"
    if len(unique_addrs) >= 2:
        changed = [a for a in unique_addrs if a != typical_addr and a]
        if changed and typical_addr:
            return VelocityRisk.HIGH, VelocityTrigger.ADDRESS_CHANGE, \
                   "Shipping address changed from typical"
    return VelocityRisk.NORMAL, None, ""


def _check_bin_testing(
        transactions: list[VelocityTransaction]) -> tuple[VelocityRisk, Optional[VelocityTrigger], str]:
    # Card testing pattern: many small transactions (< $5) in rapid succession
    small = [t for t in transactions if t.amount < 5.0]
    if len(small) >= 5:
        return VelocityRisk.CRITICAL, VelocityTrigger.BIN_TESTING, \
               f"{len(small)} micro-transactions (< $5) — possible card testing"
    if len(small) >= 3:
        return VelocityRisk.HIGH, VelocityTrigger.BIN_TESTING, \
               f"{len(small)} micro-transactions may indicate BIN testing"
    return VelocityRisk.NORMAL, None, ""


def _check_new_account(
        account_age_days: int,
        transactions: list[VelocityTransaction],
        avg_amount: float) -> tuple[VelocityRisk, Optional[VelocityTrigger], str]:
    if account_age_days >= 90:
        return VelocityRisk.NORMAL, None, ""
    total = sum(t.amount for t in transactions)
    if account_age_days < 7 and total > avg_amount * 5:
        return VelocityRisk.HIGH, VelocityTrigger.NEW_ACCOUNT, \
               f"Account only {account_age_days}d old, total spend ${total:.2f}"
    if account_age_days < 30 and len(transactions) >= 5:
        return VelocityRisk.MODERATE, VelocityTrigger.NEW_ACCOUNT, \
               f"New account ({account_age_days}d) with {len(transactions)} transactions"
    return VelocityRisk.NORMAL, None, ""


_SEVERITY_ORDER = [
    VelocityRisk.CRITICAL, VelocityRisk.HIGH, VelocityRisk.MODERATE,
    VelocityRisk.LOW, VelocityRisk.NORMAL,
]


@dataclass
class VelocityFinding:
    trigger: VelocityTrigger
    risk: VelocityRisk
    description: str


@dataclass
class VelocityReport:
    """Full velocity analysis."""
    dispute_id: str
    overall_risk: VelocityRisk
    findings: list[VelocityFinding]
    triggered_checks: list[str]
    recommended_action: str
    should_block_account: bool
    summary: str


def check_transaction_velocity(inp: VelocityInput) -> VelocityReport:
    """Run all velocity checks and produce a composite risk report."""
    # Filter to window
    if inp.recent_transactions:
        latest_ts = max(t.timestamp_epoch for t in inp.recent_transactions)
        window_cutoff = latest_ts - inp.velocity_window_hours * 3600
        txns = [t for t in inp.recent_transactions
                if t.timestamp_epoch >= window_cutoff]
    else:
        txns = []

    findings: list[VelocityFinding] = []

    checks = [
        _check_amount_spike(txns, inp.avg_transaction_amount),
        _check_frequency(txns, inp.avg_daily_transactions, inp.velocity_window_hours),
        _check_address_changes(txns, inp.typical_shipping_address_hash),
        _check_bin_testing(txns),
        _check_new_account(inp.account_age_days, txns, inp.avg_transaction_amount),
    ]

    for risk, trigger, desc in checks:
        if trigger is not None:
            findings.append(VelocityFinding(trigger=trigger, risk=risk, description=desc))

    # Overall = worst finding
    if findings:
        overall = min(findings, key=lambda f: _SEVERITY_ORDER.index(f.risk)).risk
    else:
        overall = VelocityRisk.NORMAL

    triggered = [f.trigger.value for f in findings]

    # Action
    if overall == VelocityRisk.CRITICAL:
        action = "BLOCK account immediately — review all recent transactions for fraud"
        should_block = True
    elif overall == VelocityRisk.HIGH:
        action = "Flag for manual review — confirm buyer identity before fulfillment"
        should_block = False
    elif overall == VelocityRisk.MODERATE:
        action = "Monitor — apply extra scrutiny to this account for 72h"
        should_block = False
    else:
        action = "No action required — velocity within normal bounds"
        should_block = False

    summary = (
        f"Velocity {inp.dispute_id}: "
        f"risk={overall.value} | triggers={len(triggered)} | "
        f"txns_in_window={len(txns)}"
    )

    return VelocityReport(
        dispute_id=inp.dispute_id,
        overall_risk=overall,
        findings=findings,
        triggered_checks=triggered,
        recommended_action=action,
        should_block_account=should_block,
        summary=summary,
    )
