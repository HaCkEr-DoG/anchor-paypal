"""
Dispute Complexity Estimator — forecast how much effort a dispute will
require before it is resolved.

Complexity drives:
  - Staff-hour estimates for responding
  - Routing decisions (auto-process vs. human review)
  - SLA tier assignment

Five complexity drivers scored 0–20 each:
  1. Evidence burden (how hard to assemble evidence)
  2. Legal/procedural complexity (appeals, arbitration risk)
  3. Buyer risk (serial abusers inflate complexity)
  4. Network rules complexity (CE3, pre-arb, cross-border)
  5. Time pressure (deadline proximity, escalation windows)

Output: total 0–100, tier (TRIVIAL/LOW/MEDIUM/HIGH/EXTREME), staff hours.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ComplexityTier(str, Enum):
    TRIVIAL = "TRIVIAL"   # Score 0–19   — auto-process, < 0.5 hours
    LOW     = "LOW"       # Score 20–39  — standard queue, 0.5–2 hours
    MEDIUM  = "MEDIUM"    # Score 40–59  — senior review, 2–5 hours
    HIGH    = "HIGH"      # Score 60–79  — specialist, 5–12 hours
    EXTREME = "EXTREME"   # Score 80–100 — legal/exec, 12+ hours


class ComplexityDriver(str, Enum):
    EVIDENCE_BURDEN        = "EVIDENCE_BURDEN"
    LEGAL_PROCEDURAL       = "LEGAL_PROCEDURAL"
    BUYER_RISK             = "BUYER_RISK"
    NETWORK_RULES          = "NETWORK_RULES"
    TIME_PRESSURE          = "TIME_PRESSURE"


@dataclass
class ComplexityDriverScore:
    driver: ComplexityDriver
    score: int      # 0–20
    max_score: int  # Always 20
    rationale: str


@dataclass
class DisputeComplexityInput:
    """Input for complexity estimation."""
    dispute_id: str
    reason_code: str
    network: str
    transaction_amount: float

    # Evidence signals
    evidence_completeness_score: int = 50     # 0–100
    num_evidence_types_required: int = 3
    has_cross_border_transaction: bool = False
    has_digital_goods: bool = False

    # Legal / procedural signals
    is_arbitration_risk: bool = False          # Prior escalation history
    network_has_pre_arb: bool = False          # Network supports pre-arb
    is_high_value: bool = False                # Amount ≥ $1000
    is_class_action_risk: bool = False         # Pattern of related disputes

    # Buyer risk signals
    buyer_risk_class: str = "NORMAL"           # TRUSTED/NORMAL/ELEVATED/HIGH_RISK/FRAUDSTER
    buyer_dispute_count: int = 0
    buyer_represented: bool = False            # Buyer has an advocate/lawyer

    # Network rule complexity
    is_ce3_applicable: bool = False            # CE3.0 may apply
    is_subscription: bool = False
    num_prior_appeals: int = 0

    # Time pressure
    days_remaining: int = 20                   # Days left in response window
    is_in_pre_arb: bool = False
    is_in_arbitration: bool = False


@dataclass
class DisputeComplexityReport:
    """Full complexity assessment."""
    dispute_id: str
    total_score: int
    complexity_tier: ComplexityTier

    driver_scores: list[ComplexityDriverScore]

    estimated_staff_hours: float
    recommended_assignee: str       # e.g. "junior_agent", "senior_agent", "legal_team"
    sla_hours: int                  # Target resolution SLA

    complexity_flags: list[str]
    summary: str


def _score_evidence_burden(inp: DisputeComplexityInput) -> ComplexityDriverScore:
    score = 0
    notes: list[str] = []

    # Low completeness = high burden to gather
    if inp.evidence_completeness_score < 30:
        score += 10
        notes.append("low completeness (<30) — heavy gathering needed")
    elif inp.evidence_completeness_score < 60:
        score += 5

    if inp.num_evidence_types_required >= 5:
        score += 6
        notes.append(f"{inp.num_evidence_types_required} evidence types required")
    elif inp.num_evidence_types_required >= 3:
        score += 3

    if inp.has_cross_border_transaction:
        score += 3
        notes.append("cross-border logistics add evidence complexity")
    if inp.has_digital_goods:
        score += 2
        notes.append("digital goods: usage logs, IP, access records needed")

    rationale = "; ".join(notes) if notes else "standard evidence burden"
    return ComplexityDriverScore(ComplexityDriver.EVIDENCE_BURDEN, min(20, score), 20, rationale)


def _score_legal_procedural(inp: DisputeComplexityInput) -> ComplexityDriverScore:
    score = 0
    notes: list[str] = []

    if inp.is_arbitration_risk:
        score += 10
        notes.append("arbitration history — legal review required")
    if inp.is_high_value:
        score += 6
        notes.append(f"high value dispute: ${inp.transaction_amount:.0f}")
    if inp.network_has_pre_arb:
        score += 2
        notes.append("pre-arb stage available — adds procedural step")
    if inp.is_class_action_risk:
        score += 8
        notes.append("possible class action pattern — escalation required")

    rationale = "; ".join(notes) if notes else "standard procedural complexity"
    return ComplexityDriverScore(ComplexityDriver.LEGAL_PROCEDURAL, min(20, score), 20, rationale)


def _score_buyer_risk(inp: DisputeComplexityInput) -> ComplexityDriverScore:
    score = 0
    notes: list[str] = []

    risk_scores = {
        "TRUSTED": 0, "NORMAL": 2, "ELEVATED": 6,
        "HIGH_RISK": 12, "FRAUDSTER": 15,
    }
    base = risk_scores.get(inp.buyer_risk_class.upper(), 2)
    score += base
    if base > 0:
        notes.append(f"buyer risk class: {inp.buyer_risk_class}")

    if inp.buyer_dispute_count >= 5:
        score += 4
        notes.append(f"{inp.buyer_dispute_count} prior disputes")
    elif inp.buyer_dispute_count >= 2:
        score += 2

    if inp.buyer_represented:
        score += 5
        notes.append("buyer has legal representation — formal correspondence expected")

    rationale = "; ".join(notes) if notes else "low buyer risk"
    return ComplexityDriverScore(ComplexityDriver.BUYER_RISK, min(20, score), 20, rationale)


def _score_network_rules(inp: DisputeComplexityInput) -> ComplexityDriverScore:
    score = 0
    notes: list[str] = []

    if inp.is_ce3_applicable:
        score += 7
        notes.append("CE3.0 package required — specific form and prior txn documentation")
    if inp.is_subscription:
        score += 5
        notes.append("subscription dispute: terms, renewal, usage evidence needed")
    if inp.num_prior_appeals >= 2:
        score += 8
        notes.append(f"{inp.num_prior_appeals} prior appeals — history review required")
    elif inp.num_prior_appeals == 1:
        score += 4
        notes.append("1 prior appeal — review prior submission")

    # Cross-network complexity
    net = inp.network.upper()
    if net in ("VISA", "MC"):
        score += 1  # Most complex network rules

    rationale = "; ".join(notes) if notes else "standard network rules"
    return ComplexityDriverScore(ComplexityDriver.NETWORK_RULES, min(20, score), 20, rationale)


def _score_time_pressure(inp: DisputeComplexityInput) -> ComplexityDriverScore:
    score = 0
    notes: list[str] = []

    if inp.is_in_arbitration:
        score += 15
        notes.append("IN ARBITRATION — maximum time pressure")
    elif inp.is_in_pre_arb:
        score += 10
        notes.append("in pre-arb stage — elevated urgency")
    elif inp.days_remaining <= 2:
        score += 15
        notes.append(f"only {inp.days_remaining} day(s) remaining — critical")
    elif inp.days_remaining <= 5:
        score += 10
        notes.append(f"{inp.days_remaining} days remaining — high urgency")
    elif inp.days_remaining <= 10:
        score += 5
        notes.append(f"{inp.days_remaining} days remaining — moderate urgency")

    rationale = "; ".join(notes) if notes else f"{inp.days_remaining} days remaining — low pressure"
    return ComplexityDriverScore(ComplexityDriver.TIME_PRESSURE, min(20, score), 20, rationale)


_HOURS_BY_TIER: dict[ComplexityTier, tuple[float, str, int]] = {
    # tier → (staff_hours, assignee, sla_hours)
    ComplexityTier.TRIVIAL: (0.25, "auto_process",   24),
    ComplexityTier.LOW:     (1.0,  "junior_agent",   48),
    ComplexityTier.MEDIUM:  (3.5,  "senior_agent",   72),
    ComplexityTier.HIGH:    (8.0,  "specialist",    120),
    ComplexityTier.EXTREME: (20.0, "legal_team",    240),
}


def estimate_dispute_complexity(inp: DisputeComplexityInput) -> DisputeComplexityReport:
    """Estimate end-to-end complexity of resolving a dispute."""
    drivers = [
        _score_evidence_burden(inp),
        _score_legal_procedural(inp),
        _score_buyer_risk(inp),
        _score_network_rules(inp),
        _score_time_pressure(inp),
    ]

    total = sum(d.score for d in drivers)
    total = max(0, min(100, total))

    if total >= 80:
        tier = ComplexityTier.EXTREME
    elif total >= 60:
        tier = ComplexityTier.HIGH
    elif total >= 40:
        tier = ComplexityTier.MEDIUM
    elif total >= 20:
        tier = ComplexityTier.LOW
    else:
        tier = ComplexityTier.TRIVIAL

    hours, assignee, sla = _HOURS_BY_TIER[tier]

    flags: list[str] = []
    for d in drivers:
        if d.score >= 15:
            flags.append(f"[HIGH] {d.driver.value}: {d.rationale}")
        elif d.score >= 10:
            flags.append(f"[MODERATE] {d.driver.value}: {d.rationale}")

    summary = (
        f"Complexity {inp.dispute_id}: "
        f"score={total}/100 ({tier.value}) | "
        f"est_hours={hours} | "
        f"assignee={assignee} | sla={sla}h"
    )

    return DisputeComplexityReport(
        dispute_id=inp.dispute_id,
        total_score=total,
        complexity_tier=tier,
        driver_scores=drivers,
        estimated_staff_hours=hours,
        recommended_assignee=assignee,
        sla_hours=sla,
        complexity_flags=flags,
        summary=summary,
    )
