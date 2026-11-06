"""
Dispute Triage Engine — prioritize and classify incoming disputes for
automated vs. manual handling.

Given a batch of dispute signals, this oracle:
  - Assigns a triage priority (URGENT / HIGH / MEDIUM / LOW / DEFER)
  - Recommends routing: AUTO_ACCEPT, AUTO_FIGHT, MANUAL_REVIEW
  - Flags auto-accept candidates (EV negative, tiny amounts, losing cases)
  - Flags auto-fight candidates (strong evidence, high win probability, SP eligible)
  - Surfaces manual review items (high LTV, edge cases, fraud signals)

Core decision logic:
  - EV(fight) = p_win × amount − fight_cost − (1−p_win) × cb_fee
  - AUTO_ACCEPT if: amount ≤ $10 OR (EV(fight) < -15 AND NOT vip AND NOT fraud)
  - AUTO_FIGHT if: EV(fight) > 0 AND sp_eligible AND win_prob ≥ 0.60 AND fraud_signal ≤ LOW
  - MANUAL_REVIEW: everything else

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TriagePriority(str, Enum):
    URGENT = "URGENT"   # Must act today (deadline ≤2d or amount ≥$500 + fraud)
    HIGH   = "HIGH"     # Act within 24–48h (deadline 3–5d or high LTV at risk)
    MEDIUM = "MEDIUM"   # Act within 72h (deadline 6–14d)
    LOW    = "LOW"      # Act within normal cycle (>14d)
    DEFER  = "DEFER"    # Can wait (no deadline pressure, low EV impact)


class TriageRouting(str, Enum):
    AUTO_ACCEPT    = "AUTO_ACCEPT"     # Accept the chargeback automatically
    AUTO_FIGHT     = "AUTO_FIGHT"      # Submit evidence automatically
    MANUAL_REVIEW  = "MANUAL_REVIEW"  # Queue for human agent review


class FraudSignal(str, Enum):
    VERY_HIGH = "VERY_HIGH"
    HIGH      = "HIGH"
    MODERATE  = "MODERATE"
    LOW       = "LOW"
    MINIMAL   = "MINIMAL"


@dataclass
class DisputeTriageInput:
    """Signals about a dispute for triage."""
    dispute_id: str
    transaction_amount: float
    reason_code: str

    win_probability: float = 0.50
    fight_cost: float = 15.0
    chargeback_fee: float = 20.0

    seller_protection_eligible: bool = False
    has_tracking_evidence: bool = False
    has_delivery_confirmation: bool = False

    is_vip: bool = False
    buyer_ltv: float = 0.0
    buyer_prior_disputes: int = 0

    fraud_signal: FraudSignal = FraudSignal.MINIMAL
    days_until_deadline: int = 10
    is_subscription: bool = False
    already_refunded: bool = False


@dataclass
class TriageDecision:
    """The triage outcome for a single dispute."""
    dispute_id: str
    priority: TriagePriority
    routing: TriageRouting

    ev_fight: float
    ev_accept: float

    rationale: list[str]
    flags: list[str]              # e.g. ["HIGH_LTV", "FRAUD_RISK", "NEAR_DEADLINE"]

    suggested_next_action: str
    summary: str


@dataclass
class BatchTriageReport:
    """Triage report for a batch of disputes."""
    decisions: list[TriageDecision]

    urgent_count: int
    auto_accept_count: int
    auto_fight_count: int
    manual_review_count: int

    total_auto_accept_amount: float
    total_auto_fight_amount: float

    summary: str


def _ev_fight(inp: DisputeTriageInput) -> float:
    return (inp.win_probability * inp.transaction_amount
            - inp.fight_cost
            - (1 - inp.win_probability) * inp.chargeback_fee)


def triage_dispute(inp: DisputeTriageInput) -> TriageDecision:
    """Triage a single dispute."""
    ev_f = round(_ev_fight(inp), 2)
    ev_a = round(-inp.transaction_amount, 2)

    rationale: list[str] = []
    flags: list[str] = []

    # ── Flag collection ───────────────────────────────────────────
    if inp.days_until_deadline <= 2:
        flags.append("NEAR_DEADLINE_CRITICAL")
    elif inp.days_until_deadline <= 5:
        flags.append("NEAR_DEADLINE_HIGH")

    if inp.is_vip or inp.buyer_ltv >= 500:
        flags.append("HIGH_LTV")

    if inp.fraud_signal in (FraudSignal.VERY_HIGH, FraudSignal.HIGH):
        flags.append("FRAUD_RISK")

    if inp.buyer_prior_disputes >= 3:
        flags.append("REPEAT_DISPUTER")

    if inp.already_refunded:
        flags.append("ALREADY_REFUNDED")

    if inp.seller_protection_eligible:
        flags.append("SELLER_PROTECTION")

    # ── Priority ─────────────────────────────────────────────────
    if ("NEAR_DEADLINE_CRITICAL" in flags
            or (inp.transaction_amount >= 500 and "FRAUD_RISK" in flags)):
        priority = TriagePriority.URGENT
    elif "NEAR_DEADLINE_HIGH" in flags or "HIGH_LTV" in flags:
        priority = TriagePriority.HIGH
    elif inp.days_until_deadline <= 14:
        priority = TriagePriority.MEDIUM
    elif inp.transaction_amount < 15:
        priority = TriagePriority.DEFER
    else:
        priority = TriagePriority.LOW

    # ── Routing ───────────────────────────────────────────────────
    if inp.already_refunded:
        routing = TriageRouting.AUTO_ACCEPT
        rationale.append("Already refunded — accept chargeback automatically")
    elif inp.transaction_amount <= 10:
        routing = TriageRouting.AUTO_ACCEPT
        rationale.append(f"Amount ${inp.transaction_amount:.2f} ≤ $10 — not worth fighting")
    elif (ev_f < -15 and not inp.is_vip
          and inp.fraud_signal not in (FraudSignal.VERY_HIGH, FraudSignal.HIGH)):
        routing = TriageRouting.AUTO_ACCEPT
        rationale.append(f"EV(fight)={ev_f:.2f} < -15 — accept is better")
    elif (ev_f > 0
          and inp.seller_protection_eligible
          and inp.win_probability >= 0.60
          and inp.fraud_signal in (FraudSignal.LOW, FraudSignal.MINIMAL)
          and inp.has_tracking_evidence):
        routing = TriageRouting.AUTO_FIGHT
        rationale.append(
            f"SP eligible + EV={ev_f:.2f} + win_prob={inp.win_probability:.0%}"
            " + clean fraud signal — auto-fight"
        )
    else:
        routing = TriageRouting.MANUAL_REVIEW
        if "HIGH_LTV" in flags:
            rationale.append("VIP/high-LTV buyer — manual review to protect relationship")
        if "FRAUD_RISK" in flags:
            rationale.append("Fraud signals present — human judgment needed")
        if not inp.seller_protection_eligible:
            rationale.append("Not SP eligible — manual evidence assessment required")
        if not rationale:
            rationale.append("Borderline EV — manual review for optimal strategy")

    # ── Suggested action ──────────────────────────────────────────
    if routing == TriageRouting.AUTO_ACCEPT:
        action = "Accept chargeback and close"
    elif routing == TriageRouting.AUTO_FIGHT:
        action = "Submit tracking + delivery confirmation evidence"
    else:
        action = "Assign to dispute specialist for manual review"

    summary = (
        f"Dispute {inp.dispute_id}: {priority.value} | {routing.value} | "
        f"EV={ev_f:+.0f} | flags={','.join(flags) or 'none'}"
    )

    return TriageDecision(
        dispute_id=inp.dispute_id,
        priority=priority,
        routing=routing,
        ev_fight=ev_f,
        ev_accept=ev_a,
        rationale=rationale,
        flags=flags,
        suggested_next_action=action,
        summary=summary,
    )


def triage_batch(disputes: list[DisputeTriageInput]) -> BatchTriageReport:
    """Triage a batch of disputes and produce aggregate stats."""
    decisions = [triage_dispute(d) for d in disputes]

    urgent      = sum(1 for d in decisions if d.priority == TriagePriority.URGENT)
    auto_accept = sum(1 for d in decisions if d.routing == TriageRouting.AUTO_ACCEPT)
    auto_fight  = sum(1 for d in decisions if d.routing == TriageRouting.AUTO_FIGHT)
    manual      = sum(1 for d in decisions if d.routing == TriageRouting.MANUAL_REVIEW)

    aa_amount = sum(
        disputes[i].transaction_amount
        for i, d in enumerate(decisions)
        if d.routing == TriageRouting.AUTO_ACCEPT
    )
    af_amount = sum(
        disputes[i].transaction_amount
        for i, d in enumerate(decisions)
        if d.routing == TriageRouting.AUTO_FIGHT
    )

    # Sort decisions: URGENT first, then by priority rank
    priority_rank = {
        TriagePriority.URGENT: 0,
        TriagePriority.HIGH:   1,
        TriagePriority.MEDIUM: 2,
        TriagePriority.LOW:    3,
        TriagePriority.DEFER:  4,
    }
    decisions.sort(key=lambda d: priority_rank[d.priority])

    summary = (
        f"Batch triage: {len(decisions)} disputes | "
        f"urgent={urgent} auto_accept={auto_accept} auto_fight={auto_fight} manual={manual}"
    )

    return BatchTriageReport(
        decisions=decisions,
        urgent_count=urgent,
        auto_accept_count=auto_accept,
        auto_fight_count=auto_fight,
        manual_review_count=manual,
        total_auto_accept_amount=round(aa_amount, 2),
        total_auto_fight_amount=round(af_amount, 2),
        summary=summary,
    )
