"""
Evidence Gap Analyzer — map a dispute's reason code to required evidence,
score how much is present, and surface critical gaps with collection advice.

For each dispute this oracle computes:
  - Required evidence checklist per reason code + network
  - Completeness score 0–100
  - Critical gaps (evidence that is required but missing)
  - Suggested remediation steps for each gap
  - Overall evidence readiness tier

Evidence types span: shipping/tracking, authentication, communication,
policy display, identity, and transaction history records.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceType(str, Enum):
    # Shipping / delivery
    TRACKING_NUMBER       = "TRACKING_NUMBER"
    DELIVERY_CONFIRMATION = "DELIVERY_CONFIRMATION"
    SIGNATURE_PROOF       = "SIGNATURE_PROOF"
    CARRIER_SCAN_LOG      = "CARRIER_SCAN_LOG"

    # Authentication
    THREE_D_SECURE        = "THREE_D_SECURE"
    CVV_MATCH             = "CVV_MATCH"
    AVS_MATCH             = "AVS_MATCH"
    IP_ADDRESS_LOG        = "IP_ADDRESS_LOG"
    DEVICE_FINGERPRINT    = "DEVICE_FINGERPRINT"

    # Communication
    BUYER_CORRESPONDENCE  = "BUYER_CORRESPONDENCE"
    RESOLUTION_ATTEMPT    = "RESOLUTION_ATTEMPT"

    # Policy / agreement
    TERMS_OF_SERVICE      = "TERMS_OF_SERVICE"
    POLICY_DISPLAY_PROOF  = "POLICY_DISPLAY_PROOF"
    SUBSCRIPTION_TERMS    = "SUBSCRIPTION_TERMS"
    RENEWAL_NOTICE        = "RENEWAL_NOTICE"

    # Product / transaction
    ITEM_LISTING_PHOTOS   = "ITEM_LISTING_PHOTOS"
    INVOICE_RECEIPT       = "INVOICE_RECEIPT"
    TRANSACTION_HISTORY   = "TRANSACTION_HISTORY"
    PRIOR_NON_DISPUTED_TXNS = "PRIOR_NON_DISPUTED_TXNS"


class EvidenceStatus(str, Enum):
    PRESENT     = "PRESENT"
    PARTIAL     = "PARTIAL"
    MISSING     = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"   # Cannot be obtained (e.g., 3DS not run)


class EvidenceReadiness(str, Enum):
    STRONG       = "STRONG"       # Score ≥ 80
    ADEQUATE     = "ADEQUATE"     # Score 60–79
    WEAK         = "WEAK"         # Score 40–59
    INSUFFICIENT = "INSUFFICIENT" # Score < 40


@dataclass
class EvidenceItem:
    """One evidence requirement for a dispute."""
    evidence_type: EvidenceType
    is_required: bool          # False = nice-to-have
    status: EvidenceStatus
    weight: int                # Points contributed when PRESENT (0–20)
    gap_severity: str          # CRITICAL / MODERATE / LOW
    collection_tip: str        # How to get it if missing


@dataclass
class EvidenceGapInput:
    """Input for evidence gap analysis."""
    dispute_id: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / etc.
    network: str                # VISA / MC / PAYPAL / AMEX / DISCOVER
    transaction_amount: float

    # Shipping
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_signature_proof: bool = False
    has_carrier_scan_log: bool = False

    # Auth
    three_d_secure_run: bool = False
    three_d_secure_passed: bool = False
    cvv_matched: bool = False
    avs_matched: bool = False
    has_ip_log: bool = False
    has_device_fingerprint: bool = False

    # Communication
    has_buyer_correspondence: bool = False
    made_resolution_attempt: bool = False

    # Policy
    has_tos_accepted: bool = False
    has_policy_display_proof: bool = False
    has_subscription_terms: bool = False
    has_renewal_notice: bool = False

    # Product / transaction
    has_item_listing_photos: bool = False
    has_invoice: bool = False
    has_transaction_history: bool = False
    prior_non_disputed_txn_count: int = 0


@dataclass
class EvidenceGapReport:
    """Full evidence gap analysis."""
    dispute_id: str
    reason_code: str
    network: str

    items: list[EvidenceItem]
    completeness_score: int        # 0–100
    readiness: EvidenceReadiness

    critical_gaps: list[str]       # Required+missing evidence names
    gap_actions: list[str]         # What to do to fill the gaps
    strengths: list[str]           # Evidence already present

    win_rate_adjustment: float     # Based on completeness score
    summary: str


# Evidence requirements by reason code
_REQUIRED_BY_CODE: dict[str, list[EvidenceType]] = {
    "INR": [
        EvidenceType.TRACKING_NUMBER,
        EvidenceType.DELIVERY_CONFIRMATION,
        EvidenceType.INVOICE_RECEIPT,
    ],
    "SNAD": [
        EvidenceType.ITEM_LISTING_PHOTOS,
        EvidenceType.INVOICE_RECEIPT,
        EvidenceType.BUYER_CORRESPONDENCE,
    ],
    "UNAUTHORIZED": [
        EvidenceType.IP_ADDRESS_LOG,
        EvidenceType.THREE_D_SECURE,
        EvidenceType.INVOICE_RECEIPT,
        EvidenceType.TRANSACTION_HISTORY,
    ],
    "SUBSCRIPTION": [
        EvidenceType.SUBSCRIPTION_TERMS,
        EvidenceType.RENEWAL_NOTICE,
        EvidenceType.TERMS_OF_SERVICE,
    ],
    # Visa reason codes
    "10.4": [
        EvidenceType.THREE_D_SECURE,
        EvidenceType.IP_ADDRESS_LOG,
        EvidenceType.DEVICE_FINGERPRINT,
        EvidenceType.PRIOR_NON_DISPUTED_TXNS,
    ],
    "13.1": [
        EvidenceType.TRACKING_NUMBER,
        EvidenceType.DELIVERY_CONFIRMATION,
        EvidenceType.CARRIER_SCAN_LOG,
    ],
    "13.3": [
        EvidenceType.ITEM_LISTING_PHOTOS,
        EvidenceType.INVOICE_RECEIPT,
        EvidenceType.BUYER_CORRESPONDENCE,
        EvidenceType.RESOLUTION_ATTEMPT,
    ],
    "12.6": [
        EvidenceType.SUBSCRIPTION_TERMS,
        EvidenceType.TERMS_OF_SERVICE,
        EvidenceType.RENEWAL_NOTICE,
        EvidenceType.TRANSACTION_HISTORY,
    ],
    # MC codes
    "4807": [
        EvidenceType.THREE_D_SECURE,
        EvidenceType.CVV_MATCH,
        EvidenceType.AVS_MATCH,
        EvidenceType.IP_ADDRESS_LOG,
    ],
    "4853": [
        EvidenceType.ITEM_LISTING_PHOTOS,
        EvidenceType.INVOICE_RECEIPT,
        EvidenceType.BUYER_CORRESPONDENCE,
    ],
}

_ALWAYS_NICE_TO_HAVE: list[EvidenceType] = [
    EvidenceType.RESOLUTION_ATTEMPT,
    EvidenceType.BUYER_CORRESPONDENCE,
    EvidenceType.INVOICE_RECEIPT,
]

_WEIGHTS: dict[EvidenceType, int] = {
    EvidenceType.THREE_D_SECURE:          15,
    EvidenceType.DELIVERY_CONFIRMATION:   14,
    EvidenceType.TRACKING_NUMBER:         12,
    EvidenceType.SIGNATURE_PROOF:         12,
    EvidenceType.IP_ADDRESS_LOG:          10,
    EvidenceType.DEVICE_FINGERPRINT:      10,
    EvidenceType.PRIOR_NON_DISPUTED_TXNS: 10,
    EvidenceType.SUBSCRIPTION_TERMS:      10,
    EvidenceType.CVV_MATCH:                8,
    EvidenceType.AVS_MATCH:                7,
    EvidenceType.RENEWAL_NOTICE:           8,
    EvidenceType.TERMS_OF_SERVICE:         8,
    EvidenceType.CARRIER_SCAN_LOG:         8,
    EvidenceType.ITEM_LISTING_PHOTOS:      8,
    EvidenceType.BUYER_CORRESPONDENCE:     7,
    EvidenceType.RESOLUTION_ATTEMPT:       6,
    EvidenceType.INVOICE_RECEIPT:          6,
    EvidenceType.TRANSACTION_HISTORY:      7,
    EvidenceType.POLICY_DISPLAY_PROOF:     5,
}

_COLLECTION_TIPS: dict[EvidenceType, str] = {
    EvidenceType.TRACKING_NUMBER:          "Export tracking number from shipping portal",
    EvidenceType.DELIVERY_CONFIRMATION:    "Pull carrier delivery scan from UPS/FedEx/USPS portal",
    EvidenceType.SIGNATURE_PROOF:          "Request signed delivery receipt from carrier",
    EvidenceType.CARRIER_SCAN_LOG:         "Download full scan history PDF from carrier dashboard",
    EvidenceType.THREE_D_SECURE:           "Retrieve 3DS authentication record from payment gateway logs",
    EvidenceType.CVV_MATCH:                "Export CVV verification result from payment processor",
    EvidenceType.AVS_MATCH:                "Export AVS result from payment processor",
    EvidenceType.IP_ADDRESS_LOG:           "Pull IP address at time of purchase from analytics/server log",
    EvidenceType.DEVICE_FINGERPRINT:       "Query device fingerprint service for session at purchase time",
    EvidenceType.BUYER_CORRESPONDENCE:     "Export all support ticket messages with buyer",
    EvidenceType.RESOLUTION_ATTEMPT:       "Document any refund offer or resolution attempt made",
    EvidenceType.TERMS_OF_SERVICE:         "Export TOS page with timestamps from CMS",
    EvidenceType.POLICY_DISPLAY_PROOF:     "Screenshot return/refund policy from checkout flow",
    EvidenceType.SUBSCRIPTION_TERMS:       "Export subscription agreement accepted at sign-up",
    EvidenceType.RENEWAL_NOTICE:           "Pull renewal reminder email sent record from ESP",
    EvidenceType.ITEM_LISTING_PHOTOS:      "Export original product listing with photos and description",
    EvidenceType.INVOICE_RECEIPT:          "Pull order confirmation email / invoice PDF",
    EvidenceType.TRANSACTION_HISTORY:      "Export buyer's full order history from your platform",
    EvidenceType.PRIOR_NON_DISPUTED_TXNS:  "Export 2+ prior non-disputed transactions on same card",
}


def _status_for(evidence_type: EvidenceType, inp: EvidenceGapInput) -> EvidenceStatus:
    mapping: dict[EvidenceType, bool] = {
        EvidenceType.TRACKING_NUMBER:          inp.has_tracking_number,
        EvidenceType.DELIVERY_CONFIRMATION:    inp.has_delivery_confirmation,
        EvidenceType.SIGNATURE_PROOF:          inp.has_signature_proof,
        EvidenceType.CARRIER_SCAN_LOG:         inp.has_carrier_scan_log,
        EvidenceType.THREE_D_SECURE:           inp.three_d_secure_passed,
        EvidenceType.CVV_MATCH:                inp.cvv_matched,
        EvidenceType.AVS_MATCH:                inp.avs_matched,
        EvidenceType.IP_ADDRESS_LOG:           inp.has_ip_log,
        EvidenceType.DEVICE_FINGERPRINT:       inp.has_device_fingerprint,
        EvidenceType.BUYER_CORRESPONDENCE:     inp.has_buyer_correspondence,
        EvidenceType.RESOLUTION_ATTEMPT:       inp.made_resolution_attempt,
        EvidenceType.TERMS_OF_SERVICE:         inp.has_tos_accepted,
        EvidenceType.POLICY_DISPLAY_PROOF:     inp.has_policy_display_proof,
        EvidenceType.SUBSCRIPTION_TERMS:       inp.has_subscription_terms,
        EvidenceType.RENEWAL_NOTICE:           inp.has_renewal_notice,
        EvidenceType.ITEM_LISTING_PHOTOS:      inp.has_item_listing_photos,
        EvidenceType.INVOICE_RECEIPT:          inp.has_invoice,
        EvidenceType.TRANSACTION_HISTORY:      inp.has_transaction_history,
        EvidenceType.PRIOR_NON_DISPUTED_TXNS:  inp.prior_non_disputed_txn_count >= 2,
    }
    if evidence_type == EvidenceType.THREE_D_SECURE and not inp.three_d_secure_run:
        return EvidenceStatus.UNAVAILABLE
    return EvidenceStatus.PRESENT if mapping.get(evidence_type, False) else EvidenceStatus.MISSING


def analyze_evidence_gaps(inp: EvidenceGapInput) -> EvidenceGapReport:
    """Analyze evidence completeness and surface critical gaps."""
    code = inp.reason_code.upper()
    required_types = _REQUIRED_BY_CODE.get(code, [
        EvidenceType.INVOICE_RECEIPT,
        EvidenceType.BUYER_CORRESPONDENCE,
    ])

    # Build all evidence items (required + nice-to-have)
    seen: set[EvidenceType] = set()
    all_types: list[tuple[EvidenceType, bool]] = []
    for et in required_types:
        all_types.append((et, True))
        seen.add(et)
    for et in _ALWAYS_NICE_TO_HAVE:
        if et not in seen:
            all_types.append((et, False))
            seen.add(et)

    items: list[EvidenceItem] = []
    total_possible = 0
    total_earned   = 0
    critical_gaps: list[str] = []
    gap_actions: list[str] = []
    strengths: list[str] = []

    for ev_type, is_required in all_types:
        status = _status_for(ev_type, inp)
        weight = _WEIGHTS.get(ev_type, 5)
        severity = "CRITICAL" if is_required else "LOW"
        tip = _COLLECTION_TIPS.get(ev_type, f"Gather {ev_type.value.lower().replace('_', ' ')}")

        if is_required:
            total_possible += weight
        else:
            total_possible += weight // 2   # Nice-to-have contributes less

        if status == EvidenceStatus.PRESENT:
            total_earned += weight if is_required else weight // 2
            strengths.append(ev_type.value.replace("_", " ").title())
        elif status == EvidenceStatus.MISSING and is_required:
            critical_gaps.append(ev_type.value.replace("_", " ").title())
            gap_actions.append(f"[CRITICAL] {tip}")
        elif status == EvidenceStatus.MISSING and not is_required:
            gap_actions.append(f"[OPTIONAL] {tip}")
        elif status == EvidenceStatus.UNAVAILABLE and is_required:
            severity = "MODERATE"   # Can't fix retroactively; note for future
            gap_actions.append(f"[FUTURE] Enable {ev_type.value.lower().replace('_', ' ')} for future transactions")

        items.append(EvidenceItem(
            evidence_type=ev_type,
            is_required=is_required,
            status=status,
            weight=weight,
            gap_severity=severity,
            collection_tip=tip,
        ))

    score = round((total_earned / max(total_possible, 1)) * 100)

    if score >= 80:
        readiness = EvidenceReadiness.STRONG
        win_adj = +0.08
    elif score >= 60:
        readiness = EvidenceReadiness.ADEQUATE
        win_adj = +0.02
    elif score >= 40:
        readiness = EvidenceReadiness.WEAK
        win_adj = -0.08
    else:
        readiness = EvidenceReadiness.INSUFFICIENT
        win_adj = -0.18

    summary = (
        f"Evidence {inp.dispute_id}: "
        f"score={score}/100 ({readiness.value}) | "
        f"critical_gaps={len(critical_gaps)} | "
        f"win_adj={win_adj:+.2f}"
    )

    return EvidenceGapReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        network=inp.network,
        items=items,
        completeness_score=score,
        readiness=readiness,
        critical_gaps=critical_gaps,
        gap_actions=gap_actions,
        strengths=strengths,
        win_rate_adjustment=win_adj,
        summary=summary,
    )
