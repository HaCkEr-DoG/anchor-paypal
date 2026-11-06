"""
Chargeback Reason Code Classifier — decode card network reason codes into
actionable dispute strategy.

Each card network (Visa, Mastercard, Amex, Discover) has its own set of
chargeback reason codes. The same underlying issue (e.g. "item not received")
maps to different codes across networks. This module:

  1. Identifies the dispute category from a reason code + network
  2. Determines whether Compelling Evidence 3.0 (CE3.0) applies
  3. Maps the code to a PayPal claim type for oracle module routing
  4. Rates the merchant's inherent win difficulty for this code
  5. Lists the specific evidence required for each code

Supported networks: VISA, MASTERCARD, AMEX, DISCOVER

CE3.0 applicability:
  - Visa only
  - Only for unauthorized transaction codes (10.4, 11.x)
  - Requires 2+ prior non-disputed transactions to same billing address / device

Win difficulty scale (1=easy, 5=hard):
  1 — Strong merchant protections (tracking + delivery = win)
  2 — Good evidence usually sufficient
  3 — Mixed; depends heavily on evidence quality
  4 — Difficult; buyer has strong presumption
  5 — Very difficult; near-impossible to win

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CardNetwork(str, Enum):
    VISA       = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX       = "AMEX"
    DISCOVER   = "DISCOVER"
    PAYPAL     = "PAYPAL"       # PayPal internal (not card network)


class DisputeCategory(str, Enum):
    ITEM_NOT_RECEIVED          = "ITEM_NOT_RECEIVED"
    NOT_AS_DESCRIBED           = "NOT_AS_DESCRIBED"
    UNAUTHORIZED               = "UNAUTHORIZED"
    DUPLICATE_CHARGE           = "DUPLICATE_CHARGE"
    CREDIT_NOT_PROCESSED       = "CREDIT_NOT_PROCESSED"
    SUBSCRIPTION_CANCELLED     = "SUBSCRIPTION_CANCELLED"
    GENERAL_INQUIRY            = "GENERAL_INQUIRY"
    OTHER                      = "OTHER"


class PayPalClaimType(str, Enum):
    INR          = "INR"
    SNAD         = "SNAD"
    UNAUTHORIZED = "UNAUTHORIZED"
    SUBSCRIPTION = "SUBSCRIPTION"
    DUPLICATE    = "DUPLICATE"
    OTHER        = "OTHER"


@dataclass
class ReasonCodeInput:
    """Input for chargeback reason code classification."""
    dispute_id: str
    network: CardNetwork
    reason_code: str             # e.g. "10.4", "4853", "C08"
    dispute_amount: float

    # Optional enrichment
    has_prior_transactions_same_buyer: bool = False
    prior_non_disputed_count: int = 0      # For CE3.0 eligibility
    transaction_days_ago: int = 0


@dataclass
class ReasonCodeReport:
    """Classification and strategy for a chargeback reason code."""
    dispute_id: str
    network: CardNetwork
    reason_code: str
    dispute_amount: float

    # Classification
    category: DisputeCategory
    paypal_claim_type: PayPalClaimType
    description: str             # Human-readable description of this code

    # Strategy
    win_difficulty: int          # 1 (easy) – 5 (hard)
    ce30_eligible: bool
    seller_protection_applicable: bool

    # Evidence requirements
    required_evidence: list[str]
    optional_evidence: list[str]

    # Deadlines
    response_deadline_days: int  # Days to respond from chargeback date

    # Guidance
    key_insight: str             # One-line tactical note
    summary: str


# ─────────────────────────────────────────────────────────────
# Code databases
# ─────────────────────────────────────────────────────────────

@dataclass
class _CodeDef:
    category: DisputeCategory
    paypal_type: PayPalClaimType
    description: str
    win_difficulty: int
    sp_applicable: bool
    required_evidence: list[str]
    optional_evidence: list[str]
    response_days: int
    key_insight: str


_VISA_CODES: dict[str, _CodeDef] = {
    "10.1": _CodeDef(
        category=DisputeCategory.OTHER,
        paypal_type=PayPalClaimType.OTHER,
        description="EMV Liability Shift — Counterfeit",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Proof chip card was used", "POS terminal supports EMV"],
        optional_evidence=["Signed receipt"],
        response_days=20,
        key_insight="Requires EMV chip terminal; card-present only",
    ),
    "10.4": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="Other Fraud — Card Absent Environment (CNP fraud)",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Proof of delivery to confirmed address", "AVS/CVV match"],
        optional_evidence=["Device fingerprint", "IP geolocation", "Login history"],
        response_days=20,
        key_insight="CE3.0 eligible with 2+ prior non-disputed transactions",
    ),
    "11.1": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="Card Recovery Bulletin",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Proof card was not on bulletin at time of sale"],
        optional_evidence=[],
        response_days=20,
        key_insight="Difficult to win; card was flagged before sale",
    ),
    "12.1": _CodeDef(
        category=DisputeCategory.DUPLICATE_CHARGE,
        paypal_type=PayPalClaimType.DUPLICATE,
        description="Late Presentment",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Proof presentment was within 30 days of transaction"],
        optional_evidence=["Transaction log"],
        response_days=20,
        key_insight="Simple documentation fix; check transaction timing",
    ),
    "12.5": _CodeDef(
        category=DisputeCategory.CREDIT_NOT_PROCESSED,
        paypal_type=PayPalClaimType.OTHER,
        description="Incorrect Transaction Amount",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Original authorization record showing correct amount"],
        optional_evidence=["Signed receipt"],
        response_days=20,
        key_insight="Show the authorized amount matches the charged amount",
    ),
    "13.1": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="Merchandise / Services Not Received",
        win_difficulty=2,
        sp_applicable=True,
        required_evidence=["Proof of delivery (tracking)", "Delivery confirmation"],
        optional_evidence=["Signature confirmation", "Carrier affidavit"],
        response_days=20,
        key_insight="Tracking showing delivery to confirmed address typically wins",
    ),
    "13.2": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="Cancelled Recurring Transaction",
        win_difficulty=3,
        sp_applicable=False,
        required_evidence=["Proof cancellation was not received", "Terms of service"],
        optional_evidence=["Communication logs"],
        response_days=20,
        key_insight="Show buyer did not cancel or cancellation wasn't processed in time",
    ),
    "13.3": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="Not as Described or Defective Merchandise",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Product listing/description", "Photos of item sent", "Buyer communications"],
        optional_evidence=["Independent condition assessment"],
        response_days=20,
        key_insight="Hardest Visa code to beat; focus on listing accuracy",
    ),
    "13.6": _CodeDef(
        category=DisputeCategory.CREDIT_NOT_PROCESSED,
        paypal_type=PayPalClaimType.OTHER,
        description="Credit Not Processed",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Proof credit was issued", "Refund transaction ID"],
        optional_evidence=["Customer communication confirming refund"],
        response_days=20,
        key_insight="Show the refund was already processed",
    ),
    "13.7": _CodeDef(
        category=DisputeCategory.SUBSCRIPTION_CANCELLED,
        paypal_type=PayPalClaimType.SUBSCRIPTION,
        description="Cancelled Merchandise / Services",
        win_difficulty=3,
        sp_applicable=False,
        required_evidence=["Proof service was delivered", "Terms and cancellation policy"],
        optional_evidence=["Login/usage logs"],
        response_days=20,
        key_insight="Show service was used after alleged cancellation",
    ),
}

_MC_CODES: dict[str, _CodeDef] = {
    "4853": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="Cardholder Dispute — Not as Described",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Product description from listing", "Photos/documentation", "Any buyer communication"],
        optional_evidence=["Return policy", "Quality control records"],
        response_days=45,
        key_insight="Mastercard gives merchants 45 days; use the extra time to build evidence",
    ),
    "4855": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="Goods or Services Not Provided",
        win_difficulty=2,
        sp_applicable=True,
        required_evidence=["Proof of delivery", "Tracking number"],
        optional_evidence=["Signature confirmation", "Delivery photo"],
        response_days=45,
        key_insight="Tracking to confirmed address is usually sufficient",
    ),
    "4859": _CodeDef(
        category=DisputeCategory.SUBSCRIPTION_CANCELLED,
        paypal_type=PayPalClaimType.SUBSCRIPTION,
        description="Addendum, No-Show, or ATM Dispute",
        win_difficulty=3,
        sp_applicable=False,
        required_evidence=["Signed agreement", "Proof service was provided"],
        optional_evidence=["Communication logs", "Terms of service"],
        response_days=45,
        key_insight="Show the agreed service was delivered",
    ),
    "4860": _CodeDef(
        category=DisputeCategory.CREDIT_NOT_PROCESSED,
        paypal_type=PayPalClaimType.OTHER,
        description="Credit Not Processed",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Proof credit was issued"],
        optional_evidence=[],
        response_days=45,
        key_insight="Provide refund transaction ID immediately",
    ),
    "4863": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="Cardholder Does Not Recognize",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Proof cardholder made purchase", "AVS/CVV data", "Delivery proof"],
        optional_evidence=["Prior purchase history", "IP address match"],
        response_days=45,
        key_insight="Softer unauthorized code — prior purchases can help identification",
    ),
    "4870": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="Chip Liability Shift",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Proof EMV chip was used"],
        optional_evidence=[],
        response_days=45,
        key_insight="Card-present only; requires chip terminal evidence",
    ),
}

_AMEX_CODES: dict[str, _CodeDef] = {
    "C02": _CodeDef(
        category=DisputeCategory.CREDIT_NOT_PROCESSED,
        paypal_type=PayPalClaimType.OTHER,
        description="Credit Not Received",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Proof of credit/refund issuance"],
        optional_evidence=["Customer communication"],
        response_days=20,
        key_insight="Prove refund was issued; Amex resolves quickly",
    ),
    "C08": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="Goods / Services Not Received or Only Partially Received",
        win_difficulty=2,
        sp_applicable=True,
        required_evidence=["Proof of delivery", "Tracking"],
        optional_evidence=["Signature confirmation"],
        response_days=20,
        key_insight="Similar to Visa 13.1; tracking to cardholder address wins",
    ),
    "C14": _CodeDef(
        category=DisputeCategory.DUPLICATE_CHARGE,
        paypal_type=PayPalClaimType.DUPLICATE,
        description="Paid by Other Means",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Proof only one payment was processed"],
        optional_evidence=["Transaction log"],
        response_days=20,
        key_insight="Show no duplicate transaction occurred",
    ),
    "C31": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="Goods / Services Not as Described",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Product listing", "Accurate description documentation"],
        optional_evidence=["Photos of item sent", "Return policy"],
        response_days=20,
        key_insight="Amex SNAD — accurate listing documentation is critical",
    ),
    "F24": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="No Cardmember Authorization",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Delivery proof to confirmed address", "AVS/CVV data"],
        optional_evidence=["Device fingerprint", "Purchase history"],
        response_days=20,
        key_insight="Amex unauthorized — delivery proof plus prior transactions help",
    ),
    "F29": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="Card Not Present — Fraud",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Proof of delivery", "AVS match", "CVV match"],
        optional_evidence=["3DS authentication data", "IP geolocation"],
        response_days=20,
        key_insight="3DS authentication greatly strengthens this defense",
    ),
}

_DISCOVER_CODES: dict[str, _CodeDef] = {
    "AA": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="Does Not Recognize",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Delivery proof", "Order details", "IP/device match"],
        optional_evidence=["Prior orders to same address"],
        response_days=20,
        key_insight="Discover code maps similarly to Visa 10.4",
    ),
    "AP": _CodeDef(
        category=DisputeCategory.SUBSCRIPTION_CANCELLED,
        paypal_type=PayPalClaimType.SUBSCRIPTION,
        description="Recurring Payments",
        win_difficulty=3,
        sp_applicable=False,
        required_evidence=["Authorization for recurring billing", "Terms of service"],
        optional_evidence=["Cancellation policy", "Customer communication"],
        response_days=20,
        key_insight="Show valid recurring authorization was obtained",
    ),
    "CD": _CodeDef(
        category=DisputeCategory.DUPLICATE_CHARGE,
        paypal_type=PayPalClaimType.DUPLICATE,
        description="Credit Posted as Charge",
        win_difficulty=2,
        sp_applicable=False,
        required_evidence=["Transaction log showing no duplicate"],
        optional_evidence=[],
        response_days=20,
        key_insight="Straightforward — show correct transaction type",
    ),
    "NF": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="Non-Receipt of Merchandise",
        win_difficulty=2,
        sp_applicable=True,
        required_evidence=["Proof of delivery", "Tracking number"],
        optional_evidence=["Delivery photo", "Signature"],
        response_days=20,
        key_insight="Similar to Visa 13.1; standard INR defense",
    ),
    "RM": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="Quality Discrepancy",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Product description from listing", "Quality evidence"],
        optional_evidence=["Return policy", "Photos of item sent"],
        response_days=20,
        key_insight="SNAD variant; focus on listing accuracy",
    ),
}

_PAYPAL_CODES: dict[str, _CodeDef] = {
    "INR": _CodeDef(
        category=DisputeCategory.ITEM_NOT_RECEIVED,
        paypal_type=PayPalClaimType.INR,
        description="PayPal Item Not Received",
        win_difficulty=2,
        sp_applicable=True,
        required_evidence=["Proof of shipment", "Tracking number showing delivery"],
        optional_evidence=["Signature confirmation for ≥$750", "Carrier affidavit"],
        response_days=10,
        key_insight="Respond within 10 days; tracking to confirmed address wins",
    ),
    "SNAD": _CodeDef(
        category=DisputeCategory.NOT_AS_DESCRIBED,
        paypal_type=PayPalClaimType.SNAD,
        description="PayPal Significantly Not as Described",
        win_difficulty=4,
        sp_applicable=False,
        required_evidence=["Product listing", "Photos of item sent", "Buyer communications"],
        optional_evidence=["Return policy", "Independent quality assessment"],
        response_days=10,
        key_insight="SP does not apply; focus on listing accuracy and buyer comms",
    ),
    "UNAUTHORIZED": _CodeDef(
        category=DisputeCategory.UNAUTHORIZED,
        paypal_type=PayPalClaimType.UNAUTHORIZED,
        description="PayPal Unauthorized Transaction",
        win_difficulty=3,
        sp_applicable=True,
        required_evidence=["Delivery to confirmed address", "Proof of SP eligibility"],
        optional_evidence=["CE3.0 prior transactions", "Device fingerprint"],
        response_days=10,
        key_insight="SP + CE3.0 eligible; prior non-disputed transactions are powerful",
    ),
}


def _lookup(network: CardNetwork, code: str) -> Optional[_CodeDef]:
    db = {
        CardNetwork.VISA:       _VISA_CODES,
        CardNetwork.MASTERCARD: _MC_CODES,
        CardNetwork.AMEX:       _AMEX_CODES,
        CardNetwork.DISCOVER:   _DISCOVER_CODES,
        CardNetwork.PAYPAL:     _PAYPAL_CODES,
    }.get(network, {})
    return db.get(code.upper())


def _ce30_eligible(inp: ReasonCodeInput, defn: _CodeDef) -> bool:
    if inp.network != CardNetwork.VISA:
        return False
    if defn.category != DisputeCategory.UNAUTHORIZED:
        return False
    return inp.prior_non_disputed_count >= 2


def classify_reason_code(inp: ReasonCodeInput) -> ReasonCodeReport:
    """
    Classify a chargeback reason code and produce dispute strategy guidance.
    """
    defn = _lookup(inp.network, inp.reason_code)

    if defn is None:
        # Unknown code — produce generic report
        defn = _CodeDef(
            category=DisputeCategory.OTHER,
            paypal_type=PayPalClaimType.OTHER,
            description=f"Unknown {inp.network.value} reason code: {inp.reason_code}",
            win_difficulty=3,
            sp_applicable=False,
            required_evidence=["Transaction record", "Order details"],
            optional_evidence=["Any relevant communication"],
            response_days=20,
            key_insight="Unknown code — verify with card network documentation",
        )

    ce30 = _ce30_eligible(inp, defn)

    summary = (
        f"Chargeback {inp.dispute_id}: {inp.network.value} {inp.reason_code} | "
        f"category={defn.category.value} | claim_type={defn.paypal_type.value} | "
        f"win_difficulty={defn.win_difficulty}/5 | ce30={ce30} | "
        f"respond_by={defn.response_days}d"
    )

    return ReasonCodeReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        reason_code=inp.reason_code,
        dispute_amount=inp.dispute_amount,
        category=defn.category,
        paypal_claim_type=defn.paypal_type,
        description=defn.description,
        win_difficulty=defn.win_difficulty,
        ce30_eligible=ce30,
        seller_protection_applicable=defn.sp_applicable,
        required_evidence=list(defn.required_evidence),
        optional_evidence=list(defn.optional_evidence),
        response_deadline_days=defn.response_days,
        key_insight=defn.key_insight,
        summary=summary,
    )
