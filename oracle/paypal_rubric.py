"""
PayPal Rubric Engine — model PayPal's own evidence scoring rubric exactly.

PayPal's review AI is powerful: trained on billions of transactions, it has
network-wide visibility — device fingerprints, IP reputation, login patterns,
card BIN data, graph neural networks, velocity signals. We can't replicate it.

The winning strategy is NOT to outsmart it. It is to speak its own language:
submit evidence that scores highest on PayPal's known grading criteria, hit
every Seller Protection requirement automatically, and present proof in the
exact format their machine reader expects.

This module encodes:
  1. Seller Protection compliance checker — are all SP requirements met?
  2. Per-claim evidence requirements — what PayPal's AI actually checks for each type
  3. Evidence quality scorer — how complete and credible is each proof element?
  4. Submission optimizer — what to include, in what order, with what language

Sources: PayPal Seller Protection policy (2024/2025), PayPal Resolution Center
guidance, PayPal merchant help center, known dispute outcome patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ClaimType(str, Enum):
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED"
    SIGNIFICANTLY_NOT_AS_DESCRIBED = "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    UNAUTHORIZED = "UNAUTHORIZED"
    BILLING_ISSUE = "BILLING_ISSUE"


class ProtectionStatus(str, Enum):
    FULLY_COVERED = "FULLY_COVERED"       # All SP requirements met — strongest position
    PARTIALLY_COVERED = "PARTIALLY_COVERED"  # Some requirements met — fight with caveats
    NOT_COVERED = "NOT_COVERED"           # SP doesn't apply — fight on evidence merits only
    INELIGIBLE = "INELIGIBLE"             # Category excluded from SP (digital goods, etc.)


@dataclass
class SellerProtectionRequirement:
    name: str
    met: bool
    critical: bool          # If critical and not met, SP protection is void
    description: str
    remediation: str = ""   # What to do if not met


@dataclass
class SellerProtectionResult:
    status: ProtectionStatus
    requirements: list[SellerProtectionRequirement] = field(default_factory=list)
    critical_gaps: list[str] = field(default_factory=list)
    sp_note: str = ""

    @property
    def is_auto_win_eligible(self) -> bool:
        return self.status == ProtectionStatus.FULLY_COVERED


@dataclass
class EvidenceRequirement:
    name: str
    required: bool          # True = PayPal expects this for the claim type
    have_it: bool           # True = merchant has this evidence
    weight: float           # 0.0–1.0, how much this matters to PayPal's AI
    submission_format: str  # Exact format PayPal's machine reader expects
    language_hint: str      # Language pattern that correlates with wins


@dataclass
class RubricScore:
    """
    How well the merchant's evidence package aligns with PayPal's rubric.

    score 0.9–1.0: Near-perfect — all required evidence present in correct format
    score 0.7–0.9: Strong — missing minor items; likely win
    score 0.5–0.7: Moderate — missing important items; fight carefully
    score 0.0–0.5: Weak — critical gaps; consider accepting
    """
    score: float                            # 0.0–1.0 rubric alignment
    seller_protection: SellerProtectionResult
    evidence_requirements: list[EvidenceRequirement] = field(default_factory=list)
    missing_critical: list[str] = field(default_factory=list)
    missing_helpful: list[str] = field(default_factory=list)
    submission_order: list[str] = field(default_factory=list)   # Optimal submission order
    evidence_note: str = ""                 # Exact note text optimized for PayPal's AI
    win_probability_boost: float = 0.0      # Additive boost to Oracle base probability
    reasoning: list[str] = field(default_factory=list)


# ── Seller Protection requirements ───────────────────────────────────────────

# PayPal Seller Protection applies automatically when ALL of these are met.
# A fully-covered dispute is effectively an auto-win for INR and UNAUTHORIZED.
# Source: paypal.com/us/webapps/mpp/ua/seller-protection

SP_BASE_REQUIREMENTS = [
    "paypal_payment",           # Paid through PayPal (not offline / invoice)
    "us_account",               # Seller account based in eligible country
    "single_payment",           # Single full-amount transaction (not split)
    "not_excluded_category",    # Item not on exclusion list (vehicles, real estate, etc.)
]

# Tangible goods (physical items) — additional requirements
SP_TANGIBLE_REQUIREMENTS = [
    "shipped_to_paypal_address",    # Shipped to the address PayPal provided at checkout
    "online_tracking",              # Shipment tracked via online carrier (USPS, FedEx, UPS, DHL, etc.)
    "tracking_shows_delivered",     # Tracking status = DELIVERED (not in-transit)
]

# High-value items (>$750 for INR, >$750 for UNAUTHORIZED)
SP_HIGH_VALUE_REQUIREMENTS = [
    "signature_confirmation",   # Carrier signature confirmation required by PayPal
]

# Categories excluded from Seller Protection
SP_EXCLUDED_CATEGORIES = {
    "real_estate", "vehicles", "custom_made_items",
    "industrial_machinery", "aircraft", "precious_metals_bullion",
    "financial_instruments", "gambling", "cryptocurrency",
}

# ── Per-claim evidence requirements ──────────────────────────────────────────

# What PayPal's review system actually checks, by claim type.
# Each entry maps: (evidence_key, weight, required, submission_format, language_hint)

CLAIM_EVIDENCE_REQUIREMENTS = {
    ClaimType.ITEM_NOT_RECEIVED: [
        ("tracking_number",         0.35, True,
         "Carrier name + tracking number on first line of note",
         "Tracking number [CARRIER] [NUMBER] shows delivery to buyer's address on [DATE]"),
        ("delivery_confirmation",   0.30, True,
         "Screenshot or PDF of carrier tracking page showing DELIVERED status + date + address",
         "Attached: carrier tracking page showing DELIVERED status on [DATE] to [CITY, STATE ZIP]"),
        ("ship_date",               0.10, True,
         "Date item shipped (must be within 7 days of payment for SP coverage)",
         "Item shipped on [DATE], [X] days after payment on [DATE]"),
        ("carrier_name",            0.08, True,
         "Name carrier (USPS, FedEx, UPS, DHL, etc.) — exact match to tracking URL domain",
         "Shipped via [CARRIER]"),
        ("buyer_address_match",     0.10, True,
         "Confirm ship-to address matches PayPal-provided address",
         "Shipped to PayPal-confirmed address: [ADDRESS] — matches checkout address exactly"),
        ("signature_confirmation",  0.07, False,  # Required only >$750
         "Signature confirmation PDF — shows recipient name + date + carrier reference",
         "Signature confirmation attached (required for $[AMOUNT] transaction)"),
    ],

    ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED: [
        ("item_description_proof",  0.30, True,
         "Original listing screenshot/PDF showing exact item description at time of sale",
         "Attached: original listing as it appeared on [DATE] — description matches item shipped"),
        ("buyer_communication",     0.25, True,
         "Full message thread (PDF or screenshot) showing buyer accepted or did not complain",
         "Attached: full message history — buyer confirmed receipt on [DATE] with no complaint"),
        ("photos_of_item",          0.20, True,
         "Photos of actual item shipped — must match listing description exactly",
         "Attached: photos of item shipped — all specifications match listing description"),
        ("return_policy",           0.10, True,
         "Screenshot or text of published return policy at time of sale",
         "Return policy at time of sale: [POLICY TEXT] — buyer did not follow return process"),
        ("item_condition_match",    0.08, False,
         "Condition stated in listing vs. condition item was shipped in",
         "Item condition as listed: [CONDITION]. Item shipped in [CONDITION] matching listing"),
        ("no_prior_complaint",      0.07, False,
         "Absence of prior complaints from this buyer or similar buyers",
         "No prior complaints received about this item or seller"),
    ],

    ClaimType.UNAUTHORIZED: [
        ("3ds_authentication",      0.40, False,  # If present = very strong; if absent = very weak
         "3DS authentication confirmation from payment processor",
         "Transaction authenticated via 3D Secure (Verified by Visa / Mastercard SecureCode)"),
        ("buyer_communication",     0.25, True,
         "Any message from buyer referencing the order (confirms they knew about it)",
         "Attached: message from buyer on [DATE] referencing order [ID] — confirms buyer awareness"),
        ("login_ip_match",          0.15, False,
         "IP address at checkout matches buyer's account history",
         "Transaction IP [IP] matches buyer's account login history"),
        ("delivery_to_account_address", 0.10, True,
         "Item delivered to address on buyer's PayPal account (not a different address)",
         "Delivered to address registered to buyer's PayPal account on [DATE]"),
        ("order_history",           0.10, False,
         "Prior orders from same buyer — no prior chargebacks",
         "Buyer has [N] prior orders with no chargebacks or disputes"),
    ],

    ClaimType.BILLING_ISSUE: [
        ("transaction_record",      0.30, True,
         "Transaction record showing amount, date, description",
         "Transaction record: [AMOUNT] on [DATE] for [DESCRIPTION]"),
        ("service_delivery_proof",  0.25, True,
         "Proof service/subscription was delivered and active",
         "Service delivered: [PROOF TYPE] showing access/delivery on [DATE]"),
        ("terms_of_service",        0.20, True,
         "Terms of service screenshot showing billing terms buyer agreed to",
         "Attached: ToS showing recurring billing terms — buyer agreed on [DATE]"),
        ("cancellation_policy",     0.15, False,
         "Cancellation policy and whether buyer cancelled before billing",
         "Cancellation policy: [POLICY]. No cancellation received before [DATE]"),
        ("refund_already_issued",   0.10, False,
         "If partial refund issued, include proof",
         "Partial refund of $[AMOUNT] issued on [DATE] — transaction reference [ID]"),
    ],
}


def check_seller_protection(
    claim_type: ClaimType,
    *,
    paid_via_paypal: bool = True,
    us_or_eligible_account: bool = True,
    is_tangible_goods: bool = True,
    shipped_to_paypal_address: bool = False,
    has_online_tracking: bool = False,
    tracking_shows_delivered: bool = False,
    dispute_amount: float = 0.0,
    has_signature_confirmation: bool = False,
    item_category: str = "",
) -> SellerProtectionResult:
    """
    Check whether a dispute qualifies for PayPal Seller Protection.

    SP is binary: either fully covered (auto-win for INR and UNAUTH) or not.
    The goal is to verify ALL requirements are met before filing a response,
    because a SP-eligible dispute that lacks one requirement loses that shield.
    """
    requirements = []
    critical_gaps = []

    # Category exclusion check first
    if item_category.lower() in SP_EXCLUDED_CATEGORIES:
        return SellerProtectionResult(
            status=ProtectionStatus.INELIGIBLE,
            sp_note=f"Item category '{item_category}' is excluded from PayPal Seller Protection",
        )

    # SP doesn't apply to SNAD claims — only INR and UNAUTHORIZED
    if claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        return SellerProtectionResult(
            status=ProtectionStatus.NOT_COVERED,
            sp_note="Seller Protection does not cover SNAD claims. Fight on evidence merits.",
        )

    if claim_type == ClaimType.BILLING_ISSUE:
        return SellerProtectionResult(
            status=ProtectionStatus.NOT_COVERED,
            sp_note="Seller Protection does not cover billing issue claims. Fight on evidence merits.",
        )

    # Base requirements
    req_paypal = SellerProtectionRequirement(
        name="Paid via PayPal",
        met=paid_via_paypal,
        critical=True,
        description="Transaction completed through PayPal checkout",
        remediation="Cannot retroactively establish — if not paid via PayPal, SP doesn't apply",
    )
    requirements.append(req_paypal)
    if not paid_via_paypal:
        critical_gaps.append("Payment not through PayPal — SP void")

    req_eligible = SellerProtectionRequirement(
        name="Eligible seller account",
        met=us_or_eligible_account,
        critical=True,
        description="Seller account in US or other SP-eligible country",
        remediation="Check account country in PayPal profile settings",
    )
    requirements.append(req_eligible)
    if not us_or_eligible_account:
        critical_gaps.append("Seller account country not SP-eligible")

    # Tangible goods requirements
    if is_tangible_goods:
        req_address = SellerProtectionRequirement(
            name="Shipped to PayPal address",
            met=shipped_to_paypal_address,
            critical=True,
            description="Item shipped to the exact address PayPal provided at checkout",
            remediation="If shipped to different address (even at buyer request), SP protection is void. Never ship to an address not provided by PayPal.",
        )
        requirements.append(req_address)
        if not shipped_to_paypal_address:
            critical_gaps.append("Shipped to non-PayPal address — SP void")

        req_tracking = SellerProtectionRequirement(
            name="Online tracking",
            met=has_online_tracking,
            critical=True,
            description="Shipment tracked via carrier with online tracking",
            remediation="Always use a carrier with tracking. USPS First Class with tracking, UPS, FedEx, DHL all qualify.",
        )
        requirements.append(req_tracking)
        if not has_online_tracking:
            critical_gaps.append("No online tracking — SP requires trackable shipment")

        req_delivered = SellerProtectionRequirement(
            name="Tracking shows DELIVERED",
            met=tracking_shows_delivered,
            critical=True,
            description="Tracking status is DELIVERED (not 'out for delivery', 'attempted', etc.)",
            remediation="Wait until tracking status changes to DELIVERED before filing response. 'Out for delivery' is not sufficient.",
        )
        requirements.append(req_delivered)
        if not tracking_shows_delivered:
            critical_gaps.append("Tracking not yet showing DELIVERED status")

        # High-value signature requirement
        if dispute_amount > 750:
            req_sig = SellerProtectionRequirement(
                name="Signature confirmation (>$750)",
                met=has_signature_confirmation,
                critical=True,
                description="PayPal requires signature confirmation for transactions over $750",
                remediation="For future orders >$750, always require carrier signature confirmation. Without it, SP is void even if delivered.",
            )
            requirements.append(req_sig)
            if not has_signature_confirmation:
                critical_gaps.append(f"No signature confirmation — required for ${dispute_amount:.0f} transaction")

    # Determine status
    has_critical_gap = any(r.critical and not r.met for r in requirements)

    if has_critical_gap:
        status = ProtectionStatus.NOT_COVERED
        note = (
            "Seller Protection VOID — critical requirements not met: "
            + "; ".join(critical_gaps)
            + ". Fight on evidence merits only."
        )
    else:
        all_met = all(r.met for r in requirements)
        status = ProtectionStatus.FULLY_COVERED if all_met else ProtectionStatus.PARTIALLY_COVERED
        note = (
            "Seller Protection FULLY ACTIVE — all requirements met. "
            "PayPal's policy states SP-eligible INR/UNAUTH disputes are covered."
            if all_met
            else "Seller Protection PARTIAL — some requirements met. Document everything."
        )

    return SellerProtectionResult(
        status=status,
        requirements=requirements,
        critical_gaps=critical_gaps,
        sp_note=note,
    )


def score_rubric(
    claim_type: ClaimType,
    dispute_amount: float,
    *,
    # Evidence availability flags
    has_tracking_number: bool = False,
    has_delivery_confirmation: bool = False,
    has_buyer_communication: bool = False,
    has_item_description_proof: bool = False,
    has_photos: bool = False,
    has_return_policy: bool = False,
    has_signature_confirmation: bool = False,
    has_3ds_authentication: bool = False,
    has_service_delivery_proof: bool = False,
    has_terms_of_service: bool = False,
    # SP inputs
    shipped_to_paypal_address: bool = False,
    has_online_tracking: bool = False,
    tracking_shows_delivered: bool = False,
    paid_via_paypal: bool = True,
    is_tangible_goods: bool = True,
    us_or_eligible_account: bool = True,
) -> RubricScore:
    """
    Score how well the merchant's evidence package aligns with PayPal's rubric.

    Returns a RubricScore with the optimal submission order, evidence note text,
    and a win probability boost based on rubric alignment.
    """
    reasoning = []

    # ── 1. Seller Protection check ───────────────────────────────────────────
    sp_result = check_seller_protection(
        claim_type,
        paid_via_paypal=paid_via_paypal,
        us_or_eligible_account=us_or_eligible_account,
        is_tangible_goods=is_tangible_goods,
        shipped_to_paypal_address=shipped_to_paypal_address,
        has_online_tracking=has_online_tracking,
        tracking_shows_delivered=tracking_shows_delivered,
        dispute_amount=dispute_amount,
        has_signature_confirmation=has_signature_confirmation,
    )

    if sp_result.is_auto_win_eligible:
        reasoning.append("Seller Protection FULLY ACTIVE — strongest possible position")
    elif sp_result.critical_gaps:
        for gap in sp_result.critical_gaps:
            reasoning.append(f"⚠ SP gap: {gap}")

    # ── 2. Score evidence requirements ──────────────────────────────────────
    evidence_map = {
        "tracking_number": has_tracking_number,
        "delivery_confirmation": has_delivery_confirmation,
        "buyer_communication": has_buyer_communication,
        "item_description_proof": has_item_description_proof,
        "photos_of_item": has_photos,
        "return_policy": has_return_policy,
        "signature_confirmation": has_signature_confirmation,
        "3ds_authentication": has_3ds_authentication,
        "service_delivery_proof": has_service_delivery_proof,
        "terms_of_service": has_terms_of_service,
        # Derived from SP inputs
        "ship_date": is_tangible_goods,
        "carrier_name": has_tracking_number,
        "buyer_address_match": shipped_to_paypal_address,
        "delivery_to_account_address": tracking_shows_delivered and shipped_to_paypal_address,
        "transaction_record": paid_via_paypal,
    }

    claim_reqs = CLAIM_EVIDENCE_REQUIREMENTS.get(claim_type, [])
    ev_requirements = []
    missing_critical = []
    missing_helpful = []
    weighted_score = 0.0
    total_weight = 0.0

    for (key, weight, required, fmt, lang) in claim_reqs:
        have_it = evidence_map.get(key, False)
        # Signature confirmation: only required when dispute_amount > 750
        if key == "signature_confirmation":
            required = dispute_amount > 750
        er = EvidenceRequirement(
            name=key,
            required=required,
            have_it=have_it,
            weight=weight,
            submission_format=fmt,
            language_hint=lang,
        )
        ev_requirements.append(er)
        total_weight += weight
        if have_it:
            weighted_score += weight
        elif required:
            missing_critical.append(key)
        else:
            missing_helpful.append(key)

    base_rubric_score = weighted_score / total_weight if total_weight > 0 else 0.0

    # ── 3. SP bonus ──────────────────────────────────────────────────────────
    sp_bonus = 0.0
    if sp_result.status == ProtectionStatus.FULLY_COVERED:
        sp_bonus = 0.15   # SP-eligible = strong systematic win signal
        base_rubric_score = min(base_rubric_score + sp_bonus, 1.0)
        reasoning.append("Seller Protection bonus: +0.15 rubric score")
    elif sp_result.status == ProtectionStatus.PARTIALLY_COVERED:
        sp_bonus = 0.05
        base_rubric_score = min(base_rubric_score + sp_bonus, 1.0)

    # ── 4. Win probability boost ─────────────────────────────────────────────
    # Map rubric score to a win probability boost for the Oracle
    if base_rubric_score >= 0.90:
        win_boost = 0.20
    elif base_rubric_score >= 0.75:
        win_boost = 0.12
    elif base_rubric_score >= 0.60:
        win_boost = 0.06
    elif base_rubric_score >= 0.40:
        win_boost = 0.0
    else:
        win_boost = -0.10  # Weak rubric = penalty

    reasoning.append(
        f"Rubric score: {base_rubric_score:.0%} → win probability boost: {win_boost:+.0%}"
    )

    if missing_critical:
        reasoning.append(f"Missing critical evidence: {', '.join(missing_critical)}")
    if missing_helpful:
        reasoning.append(f"Optional evidence to add: {', '.join(missing_helpful)}")

    # ── 5. Optimal submission order ───────────────────────────────────────────
    # PayPal's machine reader weights items in submission order:
    # Put the strongest proof first (tracking/delivery), then supporting evidence
    submission_order = _build_submission_order(claim_type, evidence_map, dispute_amount)

    # ── 6. Build optimized evidence note ─────────────────────────────────────
    evidence_note = _build_evidence_note(claim_type, evidence_map, sp_result, dispute_amount)

    return RubricScore(
        score=round(base_rubric_score, 3),
        seller_protection=sp_result,
        evidence_requirements=ev_requirements,
        missing_critical=missing_critical,
        missing_helpful=missing_helpful,
        submission_order=submission_order,
        evidence_note=evidence_note,
        win_probability_boost=round(win_boost, 3),
        reasoning=reasoning,
    )


def _build_submission_order(
    claim_type: ClaimType,
    evidence_map: dict,
    dispute_amount: float,
) -> list[str]:
    """
    Build the optimal order to submit evidence to PayPal.

    PayPal's review system reads evidence in submission order. Put the most
    decisive proof first — the proof that by itself, if true, would settle the
    dispute. Supporting evidence follows. Notes and explanations last.
    """
    order = []

    if claim_type == ClaimType.ITEM_NOT_RECEIVED:
        if evidence_map.get("delivery_confirmation"):
            order.append("DELIVERY_CONFIRMATION — carrier tracking screenshot/PDF showing DELIVERED")
        if evidence_map.get("tracking_number"):
            order.append("TRACKING_NUMBER — carrier + number on first line")
        if dispute_amount > 750 and evidence_map.get("signature_confirmation"):
            order.append("SIGNATURE_CONFIRMATION — required for transactions >$750")
        if evidence_map.get("buyer_address_match"):
            order.append("ADDRESS_MATCH — confirm shipped to PayPal-provided address")
        order.append("PROOF_OF_SHIPMENT — ship date, weight, dimensions if available")

    elif claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        if evidence_map.get("item_description_proof"):
            order.append("ITEM_DESCRIPTION — original listing as it appeared at time of sale")
        if evidence_map.get("photos_of_item"):
            order.append("PROOF_OF_ITEM_SHIPPED — photos showing item matches description")
        if evidence_map.get("buyer_communication"):
            order.append("BUYER_COMMUNICATION — full message thread showing buyer received item")
        if evidence_map.get("return_policy"):
            order.append("RETURN_POLICY — published policy buyer agreed to at checkout")
        order.append("PROOF_OF_FULFILLMENT — invoice and shipping confirmation")

    elif claim_type == ClaimType.UNAUTHORIZED:
        if evidence_map.get("3ds_authentication"):
            order.append("PROOF_OF_AUTHORIZATION — 3DS authentication confirmation (strongest possible)")
        if evidence_map.get("buyer_communication"):
            order.append("BUYER_COMMUNICATION — message proving buyer awareness of transaction")
        if evidence_map.get("delivery_to_account_address"):
            order.append("DELIVERY_CONFIRMATION — delivered to PayPal account address")
        if evidence_map.get("tracking_number"):
            order.append("TRACKING_NUMBER — carrier + number")

    elif claim_type == ClaimType.BILLING_ISSUE:
        if evidence_map.get("service_delivery_proof"):
            order.append("PROOF_OF_FULFILLMENT — service/subscription delivery confirmation")
        if evidence_map.get("terms_of_service"):
            order.append("TERMS_OF_SERVICE — billing terms buyer agreed to at signup")
        if evidence_map.get("transaction_record"):
            order.append("TRANSACTION_RECORD — payment record with amount, date, description")
        order.append("BILLING_AGREEMENT — confirmation of buyer's subscription agreement")

    return order


def _build_evidence_note(
    claim_type: ClaimType,
    evidence_map: dict,
    sp_result: SellerProtectionResult,
    dispute_amount: float,
) -> str:
    """
    Build an evidence note optimized for PayPal's AI review system.

    Language patterns that correlate with PayPal ruling for the merchant:
    - Use exact phrase "PayPal Seller Protection" when SP is active
    - Lead with the strongest proof statement
    - Use carrier-exact language ("tracking shows DELIVERED", not "it was sent")
    - For SNAD: contrast "as listed" vs "as shipped" with specific details
    - Avoid defensive language ("I believe", "I think", "probably")
    - Use dates and amounts precisely
    """
    lines = []

    if sp_result.is_auto_win_eligible:
        lines.append(
            "This transaction qualifies for PayPal Seller Protection. "
            "All Seller Protection requirements have been met."
        )

    if claim_type == ClaimType.ITEM_NOT_RECEIVED:
        if evidence_map.get("delivery_confirmation") and evidence_map.get("tracking_number"):
            lines.append(
                "Item was delivered. Carrier tracking shows status DELIVERED. "
                "Tracking number and carrier delivery confirmation are attached."
            )
        elif evidence_map.get("tracking_number"):
            lines.append(
                "Shipment tracking information is attached. "
                "Tracking number shows the item was shipped to the buyer's address."
            )
        if evidence_map.get("buyer_address_match"):
            lines.append(
                "Item was shipped to the exact address provided by PayPal at checkout. "
                "The shipping address matches the PayPal-confirmed delivery address."
            )
        if dispute_amount > 750 and evidence_map.get("signature_confirmation"):
            lines.append(
                "Signature confirmation is attached as required for this transaction amount. "
                "Recipient signature is on file with the carrier."
            )

    elif claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        lines.append(
            "The item was accurately described in the listing and matches what was shipped. "
            "The original listing is attached."
        )
        if evidence_map.get("photos_of_item"):
            lines.append(
                "Photos of the item shipped are attached. "
                "The item condition and specifications match the listing description exactly."
            )
        if evidence_map.get("buyer_communication"):
            lines.append(
                "Communication with the buyer is attached. "
                "The buyer confirmed receipt and raised no complaint about the item at time of delivery."
            )
        if evidence_map.get("return_policy"):
            lines.append(
                "Our return policy, which was available at time of purchase, is attached. "
                "The buyer did not follow the return process before filing this dispute."
            )

    elif claim_type == ClaimType.UNAUTHORIZED:
        if evidence_map.get("3ds_authentication"):
            lines.append(
                "This transaction was authenticated via 3D Secure (Verified by Visa / Mastercard SecureCode). "
                "Authentication confirmation is attached. The cardholder authorized this payment."
            )
        elif evidence_map.get("buyer_communication"):
            lines.append(
                "Communication with the buyer referencing this order is attached. "
                "The buyer was aware of and engaged with this transaction."
            )
        if evidence_map.get("delivery_to_account_address"):
            lines.append(
                "Item was delivered to the address registered to the buyer's PayPal account. "
                "Delivery confirmation is attached."
            )

    elif claim_type == ClaimType.BILLING_ISSUE:
        if evidence_map.get("service_delivery_proof"):
            lines.append(
                "Service was delivered as agreed. Proof of delivery and access is attached."
            )
        if evidence_map.get("terms_of_service"):
            lines.append(
                "The billing terms are attached. The buyer agreed to these terms at time of subscription. "
                "The charge is in accordance with the agreed billing schedule."
            )

    if not lines:
        lines.append("Evidence of fulfillment is attached. Please review the attached documentation.")

    return " ".join(lines)


# ── Quick sanity check ────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Scenario: $850 INR — fully SP-eligible
    result = score_rubric(
        ClaimType.ITEM_NOT_RECEIVED,
        dispute_amount=850.0,
        has_tracking_number=True,
        has_delivery_confirmation=True,
        has_signature_confirmation=True,
        shipped_to_paypal_address=True,
        has_online_tracking=True,
        tracking_shows_delivered=True,
        paid_via_paypal=True,
        is_tangible_goods=True,
    )
    print(f"\nScenario: $850 INR — fully SP-eligible")
    print(f"  Rubric score: {result.score:.0%}")
    print(f"  SP status: {result.seller_protection.status.value}")
    print(f"  Win prob boost: {result.win_probability_boost:+.0%}")
    print(f"  Submission order:")
    for item in result.submission_order:
        print(f"    • {item}")
    print(f"  Evidence note:\n    {result.evidence_note}")
    for r in result.reasoning:
        print(f"  → {r}")

    # Scenario: $850 INR — missing signature confirmation
    result2 = score_rubric(
        ClaimType.ITEM_NOT_RECEIVED,
        dispute_amount=850.0,
        has_tracking_number=True,
        has_delivery_confirmation=True,
        has_signature_confirmation=False,  # Missing!
        shipped_to_paypal_address=True,
        has_online_tracking=True,
        tracking_shows_delivered=True,
    )
    print(f"\nScenario: $850 INR — missing signature confirmation")
    print(f"  Rubric score: {result2.score:.0%}")
    print(f"  SP status: {result2.seller_protection.status.value}")
    print(f"  Critical gaps: {result2.seller_protection.critical_gaps}")
    print(f"  Missing critical evidence: {result2.missing_critical}")
    for r in result2.reasoning:
        print(f"  → {r}")
