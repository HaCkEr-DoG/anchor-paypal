"""
Subscription Cancellation Analyzer — assess subscription dispute defensibility
by evaluating the cancellation timeline, notification history, and billing
notice evidence.

For SUBSCRIPTION and 13.3 disputes (recurring billing without authorization),
this oracle determines:
  - Whether merchant sent proper advance notice of renewal
  - Whether buyer's cancellation request (if any) was processed correctly
  - Whether the charge was billed after a valid cancellation
  - Win probability adjustment based on notice quality
  - Required evidence for a defensible response

Output: analysis of subscription evidence quality and recommended strategy

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NoticeQuality(str, Enum):
    EXCELLENT  = "EXCELLENT"   # Sent 7+ days advance, confirmed delivery
    ADEQUATE   = "ADEQUATE"    # Sent 3–6 days advance, or unconfirmed
    POOR       = "POOR"        # <3 days advance, or no confirmation
    NONE       = "NONE"        # No renewal notice sent


class CancellationOutcome(str, Enum):
    NO_CANCELLATION_REQUESTED  = "NO_CANCELLATION_REQUESTED"
    CANCELLED_BEFORE_BILLING   = "CANCELLED_BEFORE_BILLING"   # Merchant error — do not fight
    CANCELLED_AFTER_BILLING    = "CANCELLED_AFTER_BILLING"    # Legitimate billing
    CANCELLATION_DISPUTED      = "CANCELLATION_DISPUTED"      # Buyer claims they cancelled
    CANCELLATION_NOT_PROCESSED = "CANCELLATION_NOT_PROCESSED" # Request received, ignored


class SubscriptionDefense(str, Enum):
    STRONG       = "STRONG"      # Clear notice + no valid cancel = fight
    ADEQUATE     = "ADEQUATE"    # Some notice + procedural compliance = fight
    WEAK         = "WEAK"        # Gaps in notice or procedure
    INDEFENSIBLE = "INDEFENSIBLE"  # Billed after cancel, or no notice at all


@dataclass
class SubscriptionEvidenceInput:
    """Input describing subscription billing and cancellation evidence."""
    dispute_id: str
    transaction_amount: float
    reason_code: str            # SUBSCRIPTION or 13.3

    # Renewal notice evidence
    notice_quality: NoticeQuality = NoticeQuality.NONE
    notice_sent_days_before_billing: int = 0  # Days before billing notice was sent
    notice_delivery_confirmed: bool = False    # E.g. email open/click confirmed

    # Cancellation evidence
    cancellation_outcome: CancellationOutcome = CancellationOutcome.NO_CANCELLATION_REQUESTED
    cancellation_request_epoch: Optional[int] = None
    billing_epoch: Optional[int] = None
    cancellation_processed_epoch: Optional[int] = None

    # Policy evidence
    terms_accepted_at_signup: bool = False
    recurring_billing_disclosed: bool = False
    cancellation_method_clear: bool = False

    # Buyer behavior
    prior_renewals_accepted: int = 0   # Times buyer was billed without dispute


@dataclass
class SubscriptionAnalysisReport:
    """Full analysis of subscription cancellation dispute."""
    dispute_id: str
    transaction_amount: float

    defense_quality: SubscriptionDefense
    cancellation_outcome: CancellationOutcome

    win_rate_adjustment: float     # Additive to base win probability
    notice_quality_assessment: str
    cancellation_assessment: str

    required_evidence: list[str]
    strengths: list[str]
    weaknesses: list[str]

    recommendation: str
    summary: str


def analyze_subscription_cancellation(inp: SubscriptionEvidenceInput) -> SubscriptionAnalysisReport:
    """Analyze subscription evidence and assess defensibility."""
    strengths: list[str] = []
    weaknesses: list[str] = []
    required_evidence: list[str] = []

    # ── Notice quality assessment ─────────────────────────────────
    if inp.notice_quality == NoticeQuality.EXCELLENT:
        notice_assessment = "Excellent — advance notice sent ≥7 days before billing with confirmation"
        strengths.append("Renewal notice sent 7+ days in advance")
        if inp.notice_delivery_confirmed:
            strengths.append("Notice delivery confirmed (open/click tracking)")
    elif inp.notice_quality == NoticeQuality.ADEQUATE:
        notice_assessment = "Adequate — notice sent 3–6 days before billing"
        strengths.append("Renewal notice provided before billing")
        if not inp.notice_delivery_confirmed:
            weaknesses.append("Notice delivery not confirmed — buyer may claim non-receipt")
    elif inp.notice_quality == NoticeQuality.POOR:
        notice_assessment = "Poor — notice sent < 3 days before billing or unconfirmed"
        weaknesses.append("Insufficient advance notice of renewal")
        required_evidence.append("Provide email/notification logs showing notice date and time")
    else:
        notice_assessment = "None — no renewal notice sent"
        weaknesses.append("No renewal notice sent — Visa/MC require advance notification")
        required_evidence.append("Send renewal notice immediately for future billings")

    # ── Cancellation outcome assessment ──────────────────────────
    if inp.cancellation_outcome == CancellationOutcome.NO_CANCELLATION_REQUESTED:
        cancel_assessment = "No cancellation was requested prior to billing"
        strengths.append("No cancellation request on record before the billing")

    elif inp.cancellation_outcome == CancellationOutcome.CANCELLED_BEFORE_BILLING:
        cancel_assessment = "Cancellation was processed BEFORE billing — this is a merchant error"
        weaknesses.append("CRITICAL: Subscription was cancelled before the disputed charge")
        required_evidence.append("Provide cancellation processing logs and billing timeline")

    elif inp.cancellation_outcome == CancellationOutcome.CANCELLED_AFTER_BILLING:
        cancel_assessment = "Cancellation processed after billing — charge was legitimate"
        strengths.append("Cancellation request received after billing date")
        if inp.cancellation_request_epoch and inp.billing_epoch:
            if inp.cancellation_request_epoch > inp.billing_epoch:
                strengths.append("Timeline confirms: billing predates cancellation request")

    elif inp.cancellation_outcome == CancellationOutcome.CANCELLATION_DISPUTED:
        cancel_assessment = "Buyer claims cancellation was sent — disputed timeline"
        weaknesses.append("Buyer claims prior cancellation — no confirmation record")
        required_evidence.append("Provide full cancellation request log with timestamps")
        required_evidence.append("Show cancellation confirmation email sent to buyer")

    elif inp.cancellation_outcome == CancellationOutcome.CANCELLATION_NOT_PROCESSED:
        cancel_assessment = "Cancellation requested but NOT processed — merchant liability"
        weaknesses.append("CRITICAL: Cancellation request was not actioned — refund immediately")

    else:
        cancel_assessment = "Cancellation status unknown"

    # ── Policy evidence ───────────────────────────────────────────
    if inp.terms_accepted_at_signup:
        strengths.append("Buyer accepted terms at signup including recurring billing disclosure")
    else:
        weaknesses.append("No record of terms acceptance at signup")

    if inp.recurring_billing_disclosed:
        strengths.append("Recurring billing disclosed clearly at checkout")
    else:
        weaknesses.append("Recurring billing disclosure not confirmed")
        required_evidence.append("Provide screenshot of checkout page with recurring billing disclosure")

    if inp.cancellation_method_clear:
        strengths.append("Clear cancellation instructions provided to buyer")
    else:
        weaknesses.append("Cancellation method not clearly documented")

    if inp.prior_renewals_accepted >= 2:
        strengths.append(
            f"Buyer accepted {inp.prior_renewals_accepted} prior renewals without dispute"
        )

    # ── Defense quality ───────────────────────────────────────────
    if inp.cancellation_outcome in (
        CancellationOutcome.CANCELLED_BEFORE_BILLING,
        CancellationOutcome.CANCELLATION_NOT_PROCESSED,
    ):
        defense = SubscriptionDefense.INDEFENSIBLE
        win_adj = -0.25
    elif inp.notice_quality == NoticeQuality.NONE:
        defense = SubscriptionDefense.WEAK
        win_adj = -0.12
    elif (inp.notice_quality in (NoticeQuality.EXCELLENT, NoticeQuality.ADEQUATE)
          and inp.cancellation_outcome in (
              CancellationOutcome.NO_CANCELLATION_REQUESTED,
              CancellationOutcome.CANCELLED_AFTER_BILLING,
          )
          and inp.terms_accepted_at_signup):
        defense = SubscriptionDefense.STRONG
        win_adj = +0.10
    elif (inp.notice_quality != NoticeQuality.NONE
          and inp.cancellation_outcome != CancellationOutcome.CANCELLATION_DISPUTED):
        defense = SubscriptionDefense.ADEQUATE
        win_adj = +0.03
    else:
        defense = SubscriptionDefense.WEAK
        win_adj = -0.08

    # ── Recommendation ────────────────────────────────────────────
    if defense == SubscriptionDefense.INDEFENSIBLE:
        rec = "Refund immediately — this charge is indefensible"
    elif defense == SubscriptionDefense.WEAK:
        rec = "Gather missing evidence before filing; consider partial refund to close quickly"
    elif defense == SubscriptionDefense.ADEQUATE:
        rec = "File response with available evidence; highlight prior renewal acceptance"
    else:
        rec = "File assertive response — lead with renewal notice and terms acceptance evidence"

    summary = (
        f"Subscription {inp.dispute_id}: "
        f"defense={defense.value} win_adj={win_adj:+.2f} | "
        f"cancel={inp.cancellation_outcome.value}"
    )

    return SubscriptionAnalysisReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        defense_quality=defense,
        cancellation_outcome=inp.cancellation_outcome,
        win_rate_adjustment=win_adj,
        notice_quality_assessment=notice_assessment,
        cancellation_assessment=cancel_assessment,
        required_evidence=required_evidence,
        strengths=strengths,
        weaknesses=weaknesses,
        recommendation=rec,
        summary=summary,
    )
