"""
Evidence Gap Detector — identify missing evidence for a dispute reason code.

Before submitting a dispute response, a merchant needs to know not just
whether their evidence is "good enough" but specifically WHICH pieces are
missing or weak. This oracle:

  1. Loads the canonical evidence checklist per reason code
  2. Compares it against what the merchant has actually submitted
  3. Scores each gap by urgency (how much win probability it costs to omit it)
  4. Returns a ranked "close-first" list of evidence to gather before deadline

Evidence weights per reason code (sum to 1.0):
  INR (Item Not Received):
    tracking_number      0.35  — proves item was shipped
    delivery_scan        0.25  — proves item was delivered
    carrier_name         0.15  — corroborates shipping method
    ship_date_proof      0.15  — proves shipping before dispute
    order_confirmation   0.10  — baseline order record

  SNAD (Significantly Not As Described):
    product_photos       0.30  — visual match to listing
    listing_screenshot   0.25  — proves description was accurate
    return_policy_proof  0.20  — shows buyer accepted terms
    order_confirmation   0.15  — baseline order record
    customer_comms       0.10  — any interaction proving buyer knew

  UNAUTHORIZED:
    avs_cvv_match        0.35  — card verification passed
    ip_address_match     0.25  — IP matches buyer's known location
    device_fingerprint   0.20  — device matches buyer's history
    delivery_address_match 0.15 — ships to buyer's billing address
    buyer_prior_orders   0.05  — buyer has history with merchant

  SUBSCRIPTION:
    signup_proof         0.35  — buyer enrolled
    notification_sent    0.25  — renewal notification was sent
    terms_acceptance     0.20  — buyer accepted recurring terms
    cancellation_absent  0.15  — no cancellation before charge
    login_activity       0.05  — buyer used the service

PayPal benefit:
  - Better-evidenced disputes resolve faster, reducing PayPal case load
  - Merchants coached to full evidence sets win more disputes (fewer chargebacks)
  - Gap scores can feed PayPal's triage queue: a dispute with big gaps
    can be pre-flagged for outcome risk before merchant even responds
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Set


class GapSeverity(str, Enum):
    CRITICAL = "CRITICAL"   # weight ≥ 0.30 — costs most if missing
    MAJOR    = "MAJOR"      # weight 0.15 – 0.29
    MINOR    = "MINOR"      # weight < 0.15


# Evidence checklist: reason_code → {evidence_key: weight}
_EVIDENCE_WEIGHTS: dict = {
    "INR": {
        "tracking_number":    0.35,
        "delivery_scan":      0.25,
        "carrier_name":       0.15,
        "ship_date_proof":    0.15,
        "order_confirmation": 0.10,
    },
    "SNAD": {
        "product_photos":      0.30,
        "listing_screenshot":  0.25,
        "return_policy_proof": 0.20,
        "order_confirmation":  0.15,
        "customer_comms":      0.10,
    },
    "UNAUTHORIZED": {
        "avs_cvv_match":          0.35,
        "ip_address_match":       0.25,
        "device_fingerprint":     0.20,
        "delivery_address_match": 0.15,
        "buyer_prior_orders":     0.05,
    },
    "SUBSCRIPTION": {
        "signup_proof":       0.35,
        "notification_sent":  0.25,
        "terms_acceptance":   0.20,
        "cancellation_absent":0.15,
        "login_activity":     0.05,
    },
}

_GENERIC_WEIGHTS: dict = {
    "order_confirmation": 0.40,
    "communication_log":  0.35,
    "policy_document":    0.25,
}


@dataclass
class EvidenceGap:
    evidence_key: str
    weight: float
    severity: GapSeverity
    win_probability_cost: float    # estimated win probability lost by missing this
    action_description: str        # what to gather / how to prove it


@dataclass
class EvidenceGapInput:
    dispute_id: str
    reason_code: str               # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / other
    submitted_evidence_keys: List[str]
    deadline_hours_remaining: float = 120.0
    transaction_amount: float = 100.0


@dataclass
class EvidenceGapReport:
    dispute_id: str
    reason_code: str
    total_gap_score: float          # 0–1 (sum of missing weights)
    coverage_score: float           # 1 - total_gap_score (how much is covered)
    critical_gaps: List[EvidenceGap]
    all_gaps: List[EvidenceGap]     # sorted by weight desc
    is_submission_ready: bool       # True if gap_score < 0.20
    close_first_recommendation: str
    paypal_signal: str


_ACTION_DESCRIPTIONS: dict = {
    # INR
    "tracking_number":    "Obtain tracking number from your shipping carrier (UPS/FedEx/USPS).",
    "delivery_scan":      "Download delivery confirmation scan from carrier portal.",
    "carrier_name":       "Include carrier name in your response (UPS, FedEx, USPS, DHL, etc.).",
    "ship_date_proof":    "Attach shipping receipt or carrier pickup scan showing ship date.",
    "order_confirmation": "Export order confirmation email or system record.",
    # SNAD
    "product_photos":     "Take photos of the product matching your listing description.",
    "listing_screenshot": "Screenshot the original listing page showing description and photos.",
    "return_policy_proof":"Attach return/refund policy that was visible at checkout.",
    "customer_comms":     "Export email or chat history showing buyer acknowledged the product.",
    # UNAUTHORIZED
    "avs_cvv_match":      "Pull AVS/CVV verification result from payment processor.",
    "ip_address_match":   "Export transaction log showing IP address at checkout.",
    "device_fingerprint": "Pull device fingerprint / browser ID from fraud tool.",
    "delivery_address_match": "Show shipping address matches cardholder's billing address.",
    "buyer_prior_orders": "List prior orders from this buyer/card combination.",
    # SUBSCRIPTION
    "signup_proof":       "Export signup confirmation email or account creation record.",
    "notification_sent":  "Export renewal notification email(s) with timestamps.",
    "terms_acceptance":   "Screenshot or log showing buyer checked 'I agree to recurring billing'.",
    "cancellation_absent":"Confirm no cancellation request was received before charge.",
    "login_activity":     "Export login/activity log showing buyer used the service after billing.",
    # Generic
    "communication_log":  "Export all buyer-merchant communications related to this transaction.",
    "policy_document":    "Attach any relevant policy document shown at checkout.",
}


def _get_weights(reason_code: str) -> dict:
    return _EVIDENCE_WEIGHTS.get(reason_code.upper(), _GENERIC_WEIGHTS)


def _severity(weight: float) -> GapSeverity:
    if weight >= 0.30:
        return GapSeverity.CRITICAL
    if weight >= 0.15:
        return GapSeverity.MAJOR
    return GapSeverity.MINOR


def detect_evidence_gaps(inp: EvidenceGapInput) -> EvidenceGapReport:
    weights = _get_weights(inp.reason_code)
    submitted: Set[str] = set(k.lower() for k in inp.submitted_evidence_keys)

    gaps: List[EvidenceGap] = []
    for key, weight in weights.items():
        if key not in submitted:
            # Win probability cost: weight × 0.5 (rough: full coverage = +50pp win prob)
            cost = round(weight * 0.50, 4)
            gaps.append(EvidenceGap(
                evidence_key=key,
                weight=weight,
                severity=_severity(weight),
                win_probability_cost=cost,
                action_description=_ACTION_DESCRIPTIONS.get(
                    key, f"Gather {key.replace('_', ' ')} documentation."
                ),
            ))

    gaps.sort(key=lambda g: g.weight, reverse=True)
    critical = [g for g in gaps if g.severity == GapSeverity.CRITICAL]
    total_gap = round(sum(g.weight for g in gaps), 4)
    coverage = round(1.0 - total_gap, 4)
    submission_ready = total_gap < 0.20

    # Close-first recommendation
    if not gaps:
        rec = "Evidence set is complete for this reason code. Ready to submit."
    elif submission_ready:
        top = gaps[0]
        rec = (
            f"Evidence is mostly complete (gap={total_gap:.0%}). "
            f"Optionally add: {top.evidence_key.replace('_', ' ')} to maximize win probability."
        )
    else:
        top = gaps[0]
        rec = (
            f"Close critical gaps before submitting. Start with "
            f"'{top.evidence_key.replace('_', ' ')}' (costs {top.win_probability_cost:.0%} win prob if missing). "
            + (f"Deadline in {inp.deadline_hours_remaining:.0f}h." if inp.deadline_hours_remaining < 48 else "")
        )

    paypal_signal = (
        f"Dispute {inp.dispute_id} [{inp.reason_code}]: "
        f"coverage={coverage:.0%}, gaps={len(gaps)}, "
        f"critical_gaps={len(critical)}, ready={submission_ready}."
    )

    return EvidenceGapReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code.upper(),
        total_gap_score=total_gap,
        coverage_score=coverage,
        critical_gaps=critical,
        all_gaps=gaps,
        is_submission_ready=submission_ready,
        close_first_recommendation=rec,
        paypal_signal=paypal_signal,
    )
