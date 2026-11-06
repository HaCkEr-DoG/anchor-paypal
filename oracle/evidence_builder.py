"""
Evidence Package Builder — assemble the optimal submission in the right order.

The oracle modules tell you WHAT evidence to gather. This module takes everything
you have and builds the final submission package, ordered for maximum impact.

PayPal's AI reads evidence in the order submitted. Research on successful dispute
outcomes shows a consistent winning structure:

  1. Lead with Seller Protection compliance (if applicable) — it's binary and
     immediately decisive. If SP is established, everything else is backup.
  2. Delivery proof next — the core factual claim
  3. Authentication signals (3DS, IP match) — proves intent
  4. Communication evidence — shows buyer was aware
  5. Description match / photos — counters SNAD
  6. Policy compliance — ToS, cancellation policy, return policy

Merchants who front-load with attachments and bury the SP statement lose cases
they should win. The submission needs a clear narrative first, evidence second.

This module also:
  - Scores the overall evidence completeness (0.0–1.0)
  - Identifies the single highest-impact missing piece
  - Generates a word-count-optimized cover letter (PayPal truncates long submissions)
  - Flags evidence that can HURT the case (too many attachments signal desperation)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ClaimType(str, Enum):
    INR         = "ITEM_NOT_RECEIVED"
    SNAD        = "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    UNAUTHORIZED = "UNAUTHORIZED"
    BILLING     = "BILLING_ISSUE"


@dataclass
class EvidenceItem:
    label: str                  # Short name for the evidence piece
    content_summary: str        # One line describing what it shows
    is_present: bool            # Do we have this evidence right now?
    impact: float               # How much this moves win probability (0.0–1.0)
    section: int                # Submission order (1 = lead, higher = later)
    is_critical: bool = False   # Missing this is usually a loss


@dataclass
class EvidencePackage:
    claim_type: ClaimType
    completeness: float                    # 0.0–1.0 overall evidence completeness
    win_probability_estimate: float        # Estimated win probability with this package
    cover_letter: str                      # Optimized submission text
    ordered_evidence: list[EvidenceItem]   # In optimal submission order
    highest_impact_missing: Optional[str]  # Single most important missing piece
    warnings: list[str]                    # Things that could hurt the case
    sp_statement: str                      # Seller Protection compliance statement
    reasoning: list[str] = field(default_factory=list)


def build_evidence_package(
    claim_type: ClaimType,
    dispute_amount: float,
    *,
    # Seller Protection signals
    paid_via_paypal: bool = True,
    shipped_to_paypal_address: bool = False,
    has_online_tracking: bool = False,
    tracking_shows_delivered: bool = False,
    has_signature_confirmation: bool = False,
    seller_account_in_good_standing: bool = True,
    # Physical goods delivery
    has_tracking_number: bool = False,
    carrier_name: str = "",
    delivery_date: str = "",
    delivery_address_matches: bool = False,
    # Authentication
    has_3ds_authentication: bool = False,
    has_avs_cvv_match: bool = False,
    # Buyer communication
    has_buyer_messages: bool = False,
    buyer_acknowledged_receipt: bool = False,
    # Item description
    has_listing_screenshot: bool = False,
    has_item_photos: bool = False,
    photos_match_listing: bool = False,
    # Policy
    has_return_policy: bool = False,
    return_requested_and_refused: bool = False,
    has_terms_of_service: bool = False,
    # Digital goods
    has_access_log: bool = False,
    access_before_dispute: bool = False,
    # Additional
    has_order_confirmation: bool = False,
    has_payment_record: bool = False,
) -> EvidencePackage:
    """
    Build the optimal ordered evidence package for a PayPal dispute submission.

    The output cover_letter is ready to paste into the PayPal dispute response
    field. Keep it under 2000 characters — PayPal truncates longer submissions
    and the truncation often cuts off the most important evidence references.
    """
    reasoning = []
    warnings = []
    items: list[EvidenceItem] = []

    # ── SP eligibility check ──────────────────────────────────────────────────
    sp_eligible = (
        paid_via_paypal
        and shipped_to_paypal_address
        and has_online_tracking
        and tracking_shows_delivered
        and claim_type in (ClaimType.INR, ClaimType.UNAUTHORIZED)
    )
    sp_fully_met = sp_eligible and (
        dispute_amount <= 750 or has_signature_confirmation
    )

    if sp_fully_met:
        sp_statement = (
            "This transaction qualifies for PayPal Seller Protection under the "
            f"{claim_type.value.replace('_', ' ').title()} policy. All requirements "
            "are met: (1) payment processed through PayPal, (2) item shipped to the "
            "PayPal-confirmed checkout address, (3) online carrier tracking number "
            "provided, (4) carrier tracking confirms DELIVERED status"
            + (", (5) signature confirmation obtained (required for transactions over $750)." if has_signature_confirmation else ".")
        )
        reasoning.append("Seller Protection FULLY MET — lead with this")
    elif sp_eligible:
        sp_statement = (
            "This transaction meets most Seller Protection requirements. "
            "Tracking confirms DELIVERED to the PayPal-confirmed address."
        )
        if dispute_amount > 750 and not has_signature_confirmation:
            warnings.append(
                f"Dispute amount ${dispute_amount:.0f} > $750: signature confirmation "
                "REQUIRED for full SP coverage — obtain from carrier"
            )
        reasoning.append("Seller Protection partially met")
    else:
        sp_statement = ""
        if claim_type in (ClaimType.INR, ClaimType.UNAUTHORIZED):
            if not shipped_to_paypal_address:
                warnings.append("Not shipped to PayPal address — SP not available")
            elif not tracking_shows_delivered:
                warnings.append("No DELIVERED status — SP requires confirmed delivery")

    # ── Build evidence items by claim type ───────────────────────────────────

    # Section 1: SP compliance (always first if applicable)
    if sp_fully_met:
        items.append(EvidenceItem(
            label="Seller Protection Compliance",
            content_summary="All SP requirements confirmed — liability shield active",
            is_present=True,
            impact=0.40,
            section=1,
            is_critical=True,
        ))

    # Section 2: Core delivery/fulfillment proof
    if claim_type in (ClaimType.INR, ClaimType.UNAUTHORIZED):
        delivered = tracking_shows_delivered or buyer_acknowledged_receipt
        items.append(EvidenceItem(
            label="Carrier Delivery Confirmation",
            content_summary=f"DELIVERED status to {'PayPal address' if delivery_address_matches else 'shipping address'}"
                           + (f" on {delivery_date}" if delivery_date else ""),
            is_present=delivered,
            impact=0.35,
            section=2,
            is_critical=True,
        ))
        if not delivered:
            reasoning.append("CRITICAL GAP: No delivery confirmation")

        items.append(EvidenceItem(
            label="Tracking Number Record",
            content_summary=f"Online tracking via {carrier_name or 'carrier'}" if has_tracking_number else "Tracking number",
            is_present=has_tracking_number,
            impact=0.15,
            section=2,
        ))

        if dispute_amount > 750:
            items.append(EvidenceItem(
                label="Signature Confirmation",
                content_summary="Carrier signature proof for high-value item",
                is_present=has_signature_confirmation,
                impact=0.20,
                section=2,
                is_critical=dispute_amount > 750,
            ))

    elif claim_type == ClaimType.SNAD:
        items.append(EvidenceItem(
            label="Original Listing",
            content_summary="Listing as it appeared at time of sale",
            is_present=has_listing_screenshot,
            impact=0.30,
            section=2,
            is_critical=True,
        ))
        items.append(EvidenceItem(
            label="Item Photos",
            content_summary="Photos of item shipped vs. listing description",
            is_present=has_item_photos and photos_match_listing,
            impact=0.25,
            section=2,
            is_critical=True,
        ))

    # Section 3: Authentication
    if has_3ds_authentication:
        items.append(EvidenceItem(
            label="3D Secure Authentication",
            content_summary="Buyer authenticated transaction — liability shifts to issuer",
            is_present=True,
            impact=0.35,
            section=3,
            is_critical=claim_type == ClaimType.UNAUTHORIZED,
        ))
    elif claim_type == ClaimType.UNAUTHORIZED:
        items.append(EvidenceItem(
            label="Transaction Authentication",
            content_summary="AVS/CVV match / IP verification",
            is_present=has_avs_cvv_match,
            impact=0.20,
            section=3,
            is_critical=True,
        ))
        if has_access_log:
            items.append(EvidenceItem(
                label="Account Access Log",
                content_summary="Buyer's account accessed after purchase" + (" before dispute" if access_before_dispute else ""),
                is_present=True,
                impact=0.25 if access_before_dispute else 0.15,
                section=3,
            ))

    # Section 4: Buyer communication
    items.append(EvidenceItem(
        label="Buyer Communication",
        content_summary="Messages showing buyer was aware of / engaged with order",
        is_present=has_buyer_messages,
        impact=0.12,
        section=4,
    ))
    if buyer_acknowledged_receipt:
        items.append(EvidenceItem(
            label="Receipt Acknowledgment",
            content_summary="Buyer explicitly confirmed receipt",
            is_present=True,
            impact=0.20,
            section=4,
            is_critical=True,
        ))

    # Section 5: Digital goods
    if has_access_log and claim_type == ClaimType.INR:
        items.append(EvidenceItem(
            label="Digital Access Log",
            content_summary="Server log showing product accessed" + (" before dispute filing" if access_before_dispute else ""),
            is_present=True,
            impact=0.30 if access_before_dispute else 0.15,
            section=3,
            is_critical=True,
        ))

    # Section 6: Policy compliance
    items.append(EvidenceItem(
        label="Order Confirmation",
        content_summary="Order details showing what was purchased",
        is_present=has_order_confirmation,
        impact=0.05,
        section=6,
    ))
    if claim_type == ClaimType.SNAD:
        items.append(EvidenceItem(
            label="Return Policy",
            content_summary="Return policy available but not used by buyer",
            is_present=has_return_policy,
            impact=0.10,
            section=6,
        ))

    # ── Completeness score ────────────────────────────────────────────────────
    total_impact = sum(e.impact for e in items)
    present_impact = sum(e.impact for e in items if e.is_present)
    completeness = round(present_impact / total_impact, 3) if total_impact > 0 else 0.0

    # ── Win probability estimate ──────────────────────────────────────────────
    base_win = {
        ClaimType.INR: 0.65,
        ClaimType.SNAD: 0.38,
        ClaimType.UNAUTHORIZED: 0.55,
        ClaimType.BILLING: 0.60,
    }.get(claim_type, 0.50)

    win_prob = base_win * completeness + base_win * (1 - completeness) * 0.3
    if sp_fully_met:
        win_prob = max(win_prob, 0.80)
    win_prob = round(min(win_prob, 0.93), 3)

    # ── Highest-impact missing piece ──────────────────────────────────────────
    missing = [e for e in items if not e.is_present]
    missing.sort(key=lambda e: e.impact, reverse=True)
    highest_impact_missing = missing[0].label if missing else None

    # ── Warnings ─────────────────────────────────────────────────────────────
    present_items = [e for e in items if e.is_present]
    if len(present_items) > 8:
        warnings.append(
            "Too many evidence items (>8) can signal desperation to PayPal's AI. "
            "Lead with the 3-4 strongest pieces and reference the rest as 'additional attachments'."
        )

    critical_missing = [e for e in items if e.is_critical and not e.is_present]
    for cm in critical_missing:
        warnings.append(f"CRITICAL missing: {cm.label} — this is likely a losing case without it")

    # ── Build cover letter ────────────────────────────────────────────────────
    cover_letter = _build_cover_letter(
        claim_type, dispute_amount, sp_statement, sp_fully_met,
        [e for e in items if e.is_present], tracking_shows_delivered,
        buyer_acknowledged_receipt, has_access_log, access_before_dispute,
        has_listing_screenshot, has_item_photos,
    )

    # Sort items by section then by impact (descending)
    items.sort(key=lambda e: (e.section, -e.impact))

    return EvidencePackage(
        claim_type=claim_type,
        completeness=completeness,
        win_probability_estimate=win_prob,
        cover_letter=cover_letter,
        ordered_evidence=items,
        highest_impact_missing=highest_impact_missing,
        warnings=warnings,
        sp_statement=sp_statement,
        reasoning=reasoning,
    )


def _build_cover_letter(
    claim_type: ClaimType,
    amount: float,
    sp_statement: str,
    sp_met: bool,
    present_items: list[EvidenceItem],
    delivered: bool,
    acknowledged: bool,
    has_access_log: bool,
    access_before: bool,
    has_listing: bool,
    has_photos: bool,
) -> str:
    """
    Build the cover letter. Keep under 2000 characters.
    PayPal's AI scores the first paragraph most heavily.
    """
    paragraphs = []

    # Lead with SP if applicable
    if sp_statement:
        paragraphs.append(sp_statement)

    # Core fulfillment claim
    if claim_type in (ClaimType.INR, ClaimType.UNAUTHORIZED):
        if delivered:
            paragraphs.append(
                "The item was delivered to the buyer. Attached is carrier tracking "
                "confirmation showing DELIVERED status with the delivery date and address."
            )
        elif has_access_log and access_before:
            paragraphs.append(
                "Server logs confirm the buyer accessed the digital product prior to "
                "filing this dispute. Access logs with timestamps are attached."
            )
        if acknowledged:
            paragraphs.append(
                "Additionally, the buyer directly acknowledged receipt in prior "
                "communication, which is attached."
            )

    elif claim_type == ClaimType.SNAD:
        if has_listing and has_photos:
            paragraphs.append(
                "The item shipped matches the listing description exactly. "
                "Attached: (1) the original listing as it appeared at time of sale, "
                "(2) photos of the item shipped, showing all described specifications."
            )
        elif has_listing:
            paragraphs.append(
                "The original listing is attached. The item shipped matches the "
                "description in full detail."
            )

    # Evidence summary
    if present_items:
        evidence_labels = [e.label for e in present_items[:5]]
        paragraphs.append(
            f"Evidence attached: {', '.join(evidence_labels)}."
            + (" Additional documentation available on request." if len(present_items) > 5 else "")
        )

    paragraphs.append(
        "We respectfully request that this dispute be resolved in our favor "
        "based on the evidence submitted."
    )

    full_text = " ".join(paragraphs)
    # Hard truncate at 1900 chars to stay safe of PayPal's limit
    if len(full_text) > 1900:
        full_text = full_text[:1897] + "..."

    return full_text


if __name__ == "__main__":
    pkg = build_evidence_package(
        ClaimType.INR,
        dispute_amount=180.0,
        paid_via_paypal=True,
        shipped_to_paypal_address=True,
        has_online_tracking=True,
        tracking_shows_delivered=True,
        has_tracking_number=True,
        carrier_name="USPS",
        delivery_date="2026-10-28",
        delivery_address_matches=True,
        has_buyer_messages=True,
        has_order_confirmation=True,
    )
    print(f"Completeness: {pkg.completeness:.0%}")
    print(f"Win probability: {pkg.win_probability_estimate:.0%}")
    print(f"Highest impact missing: {pkg.highest_impact_missing}")
    for w in pkg.warnings:
        print(f"  ⚠ {w}")
    print(f"\nCover letter ({len(pkg.cover_letter)} chars):")
    print(pkg.cover_letter)
