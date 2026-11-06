"""
Claim Router — intelligent dispute routing engine.

Determines the handling path for each dispute: auto-fight, human review,
or auto-refund (with partial-refund and settle as intermediary paths).

The routing decision is a portfolio-level policy question, not a per-dispute
calculation in isolation. It answers:
  "Given a merchant's risk tolerance, staffing capacity, and current HDR
   exposure, which disputes should Claude fight automatically, which need
   a human, and which should be refunded now to avoid escalation costs?"

Three primary routes:
  AUTO_FIGHT    — high-confidence, evidence is ready, EV positive
  HUMAN_REVIEW  — uncertain evidence, borderline EV, or policy flag
  AUTO_REFUND   — negative EV, low amount, or HDR pressure requires quick close

Two secondary routes:
  PARTIAL_REFUND  — buyer-satisfaction play: offer partial now to avoid full loss
  SETTLE          — direct buyer contact before PayPal ruling (saves dispute fees)

Configurable thresholds allow merchants to tune aggressiveness:
  - fight_threshold: minimum fight score for AUTO_FIGHT (default 72)
  - review_threshold: minimum fight score for HUMAN_REVIEW vs refund (default 45)
  - max_auto_fight_per_day: capacity cap for autonomous actions (default 50)
  - hdr_pressure_mode: when True, tighten thresholds to minimize new disputes

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Route(str, Enum):
    AUTO_FIGHT    = "AUTO_FIGHT"      # Submit response automatically
    HUMAN_REVIEW  = "HUMAN_REVIEW"    # Queue for human review before acting
    AUTO_REFUND   = "AUTO_REFUND"     # Issue full refund automatically
    PARTIAL_REFUND = "PARTIAL_REFUND"  # Issue partial refund
    SETTLE        = "SETTLE"          # Contact buyer to settle out of PayPal
    ESCALATE      = "ESCALATE"        # Escalate to specialist (legal, fraud team)


class RoutingReason(str, Enum):
    HIGH_EV_STRONG_EVIDENCE  = "HIGH_EV_STRONG_EVIDENCE"
    HIGH_SCORE_AUTO_ELIGIBLE = "HIGH_SCORE_AUTO_ELIGIBLE"
    HDR_PRESSURE_REFUND      = "HDR_PRESSURE_REFUND"
    LOW_AMOUNT_NOT_WORTH_FIGHTING = "LOW_AMOUNT_NOT_WORTH_FIGHTING"
    NEGATIVE_EV              = "NEGATIVE_EV"
    WEAK_EVIDENCE_REVIEW     = "WEAK_EVIDENCE_REVIEW"
    BORDERLINE_SCORE         = "BORDERLINE_SCORE"
    MISSING_EVIDENCE         = "MISSING_EVIDENCE"
    REPEAT_FRAUDSTER_ESCALATE = "REPEAT_FRAUDSTER_ESCALATE"
    HIGH_VALUE_HUMAN_REQUIRED = "HIGH_VALUE_HUMAN_REQUIRED"
    CAPACITY_LIMIT_REVIEW    = "CAPACITY_LIMIT_REVIEW"
    SNAD_LOW_SCORE_SETTLE    = "SNAD_LOW_SCORE_SETTLE"
    PARTIAL_EVIDENCE_PARTIAL_REFUND = "PARTIAL_EVIDENCE_PARTIAL_REFUND"


@dataclass
class RoutingPolicy:
    """
    Merchant-level policy for routing decisions.
    All thresholds are tunable per merchant.
    """
    # Score thresholds (0-100 fight score)
    auto_fight_threshold: float = 72.0       # Score ≥ this → AUTO_FIGHT
    human_review_threshold: float = 45.0     # Score ≥ this → HUMAN_REVIEW
    # Below human_review_threshold → AUTO_REFUND or SETTLE

    # Amount thresholds
    min_amount_to_fight: float = 30.0        # Disputes < this → AUTO_REFUND
    high_value_threshold: float = 1000.0     # Disputes ≥ this → always human review

    # EV guard
    min_ev_to_auto_fight: float = 10.0       # EV(fight) must exceed this for AUTO_FIGHT
    min_ev_to_review: float = -20.0          # EV below this → skip review, auto-refund

    # Capacity cap (per day)
    max_auto_fight_per_day: int = 50

    # HDR pressure mode: tighten thresholds when HDR is elevated
    hdr_pressure_mode: bool = False          # If True, use elevated thresholds
    hdr_pressure_fight_threshold: float = 82.0
    hdr_pressure_review_threshold: float = 60.0

    # Claim-type policy overrides
    always_human_review_snad: bool = True    # SNAD always human (evidence is complex)
    always_fight_duplicate: bool = True      # Duplicate charges: always fight (easy win)
    escalate_repeat_fraudsters: bool = True  # Buyer with 3+ disputes → ESCALATE


@dataclass
class DisputeRoutingInput:
    """
    State of one dispute as input to the routing decision.
    Fields are outputs of the oracle pipeline (case_summarizer, etc.)
    """
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / DUPLICATE

    # Scoring (from case_summarizer / claim_scorer)
    fight_score: float            # 0-100
    ev_fight: float               # $ EV of fighting

    # Financial
    dispute_amount: float

    # Evidence state
    evidence_ready: bool          # Response can be submitted today
    evidence_strength: str        # WEAK / MODERATE / STRONG
    has_tracking: bool = True
    has_delivery_confirmation: bool = False

    # Risk signals
    buyer_dispute_count_90d: int = 0   # How many disputes this buyer filed recently
    is_hdr_contributing: bool = True   # This dispute counts toward HDR rate

    # Deadline
    days_remaining: int = 10

    # Optional override from upstream modules
    sp_protection: str = "UNKNOWN"    # FULL / PARTIAL / NONE / UNKNOWN
    is_escalated_to_claim: bool = False


@dataclass
class RoutingDecision:
    """
    Routing decision for one dispute.
    """
    dispute_id: str
    route: Route
    primary_reason: RoutingReason
    confidence: float                   # 0-1 confidence in this route

    # Context
    fight_score: float
    ev_fight: float
    dispute_amount: float

    # Action guidance
    action_summary: str                 # What to do right now
    caveats: list[str] = field(default_factory=list)
    escalation_target: Optional[str] = None  # Who to escalate to, if ESCALATE

    summary_line: str = ""


@dataclass
class PortfolioRoutingPlan:
    """
    Routing decisions for a portfolio of disputes, with daily capacity accounting.
    """
    decisions: list[RoutingDecision]
    total_disputes: int

    auto_fight_count: int
    human_review_count: int
    auto_refund_count: int
    partial_refund_count: int
    settle_count: int
    escalate_count: int

    capacity_used: int              # AUTO_FIGHT disputes scheduled today
    capacity_remaining: int         # Available slots for more AUTO_FIGHT today

    total_ev_if_all_fought: float
    total_at_risk_amount: float     # Sum of dispute amounts → human review / escalate

    summary: str


def _effective_thresholds(policy: RoutingPolicy) -> tuple[float, float]:
    """Return (fight_threshold, review_threshold) adjusted for HDR pressure."""
    if policy.hdr_pressure_mode:
        return policy.hdr_pressure_fight_threshold, policy.hdr_pressure_review_threshold
    return policy.auto_fight_threshold, policy.human_review_threshold


def route_dispute(
    inp: DisputeRoutingInput,
    policy: RoutingPolicy,
    auto_fight_used_today: int = 0,
) -> RoutingDecision:
    """
    Route one dispute to its handling path given the merchant's policy.

    auto_fight_used_today: how many AUTO_FIGHT decisions already made today
    (used for capacity cap).
    """
    fight_thr, review_thr = _effective_thresholds(policy)
    caveats: list[str] = []
    escalation_target: Optional[str] = None

    # ── Override rules ────────────────────────────────────────────────────────

    # Repeat fraudster → escalate regardless of score
    if policy.escalate_repeat_fraudsters and inp.buyer_dispute_count_90d >= 3:
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.ESCALATE,
            primary_reason=RoutingReason.REPEAT_FRAUDSTER_ESCALATE,
            confidence=0.92,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Escalate to fraud team: buyer filed {inp.buyer_dispute_count_90d} disputes in 90 days. "
                f"Flag buyer account and assemble fraud evidence package."
            ),
            caveats=["Do not issue refund until fraud team reviews — may be organized fraud"],
            escalation_target="fraud_team",
            summary_line=f"{inp.dispute_id},ESCALATE,REPEAT_FRAUDSTER,{inp.dispute_amount:.0f}",
        )

    # High-value → always human review
    if inp.dispute_amount >= policy.high_value_threshold:
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.HUMAN_REVIEW,
            primary_reason=RoutingReason.HIGH_VALUE_HUMAN_REQUIRED,
            confidence=0.95,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Human review required: ${inp.dispute_amount:.0f} exceeds high-value threshold "
                f"(${policy.high_value_threshold:.0f}). Assign to dispute specialist."
            ),
            caveats=["High-value disputes warrant human judgment regardless of score"],
            summary_line=f"{inp.dispute_id},HUMAN_REVIEW,HIGH_VALUE,{inp.dispute_amount:.0f}",
        )

    # Duplicate → always fight (easy win, no negotiation)
    if policy.always_fight_duplicate and inp.claim_type == "DUPLICATE":
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_FIGHT,
            primary_reason=RoutingReason.HIGH_EV_STRONG_EVIDENCE,
            confidence=0.90,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                "Auto-fight: DUPLICATE charge — submit payment records showing single charge. "
                "Win rate ~65%+ for duplicates with records."
            ),
            summary_line=f"{inp.dispute_id},AUTO_FIGHT,DUPLICATE_POLICY,{inp.dispute_amount:.0f}",
        )

    # SNAD → always human if policy says so
    if policy.always_human_review_snad and inp.claim_type == "SNAD":
        caveats.append("SNAD: Seller Protection does not apply — evidence quality is everything")
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.HUMAN_REVIEW,
            primary_reason=RoutingReason.WEAK_EVIDENCE_REVIEW,
            confidence=0.85,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                "Human review required (SNAD policy): review product photos, descriptions, "
                "and buyer communications before submitting response."
            ),
            caveats=caveats,
            summary_line=f"{inp.dispute_id},HUMAN_REVIEW,SNAD_POLICY,{inp.dispute_amount:.0f}",
        )

    # ── Amount too small to fight ─────────────────────────────────────────────
    if inp.dispute_amount < policy.min_amount_to_fight:
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_REFUND,
            primary_reason=RoutingReason.LOW_AMOUNT_NOT_WORTH_FIGHTING,
            confidence=0.95,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Auto-refund: ${inp.dispute_amount:.0f} < minimum fight threshold "
                f"(${policy.min_amount_to_fight:.0f}). Issue refund to close."
            ),
            summary_line=f"{inp.dispute_id},AUTO_REFUND,LOW_AMOUNT,{inp.dispute_amount:.0f}",
        )

    # ── HDR pressure mode: fast-close marginally-negative EV disputes ─────────
    if policy.hdr_pressure_mode and inp.ev_fight < 0 and inp.is_hdr_contributing:
        caveats.append("HDR pressure mode: accepting small losses to protect dispute rate")
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_REFUND,
            primary_reason=RoutingReason.HDR_PRESSURE_REFUND,
            confidence=0.88,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Auto-refund (HDR pressure): EV={inp.ev_fight:+.0f}, accepting loss to protect "
                f"HDR rate. Refund immediately to remove from dispute count."
            ),
            caveats=caveats,
            summary_line=f"{inp.dispute_id},AUTO_REFUND,HDR_PRESSURE,{inp.dispute_amount:.0f}",
        )

    # ── EV below review floor → auto-refund ───────────────────────────────────
    if inp.ev_fight < policy.min_ev_to_review:
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_REFUND,
            primary_reason=RoutingReason.NEGATIVE_EV,
            confidence=0.90,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Auto-refund: EV(fight)=${inp.ev_fight:+.0f} is deeply negative. "
                f"Issuing refund is cheaper than fighting."
            ),
            summary_line=f"{inp.dispute_id},AUTO_REFUND,NEGATIVE_EV,{inp.dispute_amount:.0f}",
        )

    # ── Score-based routing ───────────────────────────────────────────────────
    if inp.fight_score >= fight_thr and inp.ev_fight >= policy.min_ev_to_auto_fight:
        # Check daily capacity
        if auto_fight_used_today >= policy.max_auto_fight_per_day:
            caveats.append(
                f"Daily AUTO_FIGHT capacity reached ({policy.max_auto_fight_per_day}). "
                f"Routing to human review queue — fight tomorrow."
            )
            return RoutingDecision(
                dispute_id=inp.dispute_id,
                route=Route.HUMAN_REVIEW,
                primary_reason=RoutingReason.CAPACITY_LIMIT_REVIEW,
                confidence=0.80,
                fight_score=inp.fight_score,
                ev_fight=inp.ev_fight,
                dispute_amount=inp.dispute_amount,
                action_summary=(
                    f"Queue for tomorrow's auto-fight batch (capacity full today). "
                    f"Score={inp.fight_score:.0f}, EV=${inp.ev_fight:+.0f}."
                ),
                caveats=caveats,
                summary_line=f"{inp.dispute_id},HUMAN_REVIEW,CAPACITY_CAP,{inp.dispute_amount:.0f}",
            )

        # Check evidence readiness
        if not inp.evidence_ready:
            caveats.append("Evidence not yet ready — assemble before auto-fight")
            return RoutingDecision(
                dispute_id=inp.dispute_id,
                route=Route.HUMAN_REVIEW,
                primary_reason=RoutingReason.MISSING_EVIDENCE,
                confidence=0.82,
                fight_score=inp.fight_score,
                ev_fight=inp.ev_fight,
                dispute_amount=inp.dispute_amount,
                action_summary=(
                    f"Human review: score qualifies for AUTO_FIGHT ({inp.fight_score:.0f}) but "
                    f"evidence package not ready. Assemble evidence before submitting."
                ),
                caveats=caveats,
                summary_line=f"{inp.dispute_id},HUMAN_REVIEW,MISSING_EVIDENCE,{inp.dispute_amount:.0f}",
            )

        reason = (
            RoutingReason.HIGH_EV_STRONG_EVIDENCE
            if inp.evidence_strength == "STRONG"
            else RoutingReason.HIGH_SCORE_AUTO_ELIGIBLE
        )
        confidence = 0.88 if inp.evidence_strength == "STRONG" else 0.78
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_FIGHT,
            primary_reason=reason,
            confidence=confidence,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Auto-fight: score={inp.fight_score:.0f} ≥ threshold={fight_thr:.0f}, "
                f"EV=${inp.ev_fight:+.0f}, evidence={inp.evidence_strength}. "
                f"Submit response via PayPal Resolution Center."
            ),
            summary_line=f"{inp.dispute_id},AUTO_FIGHT,SCORE,{inp.dispute_amount:.0f}",
        )

    elif inp.fight_score >= review_thr:
        # Borderline — check for settlement opportunity
        if inp.claim_type == "SNAD" and inp.fight_score < 55:
            return RoutingDecision(
                dispute_id=inp.dispute_id,
                route=Route.SETTLE,
                primary_reason=RoutingReason.SNAD_LOW_SCORE_SETTLE,
                confidence=0.75,
                fight_score=inp.fight_score,
                ev_fight=inp.ev_fight,
                dispute_amount=inp.dispute_amount,
                action_summary=(
                    f"Settle: SNAD with low fight score ({inp.fight_score:.0f}). "
                    f"Contact buyer directly with partial remedy before PayPal ruling."
                ),
                caveats=["Avoid PayPal ruling for SNAD — settle directly with buyer"],
                summary_line=f"{inp.dispute_id},SETTLE,SNAD_LOW_SCORE,{inp.dispute_amount:.0f}",
            )
        # Partial evidence → partial refund offer
        if inp.evidence_strength == "WEAK" and inp.has_tracking and not inp.has_delivery_confirmation:
            return RoutingDecision(
                dispute_id=inp.dispute_id,
                route=Route.PARTIAL_REFUND,
                primary_reason=RoutingReason.PARTIAL_EVIDENCE_PARTIAL_REFUND,
                confidence=0.70,
                fight_score=inp.fight_score,
                ev_fight=inp.ev_fight,
                dispute_amount=inp.dispute_amount,
                action_summary=(
                    f"Partial refund: weak evidence (tracking but no delivery confirmation). "
                    f"Offer 50% refund to close — cheaper than a likely loss."
                ),
                caveats=["Get delivery confirmation before fighting full INR claims"],
                summary_line=f"{inp.dispute_id},PARTIAL_REFUND,WEAK_EVIDENCE,{inp.dispute_amount:.0f}",
            )
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.HUMAN_REVIEW,
            primary_reason=RoutingReason.BORDERLINE_SCORE,
            confidence=0.72,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Human review: score {inp.fight_score:.0f} in review band ({review_thr:.0f}-{fight_thr:.0f}). "
                f"Needs specialist decision — EV=${inp.ev_fight:+.0f}."
            ),
            summary_line=f"{inp.dispute_id},HUMAN_REVIEW,BORDERLINE,{inp.dispute_amount:.0f}",
        )

    else:
        # Score below review threshold
        if inp.ev_fight > 0:
            # Positive EV but low score — human should decide
            return RoutingDecision(
                dispute_id=inp.dispute_id,
                route=Route.HUMAN_REVIEW,
                primary_reason=RoutingReason.WEAK_EVIDENCE_REVIEW,
                confidence=0.65,
                fight_score=inp.fight_score,
                ev_fight=inp.ev_fight,
                dispute_amount=inp.dispute_amount,
                action_summary=(
                    f"Human review: low fight score ({inp.fight_score:.0f}) but positive EV (${inp.ev_fight:+.0f}). "
                    f"Strengthen evidence before auto-routing."
                ),
                caveats=["Low score despite positive EV suggests missing evidence"],
                summary_line=f"{inp.dispute_id},HUMAN_REVIEW,WEAK_EVIDENCE,{inp.dispute_amount:.0f}",
            )
        return RoutingDecision(
            dispute_id=inp.dispute_id,
            route=Route.AUTO_REFUND,
            primary_reason=RoutingReason.NEGATIVE_EV,
            confidence=0.85,
            fight_score=inp.fight_score,
            ev_fight=inp.ev_fight,
            dispute_amount=inp.dispute_amount,
            action_summary=(
                f"Auto-refund: score={inp.fight_score:.0f} below review threshold ({review_thr:.0f}) "
                f"and EV=${inp.ev_fight:+.0f}. Issue refund."
            ),
            summary_line=f"{inp.dispute_id},AUTO_REFUND,LOW_SCORE,{inp.dispute_amount:.0f}",
        )


def route_portfolio(
    disputes: list[DisputeRoutingInput],
    policy: RoutingPolicy,
) -> PortfolioRoutingPlan:
    """
    Route a portfolio of disputes, respecting daily capacity limits.
    Disputes sorted highest score first so best candidates fill capacity.
    """
    sorted_disputes = sorted(disputes, key=lambda d: d.fight_score, reverse=True)
    decisions: list[RoutingDecision] = []
    auto_fight_count = 0

    for d in sorted_disputes:
        decision = route_dispute(d, policy, auto_fight_used_today=auto_fight_count)
        decisions.append(decision)
        if decision.route == Route.AUTO_FIGHT:
            auto_fight_count += 1

    counts = {r: 0 for r in Route}
    for dec in decisions:
        counts[dec.route] += 1

    total_ev = sum(d.ev_fight for d in disputes)
    at_risk = sum(
        d.dispute_amount for d, dec in zip(disputes, decisions)
        if dec.route in (Route.HUMAN_REVIEW, Route.ESCALATE)
    )

    cap_remaining = max(0, policy.max_auto_fight_per_day - auto_fight_count)

    summary = (
        f"Portfolio routed: {len(disputes)} disputes — "
        f"AUTO_FIGHT={counts[Route.AUTO_FIGHT]}, "
        f"HUMAN={counts[Route.HUMAN_REVIEW]}, "
        f"REFUND={counts[Route.AUTO_REFUND]}, "
        f"PARTIAL={counts[Route.PARTIAL_REFUND]}, "
        f"SETTLE={counts[Route.SETTLE]}, "
        f"ESCALATE={counts[Route.ESCALATE]} | "
        f"total_EV=${total_ev:+.0f} | at_risk=${at_risk:.0f}"
    )

    return PortfolioRoutingPlan(
        decisions=decisions,
        total_disputes=len(disputes),
        auto_fight_count=counts[Route.AUTO_FIGHT],
        human_review_count=counts[Route.HUMAN_REVIEW],
        auto_refund_count=counts[Route.AUTO_REFUND],
        partial_refund_count=counts[Route.PARTIAL_REFUND],
        settle_count=counts[Route.SETTLE],
        escalate_count=counts[Route.ESCALATE],
        capacity_used=auto_fight_count,
        capacity_remaining=cap_remaining,
        total_ev_if_all_fought=round(total_ev, 2),
        total_at_risk_amount=round(at_risk, 2),
        summary=summary,
    )
