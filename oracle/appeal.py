"""
Appeal Engine — fight back after PayPal rules against you.

Most merchants accept a PayPal loss as final. It isn't.

PayPal gives merchants 10 days to escalate a dispute to PayPal's internal
review team after an initial ruling. This is a separate review — a human
(or a different AI pipeline) looks at the case fresh. Win rates on well-crafted
appeals are materially higher than merchants expect.

The key insight: an appeal is NOT just resubmitting the same evidence.
The appeal must:
  1. Identify SPECIFICALLY what PayPal's ruling was based on (the stated reason)
  2. Address that exact reason with new evidence or a corrected framing
  3. Invoke Seller Protection explicitly if requirements were met but SP wasn't cited
  4. Present a clear narrative — not a list of attachments

This module:
  - Parses PayPal's ruling reason codes into actionable response strategies
  - Identifies new evidence to gather that wasn't in the original submission
  - Generates an appeal letter optimized for PayPal's internal review team
  - Calculates whether appeal is worth filing (EV analysis on appeal cost/benefit)
  - Tracks appeal deadlines (10 days from initial ruling)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class RulingReason(str, Enum):
    """PayPal's stated reasons when ruling against the merchant."""
    NO_PROOF_OF_DELIVERY   = "NO_PROOF_OF_DELIVERY"
    ITEM_NOT_AS_DESCRIBED  = "ITEM_NOT_AS_DESCRIBED"
    ITEM_NOT_RECEIVED      = "ITEM_NOT_RECEIVED"
    UNAUTHORIZED_CLAIM     = "UNAUTHORIZED_CLAIM"
    INSUFFICIENT_EVIDENCE  = "INSUFFICIENT_EVIDENCE"
    SELLER_PROTECTION_VOID = "SELLER_PROTECTION_VOID"
    TRACKING_UNVERIFIED    = "TRACKING_UNVERIFIED"
    CARRIER_NOT_RECOGNIZED = "CARRIER_NOT_RECOGNIZED"
    OTHER                  = "OTHER"


class AppealWorthiness(str, Enum):
    STRONG   = "STRONG"    # Clear grounds, file immediately
    MODERATE = "MODERATE"  # Worthwhile but uncertain
    WEAK     = "WEAK"      # Low chance; consider accepting
    FUTILE   = "FUTILE"    # No new evidence possible; don't file


@dataclass
class AppealStrategy:
    worthiness: AppealWorthiness
    deadline: str                   # ISO 8601 — 10 days from ruling
    days_remaining: int
    appeal_letter: str              # Ready-to-submit appeal text
    new_evidence_needed: list[str]  # Evidence NOT in original submission
    ruling_addressed: str           # How we specifically counter the ruling
    ev_appeal: float                # Expected value of filing (vs. not filing)
    reasoning: list[str] = field(default_factory=list)


# PayPal ruling reason → counter strategy mapping
_RULING_COUNTERS: dict[RulingReason, dict] = {
    RulingReason.NO_PROOF_OF_DELIVERY: {
        "counter": "Provide carrier delivery confirmation that was NOT attached to original response",
        "new_evidence": [
            "Carrier tracking page screenshot showing DELIVERED status and delivery date",
            "Carrier delivery confirmation email (if carrier sends them)",
            "Signature confirmation PDF (required if >$750, strongly recommended otherwise)",
        ],
        "sp_angle": "If all SP requirements are met, invoke Seller Protection explicitly in appeal",
        "letter_anchor": (
            "The original ruling cited absence of delivery proof. "
            "I am attaching [CARRIER] tracking confirmation showing delivery on [DATE] to [CITY, STATE]. "
            "The tracking status is DELIVERED. This transaction qualifies for PayPal Seller Protection: "
            "it was shipped to the PayPal-confirmed address with online tracking."
        ),
    },
    RulingReason.TRACKING_UNVERIFIED: {
        "counter": "Re-submit tracking with direct carrier URL and status screenshot",
        "new_evidence": [
            "Direct carrier tracking URL (not just the number) — e.g. usps.com/tracking/[NUMBER]",
            "Dated screenshot of carrier page showing DELIVERED with full delivery details",
            "Carrier-generated delivery confirmation email",
        ],
        "sp_angle": "Cite carrier name explicitly — PayPal recognizes USPS, FedEx, UPS, DHL, Canada Post",
        "letter_anchor": (
            "I am resubmitting tracking evidence with a direct link to the carrier's tracking page "
            "and a screenshot showing DELIVERED status. The carrier is [CARRIER_NAME]. "
            "Tracking number [NUMBER] shows delivery on [DATE] to [ADDRESS]."
        ),
    },
    RulingReason.CARRIER_NOT_RECOGNIZED: {
        "counter": "Use a recognized major carrier; provide carrier SCAC code or official tracking URL",
        "new_evidence": [
            "Carrier's official tracking URL",
            "Carrier SCAC code or official name exactly as registered with PayPal",
            "Third-party verification via 17track.net if primary carrier not in PayPal's list",
        ],
        "sp_angle": "Consider whether re-shipping with a PayPal-recognized carrier is feasible",
        "letter_anchor": (
            "The carrier [CARRIER_NAME] is a registered carrier whose tracking is verifiable at [URL]. "
            "The tracking status is DELIVERED as of [DATE]. I am attaching a screenshot from the official "
            "carrier tracking page confirming delivery."
        ),
    },
    RulingReason.ITEM_NOT_AS_DESCRIBED: {
        "counter": "Show exact match between listing description and item shipped",
        "new_evidence": [
            "Original listing as it appeared at time of sale (screenshot or PDF with date)",
            "Photos of item shipped — specifically the parts buyer claims differ from listing",
            "Third-party inspection or authentication if applicable",
            "Previous buyer reviews for identical item confirming accurate description",
        ],
        "sp_angle": "SNAD is not covered by Seller Protection — fight on evidence merits only",
        "letter_anchor": (
            "The item shipped matches the listing description exactly. "
            "I am attaching: (1) the original listing as it appeared at time of sale, "
            "(2) photos of the item shipped, and (3) a detailed comparison showing each "
            "specification in the listing matches the item delivered. "
            "The buyer has not returned the item through the established return process."
        ),
    },
    RulingReason.ITEM_NOT_RECEIVED: {
        "counter": "Provide irrefutable delivery evidence — delivery confirmation + address match",
        "new_evidence": [
            "Carrier delivery confirmation showing DELIVERED + exact delivery address",
            "Proof that delivery address matches PayPal-provided checkout address",
            "Signature confirmation if available",
            "GPS delivery photo if carrier provides it (USPS, FedEx often include this)",
        ],
        "sp_angle": "Explicitly state all SP requirements are met and invoke SP coverage",
        "letter_anchor": (
            "The item was delivered. Attached is carrier delivery confirmation showing "
            "DELIVERED status on [DATE] to [ADDRESS], which is the PayPal-confirmed "
            "checkout address. This transaction qualifies for PayPal Seller Protection "
            "under the Item Not Received policy."
        ),
    },
    RulingReason.UNAUTHORIZED_CLAIM: {
        "counter": "Prove buyer knew about and engaged with the transaction",
        "new_evidence": [
            "Any message from buyer referencing the order (email, PayPal message, chat)",
            "Delivery to address registered on buyer's PayPal account (not a different address)",
            "Login from buyer's account to view/use the item after purchase",
            "3DS authentication confirmation if payment processor supports it",
        ],
        "sp_angle": "If delivered to PayPal account address with tracking, SP covers UNAUTHORIZED",
        "letter_anchor": (
            "The transaction was authorized. Attached is [EVIDENCE TYPE] showing the buyer "
            "was aware of and engaged with this transaction. The item was delivered to the "
            "address registered to the buyer's PayPal account on [DATE]. "
            "This transaction qualifies for PayPal Seller Protection under the Unauthorized "
            "Transaction policy: it was shipped to the PayPal-confirmed address with "
            "online tracking showing DELIVERED status."
        ),
    },
    RulingReason.SELLER_PROTECTION_VOID: {
        "counter": "Demonstrate SP requirements were actually met; cite specific SP policy sections",
        "new_evidence": [
            "Proof of shipment to PayPal-provided address (not the buyer's alternate address)",
            "Carrier confirmation that item was shipped to PayPal checkout address",
            "Signature confirmation if dispute amount >$750",
        ],
        "sp_angle": "Reference the exact SP policy requirement that was allegedly not met; prove it was met",
        "letter_anchor": (
            "I am appealing the determination that Seller Protection does not apply. "
            "All Seller Protection requirements were met for this transaction: "
            "(1) payment was made through PayPal, (2) the item was shipped to the "
            "PayPal-confirmed checkout address, (3) online tracking confirms delivery, "
            "(4) the item category is not excluded from SP coverage. "
            "I am attaching documentation for each requirement."
        ),
    },
    RulingReason.INSUFFICIENT_EVIDENCE: {
        "counter": "Submit a complete, ordered evidence package — every item PayPal requires",
        "new_evidence": [
            "Complete tracking history (all events, not just delivered)",
            "Proof of fulfillment matching claim type (delivery for INR, photos for SNAD)",
            "Any buyer communication referencing the order",
            "Order confirmation showing what was purchased and when",
        ],
        "sp_angle": "Explicitly checklist each SP requirement if applicable",
        "letter_anchor": (
            "I am resubmitting a complete evidence package addressing each element "
            "of this dispute. The original submission was incomplete; I have now gathered "
            "all required documentation. Please review the attached evidence in the "
            "order presented."
        ),
    },
    RulingReason.OTHER: {
        "counter": "Request the specific ruling basis and address it directly",
        "new_evidence": [
            "Contact PayPal Resolution Center to understand the exact ruling reason",
            "Submit any evidence not included in original response",
        ],
        "sp_angle": "Invoke SP if applicable; cite policy section",
        "letter_anchor": (
            "I am appealing this ruling. I have gathered additional evidence supporting "
            "my position and am attaching it for review. Please reconsider based on "
            "the attached documentation."
        ),
    },
}


def analyze_appeal(
    ruling_reason: RulingReason,
    dispute_amount: float,
    claim_type: str,
    ruling_date: str,                   # ISO 8601 date of original ruling
    *,
    original_evidence_had_tracking: bool = False,
    original_evidence_had_delivery: bool = False,
    can_obtain_signature_confirmation: bool = False,
    can_obtain_buyer_communication: bool = False,
    seller_protection_requirements_met: bool = False,
    seller_hourly_rate: float = 50.0,
    appeal_handling_hours: float = 1.0,  # Estimated time to file appeal
) -> AppealStrategy:
    """
    Analyze whether and how to file a PayPal dispute appeal.

    PayPal gives 10 days from initial ruling to escalate. The appeal goes to
    a different review path than the automated dispute system — a well-argued
    appeal with new evidence has a materially higher win rate than the original.

    Returns an AppealStrategy with worthiness assessment, deadline, appeal letter,
    and new evidence to gather before filing.
    """
    reasoning = []

    # ── Deadline calculation ──────────────────────────────────────────────────
    try:
        ruling_dt = datetime.fromisoformat(ruling_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        ruling_dt = datetime.now(timezone.utc)

    appeal_deadline = ruling_dt + timedelta(days=10)
    now = datetime.now(timezone.utc)
    days_remaining = max(0, (appeal_deadline - now).days)

    if days_remaining == 0:
        return AppealStrategy(
            worthiness=AppealWorthiness.FUTILE,
            deadline=appeal_deadline.isoformat(),
            days_remaining=0,
            appeal_letter="Appeal window closed — 10-day deadline has passed.",
            new_evidence_needed=[],
            ruling_addressed="Appeal period expired",
            ev_appeal=0.0,
            reasoning=["Appeal window closed — 10 days have passed since ruling"],
        )

    # ── Counter strategy for this ruling reason ──────────────────────────────
    strategy = _RULING_COUNTERS.get(ruling_reason, _RULING_COUNTERS[RulingReason.OTHER])
    counter = strategy["counter"]
    new_evidence = list(strategy["new_evidence"])
    sp_angle = strategy["sp_angle"]
    letter_anchor = strategy["letter_anchor"]

    # ── Assess what NEW evidence can actually be obtained ────────────────────
    obtainable_new = []

    if ruling_reason in (RulingReason.NO_PROOF_OF_DELIVERY, RulingReason.ITEM_NOT_RECEIVED,
                         RulingReason.TRACKING_UNVERIFIED):
        if not original_evidence_had_delivery:
            obtainable_new.append("Carrier delivery confirmation screenshot")
        if can_obtain_signature_confirmation and dispute_amount > 500:
            obtainable_new.append("Signature confirmation from carrier")

    if ruling_reason == RulingReason.UNAUTHORIZED_CLAIM:
        if can_obtain_buyer_communication:
            obtainable_new.append("Buyer communication showing order awareness")

    if seller_protection_requirements_met and ruling_reason not in (
        RulingReason.ITEM_NOT_AS_DESCRIBED,
    ):
        obtainable_new.append("Explicit Seller Protection compliance documentation")

    # ── Worthiness assessment ─────────────────────────────────────────────────
    # Base win rate on appeal (vs. not appealing)
    # PayPal appeal win rates: ~35% when new evidence is submitted
    #                          ~15% when just rearguing without new evidence
    #                          ~5%  when no new angle exists
    has_new_evidence = bool(obtainable_new)
    has_sp_angle = seller_protection_requirements_met and claim_type in (
        "ITEM_NOT_RECEIVED", "UNAUTHORIZED"
    )

    if has_new_evidence and has_sp_angle:
        appeal_win_rate = 0.55
        worthiness = AppealWorthiness.STRONG
        reasoning.append("Strong appeal: new evidence + explicit SP coverage angle")
    elif has_new_evidence:
        appeal_win_rate = 0.35
        worthiness = AppealWorthiness.STRONG if dispute_amount > 100 else AppealWorthiness.MODERATE
        reasoning.append(f"Good appeal: {len(obtainable_new)} new evidence item(s) available")
    elif has_sp_angle:
        appeal_win_rate = 0.40
        worthiness = AppealWorthiness.MODERATE
        reasoning.append("Moderate appeal: SP requirements met but SP was not invoked")
    elif days_remaining >= 7:
        appeal_win_rate = 0.15
        worthiness = AppealWorthiness.WEAK
        reasoning.append("Weak appeal: no new evidence; rearguing original position")
    else:
        appeal_win_rate = 0.10
        worthiness = AppealWorthiness.WEAK
        reasoning.append("Weak appeal: no new evidence and time is short")

    # ── EV of filing appeal ───────────────────────────────────────────────────
    appeal_cost = seller_hourly_rate * appeal_handling_hours
    ev_appeal = (appeal_win_rate * dispute_amount) - appeal_cost
    reasoning.append(
        f"EV(appeal) = {appeal_win_rate:.0%} × ${dispute_amount:.2f} − ${appeal_cost:.0f} "
        f"cost = ${ev_appeal:.2f}"
    )

    if ev_appeal < 0 and worthiness == AppealWorthiness.WEAK:
        worthiness = AppealWorthiness.FUTILE
        reasoning.append("Filing appeal is EV-negative — accept the loss")

    # ── Build appeal letter ───────────────────────────────────────────────────
    sp_paragraph = ""
    if seller_protection_requirements_met and claim_type in ("ITEM_NOT_RECEIVED", "UNAUTHORIZED"):
        sp_paragraph = (
            "\n\nThis transaction qualifies for PayPal Seller Protection. "
            "All requirements have been met: payment through PayPal, "
            "shipment to the PayPal-confirmed checkout address, "
            "online carrier tracking, and confirmed DELIVERED status. "
            "I respectfully request that Seller Protection be applied to this case."
        )

    appeal_letter = f"""I am writing to appeal the ruling on dispute [DISPUTE_ID].

The ruling stated: {ruling_reason.value.replace('_', ' ')}.

{letter_anchor}
{sp_paragraph}

I am attaching the following additional evidence:
{chr(10).join(f'  • {e}' for e in (obtainable_new or new_evidence[:3]))}

I respectfully request that this dispute be reviewed again in light of the attached evidence.
My records clearly show that I fulfilled my obligations as a seller, and this dispute should
be resolved in my favor.

Thank you for your reconsideration."""

    return AppealStrategy(
        worthiness=worthiness,
        deadline=appeal_deadline.isoformat(),
        days_remaining=days_remaining,
        appeal_letter=appeal_letter,
        new_evidence_needed=obtainable_new or new_evidence[:4],
        ruling_addressed=counter,
        ev_appeal=round(ev_appeal, 2),
        reasoning=reasoning,
    )


if __name__ == "__main__":
    # Scenario: INR — lost because no proof of delivery, but we have tracking
    result = analyze_appeal(
        RulingReason.NO_PROOF_OF_DELIVERY,
        dispute_amount=120.0,
        claim_type="ITEM_NOT_RECEIVED",
        ruling_date=datetime.now(timezone.utc).isoformat(),
        original_evidence_had_tracking=True,
        original_evidence_had_delivery=False,  # Forgot to attach delivery confirmation!
        seller_protection_requirements_met=True,
    )
    print(f"Worthiness: {result.worthiness.value}")
    print(f"Days remaining: {result.days_remaining}")
    print(f"EV(appeal): ${result.ev_appeal}")
    print(f"New evidence needed: {result.new_evidence_needed}")
    for r in result.reasoning:
        print(f"  → {r}")
    print(f"\nAppeal letter:\n{result.appeal_letter}")
