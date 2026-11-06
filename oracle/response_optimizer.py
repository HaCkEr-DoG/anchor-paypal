"""
Response Optimizer — optimizes WHEN to submit dispute responses for best outcomes.

PayPal reviews are processed by human reviewers (and increasingly AI).
Like any decision-making process, they are subject to timing biases:
  - Reviewer attention varies by time of day
  - Queue depth varies by day of week
  - Holiday periods have skeleton crews
  - Month-end submission spikes create backlogs

What's known about PayPal review timing:
  - Disputes submitted Tuesday-Thursday get highest reviewer attention
  - Avoid Friday afternoon submissions (reviewed Monday, often in a rush)
  - Avoid holiday weekends (skeleton crew, rubber-stamp denials)
  - Within a day: 9am-11am PT is peak quality review window
  - Disputes with response times 3-5 days (not day 1, not day 20) signal
    careful preparation and get reviewed more favorably
  - Month-end (day 25-31): reviewers clearing backlogs quickly → lower quality

This module also optimizes submission timing when you have multiple disputes:
  - Don't submit all 30 disputes at once — creates a reviewer fatigue batch
  - Spread across 3-5 days for maximum individual attention
  - Prioritize highest-value disputes in the best time slots

Output:
  - Optimal submission date/time for each dispute
  - Expected attention quality score (0–100)
  - Warnings about sub-optimal timing
  - Portfolio spread recommendation for batch submissions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date, time, datetime, timedelta


class DayQuality(str, Enum):
    OPTIMAL       = "OPTIMAL"       # Best days: Tue-Thu
    GOOD          = "GOOD"          # Acceptable: Mon
    SUBOPTIMAL    = "SUBOPTIMAL"    # Friday
    AVOID         = "AVOID"         # Weekend
    HOLIDAY_RISK  = "HOLIDAY_RISK"  # Holiday period


class TimeWindowQuality(str, Enum):
    PEAK       = "PEAK"        # 9am-11am PT
    GOOD       = "GOOD"        # 11am-2pm PT
    ACCEPTABLE = "ACCEPTABLE"  # 8am-9am or 2pm-4pm PT
    LOW        = "LOW"         # 4pm-6pm PT
    AVOID      = "AVOID"       # Evening/night PT


@dataclass
class DisputeSubmissionInput:
    """One dispute pending response."""
    dispute_id: str
    deadline: date
    dispute_amount: float
    fight_score: float          # From claim_scorer
    claim_type: str
    response_ready: bool = True  # Is evidence already assembled?


@dataclass
class SubmissionTiming:
    """Optimal submission timing for one dispute."""
    dispute_id: str
    submit_date: date
    submit_time_pt: str         # e.g. "09:30 PT"
    day_quality: DayQuality
    time_quality: TimeWindowQuality
    attention_score: float      # 0–100 expected reviewer attention
    days_before_deadline: int
    reasoning: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PortfolioSubmissionPlan:
    """
    Submission schedule for a portfolio of disputes.
    Spreads disputes across optimal days/times to maximize per-dispute attention.
    """
    total_disputes: int
    plan_start_date: date
    plan_end_date: date
    per_dispute: list[SubmissionTiming]

    # Schedule summary
    submissions_by_date: dict[str, list[str]]   # date_str → [dispute_ids]
    avg_attention_score: float
    high_priority_first: bool  # True if highest-score disputes got best slots

    warnings: list[str] = field(default_factory=list)
    summary: str = ""


# Day-of-week quality (Monday=0, Sunday=6)
_DOW_QUALITY = {
    0: DayQuality.GOOD,          # Monday
    1: DayQuality.OPTIMAL,       # Tuesday
    2: DayQuality.OPTIMAL,       # Wednesday
    3: DayQuality.OPTIMAL,       # Thursday
    4: DayQuality.SUBOPTIMAL,    # Friday
    5: DayQuality.AVOID,         # Saturday
    6: DayQuality.AVOID,         # Sunday
}

# Attention score multipliers by day quality
_DOW_ATTENTION = {
    DayQuality.OPTIMAL: 1.00,
    DayQuality.GOOD: 0.90,
    DayQuality.SUBOPTIMAL: 0.75,
    DayQuality.AVOID: 0.50,
    DayQuality.HOLIDAY_RISK: 0.60,
}

# US Federal holidays (month, day) — approximate
_HOLIDAYS = {
    (1, 1), (7, 4), (11, 25), (11, 26), (11, 27), (11, 28),
    (12, 24), (12, 25), (12, 26), (12, 31),
}


def _is_holiday_period(d: date) -> bool:
    """True if date falls in a known low-staff holiday period."""
    md = (d.month, d.day)
    if md in _HOLIDAYS:
        return True
    # Thanksgiving week (US): Nov 24-28 approximately
    if d.month == 11 and 22 <= d.day <= 29:
        return True
    # Christmas week
    if d.month == 12 and 23 <= d.day <= 31:
        return True
    if d.month == 1 and d.day <= 2:
        return True
    return False


def _day_quality(d: date) -> DayQuality:
    if _is_holiday_period(d):
        return DayQuality.HOLIDAY_RISK
    return _DOW_QUALITY[d.weekday()]


def _attention_score(day_q: DayQuality, time_q: TimeWindowQuality, day_of_month: int) -> float:
    """Base attention score 0-100."""
    base = 70.0

    # Day quality
    base *= _DOW_ATTENTION[day_q]

    # Time window quality
    time_mult = {
        TimeWindowQuality.PEAK: 1.00,
        TimeWindowQuality.GOOD: 0.92,
        TimeWindowQuality.ACCEPTABLE: 0.85,
        TimeWindowQuality.LOW: 0.75,
        TimeWindowQuality.AVOID: 0.60,
    }[time_q]
    base *= time_mult

    # Month-end backlog penalty
    if day_of_month >= 25:
        base *= 0.90

    return round(min(100.0, base), 1)


def _best_time_in_window() -> tuple[str, TimeWindowQuality]:
    """Return the optimal submission time within a day."""
    return "09:30 PT", TimeWindowQuality.PEAK


def optimize_submission_timing(
    dispute: DisputeSubmissionInput,
    today: date,
    buffer_days: int = 2,
) -> SubmissionTiming:
    """
    Find optimal submission date for a single dispute.

    Searches forward from today within the deadline window,
    preferring Tuesday-Thursday 9-11am PT and avoiding holidays/weekends.
    """
    reasoning: list[str] = []
    warnings: list[str] = []

    days_remaining = (dispute.deadline - today).days
    if days_remaining <= 0:
        # Already overdue — submit today
        submit_date = today
        reasoning.append("OVERDUE: submit immediately")
    elif days_remaining <= 1:
        submit_date = today
        reasoning.append("URGENT: deadline tomorrow, submitting today")
    else:
        # Find the best day in [today+1, deadline - buffer_days]
        latest_safe = dispute.deadline - timedelta(days=buffer_days)
        candidates: list[tuple[float, date]] = []

        d = today + timedelta(days=1)
        while d <= latest_safe:
            dq = _day_quality(d)
            tq = TimeWindowQuality.PEAK
            score = _attention_score(dq, tq, d.day)
            candidates.append((score, d))
            d += timedelta(days=1)

        if not candidates:
            # No candidates — use today
            submit_date = today
            warnings.append("No optimal window found before deadline — submitting today")
        else:
            # Best score, prefer earliest among ties
            candidates.sort(key=lambda c: (-c[0], c[1]))
            best_score, submit_date = candidates[0]
            dq = _day_quality(submit_date)
            reasoning.append(f"Best window: {submit_date} ({dq.value}, attention={best_score:.0f})")

    submit_time_str, time_q = _best_time_in_window()
    day_q = _day_quality(submit_date)
    days_before = (dispute.deadline - submit_date).days

    if day_q in (DayQuality.SUBOPTIMAL, DayQuality.AVOID):
        warnings.append(f"Deadline forces {day_q.value} submission — consider earlier prep next time")
    if day_q == DayQuality.HOLIDAY_RISK:
        warnings.append("Holiday period submission — expect reduced reviewer attention")
    if days_before < 1:
        warnings.append("Submitting on or after deadline — late submission")

    att_score = _attention_score(day_q, time_q, submit_date.day)

    return SubmissionTiming(
        dispute_id=dispute.dispute_id,
        submit_date=submit_date,
        submit_time_pt=submit_time_str,
        day_quality=day_q,
        time_quality=time_q,
        attention_score=att_score,
        days_before_deadline=days_before,
        reasoning=reasoning,
        warnings=warnings,
    )


def build_portfolio_submission_plan(
    disputes: list[DisputeSubmissionInput],
    today: date,
    max_per_day: int = 8,
) -> PortfolioSubmissionPlan:
    """
    Build a spread submission schedule for multiple disputes.

    High-value / high-score disputes get the best time slots.
    Submissions capped at max_per_day to avoid batch reviewer fatigue.
    """
    warnings: list[str] = []

    if not disputes:
        return PortfolioSubmissionPlan(
            total_disputes=0,
            plan_start_date=today,
            plan_end_date=today,
            per_dispute=[],
            submissions_by_date={},
            avg_attention_score=0.0,
            high_priority_first=True,
            summary="No disputes to schedule.",
        )

    # Sort by priority: soonest deadline first, then highest fight_score
    sorted_disputes = sorted(
        disputes,
        key=lambda d: (d.deadline, -d.fight_score)
    )

    # Build per-dispute timing, respecting max_per_day cap
    per_dispute: list[SubmissionTiming] = []
    date_counts: dict[date, int] = {}

    for d in sorted_disputes:
        timing = optimize_submission_timing(d, today)
        # If the optimal day is at capacity, push forward by 1 day
        while date_counts.get(timing.submit_date, 0) >= max_per_day:
            next_day = timing.submit_date + timedelta(days=1)
            if next_day > d.deadline:
                break  # Can't push further — accept the crowded day
            # Build a fake timing for next_day
            dq = _day_quality(next_day)
            tq = TimeWindowQuality.PEAK
            att = _attention_score(dq, tq, next_day.day)
            timing = SubmissionTiming(
                dispute_id=d.dispute_id,
                submit_date=next_day,
                submit_time_pt="09:30 PT",
                day_quality=dq,
                time_quality=tq,
                attention_score=att,
                days_before_deadline=(d.deadline - next_day).days,
                reasoning=[f"Rescheduled from crowded day to {next_day}"],
            )
        date_counts[timing.submit_date] = date_counts.get(timing.submit_date, 0) + 1
        per_dispute.append(timing)

    if not per_dispute:
        avg_attention = 0.0
    else:
        avg_attention = sum(t.attention_score for t in per_dispute) / len(per_dispute)

    # Build date → dispute map
    by_date: dict[str, list[str]] = {}
    for t in per_dispute:
        key = str(t.submit_date)
        by_date.setdefault(key, []).append(t.dispute_id)

    plan_start = min(t.submit_date for t in per_dispute)
    plan_end = max(t.submit_date for t in per_dispute)

    # Did high-priority disputes get better slots?
    if len(per_dispute) >= 2:
        high_pri_scores = [t.attention_score for t in per_dispute[:len(per_dispute) // 2]]
        low_pri_scores = [t.attention_score for t in per_dispute[len(per_dispute) // 2:]]
        high_priority_first = (
            sum(high_pri_scores) / len(high_pri_scores)
            >= sum(low_pri_scores) / len(low_pri_scores)
        )
    else:
        high_priority_first = True

    if len(disputes) > max_per_day * 5:
        warnings.append(
            f"Large portfolio ({len(disputes)} disputes) spanning {(plan_end - plan_start).days + 1} days — "
            f"consider pre-assembling evidence to compress the submission window."
        )

    summary = (
        f"{len(disputes)} disputes spread {plan_start} to {plan_end} "
        f"({(plan_end - plan_start).days + 1} days) | "
        f"avg attention: {avg_attention:.0f}/100 | "
        f"max {max_per_day}/day"
    )

    return PortfolioSubmissionPlan(
        total_disputes=len(disputes),
        plan_start_date=plan_start,
        plan_end_date=plan_end,
        per_dispute=per_dispute,
        submissions_by_date=by_date,
        avg_attention_score=round(avg_attention, 1),
        high_priority_first=high_priority_first,
        warnings=warnings,
        summary=summary,
    )
