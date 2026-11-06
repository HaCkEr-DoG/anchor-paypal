"""
Appeal Window Tracker — after a dispute ruling, track the remaining
time to file an appeal and determine whether an appeal is worthwhile.

Appeal rules:
  PayPal:      10 days after ruling
  Visa:        45 days after ruling
  Mastercard:  45 days after ruling
  Amex:        45 days after ruling
  Discover:    45 days after ruling

An appeal is worth filing when:
  1. The window has not expired
  2. New evidence is available that was not in the original submission
  3. The EV of winning the appeal exceeds the appeal cost

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AppealOutcome(str, Enum):
    WON_ORIGINAL    = "WON_ORIGINAL"    # Merchant won the first ruling
    LOST_ORIGINAL   = "LOST_ORIGINAL"   # Merchant lost; appeal possible
    PARTIAL_LOSS    = "PARTIAL_LOSS"    # Merchant got partial ruling
    UNKNOWN         = "UNKNOWN"         # Ruling not yet issued


class AppealRecommendation(str, Enum):
    APPEAL_NOW      = "APPEAL_NOW"      # Window open, EV positive, do it immediately
    APPEAL_OPTIONAL = "APPEAL_OPTIONAL" # Marginal EV; merchant's call
    DO_NOT_APPEAL   = "DO_NOT_APPEAL"   # Window closed or EV negative
    WINDOW_EXPIRED  = "WINDOW_EXPIRED"  # Too late


_APPEAL_WINDOW_DAYS: dict[str, int] = {
    "PAYPAL":      10,
    "VISA":        45,
    "MASTERCARD":  45,
    "AMEX":        45,
    "DISCOVER":    45,
}

_APPEAL_WIN_BASE: dict[str, float] = {
    # Historical appeal success rates (merchant perspective)
    "PAYPAL":      0.25,
    "VISA":        0.35,
    "MASTERCARD":  0.30,
    "AMEX":        0.28,
    "DISCOVER":    0.30,
}


@dataclass
class AppealWindowInput:
    """Input for appeal window tracking."""
    dispute_id: str
    network: str                 # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    disputed_amount: float
    ruling_outcome: AppealOutcome

    # Timing
    days_since_ruling: int       # How many days since the ruling was issued
    # (negative means ruling not yet issued)

    # New evidence for appeal
    has_new_evidence: bool = False
    new_evidence_description: str = ""
    original_evidence_quality: float = 0.5  # 0–1, quality of original submission

    # Costs
    appeal_cost: float = 25.0
    chargeback_fee: float = 20.0

    # Context
    claim_type: str = "INR"
    is_in_hdr: bool = False


def _days_remaining(network: str, days_since_ruling: int) -> int:
    window = _APPEAL_WINDOW_DAYS.get(network.upper(), 45)
    return max(0, window - days_since_ruling)


def _appeal_win_probability(inp: AppealWindowInput) -> float:
    base = _APPEAL_WIN_BASE.get(inp.network.upper(), 0.30)

    # New evidence significantly boosts success
    if inp.has_new_evidence:
        base += 0.20
    # Poor original evidence means more room to improve
    if inp.original_evidence_quality < 0.4:
        base += 0.10
    # HDR pressure means PayPal is more sympathetic
    if inp.is_in_hdr:
        base += 0.05

    return min(0.85, round(base, 2))


def _ev_appeal(p_win: float, amount: float, appeal_cost: float) -> float:
    return round(p_win * amount - appeal_cost, 2)


def _recommendation(
        days_remaining: int,
        ev: float,
        outcome: AppealOutcome,
        has_new_evidence: bool) -> AppealRecommendation:

    if outcome == AppealOutcome.WON_ORIGINAL:
        return AppealRecommendation.DO_NOT_APPEAL

    if days_remaining <= 0:
        return AppealRecommendation.WINDOW_EXPIRED

    if ev > 20 and has_new_evidence:
        return AppealRecommendation.APPEAL_NOW
    if ev > 0:
        return AppealRecommendation.APPEAL_OPTIONAL
    return AppealRecommendation.DO_NOT_APPEAL


@dataclass
class AppealWindowReport:
    """Full appeal window analysis."""
    dispute_id: str
    network: str
    ruling_outcome: AppealOutcome
    recommendation: AppealRecommendation

    # Window
    appeal_window_days: int      # Total window
    days_since_ruling: int
    days_remaining: int
    window_expired: bool
    deadline_urgency: str        # CRITICAL / HIGH / MEDIUM / LOW

    # EV
    appeal_win_probability: float
    ev_of_appealing: float
    is_ev_positive: bool

    # Guidance
    appeal_rationale: str
    required_steps: list[str]
    summary: str


def track_appeal_window(inp: AppealWindowInput) -> AppealWindowReport:
    """Analyze whether and when to file an appeal."""
    days_rem = _days_remaining(inp.network, inp.days_since_ruling)
    window = _APPEAL_WINDOW_DAYS.get(inp.network.upper(), 45)
    expired = days_rem <= 0 or inp.ruling_outcome == AppealOutcome.WON_ORIGINAL

    p_win = _appeal_win_probability(inp)
    ev = _ev_appeal(p_win, inp.disputed_amount, inp.appeal_cost)
    rec = _recommendation(days_rem, ev, inp.ruling_outcome, inp.has_new_evidence)

    # Urgency
    if days_rem <= 1:
        urgency = "CRITICAL"
    elif days_rem <= 3:
        urgency = "HIGH"
    elif days_rem <= 7:
        urgency = "MEDIUM"
    else:
        urgency = "LOW"

    # Rationale
    if rec == AppealRecommendation.APPEAL_NOW:
        rationale = (
            f"Appeal recommended: {days_rem}d remaining, "
            f"EV=${ev:.2f}, new evidence available. "
            f"Estimated win rate {p_win:.0%}."
        )
    elif rec == AppealRecommendation.APPEAL_OPTIONAL:
        rationale = (
            f"Appeal is marginal: EV=${ev:.2f} at {p_win:.0%} win rate. "
            f"{days_rem}d remaining. Consider if dispute amount justifies appeal_cost."
        )
    elif rec == AppealRecommendation.WINDOW_EXPIRED:
        rationale = f"Appeal window expired ({window}d limit). No action possible."
    elif rec == AppealRecommendation.DO_NOT_APPEAL:
        if inp.ruling_outcome == AppealOutcome.WON_ORIGINAL:
            rationale = "Merchant won — no appeal needed."
        else:
            rationale = f"EV of appeal is negative (${ev:.2f}). Accept the ruling."
    else:
        rationale = "No appeal recommended."

    # Required steps
    steps: list[str] = []
    if rec in (AppealRecommendation.APPEAL_NOW, AppealRecommendation.APPEAL_OPTIONAL):
        steps.append(f"File appeal via {inp.network} dispute portal before {days_rem}d deadline")
        if inp.has_new_evidence:
            steps.append(f"Submit new evidence: {inp.new_evidence_description or 'specify evidence'}")
        steps.append("Include original dispute ID and ruling reference number")
        steps.append(f"Pay appeal filing fee (est. ${inp.appeal_cost:.0f})")

    summary = (
        f"Appeal {inp.dispute_id}: "
        f"outcome={inp.ruling_outcome.value} | rec={rec.value} | "
        f"days_rem={days_rem} | p_win={p_win:.0%} | ev=${ev:.2f}"
    )

    return AppealWindowReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        ruling_outcome=inp.ruling_outcome,
        recommendation=rec,
        appeal_window_days=window,
        days_since_ruling=inp.days_since_ruling,
        days_remaining=days_rem,
        window_expired=expired,
        deadline_urgency=urgency,
        appeal_win_probability=p_win,
        ev_of_appealing=ev,
        is_ev_positive=ev > 0,
        appeal_rationale=rationale,
        required_steps=steps,
        summary=summary,
    )
