"""
Network Rules Engine — apply card network-specific dispute rules to determine
which evidence standards, deadlines, and dispute codes apply.

Each card network has its own dispute reason code taxonomy and evidence
requirements. This module maps PayPal claim types to network-specific codes
and surfaces the precise evidence checklist the merchant needs to win.

Networks and their dispute code systems:
  Visa:       Dispute Condition codes (10.x, 11.x, 12.x, 13.x)
  Mastercard: Chargeback reason codes (4xxx series)
  Amex:       Dispute codes (C-series, FR, etc.)
  Discover:   Reason codes (UA, AA, CD, etc.)
  PayPal:     Internal claim types (INR, SNAD, UNAUTHORIZED)

CE3.0 (Compelling Evidence 3.0) — Visa-only, unauthorized disputes:
  - Requires 2+ prior non-disputed transactions to same buyer/device/IP
  - Provides ~18pp win rate boost when applicable
  - Specific format requirements for evidence submission

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL      = "PAYPAL"
    VISA        = "VISA"
    MASTERCARD  = "MASTERCARD"
    AMEX        = "AMEX"
    DISCOVER    = "DISCOVER"


class EvidenceRequirementLevel(str, Enum):
    REQUIRED    = "REQUIRED"    # Must have to win
    RECOMMENDED = "RECOMMENDED" # Strongly helps
    OPTIONAL    = "OPTIONAL"    # May support case


@dataclass
class EvidenceRequirement:
    """One piece of evidence required by network rules."""
    name: str
    level: EvidenceRequirementLevel
    description: str
    ce30_specific: bool = False   # True if only needed for CE3.0 submissions


@dataclass
class NetworkDisputeCode:
    """Network-specific dispute/chargeback reason code."""
    network: Network
    code: str
    code_name: str
    description: str
    typical_win_rate: float
    response_window_days: int
    appeal_window_days: int


@dataclass
class NetworkRulesInput:
    """Input for network rules determination."""
    dispute_id: str
    network: str                # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    claim_type: str             # INR / SNAD / UNAUTHORIZED / DUPLICATE

    # CE3.0 eligibility factors (Visa UNAUTHORIZED only)
    prior_undisputed_transactions: int = 0
    same_device_prior_transactions: bool = False
    same_ip_prior_transactions: bool = False

    # Context
    transaction_amount: float = 100.0
    is_card_present: bool = False    # Card-present = different rules
    is_international: bool = False


# ── Network code mappings ──────────────────────────────────────────

_VISA_CODES = {
    "UNAUTHORIZED": NetworkDisputeCode(
        network=Network.VISA, code="10.4",
        code_name="Other Fraud—Card-Absent Environment",
        description="Cardholder claims they did not authorize transaction",
        typical_win_rate=0.58, response_window_days=20, appeal_window_days=45,
    ),
    "INR": NetworkDisputeCode(
        network=Network.VISA, code="13.1",
        code_name="Merchandise/Services Not Received",
        description="Cardholder did not receive goods or services",
        typical_win_rate=0.65, response_window_days=20, appeal_window_days=45,
    ),
    "SNAD": NetworkDisputeCode(
        network=Network.VISA, code="13.3",
        code_name="Not as Described or Defective Merchandise",
        description="Item received but not as described or defective",
        typical_win_rate=0.42, response_window_days=20, appeal_window_days=45,
    ),
    "DUPLICATE": NetworkDisputeCode(
        network=Network.VISA, code="12.6",
        code_name="Duplicate Processing",
        description="Same transaction processed more than once",
        typical_win_rate=0.78, response_window_days=20, appeal_window_days=45,
    ),
}

_MASTERCARD_CODES = {
    "UNAUTHORIZED": NetworkDisputeCode(
        network=Network.MASTERCARD, code="4853",
        code_name="Cardholder Dispute",
        description="Unauthorized transaction or cardholder dispute",
        typical_win_rate=0.55, response_window_days=45, appeal_window_days=45,
    ),
    "INR": NetworkDisputeCode(
        network=Network.MASTERCARD, code="4855",
        code_name="Goods or Services Not Provided",
        description="Cardholder claims goods/services were not provided",
        typical_win_rate=0.62, response_window_days=45, appeal_window_days=45,
    ),
    "SNAD": NetworkDisputeCode(
        network=Network.MASTERCARD, code="4853",
        code_name="Cardholder Dispute—Not as Described",
        description="Goods/services not as described",
        typical_win_rate=0.40, response_window_days=45, appeal_window_days=45,
    ),
    "DUPLICATE": NetworkDisputeCode(
        network=Network.MASTERCARD, code="4834",
        code_name="Duplicate Processing",
        description="Same transaction submitted more than once",
        typical_win_rate=0.80, response_window_days=45, appeal_window_days=45,
    ),
}

_AMEX_CODES = {
    "UNAUTHORIZED": NetworkDisputeCode(
        network=Network.AMEX, code="FR",
        code_name="Fraud—Card Present Environment",
        description="Cardholder claims fraudulent transaction",
        typical_win_rate=0.52, response_window_days=20, appeal_window_days=20,
    ),
    "INR": NetworkDisputeCode(
        network=Network.AMEX, code="C08",
        code_name="Goods/Services Not Received",
        description="Cardholder did not receive ordered goods/services",
        typical_win_rate=0.60, response_window_days=20, appeal_window_days=20,
    ),
    "SNAD": NetworkDisputeCode(
        network=Network.AMEX, code="C31",
        code_name="Goods/Services Not as Described",
        description="Goods/services not as described or defective",
        typical_win_rate=0.38, response_window_days=20, appeal_window_days=20,
    ),
    "DUPLICATE": NetworkDisputeCode(
        network=Network.AMEX, code="C14",
        code_name="Paid by Other Means",
        description="Transaction duplicated or already paid",
        typical_win_rate=0.75, response_window_days=20, appeal_window_days=20,
    ),
}

_DISCOVER_CODES = {
    "UNAUTHORIZED": NetworkDisputeCode(
        network=Network.DISCOVER, code="UA02",
        code_name="Fraud—Card-Not-Present",
        description="Unauthorized card-not-present transaction",
        typical_win_rate=0.54, response_window_days=20, appeal_window_days=20,
    ),
    "INR": NetworkDisputeCode(
        network=Network.DISCOVER, code="CD",
        code_name="Credit Not Processed",
        description="Goods/services not received or credit not issued",
        typical_win_rate=0.60, response_window_days=20, appeal_window_days=20,
    ),
    "SNAD": NetworkDisputeCode(
        network=Network.DISCOVER, code="RM",
        code_name="Quality Dispute",
        description="Merchandise/services not as described",
        typical_win_rate=0.40, response_window_days=20, appeal_window_days=20,
    ),
    "DUPLICATE": NetworkDisputeCode(
        network=Network.DISCOVER, code="AA",
        code_name="Does Not Recognize",
        description="Duplicate or unrecognized transaction",
        typical_win_rate=0.72, response_window_days=20, appeal_window_days=20,
    ),
}

_NETWORK_CODES = {
    "VISA":       _VISA_CODES,
    "MASTERCARD": _MASTERCARD_CODES,
    "AMEX":       _AMEX_CODES,
    "DISCOVER":   _DISCOVER_CODES,
}


def _get_evidence_requirements(
        network: str, claim_type: str, ce30_applicable: bool) -> list[EvidenceRequirement]:
    ct = claim_type.upper()
    n  = network.upper()

    reqs: list[EvidenceRequirement] = []

    # Universal requirements
    reqs.append(EvidenceRequirement(
        name="transaction_record",
        level=EvidenceRequirementLevel.REQUIRED,
        description="PayPal transaction ID and order confirmation",
    ))

    if ct in ("INR",):
        reqs.extend([
            EvidenceRequirement("tracking_number", EvidenceRequirementLevel.REQUIRED,
                                "Carrier tracking number with proof of shipment"),
            EvidenceRequirement("delivery_confirmation", EvidenceRequirementLevel.REQUIRED,
                                "Carrier delivery confirmation or signature proof"),
            EvidenceRequirement("shipping_carrier_logs", EvidenceRequirementLevel.RECOMMENDED,
                                "Carrier-stamped shipping label and scan events"),
        ])

    if ct == "SNAD":
        reqs.extend([
            EvidenceRequirement("product_listing", EvidenceRequirementLevel.REQUIRED,
                                "Original product listing with full description"),
            EvidenceRequirement("item_photos", EvidenceRequirementLevel.RECOMMENDED,
                                "Photos of item at time of shipment"),
            EvidenceRequirement("return_policy", EvidenceRequirementLevel.REQUIRED,
                                "Published return policy visible at checkout"),
            EvidenceRequirement("buyer_communications", EvidenceRequirementLevel.RECOMMENDED,
                                "All communications with buyer regarding the item"),
        ])

    if ct == "UNAUTHORIZED":
        reqs.extend([
            EvidenceRequirement("ip_address_log", EvidenceRequirementLevel.REQUIRED,
                                "IP address and device fingerprint at transaction time"),
            EvidenceRequirement("two_fa_record", EvidenceRequirementLevel.RECOMMENDED,
                                "2FA or identity verification record for the transaction"),
            EvidenceRequirement("order_history", EvidenceRequirementLevel.REQUIRED,
                                "Prior orders from same buyer showing consistent pattern"),
        ])
        if ce30_applicable and n == "VISA":
            reqs.extend([
                EvidenceRequirement(
                    "ce30_prior_transactions",
                    EvidenceRequirementLevel.REQUIRED,
                    "2+ prior non-disputed transactions: same card, device, IP",
                    ce30_specific=True,
                ),
                EvidenceRequirement(
                    "ce30_matching_elements",
                    EvidenceRequirementLevel.REQUIRED,
                    "Proof that device/IP/email matches prior undisputed orders",
                    ce30_specific=True,
                ),
            ])

    if ct == "DUPLICATE":
        reqs.extend([
            EvidenceRequirement("original_transaction", EvidenceRequirementLevel.REQUIRED,
                                "Original transaction record proving single charge"),
            EvidenceRequirement("refund_or_void_record", EvidenceRequirementLevel.RECOMMENDED,
                                "Any refund/void already issued"),
        ])

    # Amex-specific: letter of explanation often required
    if n == "AMEX":
        reqs.append(EvidenceRequirement(
            "explanation_letter", EvidenceRequirementLevel.RECOMMENDED,
            "Formal explanation letter addressing buyer's specific claim",
        ))

    return reqs


@dataclass
class NetworkRulesReport:
    """Network rules applied to a dispute."""
    dispute_id: str
    network: Network
    claim_type: str

    # Dispute code
    dispute_code: Optional[NetworkDisputeCode]
    typical_win_rate: float

    # Evidence requirements
    evidence_requirements: list[EvidenceRequirement]
    required_items: int
    recommended_items: int

    # CE3.0 eligibility (Visa + UNAUTHORIZED)
    ce30_applicable: bool
    ce30_win_rate_boost: float    # 0 or +0.18

    # Deadlines
    response_window_days: int
    appeal_window_days: int

    # Guidance
    network_specific_notes: list[str]
    summary: str


def apply_network_rules(inp: NetworkRulesInput) -> NetworkRulesReport:
    """
    Apply card network dispute rules to determine evidence requirements and win rates.
    """
    n  = inp.network.upper()
    ct = inp.claim_type.upper()

    # Look up dispute code
    code_map = _NETWORK_CODES.get(n, {})
    dispute_code = code_map.get(ct, None)

    if dispute_code:
        typical_win_rate = dispute_code.typical_win_rate
        response_days    = dispute_code.response_window_days
        appeal_days      = dispute_code.appeal_window_days
    else:
        typical_win_rate = 0.50
        response_days    = 10
        appeal_days      = 45

    # CE3.0 eligibility: Visa + UNAUTHORIZED + 2+ prior undisputed txns
    ce30_applicable = (
        n == "VISA"
        and ct == "UNAUTHORIZED"
        and inp.prior_undisputed_transactions >= 2
    )
    ce30_boost = 0.18 if ce30_applicable else 0.0

    evidence_reqs = _get_evidence_requirements(n, ct, ce30_applicable)
    required_items     = sum(1 for r in evidence_reqs
                             if r.level == EvidenceRequirementLevel.REQUIRED)
    recommended_items  = sum(1 for r in evidence_reqs
                             if r.level == EvidenceRequirementLevel.RECOMMENDED)

    # Network-specific notes
    notes: list[str] = []
    if n == "VISA" and ct == "UNAUTHORIZED":
        notes.append("Visa uses Dispute Condition 10.4 — submit via PayPal Resolution Center")
        if ce30_applicable:
            notes.append("CE3.0 eligible — submit matching prior transactions for +18pp win boost")
    if n == "MASTERCARD":
        notes.append("Mastercard 45-day response window is longer than Visa — use the full time")
    if n == "AMEX":
        notes.append("Amex chargebacks tend to favor cardholders — strong evidence is critical")
    if ct == "SNAD":
        notes.append("SNAD disputes require proof description was accurate — photos and listing key")
    if inp.is_international:
        notes.append("International transaction — additional fraud signals may apply")

    summary = (
        f"NetworkRules {inp.dispute_id} [{n}]: "
        f"code={dispute_code.code if dispute_code else 'N/A'} | "
        f"win_rate={typical_win_rate:.0%} | "
        f"ce30={ce30_applicable} (+{ce30_boost:.0%}) | "
        f"response={response_days}d | required_evidence={required_items}"
    )

    return NetworkRulesReport(
        dispute_id=inp.dispute_id,
        network=Network(n),
        claim_type=ct,
        dispute_code=dispute_code,
        typical_win_rate=typical_win_rate,
        evidence_requirements=evidence_reqs,
        required_items=required_items,
        recommended_items=recommended_items,
        ce30_applicable=ce30_applicable,
        ce30_win_rate_boost=ce30_boost,
        response_window_days=response_days,
        appeal_window_days=appeal_days,
        network_specific_notes=notes,
        summary=summary,
    )
