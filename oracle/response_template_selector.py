"""
Response Template Selector — choose the optimal response template for
a dispute based on claim type, network rules, evidence availability,
and strategy (FIGHT / SETTLE / ACCEPT / ESCALATE).

Each template has:
  - A name and description
  - Required evidence fields (must be filled before submission)
  - Tone guidance
  - Suggested opening sentence
  - Key points to include

This module is consumed by the auto-responder to populate the
template fields with dispute-specific data.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseStrategy(str, Enum):
    FIGHT    = "FIGHT"
    SETTLE   = "SETTLE"
    ACCEPT   = "ACCEPT"
    ESCALATE = "ESCALATE"    # CE3.0 / special network path


class TemplateTone(str, Enum):
    FORMAL       = "FORMAL"
    FIRM         = "FIRM"
    COOPERATIVE  = "COOPERATIVE"
    DE_ESCALATE  = "DE_ESCALATE"


@dataclass
class TemplateSelectionInput:
    """Input for template selection."""
    dispute_id: str
    claim_type: str              # INR / UNAUTHORIZED / SNAD / SUBSCRIPTION / DUPLICATE
    network: str                 # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    strategy: ResponseStrategy

    # Evidence available
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_order_confirmation: bool = False
    has_ip_log: bool = False
    has_ce30_evidence: bool = False

    # Buyer signals
    buyer_hostile: bool = False
    buyer_first_dispute: bool = True

    disputed_amount: float = 0.0
    sp_eligible: bool = False


@dataclass
class ResponseTemplate:
    """A fully specified response template."""
    template_id: str
    name: str
    tone: TemplateTone
    strategy: ResponseStrategy

    opening_sentence: str
    key_points: list[str]
    required_fields: list[str]      # Fields that must be filled before submission
    optional_fields: list[str]      # Recommended but not required

    network_specific_notes: str
    submission_checklist: list[str]


_CE30_NOTES = (
    "CE3.0 (Compelling Evidence 3.0) applies: Visa only. Attach 2+ prior "
    "non-disputed transactions from the same buyer to prove familiarity."
)

_TEMPLATES: dict[str, ResponseTemplate] = {

    "INR_FIGHT_FULL": ResponseTemplate(
        template_id="INR_FIGHT_FULL",
        name="INR Full Defense",
        tone=TemplateTone.FIRM,
        strategy=ResponseStrategy.FIGHT,
        opening_sentence=(
            "We write in response to Dispute {dispute_id} filed on {dispute_date}. "
            "The order was fulfilled and delivered as confirmed by our carrier records."
        ),
        key_points=[
            "Provide tracking number and carrier confirmation",
            "Include delivery timestamp and recipient address match",
            "If signature required (≥$750): include signed delivery receipt",
        ],
        required_fields=["tracking_number", "carrier_name", "delivery_date", "order_id"],
        optional_fields=["signature_confirmation", "buyer_communication_log"],
        network_specific_notes=(
            "PayPal: attach proof of shipment and delivery in the resolution center. "
            "Visa/MC: submit via your acquirer's dispute portal."
        ),
        submission_checklist=[
            "Tracking number included",
            "Delivery confirmation attached",
            "Order details match disputed transaction",
            "Response filed within deadline",
        ],
    ),

    "INR_FIGHT_PARTIAL": ResponseTemplate(
        template_id="INR_FIGHT_PARTIAL",
        name="INR Partial Defense (Tracking Only)",
        tone=TemplateTone.FIRM,
        strategy=ResponseStrategy.FIGHT,
        opening_sentence=(
            "We confirm order {order_id} was shipped on {ship_date} via {carrier}. "
            "Tracking number {tracking_number} shows the package as in transit."
        ),
        key_points=[
            "Provide tracking number showing shipment",
            "Note that delivery confirmation may not yet be available",
            "Offer to investigate with carrier if dispute persists",
        ],
        required_fields=["tracking_number", "carrier_name", "ship_date"],
        optional_fields=["estimated_delivery_date", "buyer_communication_log"],
        network_specific_notes=(
            "Without delivery confirmation, win rate is ~40–55%. "
            "Consider waiting for delivery scan before submitting."
        ),
        submission_checklist=[
            "Tracking number valid and shows movement",
            "Ship date before dispute date",
        ],
    ),

    "UNAUTHORIZED_FIGHT_FULL": ResponseTemplate(
        template_id="UNAUTHORIZED_FIGHT_FULL",
        name="Unauthorized Full Defense",
        tone=TemplateTone.FORMAL,
        strategy=ResponseStrategy.FIGHT,
        opening_sentence=(
            "We contest this unauthorized transaction claim. "
            "Our records show the order was placed and fulfilled for the account holder."
        ),
        key_points=[
            "Provide IP address matching buyer's registered address",
            "Include order history showing prior authorized purchases",
            "Attach login confirmation from account",
            "If CE3.0 eligible: include 2+ prior non-disputed transactions",
        ],
        required_fields=["ip_address_log", "order_history", "account_creation_date"],
        optional_fields=["2fa_confirmation", "device_fingerprint"],
        network_specific_notes=_CE30_NOTES,
        submission_checklist=[
            "IP address log attached",
            "Prior order history included",
            "CE3.0 evidence attached if Visa unauthorized",
            "Account age noted",
        ],
    ),

    "SNAD_FIGHT": ResponseTemplate(
        template_id="SNAD_FIGHT",
        name="SNAD Full Defense",
        tone=TemplateTone.FIRM,
        strategy=ResponseStrategy.FIGHT,
        opening_sentence=(
            "We contest this 'significantly not as described' claim. "
            "The item delivered matches the product listing in all material respects."
        ),
        key_points=[
            "Include original product listing / description",
            "Attach photos showing item matches listing",
            "Note return policy was clearly disclosed",
            "Reference any customer communication about the item",
        ],
        required_fields=["product_listing", "return_policy", "order_id"],
        optional_fields=["product_photos", "buyer_communication"],
        network_specific_notes=(
            "SNAD is NEVER covered by Seller Protection. "
            "Fight only if you have clear product documentation."
        ),
        submission_checklist=[
            "Product listing attached",
            "Return policy disclosed at time of purchase",
            "Listing and delivery match confirmed",
        ],
    ),

    "SETTLE_PARTIAL": ResponseTemplate(
        template_id="SETTLE_PARTIAL",
        name="Partial Settlement Offer",
        tone=TemplateTone.COOPERATIVE,
        strategy=ResponseStrategy.SETTLE,
        opening_sentence=(
            "To resolve this dispute amicably, we are prepared to offer a partial "
            "refund of ${offer_amount} as a goodwill gesture."
        ),
        key_points=[
            "State the settlement amount clearly",
            "Reference what was delivered vs. what is disputed",
            "Invite buyer to close the dispute upon receipt of refund",
        ],
        required_fields=["offer_amount", "disputed_amount"],
        optional_fields=["tracking_number", "delivery_confirmation"],
        network_specific_notes=(
            "PayPal: use 'Offer a partial refund' in the resolution center. "
            "Card networks: settlement must be agreed before arbitration."
        ),
        submission_checklist=[
            "Settlement amount within EV threshold",
            "Refund offer sent to buyer",
            "Dispute resolution timeline noted",
        ],
    ),

    "ACCEPT_FULL": ResponseTemplate(
        template_id="ACCEPT_FULL",
        name="Accept Dispute / Issue Refund",
        tone=TemplateTone.COOPERATIVE,
        strategy=ResponseStrategy.ACCEPT,
        opening_sentence=(
            "We accept this dispute and will issue a full refund of "
            "${disputed_amount} to the buyer."
        ),
        key_points=[
            "Issue refund immediately to avoid chargeback fee",
            "Confirm refund reference number for records",
        ],
        required_fields=["disputed_amount", "refund_method"],
        optional_fields=[],
        network_specific_notes=(
            "Issuing a refund before the chargeback is processed avoids the "
            "chargeback fee and does not count against HDR."
        ),
        submission_checklist=[
            "Refund issued before chargeback deadline",
            "Refund reference number recorded",
        ],
    ),

    "ESCALATE_CE30": ResponseTemplate(
        template_id="ESCALATE_CE30",
        name="CE3.0 Escalation (Visa Unauthorized)",
        tone=TemplateTone.FORMAL,
        strategy=ResponseStrategy.ESCALATE,
        opening_sentence=(
            "We submit this response under Compelling Evidence 3.0 (CE3.0) "
            "for Visa unauthorized dispute {dispute_id}."
        ),
        key_points=[
            "Attach 2 prior non-disputed transactions from same buyer/device",
            "IP address match across all 3 transactions",
            "Device fingerprint consistency",
            "Account history showing legitimate prior purchases",
        ],
        required_fields=[
            "prior_transaction_1_id",
            "prior_transaction_2_id",
            "ip_address_log",
            "device_fingerprint",
        ],
        optional_fields=["account_login_history", "2fa_confirmation"],
        network_specific_notes=(
            "CE3.0 is Visa-only, unauthorized dispute codes only (10.4). "
            "Win rate boost: +18pp when CE3.0 qualifies. "
            "Prior transactions must be non-disputed and ≥120 days old."
        ),
        submission_checklist=[
            "Two prior non-disputed transactions attached",
            "IP addresses match across all 3 transactions",
            "Device fingerprint consistent",
            "Response filed via Visa Resolve Online (VROL)",
        ],
    ),
}


def _select_template_id(inp: TemplateSelectionInput) -> str:
    ct = inp.claim_type.upper()
    strat = inp.strategy

    if strat == ResponseStrategy.ESCALATE and inp.has_ce30_evidence:
        return "ESCALATE_CE30"
    if strat == ResponseStrategy.ACCEPT:
        return "ACCEPT_FULL"
    if strat == ResponseStrategy.SETTLE:
        return "SETTLE_PARTIAL"

    # FIGHT strategies
    if ct == "INR":
        if inp.has_delivery_confirmation or inp.has_signature_confirmation:
            return "INR_FIGHT_FULL"
        return "INR_FIGHT_PARTIAL"
    if ct == "UNAUTHORIZED":
        return "UNAUTHORIZED_FIGHT_FULL"
    if ct == "SNAD":
        return "SNAD_FIGHT"
    # Fallback
    return "INR_FIGHT_PARTIAL"


@dataclass
class TemplateSelectionReport:
    """Selected template with dispute-specific notes."""
    dispute_id: str
    template: ResponseTemplate
    readiness_score: float          # 0–1: how many required fields are present
    missing_required_fields: list[str]
    can_submit_now: bool            # True if all required fields present
    warnings: list[str]
    summary: str


def select_response_template(inp: TemplateSelectionInput) -> TemplateSelectionReport:
    """Select the best response template for a dispute."""
    template_id = _select_template_id(inp)
    template = _TEMPLATES[template_id]

    # Score readiness based on available evidence flags
    evidence_map = {
        "tracking_number":       inp.has_tracking_number,
        "delivery_confirmation": inp.has_delivery_confirmation,
        "signature_confirmation": inp.has_signature_confirmation,
        "ip_address_log":        inp.has_ip_log,
        "order_id":              True,   # Always available
        "disputed_amount":       inp.disputed_amount > 0,
        "offer_amount":          inp.disputed_amount > 0,  # Can derive
        "refund_method":         True,   # Always choosable
    }

    present = sum(1 for f in template.required_fields
                  if evidence_map.get(f, False))
    total_req = max(len(template.required_fields), 1)
    readiness = round(present / total_req, 2)

    missing = [f for f in template.required_fields
               if not evidence_map.get(f, False)
               and f not in ("order_id", "refund_method", "offer_amount",
                             "disputed_amount", "carrier_name", "ship_date",
                             "product_listing", "return_policy",
                             "prior_transaction_1_id", "prior_transaction_2_id",
                             "device_fingerprint", "order_history",
                             "account_creation_date")]

    can_submit = len(missing) == 0

    warnings: list[str] = []
    if inp.claim_type.upper() == "SNAD" and inp.sp_eligible:
        warnings.append("SNAD is NOT covered by Seller Protection — fight only with strong product documentation")
    if inp.buyer_hostile and template.tone != TemplateTone.DE_ESCALATE:
        warnings.append("Buyer is hostile — consider DE_ESCALATE tone override")
    if not inp.has_ce30_evidence and inp.network.upper() == "VISA" and inp.claim_type.upper() == "UNAUTHORIZED":
        warnings.append("CE3.0 evidence not available — consider gathering prior transaction records")

    summary = (
        f"Template {inp.dispute_id}: "
        f"template={template_id} | readiness={readiness:.0%} | "
        f"can_submit={can_submit}"
    )

    return TemplateSelectionReport(
        dispute_id=inp.dispute_id,
        template=template,
        readiness_score=readiness,
        missing_required_fields=missing,
        can_submit_now=can_submit,
        warnings=warnings,
        summary=summary,
    )
