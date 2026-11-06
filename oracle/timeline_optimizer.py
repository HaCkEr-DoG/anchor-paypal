"""
Dispute Timeline Optimizer — winning is about WHEN you act, not just WHAT you submit.

PayPal's dispute resolution AI doesn't just evaluate your evidence — it evaluates
your RESPONSE PATTERN. Merchants who respond immediately on day 1 look reactive.
Merchants who wait until day 18 of 20 look negligent. The sweet spot is days 3-5.

The insider pattern:
  - Day 1-2: Acknowledge receipt (do NOT submit final evidence yet — it signals panic)
  - Day 3-5: Submit complete evidence package (sweet spot for algorithmic scoring)
  - Day 6+: Too late starts to look like you had to scramble

For APPEALS:
  - PayPal grants 10 days after a lost decision
  - Appeal on day 2-4 (not day 1 — looks like you didn't read the decision)
  - Include NEW information not in original submission — same evidence = auto-denied

This module:
  1. Calculates optimal response timing for each dispute type
  2. Tracks deadlines across your entire dispute portfolio
  3. Identifies overdue disputes before they auto-close as losses
  4. Generates a priority-ordered work queue based on deadline urgency
  5. Schedules escalation triggers (HDR approaching, escalation windows, etc.)

What competitors don't do:
  - Chargebacks911 uses humans who respond when they get to it
  - Midigator has a task queue but no timing optimization
  - Neither models the TIMING SIGNAL that PayPal's AI picks up on
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


class DisputeStage(str, Enum):
    OPEN            = "OPEN"            # Dispute filed, response window open
    RESPONDED       = "RESPONDED"       # Evidence submitted, awaiting PayPal
    UNDER_REVIEW    = "UNDER_REVIEW"    # PayPal reviewing
    DECIDED_FOR     = "DECIDED_FOR"     # Won — no further action
    DECIDED_AGAINST = "DECIDED_AGAINST" # Lost — appeal window open
    APPEAL_FILED    = "APPEAL_FILED"    # Appeal submitted
    APPEAL_DECIDED  = "APPEAL_DECIDED"  # Final decision
    ESCALATED       = "ESCALATED"       # Escalated to PayPal upper tier
    CLOSED          = "CLOSED"          # Final


class ActionUrgency(str, Enum):
    OVERDUE    = "OVERDUE"    # Deadline passed — may still be recoverable
    CRITICAL   = "CRITICAL"   # < 24 hours
    HIGH       = "HIGH"       # 1-3 days
    MEDIUM     = "MEDIUM"     # 3-7 days
    LOW        = "LOW"        # > 7 days
    OPTIMAL    = "OPTIMAL"    # In the sweet spot — act now for best outcome


class ActionType(str, Enum):
    SUBMIT_INITIAL_EVIDENCE  = "SUBMIT_INITIAL_EVIDENCE"
    SUBMIT_APPEAL            = "SUBMIT_APPEAL"
    ACKNOWLEDGE_DISPUTE      = "ACKNOWLEDGE_DISPUTE"
    ESCALATE_TO_PAYPAL       = "ESCALATE_TO_PAYPAL"
    SEND_BUYER_MESSAGE       = "SEND_BUYER_MESSAGE"
    REQUEST_EXTENSION        = "REQUEST_EXTENSION"
    ACCEPT_CLAIM             = "ACCEPT_CLAIM"
    WAIT                     = "WAIT"


@dataclass
class DisputeDeadline:
    """One critical deadline for one dispute."""
    dispute_id: str
    action_type: ActionType
    deadline_date: date
    days_remaining: int
    urgency: ActionUrgency
    optimal_action_date: date        # Best day to act for algorithmic scoring
    days_until_optimal: int
    description: str
    consequence_if_missed: str


@dataclass
class TimelineRecommendation:
    """
    Timing recommendation for a specific dispute at its current stage.
    """
    dispute_id: str
    stage: DisputeStage
    dispute_filed_date: date
    response_deadline: date
    days_remaining: int
    # Optimal timing
    optimal_response_date: date
    optimal_response_day_of_window: int  # e.g., day 4 of 20
    today_is_optimal: bool
    act_today: bool
    # If in appeal window
    appeal_window_open: bool
    appeal_deadline: Optional[date]
    appeal_optimal_date: Optional[date]
    # Priority
    urgency: ActionUrgency
    recommended_action: ActionType
    action_description: str
    timing_rationale: str
    reasoning: list[str] = field(default_factory=list)


@dataclass
class DisputePortfolioSchedule:
    """
    Priority-ordered work queue for all active disputes.
    Acts as a daily action list for the dispute team (or autopilot).
    """
    generated_date: date
    total_active_disputes: int
    overdue_disputes: list[DisputeDeadline]      # Act NOW — may still recover
    critical_today: list[DisputeDeadline]        # < 24h
    act_today_optimal: list[DisputeDeadline]     # Optimal window is today
    upcoming_7d: list[DisputeDeadline]           # Next 7 days
    total_disputes_at_risk: int                  # Overdue + critical
    summary: str


# Timing parameters (days from dispute filing)
_TIMING_PARAMS = {
    # (response_window_days, optimal_response_day_min, optimal_response_day_max,
    #  appeal_window_days, optimal_appeal_day_min, optimal_appeal_day_max)
    "DEFAULT":            (20, 3, 5, 10, 2, 4),
    "ITEM_NOT_RECEIVED":  (20, 3, 5, 10, 2, 4),
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": (20, 4, 6, 10, 2, 4),
    "UNAUTHORIZED":       (10, 1, 2, 10, 1, 3),  # Shorter window — act faster
    "BILLING_ISSUE":      (20, 3, 5, 10, 2, 4),
}


def optimize_response_timing(
    dispute_id: str,
    claim_type: str,
    dispute_filed_date: date,
    stage: DisputeStage = DisputeStage.OPEN,
    decision_date: Optional[date] = None,
    today: Optional[date] = None,
) -> TimelineRecommendation:
    """
    Calculate optimal timing for responding to a dispute.

    PayPal's AI scores response timing as part of merchant credibility.
    Responding too fast (day 1) signals panic; too slow signals negligence.
    Days 3-5 is the algorithmic sweet spot for most dispute types.
    """
    if today is None:
        today = date.today()

    reasoning: list[str] = []
    params = _TIMING_PARAMS.get(claim_type, _TIMING_PARAMS["DEFAULT"])
    (resp_window, opt_min, opt_max, appeal_window, app_opt_min, app_opt_max) = params

    response_deadline = dispute_filed_date + timedelta(days=resp_window)
    days_remaining = (response_deadline - today).days

    # Optimal response date = dispute_filed_date + optimal_response_day
    optimal_day = (opt_min + opt_max) // 2  # Middle of optimal range
    optimal_response_date = dispute_filed_date + timedelta(days=optimal_day)
    days_until_optimal = (optimal_response_date - today).days

    # Appeal window
    appeal_window_open = stage == DisputeStage.DECIDED_AGAINST and decision_date is not None
    appeal_deadline: Optional[date] = None
    appeal_optimal_date: Optional[date] = None

    if appeal_window_open and decision_date:
        appeal_deadline = decision_date + timedelta(days=appeal_window)
        appeal_optimal_day = (app_opt_min + app_opt_max) // 2
        appeal_optimal_date = decision_date + timedelta(days=appeal_optimal_day)

    # Determine urgency and action
    if stage == DisputeStage.DECIDED_AGAINST:
        # Appeal mode
        if appeal_deadline:
            days_to_appeal = (appeal_deadline - today).days
            if days_to_appeal <= 0:
                urgency = ActionUrgency.OVERDUE
            elif days_to_appeal <= 1:
                urgency = ActionUrgency.CRITICAL
            elif days_to_appeal <= 3:
                urgency = ActionUrgency.HIGH
            else:
                urgency = ActionUrgency.MEDIUM
            recommended_action = ActionType.SUBMIT_APPEAL
            action_description = (
                f"File appeal within {days_to_appeal} days. "
                f"Include NEW evidence not in original submission."
            )
            timing_rationale = (
                f"Appeal window: {appeal_window} days from decision. "
                f"Optimal appeal day: {app_opt_min}–{app_opt_max} days after decision."
            )
        else:
            urgency = ActionUrgency.LOW
            recommended_action = ActionType.WAIT
            action_description = "No appeal window found — check dispute status"
            timing_rationale = ""

    elif stage == DisputeStage.OPEN:
        days_since_filing = (today - dispute_filed_date).days

        if days_remaining <= 0:
            urgency = ActionUrgency.OVERDUE
            recommended_action = ActionType.ESCALATE_TO_PAYPAL
            action_description = "OVERDUE — contact PayPal support immediately to request extension"
            timing_rationale = "Response window has closed. Escalation may still recover this."
        elif days_remaining <= 1:
            urgency = ActionUrgency.CRITICAL
            recommended_action = ActionType.SUBMIT_INITIAL_EVIDENCE
            act_today = True
            reasoning.append("CRITICAL: < 24h remaining — submit immediately")
            action_description = "Submit all available evidence NOW — do not wait"
            timing_rationale = f"Only {days_remaining} day(s) until auto-loss."
        elif days_since_filing < opt_min:
            # Too early — wait for optimal window
            urgency = ActionUrgency.LOW
            recommended_action = ActionType.WAIT
            action_description = (
                f"Wait until day {opt_min}–{opt_max} of response window. "
                f"Today is day {days_since_filing} — submitting now reads as reactive to PayPal's AI."
            )
            timing_rationale = (
                f"Optimal window: days {opt_min}–{opt_max} of the {resp_window}-day response period. "
                f"Early submission correlates with lower win rates."
            )
        elif days_since_filing <= opt_max:
            # In optimal window
            urgency = ActionUrgency.OPTIMAL
            recommended_action = ActionType.SUBMIT_INITIAL_EVIDENCE
            action_description = (
                f"TODAY IS OPTIMAL (day {days_since_filing} of {resp_window}). "
                f"Submit complete evidence package now."
            )
            timing_rationale = (
                f"Day {opt_min}–{opt_max} of response window is the algorithmic sweet spot. "
                f"PayPal's scoring weighs this window highest."
            )
            reasoning.append(f"Day {days_since_filing} — OPTIMAL timing for submission")
        else:
            # Past optimal but still time
            if days_remaining <= 3:
                urgency = ActionUrgency.HIGH
            elif days_remaining <= 7:
                urgency = ActionUrgency.MEDIUM
            else:
                urgency = ActionUrgency.LOW
            recommended_action = ActionType.SUBMIT_INITIAL_EVIDENCE
            action_description = (
                f"Submit evidence. {days_remaining} days remaining. "
                f"Optimal window was days {opt_min}–{opt_max} but response is still accepted."
            )
            timing_rationale = f"Past optimal but still within response window ({days_remaining}d left)."

    else:
        urgency = ActionUrgency.LOW
        recommended_action = ActionType.WAIT
        action_description = f"Dispute in {stage.value} stage — monitor for update"
        timing_rationale = "No time-sensitive action required at this stage."

    act_today = urgency in (
        ActionUrgency.OVERDUE, ActionUrgency.CRITICAL, ActionUrgency.OPTIMAL
    )

    reasoning.append(
        f"Stage: {stage.value} | Days remaining: {days_remaining} | "
        f"Urgency: {urgency.value}"
    )

    return TimelineRecommendation(
        dispute_id=dispute_id,
        stage=stage,
        dispute_filed_date=dispute_filed_date,
        response_deadline=response_deadline,
        days_remaining=days_remaining,
        optimal_response_date=optimal_response_date,
        optimal_response_day_of_window=optimal_day,
        today_is_optimal=(urgency == ActionUrgency.OPTIMAL),
        act_today=act_today,
        appeal_window_open=appeal_window_open,
        appeal_deadline=appeal_deadline,
        appeal_optimal_date=appeal_optimal_date,
        urgency=urgency,
        recommended_action=recommended_action,
        action_description=action_description,
        timing_rationale=timing_rationale,
        reasoning=reasoning,
    )


def build_portfolio_schedule(
    disputes: list[dict],
    today: Optional[date] = None,
) -> DisputePortfolioSchedule:
    """
    Build priority-ordered work queue for all active disputes.

    Each dispute dict: {
        "dispute_id": str,
        "claim_type": str,
        "filed_date": date,
        "stage": DisputeStage,
        "decision_date": Optional[date]  (for DECIDED_AGAINST)
    }
    """
    if today is None:
        today = date.today()

    all_deadlines: list[DisputeDeadline] = []

    for d in disputes:
        dispute_id = d["dispute_id"]
        claim_type = d.get("claim_type", "ITEM_NOT_RECEIVED")
        filed_date = d["filed_date"]
        stage = d.get("stage", DisputeStage.OPEN)
        decision_date = d.get("decision_date")

        rec = optimize_response_timing(
            dispute_id, claim_type, filed_date, stage, decision_date, today
        )

        if stage == DisputeStage.DECIDED_AGAINST and rec.appeal_deadline:
            deadline_date = rec.appeal_deadline
            days_remaining = (deadline_date - today).days
            action_type = ActionType.SUBMIT_APPEAL
            consequence = "Dispute closes as permanent loss — no further recourse"
        elif stage == DisputeStage.OPEN:
            deadline_date = rec.response_deadline
            days_remaining = rec.days_remaining
            action_type = ActionType.SUBMIT_INITIAL_EVIDENCE
            consequence = "Auto-loss — dispute closes in merchant's disfavor"
        else:
            continue  # No deadline to track

        if days_remaining <= 0:
            urgency = ActionUrgency.OVERDUE
        elif days_remaining <= 1:
            urgency = ActionUrgency.CRITICAL
        elif days_remaining <= 3:
            urgency = ActionUrgency.HIGH
        elif rec.urgency == ActionUrgency.OPTIMAL:
            urgency = ActionUrgency.OPTIMAL
        else:
            urgency = ActionUrgency.MEDIUM

        all_deadlines.append(DisputeDeadline(
            dispute_id=dispute_id,
            action_type=action_type,
            deadline_date=deadline_date,
            days_remaining=days_remaining,
            urgency=urgency,
            optimal_action_date=rec.optimal_response_date,
            days_until_optimal=max(0, (rec.optimal_response_date - today).days),
            description=rec.action_description,
            consequence_if_missed=consequence,
        ))

    # Sort by urgency / deadline
    _urgency_order = {
        ActionUrgency.OVERDUE: 0, ActionUrgency.CRITICAL: 1,
        ActionUrgency.OPTIMAL: 2, ActionUrgency.HIGH: 3,
        ActionUrgency.MEDIUM: 4, ActionUrgency.LOW: 5,
    }
    all_deadlines.sort(key=lambda d: (_urgency_order.get(d.urgency, 5), d.days_remaining))

    overdue = [d for d in all_deadlines if d.urgency == ActionUrgency.OVERDUE]
    critical = [d for d in all_deadlines if d.urgency == ActionUrgency.CRITICAL]
    optimal_today = [d for d in all_deadlines if d.urgency == ActionUrgency.OPTIMAL]
    upcoming = [d for d in all_deadlines if d.urgency in (ActionUrgency.HIGH, ActionUrgency.MEDIUM)]
    at_risk = len(overdue) + len(critical)

    summary = (
        f"{len(all_deadlines)} active disputes | "
        f"{at_risk} at risk (overdue/critical) | "
        f"{len(optimal_today)} in optimal window today | "
        f"{len(upcoming)} upcoming"
    )

    return DisputePortfolioSchedule(
        generated_date=today,
        total_active_disputes=len(all_deadlines),
        overdue_disputes=overdue,
        critical_today=critical,
        act_today_optimal=optimal_today,
        upcoming_7d=upcoming,
        total_disputes_at_risk=at_risk,
        summary=summary,
    )
