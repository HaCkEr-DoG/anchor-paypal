"""
Batch Dispatcher — portfolio-level dispute action queue generator.

The autopilot's daily brief: takes every open dispute, runs claim_scorer
on each, and returns a priority-ordered work queue with time estimates,
parallelization opportunities, and risk alerts.

What it solves:
  - A merchant with 30 open disputes can't review all 30 manually
  - Some need action TODAY (overdue), some are optimal-window, some can wait
  - The optimal strategy is NOT to process disputes in submission order

The dispatcher produces:
  1. MUST_ACT_TODAY: Overdue or critical deadline disputes
  2. ACT_NOW_OPTIMAL: In the algorithmic sweet spot for response timing
  3. REVIEW_QUEUE: Needs human review before fighting (borderline scores)
  4. AUTO_FIGHT: High-confidence fights that can be processed automatically
  5. AUTO_ACCEPT: Clear accepts — negative EV, don't waste time
  6. SETTLE_OUTREACH: Disputes where proactive settlement offer is best move

This is the layer that enables Anchor's autopilot:
  - Every dispute in AUTO_FIGHT → submit evidence without human review
  - Every dispute in AUTO_ACCEPT → accept and log
  - REVIEW_QUEUE → human reviews one per queue entry
  - The human only touches 10-20% of disputes; autopilot handles the rest

Competitor gap:
  - Chargebacks911 manually reviews every dispute (expensive, slow, inconsistent)
  - Midigator has automation but no fight/accept scoring integration
  - Neither produces an explainable priority queue with time estimates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date

from oracle.claim_scorer import ClaimScore, DisputeSignals, FightVerdict, score_claim


class DispatchCategory(str, Enum):
    MUST_ACT_TODAY    = "MUST_ACT_TODAY"    # Overdue or critical deadline
    ACT_NOW_OPTIMAL   = "ACT_NOW_OPTIMAL"   # In algorithmic sweet spot
    AUTO_FIGHT        = "AUTO_FIGHT"        # High-confidence → autopilot submits
    AUTO_ACCEPT       = "AUTO_ACCEPT"       # Clear accept → no action needed
    SETTLE_OUTREACH   = "SETTLE_OUTREACH"   # Proactive settlement offer
    REVIEW_QUEUE      = "REVIEW_QUEUE"      # Borderline — needs human eyes
    MONITOR_ONLY      = "MONITOR_ONLY"      # No action yet; watch for updates


@dataclass
class DispatchedDispute:
    """One dispute with its dispatch decision and work estimate."""
    dispute_id: str
    claim_type: str
    dispute_amount: float
    score: ClaimScore
    category: DispatchCategory
    priority_rank: int              # 1 = highest priority
    action_description: str
    estimated_minutes: int          # Time estimate to handle this dispute
    can_autopilot: bool             # True if no human review needed
    deadline_today: bool            # True if must act today
    notes: list[str] = field(default_factory=list)


@dataclass
class DailyDispatchReport:
    """
    The daily action brief for a dispute portfolio.
    Produced once per day by the autopilot scheduler.
    """
    generated_date: date
    total_open_disputes: int

    # Prioritized work queues
    must_act_today: list[DispatchedDispute]      # Do these FIRST
    act_now_optimal: list[DispatchedDispute]     # Do these today for best timing
    auto_fight: list[DispatchedDispute]          # Autopilot handles these
    auto_accept: list[DispatchedDispute]         # No action needed
    settle_outreach: list[DispatchedDispute]     # Send settlement offer
    review_queue: list[DispatchedDispute]        # Human review needed
    monitor_only: list[DispatchedDispute]        # No action today

    # Metrics
    total_autopilot_disputes: int      # Can be handled without human review
    total_human_review_needed: int     # Requires a human
    total_at_risk_amount: float        # Sum of amounts needing action today
    total_potential_ev: float          # EV of fighting all auto-fight disputes

    # Time estimates
    estimated_human_minutes: int       # Total human time needed today
    estimated_autopilot_handles: int   # Disputes autopilot will handle

    summary: str
    priority_alerts: list[str]         # Critical things to know right now


@dataclass
class DisputeInput:
    """
    Input for one dispute to dispatch.
    Pass whatever you have — dispatcher handles missing signals gracefully.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float
    # Signals (all optional — will use defaults where missing)
    calibrated_win_rate: Optional[float] = None
    evidence_completeness: Optional[float] = None
    evidence_quality_tier: Optional[str] = None
    network_risk_level: Optional[str] = None
    network_risk_score: Optional[float] = None
    sp_covered: Optional[bool] = None
    sp_score: Optional[float] = None
    sp_has_blocking_issues: Optional[bool] = None
    carrier_trust_level: Optional[str] = None
    carrier_inr_win_rate: Optional[float] = None
    response_in_optimal_window: Optional[bool] = None
    days_remaining: Optional[int] = None
    legal_escalation_available: Optional[bool] = None
    legal_escalation_ev: Optional[float] = None


def dispatch_portfolio(
    disputes: list[DisputeInput],
    dispatch_date: Optional[date] = None,
    auto_fight_threshold: float = 72.0,  # fight_score >= this → auto-fight
    auto_accept_threshold: float = 32.0, # fight_score <= this → auto-accept
    settle_max_score: float = 50.0,      # Only offer settlement if score <= this
) -> DailyDispatchReport:
    """
    Score and dispatch an entire dispute portfolio.

    Returns a prioritized daily action queue split across autopilot
    and human-review tiers. The autopilot handles everything it can;
    humans only see the borderline cases.
    """
    if dispatch_date is None:
        dispatch_date = date.today()

    all_dispatched: list[DispatchedDispute] = []
    priority_alerts: list[str] = []

    for inp in disputes:
        signals = DisputeSignals(
            claim_type=inp.claim_type,
            dispute_amount=inp.dispute_amount,
            calibrated_win_rate=inp.calibrated_win_rate,
            evidence_completeness=inp.evidence_completeness,
            evidence_quality_tier=inp.evidence_quality_tier,
            network_risk_level=inp.network_risk_level,
            network_risk_score=inp.network_risk_score,
            sp_covered=inp.sp_covered,
            sp_score=inp.sp_score,
            sp_has_blocking_issues=inp.sp_has_blocking_issues,
            carrier_trust_level=inp.carrier_trust_level,
            carrier_inr_win_rate=inp.carrier_inr_win_rate,
            response_in_optimal_window=inp.response_in_optimal_window,
            days_remaining=inp.days_remaining,
            legal_escalation_available=inp.legal_escalation_available,
            legal_escalation_ev=inp.legal_escalation_ev,
        )
        score = score_claim(inp.dispute_id, signals)

        # Determine category
        deadline_today = False
        if inp.days_remaining is not None and inp.days_remaining <= 0:
            category = DispatchCategory.MUST_ACT_TODAY
            deadline_today = True
            priority_alerts.append(
                f"OVERDUE: {inp.dispute_id} (${inp.dispute_amount:.0f}) — response deadline passed"
            )
        elif inp.days_remaining is not None and inp.days_remaining <= 1:
            category = DispatchCategory.MUST_ACT_TODAY
            deadline_today = True
            priority_alerts.append(
                f"CRITICAL: {inp.dispute_id} (${inp.dispute_amount:.0f}) — < 24h remaining"
            )
        elif inp.response_in_optimal_window:
            category = DispatchCategory.ACT_NOW_OPTIMAL
            deadline_today = True  # Today is the optimal day — act today
        elif score.fight_score >= auto_fight_threshold:
            category = DispatchCategory.AUTO_FIGHT
        elif score.fight_score <= auto_accept_threshold:
            # Also check if settlement is better
            if inp.dispute_amount >= 50 and score.fight_score >= 20:
                category = DispatchCategory.SETTLE_OUTREACH
            else:
                category = DispatchCategory.AUTO_ACCEPT
        elif score.fight_verdict == FightVerdict.ESCALATE_FIRST:
            category = DispatchCategory.REVIEW_QUEUE
        elif score.fight_score <= settle_max_score:
            category = DispatchCategory.SETTLE_OUTREACH
        else:
            category = DispatchCategory.REVIEW_QUEUE

        # Time estimates
        if category == DispatchCategory.AUTO_FIGHT:
            minutes = 2    # Autopilot: just submit
            can_autopilot = True
        elif category == DispatchCategory.AUTO_ACCEPT:
            minutes = 1    # Log and move on
            can_autopilot = True
        elif category == DispatchCategory.MUST_ACT_TODAY:
            minutes = 20   # Urgent manual review + submit
            can_autopilot = False
        elif category == DispatchCategory.ACT_NOW_OPTIMAL:
            minutes = 15   # Submit now for optimal timing
            can_autopilot = score.fight_score >= 65
        elif category == DispatchCategory.SETTLE_OUTREACH:
            minutes = 10   # Draft and send settlement message
            can_autopilot = True
        else:
            minutes = 25   # Full human review
            can_autopilot = False

        action = _action_for_category(category, score)

        all_dispatched.append(DispatchedDispute(
            dispute_id=inp.dispute_id,
            claim_type=inp.claim_type,
            dispute_amount=inp.dispute_amount,
            score=score,
            category=category,
            priority_rank=0,   # Set after sorting
            action_description=action,
            estimated_minutes=minutes,
            can_autopilot=can_autopilot,
            deadline_today=deadline_today,
            notes=score.warnings,
        ))

    # Sort: MUST_ACT_TODAY first, then by score descending within category
    _category_order = {
        DispatchCategory.MUST_ACT_TODAY: 0,
        DispatchCategory.ACT_NOW_OPTIMAL: 1,
        DispatchCategory.REVIEW_QUEUE: 2,
        DispatchCategory.AUTO_FIGHT: 3,
        DispatchCategory.SETTLE_OUTREACH: 4,
        DispatchCategory.AUTO_ACCEPT: 5,
        DispatchCategory.MONITOR_ONLY: 6,
    }
    all_dispatched.sort(
        key=lambda d: (_category_order[d.category], -d.score.fight_score)
    )
    for i, d in enumerate(all_dispatched, start=1):
        d.priority_rank = i

    # Build category lists
    must_act = [d for d in all_dispatched if d.category == DispatchCategory.MUST_ACT_TODAY]
    optimal = [d for d in all_dispatched if d.category == DispatchCategory.ACT_NOW_OPTIMAL]
    auto_fight = [d for d in all_dispatched if d.category == DispatchCategory.AUTO_FIGHT]
    auto_accept = [d for d in all_dispatched if d.category == DispatchCategory.AUTO_ACCEPT]
    settle = [d for d in all_dispatched if d.category == DispatchCategory.SETTLE_OUTREACH]
    review = [d for d in all_dispatched if d.category == DispatchCategory.REVIEW_QUEUE]
    monitor = [d for d in all_dispatched if d.category == DispatchCategory.MONITOR_ONLY]

    autopilot_count = sum(1 for d in all_dispatched if d.can_autopilot)
    human_count = sum(1 for d in all_dispatched if not d.can_autopilot)
    at_risk_amount = sum(d.dispute_amount for d in must_act + optimal)
    potential_ev = sum(d.score.estimated_ev for d in auto_fight if d.score.estimated_ev > 0)
    human_minutes = sum(d.estimated_minutes for d in all_dispatched if not d.can_autopilot)
    autopilot_handles = autopilot_count

    summary = (
        f"{len(disputes)} disputes | "
        f"{len(must_act)+len(optimal)} act today | "
        f"{len(auto_fight)} auto-fight | "
        f"{len(auto_accept)} accept | "
        f"{len(settle)} settle | "
        f"{len(review)} need review | "
        f"${at_risk_amount:.0f} at risk | "
        f"human time: {human_minutes}min"
    )

    return DailyDispatchReport(
        generated_date=dispatch_date,
        total_open_disputes=len(disputes),
        must_act_today=must_act,
        act_now_optimal=optimal,
        auto_fight=auto_fight,
        auto_accept=auto_accept,
        settle_outreach=settle,
        review_queue=review,
        monitor_only=monitor,
        total_autopilot_disputes=autopilot_count,
        total_human_review_needed=human_count,
        total_at_risk_amount=round(at_risk_amount, 2),
        total_potential_ev=round(potential_ev, 2),
        estimated_human_minutes=human_minutes,
        estimated_autopilot_handles=autopilot_handles,
        summary=summary,
        priority_alerts=priority_alerts,
    )


def _action_for_category(category: DispatchCategory, score: ClaimScore) -> str:
    if category == DispatchCategory.MUST_ACT_TODAY:
        return f"URGENT: {score.primary_action}"
    elif category == DispatchCategory.ACT_NOW_OPTIMAL:
        return f"TODAY IS OPTIMAL: {score.primary_action}"
    elif category == DispatchCategory.AUTO_FIGHT:
        return f"Autopilot: submit evidence (score={score.fight_score:.0f}, EV=${score.estimated_ev:.0f})"
    elif category == DispatchCategory.AUTO_ACCEPT:
        return f"Accept: EV=${score.estimated_ev:.0f} does not justify fighting"
    elif category == DispatchCategory.SETTLE_OUTREACH:
        return "Send proactive settlement offer to avoid dispute escalation"
    else:
        return f"Human review required: score={score.fight_score:.0f} (borderline)"
