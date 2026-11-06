"""
Case Summarizer — machine-readable dispute summary for dashboards and AI pipelines.

Every other oracle module answers one specific question.
The case summarizer answers: "What is the complete current picture of this dispute?"

It aggregates all oracle signals into a single CaseSummary that:
  - Is self-contained: a downstream LLM or dashboard can act on it without reading anything else
  - Surfaces the single most important action right now
  - Encodes confidence and urgency
  - Provides a plain-English narrative for human review

This is the "case record" that ties all oracle modules together.

Typical consumers:
  - FastAPI endpoints serving the Anchor dashboard
  - APScheduler daily digest jobs
  - LLM pipelines that need structured dispute context
  - Alerting rules (escalate if urgency ≥ HIGH and days_remaining ≤ 3)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date, timedelta


class DisputePhase(str, Enum):
    NEW          = "NEW"           # < 3 days old, evidence not yet assembled
    IN_PROGRESS  = "IN_PROGRESS"   # Evidence assembled, response not yet submitted
    SUBMITTED    = "SUBMITTED"     # Response submitted, awaiting PayPal decision
    ESCALATED    = "ESCALATED"     # Escalated to PayPal claim (not just dispute)
    RESOLVED_WIN = "RESOLVED_WIN"
    RESOLVED_LOSS = "RESOLVED_LOSS"
    EXPIRED      = "EXPIRED"       # Missed deadline


class ActionUrgency(str, Enum):
    CRITICAL  = "CRITICAL"   # Act today — deadline imminent or account at risk
    HIGH      = "HIGH"       # Act within 24h
    MEDIUM    = "MEDIUM"     # Act within 3 days
    LOW       = "LOW"        # Can wait up to 7 days
    MONITOR   = "MONITOR"    # No action needed; just watch


class FightRecommendation(str, Enum):
    AUTO_FIGHT    = "AUTO_FIGHT"    # Fight automatically, evidence is strong
    FIGHT         = "FIGHT"         # Fight, evidence is good
    REVIEW        = "REVIEW"        # Review manually before deciding
    SETTLE        = "SETTLE"        # Settle proactively — cheaper than fighting
    REFUND        = "REFUND"        # Preemptive full refund
    PARTIAL_REFUND = "PARTIAL_REFUND"  # Offer partial refund to close


@dataclass
class DisputeCaseInput:
    """
    Current state of a PayPal dispute, as collected from all system signals.
    Fields map directly to outputs of other oracle modules.
    """
    # Identity
    dispute_id: str
    order_id: str
    opened_date: date
    deadline: date

    # Financial
    dispute_amount: float
    order_amount: float
    merchant_win_rate_90d: float = 0.65

    # Claim
    claim_type: str = "ITEM_NOT_RECEIVED"   # INR / SNAD / UNAUTHORIZED
    phase: DisputePhase = DisputePhase.NEW

    # Evidence state
    evidence_package_strength: str = "MODERATE"  # WEAK / MODERATE / STRONG
    response_ready: bool = False

    # Risk scores (from other oracle modules)
    fight_score: Optional[float] = None           # 0-100 from claim_scorer
    buyer_trust_score: Optional[float] = None     # 0-100 from buyer_lifecycle
    predicted_dispute_probability: Optional[float] = None  # from dispute_predictor

    # SP eligibility (from seller_protection_advisor)
    sp_protection_level: str = "UNKNOWN"  # FULL / PARTIAL / NONE / UNKNOWN

    # Timeline
    days_to_respond: Optional[int] = None   # PayPal's deadline for response
    is_escalated_to_claim: bool = False

    # Context flags
    is_repeat_buyer_dispute: bool = False
    buyer_stage: str = "UNKNOWN"            # From buyer_lifecycle
    hdr_impact: bool = False                # Counts toward HDR rate


@dataclass
class CaseSummary:
    """
    Complete, self-contained dispute case summary.
    Designed to be passed to a dashboard, LLM, or alerting system.
    """
    dispute_id: str
    order_id: str

    # Time state
    opened_date: date
    deadline: date
    days_remaining: int
    phase: DisputePhase

    # Financials
    dispute_amount: float
    expected_loss_if_uncontested: float   # dispute_amount × (1 - win_rate)
    ev_fight: float                       # EV of fighting: win_rate × amount - fight_cost

    # Decision
    fight_recommendation: FightRecommendation
    action_urgency: ActionUrgency
    next_action: str                      # Single most important action right now
    next_action_by: Optional[date]        # Deadline for the next action

    # Scores
    fight_score: float                    # 0-100 composite
    confidence: float                     # 0-1 confidence in recommendation
    sp_protection: str                    # FULL / PARTIAL / NONE / UNKNOWN

    # Narrative
    headline: str                         # One-liner for dashboard card
    narrative: str                        # 2-3 sentence human-readable summary
    key_facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    summary_line: str = ""               # Machine-readable CSV-friendly single line


_CLAIM_TYPE_WIN_RATE_BONUS = {
    "ITEM_NOT_RECEIVED": 0.05,          # PayPal favors merchants with tracking
    "SNAD": -0.08,                       # SNAD is harder to win
    "UNAUTHORIZED": 0.02,               # Reasonable with login evidence
}

_EVIDENCE_STRENGTH_MULTIPLIER = {
    "STRONG": 1.15,
    "MODERATE": 1.0,
    "WEAK": 0.80,
}

_FIGHT_COST_DEFAULT = 25.0  # $ internal cost per dispute fought


def _compute_fight_score(inp: DisputeCaseInput) -> float:
    """Compute composite fight score 0-100."""
    if inp.fight_score is not None:
        base = inp.fight_score
    else:
        # Derive from available signals
        base = 50.0

        # Win rate signal
        wr = inp.merchant_win_rate_90d
        wr_bonus = _CLAIM_TYPE_WIN_RATE_BONUS.get(inp.claim_type, 0.0)
        adjusted_wr = max(0.0, min(1.0, wr + wr_bonus))
        base += (adjusted_wr - 0.50) * 60   # -30 to +30

        # Evidence strength
        ev_mult = _EVIDENCE_STRENGTH_MULTIPLIER.get(inp.evidence_package_strength, 1.0)
        base *= ev_mult

        # SP bonus
        if inp.sp_protection_level == "FULL":
            base = min(100, base + 15)
        elif inp.sp_protection_level == "NONE":
            base -= 10

        # Buyer trust
        if inp.buyer_trust_score is not None:
            base += (inp.buyer_trust_score - 50) * 0.1

    return round(max(0.0, min(100.0, base)), 1)


def _compute_ev_fight(inp: DisputeCaseInput, fight_score: float) -> float:
    """EV(fight) = P(win) × amount − fight_cost"""
    wr = inp.merchant_win_rate_90d
    wr_adj = wr + _CLAIM_TYPE_WIN_RATE_BONUS.get(inp.claim_type, 0.0)
    p_win = max(0.0, min(1.0, wr_adj * (fight_score / 65.0)))  # scale by score
    return round(p_win * inp.dispute_amount - _FIGHT_COST_DEFAULT, 2)


def _fight_recommendation(
    inp: DisputeCaseInput,
    fight_score: float,
    ev_fight: float,
    days_remaining: int,
) -> FightRecommendation:
    if fight_score >= 80 and ev_fight > 0:
        return FightRecommendation.AUTO_FIGHT
    if fight_score >= 65 and ev_fight > 0:
        return FightRecommendation.FIGHT
    if fight_score >= 45:
        return FightRecommendation.REVIEW
    if inp.dispute_amount <= 30 or ev_fight < -50:
        return FightRecommendation.REFUND
    if fight_score < 45 and inp.dispute_amount > 100:
        return FightRecommendation.SETTLE
    return FightRecommendation.PARTIAL_REFUND


def _action_urgency(days_remaining: int, phase: DisputePhase) -> ActionUrgency:
    if phase in (DisputePhase.RESOLVED_WIN, DisputePhase.RESOLVED_LOSS, DisputePhase.EXPIRED, DisputePhase.SUBMITTED):
        return ActionUrgency.MONITOR
    if days_remaining <= 1:
        return ActionUrgency.CRITICAL
    if days_remaining <= 3:
        return ActionUrgency.HIGH
    if days_remaining <= 7:
        return ActionUrgency.MEDIUM
    return ActionUrgency.LOW


def _next_action(
    inp: DisputeCaseInput,
    rec: FightRecommendation,
    urgency: ActionUrgency,
) -> tuple[str, Optional[date]]:
    if inp.phase == DisputePhase.SUBMITTED:
        return "Monitor PayPal decision — no action needed until ruling", None
    if inp.phase in (DisputePhase.RESOLVED_WIN, DisputePhase.RESOLVED_LOSS):
        return "Log outcome and update win/loss model", None
    if inp.phase == DisputePhase.EXPIRED:
        return "Mark as expired — assess post-mortem for missed deadline", None

    if rec == FightRecommendation.REFUND:
        return "Issue full refund to close dispute preemptively", date.today()
    if rec == FightRecommendation.PARTIAL_REFUND:
        return "Contact buyer with partial refund offer before submitting PayPal response", date.today() + timedelta(days=1)
    if rec == FightRecommendation.SETTLE:
        return "Initiate settlement offer to buyer directly", date.today() + timedelta(days=1)

    if not inp.response_ready and inp.evidence_package_strength in ("WEAK",):
        return "Assemble missing evidence (tracking, photos, description) immediately", date.today() + timedelta(days=1)
    if not inp.response_ready:
        return "Complete evidence package and schedule PayPal response submission", inp.deadline - timedelta(days=2)

    return "Submit dispute response via PayPal Resolution Center (evidence ready)", inp.deadline - timedelta(days=2)


def _build_narrative(inp: DisputeCaseInput, fight_score: float, ev_fight: float, rec: FightRecommendation) -> str:
    claim_label = {
        "ITEM_NOT_RECEIVED": "Item Not Received (INR)",
        "SNAD": "Significantly Not As Described (SNAD)",
        "UNAUTHORIZED": "Unauthorized Transaction",
    }.get(inp.claim_type, inp.claim_type)

    ev_str = f"EV(fight)=${ev_fight:+.0f}"
    sp_str = f"SP: {inp.sp_protection_level}"

    fight_label = {
        FightRecommendation.AUTO_FIGHT: "auto-fight (high confidence)",
        FightRecommendation.FIGHT: "fight",
        FightRecommendation.REVIEW: "review manually",
        FightRecommendation.SETTLE: "settle",
        FightRecommendation.REFUND: "refund",
        FightRecommendation.PARTIAL_REFUND: "partial refund",
    }[rec]

    return (
        f"Dispute {inp.dispute_id}: {claim_label} for ${inp.dispute_amount:.0f}. "
        f"Fight score {fight_score:.0f}/100 ({ev_str}, {sp_str}). "
        f"Recommendation: {fight_label}. "
        f"Evidence package: {inp.evidence_package_strength}."
    )


def _build_key_facts(inp: DisputeCaseInput, days_remaining: int) -> list[str]:
    facts: list[str] = []
    facts.append(f"Deadline: {inp.deadline} ({days_remaining} days remaining)")
    facts.append(f"Claim type: {inp.claim_type}")
    facts.append(f"Evidence: {inp.evidence_package_strength}, ready: {'Yes' if inp.response_ready else 'No'}")
    facts.append(f"Win rate (90d): {inp.merchant_win_rate_90d:.0%}")
    if inp.sp_protection_level != "UNKNOWN":
        facts.append(f"Seller Protection: {inp.sp_protection_level}")
    if inp.buyer_stage != "UNKNOWN":
        facts.append(f"Buyer stage: {inp.buyer_stage}")
    return facts


def _build_warnings(inp: DisputeCaseInput, days_remaining: int, rec: FightRecommendation) -> list[str]:
    warnings: list[str] = []
    if days_remaining <= 2:
        warnings.append(f"DEADLINE IMMINENT: {days_remaining} day(s) remaining to respond")
    if inp.claim_type == "SNAD":
        warnings.append("SNAD: Seller Protection does not apply — win depends entirely on evidence quality")
    if inp.sp_protection_level == "NONE":
        warnings.append("No Seller Protection: dispute loss = full financial loss to merchant")
    if inp.evidence_package_strength == "WEAK" and rec in (FightRecommendation.AUTO_FIGHT, FightRecommendation.FIGHT):
        warnings.append("WEAK evidence while recommending fight — reconsider or strengthen evidence first")
    if inp.hdr_impact:
        warnings.append("This dispute counts toward HDR rate — resolution affects monthly dispute rate")
    if inp.is_repeat_buyer_dispute:
        warnings.append("Repeat disputer — escalate buyer to ELEVATED_WATCH or SUSPICIOUS lifecycle stage")
    return warnings


def summarize_case(inp: DisputeCaseInput, today: Optional[date] = None) -> CaseSummary:
    """
    Generate a complete, self-contained case summary for one dispute.
    """
    if today is None:
        today = date.today()

    days_remaining = (inp.deadline - today).days
    fight_score = _compute_fight_score(inp)
    ev_fight = _compute_ev_fight(inp, fight_score)
    expected_loss = round(inp.dispute_amount * (1.0 - inp.merchant_win_rate_90d), 2)

    rec = _fight_recommendation(inp, fight_score, ev_fight, days_remaining)
    urgency = _action_urgency(days_remaining, inp.phase)
    next_action, next_by = _next_action(inp, rec, urgency)

    headline = (
        f"{inp.claim_type} ${inp.dispute_amount:.0f} — "
        f"{rec.value.replace('_', ' ').title()} "
        f"(score {fight_score:.0f}, {days_remaining}d left)"
    )

    narrative = _build_narrative(inp, fight_score, ev_fight, rec)
    key_facts = _build_key_facts(inp, days_remaining)
    warnings = _build_warnings(inp, days_remaining, rec)

    confidence_base = 0.65
    if inp.fight_score is not None:
        confidence_base += 0.10
    if inp.sp_protection_level != "UNKNOWN":
        confidence_base += 0.05
    if inp.response_ready:
        confidence_base += 0.05
    confidence = round(min(0.95, confidence_base), 2)

    summary_line = (
        f"{inp.dispute_id},{inp.claim_type},{inp.dispute_amount:.2f},"
        f"{fight_score:.0f},{ev_fight:.2f},{rec.value},{urgency.value},{days_remaining}"
    )

    return CaseSummary(
        dispute_id=inp.dispute_id,
        order_id=inp.order_id,
        opened_date=inp.opened_date,
        deadline=inp.deadline,
        days_remaining=days_remaining,
        phase=inp.phase,
        dispute_amount=inp.dispute_amount,
        expected_loss_if_uncontested=expected_loss,
        ev_fight=ev_fight,
        fight_recommendation=rec,
        action_urgency=urgency,
        next_action=next_action,
        next_action_by=next_by,
        fight_score=fight_score,
        confidence=confidence,
        sp_protection=inp.sp_protection_level,
        headline=headline,
        narrative=narrative,
        key_facts=key_facts,
        warnings=warnings,
        summary_line=summary_line,
    )


def batch_summarize(
    cases: list[DisputeCaseInput],
    today: Optional[date] = None,
) -> list[CaseSummary]:
    """
    Summarize a portfolio of disputes, sorted by urgency (CRITICAL first).
    """
    urgency_order = {
        ActionUrgency.CRITICAL: 0,
        ActionUrgency.HIGH: 1,
        ActionUrgency.MEDIUM: 2,
        ActionUrgency.LOW: 3,
        ActionUrgency.MONITOR: 4,
    }
    summaries = [summarize_case(c, today) for c in cases]
    summaries.sort(key=lambda s: (urgency_order[s.action_urgency], s.days_remaining))
    return summaries
