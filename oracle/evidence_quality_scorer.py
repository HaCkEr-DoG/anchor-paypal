"""
Evidence Quality Scorer — assess the quality and completeness of a dispute
evidence package before submission.

PayPal and card networks evaluate dispute evidence on completeness, relevance,
and specificity. Weak evidence is the leading cause of winnable disputes being
lost. This module scores an evidence package and identifies the most impactful
gaps before submission.

Evidence types by claim category:
  INR:          tracking number, proof of delivery, carrier confirmation
  SNAD:         product listing screenshots, description match, photos,
                return policy, communication logs
  UNAUTHORIZED: device fingerprint, IP logs, 2FA records, prior order history
  DUPLICATE:    original order receipt, payment records, refund confirmation
  SUBSCRIPTION: cancellation confirmation, terms, communication history

Score components (each 0–100, weighted to composite):
  COMPLETENESS   — Are the required evidence types present?
  SPECIFICITY    — Does evidence directly address the specific claim?
  DOCUMENTATION  — Are files in acceptable format and date-stamped?
  TIMELINE       — Is there a coherent chronological narrative?
  REBUTTAL       — Does the evidence directly counter buyer's claim?

Composite thresholds:
  STRONG  ≥ 80 — submit as-is; high confidence
  ADEQUATE 60–79 — submit; flag minor gaps
  WEAK    40–59 — supplement before submitting
  POOR    < 40  — do not submit; high loss risk

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceStrength(str, Enum):
    STRONG   = "STRONG"
    ADEQUATE = "ADEQUATE"
    WEAK     = "WEAK"
    POOR     = "POOR"


class EvidenceType(str, Enum):
    TRACKING_NUMBER        = "TRACKING_NUMBER"
    PROOF_OF_DELIVERY      = "PROOF_OF_DELIVERY"
    CARRIER_CONFIRMATION   = "CARRIER_CONFIRMATION"
    PRODUCT_LISTING        = "PRODUCT_LISTING"
    DESCRIPTION_MATCH      = "DESCRIPTION_MATCH"
    ITEM_PHOTOS            = "ITEM_PHOTOS"
    RETURN_POLICY          = "RETURN_POLICY"
    COMMUNICATION_LOG      = "COMMUNICATION_LOG"
    DEVICE_FINGERPRINT     = "DEVICE_FINGERPRINT"
    IP_ADDRESS_LOG         = "IP_ADDRESS_LOG"
    TWO_FA_RECORD          = "TWO_FA_RECORD"
    ORDER_HISTORY          = "ORDER_HISTORY"
    ORIGINAL_RECEIPT       = "ORIGINAL_RECEIPT"
    PAYMENT_RECORD         = "PAYMENT_RECORD"
    REFUND_CONFIRMATION    = "REFUND_CONFIRMATION"
    CANCELLATION_CONFIRM   = "CANCELLATION_CONFIRM"
    SUBSCRIPTION_TERMS     = "SUBSCRIPTION_TERMS"
    SIGNATURE_CONFIRMATION = "SIGNATURE_CONFIRMATION"
    INVOICE                = "INVOICE"
    CUSTOM_ORDER_PROOF     = "CUSTOM_ORDER_PROOF"


class ClaimType(str, Enum):
    INR          = "INR"
    SNAD         = "SNAD"
    UNAUTHORIZED = "UNAUTHORIZED"
    DUPLICATE    = "DUPLICATE"
    SUBSCRIPTION = "SUBSCRIPTION"


class EvidenceGapSeverity(str, Enum):
    CRITICAL = "CRITICAL"   # Winning without this is very unlikely
    MAJOR    = "MAJOR"      # Significantly reduces win probability
    MINOR    = "MINOR"      # Nice to have; small marginal benefit


@dataclass
class EvidenceItem:
    """One piece of evidence in the package. Class attrs serve as wave15 enum-style constants."""
    # Class-level string constants for wave15 enum-style access (non-annotated = not dataclass fields)
    TRACKING_SHOWS_DELIVERED = "TRACKING_SHOWS_DELIVERED"
    TRACKING_NUMBER          = "TRACKING_NUMBER"
    SIGNATURE_CONFIRMATION   = "SIGNATURE_CONFIRMATION"
    DELIVERY_PHOTO           = "DELIVERY_PHOTO"
    CARRIER_AFFIDAVIT        = "CARRIER_AFFIDAVIT"
    BUYER_MESSAGES_LOG       = "BUYER_MESSAGES_LOG"
    SHIPPING_LABEL           = "SHIPPING_LABEL"
    PRODUCT_PHOTOS           = "PRODUCT_PHOTOS"
    LISTING_SCREENSHOT       = "LISTING_SCREENSHOT"
    MATCHING_IP_ADDRESS      = "MATCHING_IP_ADDRESS"
    MATCHING_DEVICE          = "MATCHING_DEVICE"
    PRIOR_SUCCESSFUL_ORDERS  = "PRIOR_SUCCESSFUL_ORDERS"
    TOS_ACCEPTANCE           = "TOS_ACCEPTANCE"
    CANCELLATION_POLICY      = "CANCELLATION_POLICY"
    RETURN_POLICY_DOC        = "RETURN_POLICY_DOC"
    INVOICE                  = "INVOICE"
    COMMUNICATION_LOG        = "COMMUNICATION_LOG"
    PROOF_OF_DELIVERY        = "PROOF_OF_DELIVERY"
    CARRIER_CONFIRMATION     = "CARRIER_CONFIRMATION"

    # Dataclass instance fields
    evidence_type: EvidenceType
    is_present: bool
    is_dated: bool = False       # Does it have a clear date?
    is_specific: bool = False    # Is it specific to this transaction?
    file_format_ok: bool = True  # Acceptable file format (PDF, image, etc.)
    description: str = ""        # Optional free-text description


@dataclass
class EvidenceGap:
    """A missing or weak evidence item."""
    evidence_type: EvidenceType
    severity: EvidenceGapSeverity
    description: str
    suggested_action: str


@dataclass
class EvidenceScoreComponent:
    """Score for one quality dimension."""
    dimension: str
    score: float        # 0–100
    weight: float
    weighted_contribution: float
    notes: str


@dataclass
class EvidenceQualityInput:
    """Input for evidence quality assessment."""
    dispute_id: str
    claim_type: ClaimType
    dispute_amount: float
    evidence_items: list[EvidenceItem]

    # Context
    days_until_deadline: int = 10
    has_prior_buyer_communication: bool = False
    buyer_claimed_non_delivery: bool = True
    order_above_signature_threshold: bool = False   # ≥ $750
    is_digital_goods: bool = False


# Required evidence per claim type (ordered by criticality)
_REQUIRED: dict[ClaimType, list[tuple[EvidenceType, EvidenceGapSeverity]]] = {
    ClaimType.INR: [
        (EvidenceType.TRACKING_NUMBER,       EvidenceGapSeverity.CRITICAL),
        (EvidenceType.PROOF_OF_DELIVERY,     EvidenceGapSeverity.CRITICAL),
        (EvidenceType.CARRIER_CONFIRMATION,  EvidenceGapSeverity.MAJOR),
        (EvidenceType.COMMUNICATION_LOG,     EvidenceGapSeverity.MINOR),
    ],
    ClaimType.SNAD: [
        (EvidenceType.PRODUCT_LISTING,       EvidenceGapSeverity.CRITICAL),
        (EvidenceType.DESCRIPTION_MATCH,     EvidenceGapSeverity.CRITICAL),
        (EvidenceType.ITEM_PHOTOS,           EvidenceGapSeverity.MAJOR),
        (EvidenceType.RETURN_POLICY,         EvidenceGapSeverity.MAJOR),
        (EvidenceType.COMMUNICATION_LOG,     EvidenceGapSeverity.MINOR),
    ],
    ClaimType.UNAUTHORIZED: [
        (EvidenceType.IP_ADDRESS_LOG,        EvidenceGapSeverity.CRITICAL),
        (EvidenceType.ORDER_HISTORY,         EvidenceGapSeverity.CRITICAL),
        (EvidenceType.DEVICE_FINGERPRINT,    EvidenceGapSeverity.MAJOR),
        (EvidenceType.TWO_FA_RECORD,         EvidenceGapSeverity.MAJOR),
    ],
    ClaimType.DUPLICATE: [
        (EvidenceType.ORIGINAL_RECEIPT,      EvidenceGapSeverity.CRITICAL),
        (EvidenceType.PAYMENT_RECORD,        EvidenceGapSeverity.CRITICAL),
        (EvidenceType.REFUND_CONFIRMATION,   EvidenceGapSeverity.MAJOR),
    ],
    ClaimType.SUBSCRIPTION: [
        (EvidenceType.SUBSCRIPTION_TERMS,    EvidenceGapSeverity.CRITICAL),
        (EvidenceType.CANCELLATION_CONFIRM,  EvidenceGapSeverity.MAJOR),
        (EvidenceType.COMMUNICATION_LOG,     EvidenceGapSeverity.MAJOR),
        (EvidenceType.ORIGINAL_RECEIPT,      EvidenceGapSeverity.MINOR),
    ],
}


def _score_completeness(inp: EvidenceQualityInput,
                        present: set[EvidenceType]) -> tuple[float, str]:
    required = _REQUIRED.get(inp.claim_type, [])
    if not required:
        return 50.0, "Unknown claim type — cannot assess completeness"

    critical = [(t, s) for t, s in required if s == EvidenceGapSeverity.CRITICAL]
    major    = [(t, s) for t, s in required if s == EvidenceGapSeverity.MAJOR]

    critical_present = sum(1 for t, _ in critical if t in present)
    major_present    = sum(1 for t, _ in major    if t in present)

    score = 0.0
    if critical:
        score += 60.0 * (critical_present / len(critical))
    else:
        score += 60.0
    if major:
        score += 30.0 * (major_present / len(major))
    else:
        score += 30.0
    score += 10.0  # Minor items bonus assumed if critical + major present

    extra = ""
    if inp.order_above_signature_threshold and EvidenceType.SIGNATURE_CONFIRMATION not in present:
        score = max(0, score - 15.0)
        extra = " (signature confirmation missing for ≥$750 order)"

    return round(score, 1), f"{critical_present}/{len(critical)} critical, {major_present}/{len(major)} major{extra}"


def _score_specificity(inp: EvidenceQualityInput) -> tuple[float, str]:
    items = [i for i in inp.evidence_items if i.is_present]
    if not items:
        return 0.0, "No evidence present"
    specific_count = sum(1 for i in items if i.is_specific)
    pct = specific_count / len(items)
    if pct >= 0.90:
        return 100.0, f"{specific_count}/{len(items)} items transaction-specific"
    if pct >= 0.70:
        return 75.0, f"{specific_count}/{len(items)} items transaction-specific"
    if pct >= 0.50:
        return 50.0, f"{specific_count}/{len(items)} items transaction-specific"
    return 25.0, f"Only {specific_count}/{len(items)} items are transaction-specific"


def _score_documentation(inp: EvidenceQualityInput) -> tuple[float, str]:
    items = [i for i in inp.evidence_items if i.is_present]
    if not items:
        return 0.0, "No evidence present"
    dated_count  = sum(1 for i in items if i.is_dated)
    format_ok    = sum(1 for i in items if i.file_format_ok)
    dated_pct    = dated_count / len(items)
    format_pct   = format_ok  / len(items)
    score = round((dated_pct * 60 + format_pct * 40) * 100 / 100, 1)
    return score, f"{dated_count}/{len(items)} dated, {format_ok}/{len(items)} correct format"


def _score_timeline(inp: EvidenceQualityInput) -> tuple[float, str]:
    # Timeline quality: communication log + dated evidence creates a narrative
    items = [i for i in inp.evidence_items if i.is_present]
    dated_count = sum(1 for i in items if i.is_dated)
    has_comm = any(i.evidence_type == EvidenceType.COMMUNICATION_LOG
                   for i in items)
    has_receipt = any(i.evidence_type in (EvidenceType.ORIGINAL_RECEIPT,
                                           EvidenceType.INVOICE,
                                           EvidenceType.PAYMENT_RECORD)
                      for i in items)
    score = 0.0
    if dated_count >= 2:
        score += 40.0
    elif dated_count == 1:
        score += 20.0
    if has_comm:
        score += 30.0
    if has_receipt:
        score += 30.0
    return round(score, 1), f"dated_docs={dated_count}, has_communication={has_comm}, has_receipt={has_receipt}"


def _score_rebuttal(inp: EvidenceQualityInput,
                    present: set[EvidenceType]) -> tuple[float, str]:
    # Does evidence directly rebut the buyer's specific claim?
    score = 50.0  # Base — neutral
    notes_parts = []

    if inp.claim_type == ClaimType.INR:
        if EvidenceType.PROOF_OF_DELIVERY in present:
            score = min(100.0, score + 35.0)
            notes_parts.append("delivery proof present")
        if EvidenceType.SIGNATURE_CONFIRMATION in present:
            score = min(100.0, score + 15.0)
            notes_parts.append("signature confirmation")
        if inp.buyer_claimed_non_delivery and EvidenceType.CARRIER_CONFIRMATION in present:
            score = min(100.0, score + 10.0)
            notes_parts.append("carrier confirmation")

    elif inp.claim_type == ClaimType.SNAD:
        if EvidenceType.DESCRIPTION_MATCH in present and EvidenceType.ITEM_PHOTOS in present:
            score = min(100.0, score + 30.0)
            notes_parts.append("listing + photos match claim")
        if EvidenceType.PRODUCT_LISTING in present:
            score = min(100.0, score + 15.0)

    elif inp.claim_type == ClaimType.UNAUTHORIZED:
        if EvidenceType.IP_ADDRESS_LOG in present and EvidenceType.ORDER_HISTORY in present:
            score = min(100.0, score + 35.0)
            notes_parts.append("IP + order history rebut fraud claim")
        if EvidenceType.DEVICE_FINGERPRINT in present:
            score = min(100.0, score + 15.0)

    elif inp.claim_type == ClaimType.DUPLICATE:
        if EvidenceType.ORIGINAL_RECEIPT in present:
            score = min(100.0, score + 35.0)
        if EvidenceType.REFUND_CONFIRMATION in present:
            score = min(100.0, score + 15.0)

    elif inp.claim_type == ClaimType.SUBSCRIPTION:
        if EvidenceType.SUBSCRIPTION_TERMS in present:
            score = min(100.0, score + 30.0)
        if EvidenceType.COMMUNICATION_LOG in present:
            score = min(100.0, score + 20.0)

    notes = ", ".join(notes_parts) if notes_parts else "limited direct rebuttal evidence"
    return round(score, 1), notes


_WEIGHTS = {
    "completeness":    0.35,
    "specificity":     0.20,
    "documentation":   0.15,
    "timeline":        0.15,
    "rebuttal":        0.15,
}


@dataclass
class EvidenceQualityReport:
    """Complete evidence quality assessment."""
    dispute_id: str
    claim_type: ClaimType

    # Overall
    composite_score: float
    strength: EvidenceStrength

    # Components
    components: list[EvidenceScoreComponent]

    # Gaps
    gaps: list[EvidenceGap]
    critical_gaps: int
    major_gaps: int

    # Guidance
    submit_recommended: bool
    submission_risk: str      # "LOW" / "MEDIUM" / "HIGH"
    top_actions: list[str]    # Highest-impact actions to take now

    summary: str


def _build_gaps(inp: EvidenceQualityInput,
                present: set[EvidenceType]) -> list[EvidenceGap]:
    gaps: list[EvidenceGap] = []
    required = _REQUIRED.get(inp.claim_type, [])

    for ev_type, severity in required:
        if ev_type not in present:
            if ev_type == EvidenceType.TRACKING_NUMBER:
                action = "Retrieve shipment tracking number from carrier dashboard"
            elif ev_type == EvidenceType.PROOF_OF_DELIVERY:
                action = "Download delivery confirmation PDF from carrier portal"
            elif ev_type == EvidenceType.CARRIER_CONFIRMATION:
                action = "Contact carrier for official delivery confirmation letter"
            elif ev_type == EvidenceType.PRODUCT_LISTING:
                action = "Export product listing page as PDF with timestamps"
            elif ev_type == EvidenceType.DESCRIPTION_MATCH:
                action = "Create side-by-side comparison of listing vs. item shipped"
            elif ev_type == EvidenceType.ITEM_PHOTOS:
                action = "Retrieve item photos taken at packing/shipping time"
            elif ev_type == EvidenceType.RETURN_POLICY:
                action = "Export return policy page or policy text from order confirmation"
            elif ev_type == EvidenceType.COMMUNICATION_LOG:
                action = "Export all messages with buyer from platform messaging system"
            elif ev_type == EvidenceType.IP_ADDRESS_LOG:
                action = "Pull IP address and device metadata from order placement logs"
            elif ev_type == EvidenceType.ORDER_HISTORY:
                action = "Export prior order history showing buyer's established pattern"
            elif ev_type == EvidenceType.DEVICE_FINGERPRINT:
                action = "Retrieve device fingerprint / browser session from fraud tools"
            elif ev_type == EvidenceType.TWO_FA_RECORD:
                action = "Export 2FA verification log for the account at order time"
            elif ev_type == EvidenceType.ORIGINAL_RECEIPT:
                action = "Export original order receipt with order ID and timestamp"
            elif ev_type == EvidenceType.PAYMENT_RECORD:
                action = "Pull payment capture records from payment processor"
            elif ev_type == EvidenceType.REFUND_CONFIRMATION:
                action = "Retrieve refund confirmation email or processor record"
            elif ev_type == EvidenceType.SUBSCRIPTION_TERMS:
                action = "Export subscription terms and billing schedule accepted at sign-up"
            elif ev_type == EvidenceType.CANCELLATION_CONFIRM:
                action = "Provide cancellation confirmation email or system record"
            else:
                action = f"Obtain {ev_type.value.replace('_', ' ').lower()} for this transaction"

            gaps.append(EvidenceGap(
                evidence_type=ev_type,
                severity=severity,
                description=f"Missing: {ev_type.value.replace('_', ' ').lower()}",
                suggested_action=action,
            ))

    # Signature for high-value orders
    if (inp.order_above_signature_threshold
            and EvidenceType.SIGNATURE_CONFIRMATION not in present):
        gaps.append(EvidenceGap(
            evidence_type=EvidenceType.SIGNATURE_CONFIRMATION,
            severity=EvidenceGapSeverity.CRITICAL,
            description="Signature confirmation required for orders ≥$750",
            suggested_action="Retrieve adult signature confirmation from carrier or delivery service",
        ))

    return gaps


def score_evidence_quality(inp: EvidenceQualityInput) -> EvidenceQualityReport:
    """
    Score the quality and completeness of a dispute evidence package.
    """
    present = {item.evidence_type for item in inp.evidence_items if item.is_present}

    # Score each dimension
    s_completeness,  n_completeness  = _score_completeness(inp, present)
    s_specificity,   n_specificity   = _score_specificity(inp)
    s_documentation, n_documentation = _score_documentation(inp)
    s_timeline,      n_timeline      = _score_timeline(inp)
    s_rebuttal,      n_rebuttal      = _score_rebuttal(inp, present)

    dim_scores = {
        "completeness":  (s_completeness,  n_completeness),
        "specificity":   (s_specificity,   n_specificity),
        "documentation": (s_documentation, n_documentation),
        "timeline":      (s_timeline,      n_timeline),
        "rebuttal":      (s_rebuttal,      n_rebuttal),
    }

    components: list[EvidenceScoreComponent] = []
    composite = 0.0
    for dim, (score, notes) in dim_scores.items():
        w = _WEIGHTS[dim]
        contrib = score * w
        composite += contrib
        components.append(EvidenceScoreComponent(
            dimension=dim,
            score=round(score, 1),
            weight=w,
            weighted_contribution=round(contrib, 2),
            notes=notes,
        ))

    composite = round(composite, 1)

    strength = (EvidenceStrength.STRONG   if composite >= 80
                else EvidenceStrength.ADEQUATE if composite >= 60
                else EvidenceStrength.WEAK     if composite >= 40
                else EvidenceStrength.POOR)

    gaps = _build_gaps(inp, present)
    critical_gaps = sum(1 for g in gaps if g.severity == EvidenceGapSeverity.CRITICAL)
    major_gaps    = sum(1 for g in gaps if g.severity == EvidenceGapSeverity.MAJOR)

    submit_recommended = composite >= 60 and critical_gaps == 0
    if critical_gaps > 0:
        submission_risk = "HIGH"
    elif major_gaps > 1 or composite < 60:
        submission_risk = "MEDIUM"
    else:
        submission_risk = "LOW"

    # Top actions: critical gaps first, then major
    top_actions: list[str] = []
    for gap in sorted(gaps, key=lambda g: (0 if g.severity == EvidenceGapSeverity.CRITICAL else 1)):
        top_actions.append(gap.suggested_action)
        if len(top_actions) >= 3:
            break

    if not top_actions and strength in (EvidenceStrength.STRONG, EvidenceStrength.ADEQUATE):
        top_actions.append("Package is ready — review for date/timestamp accuracy before submission")

    summary = (
        f"Evidence {inp.dispute_id} [{inp.claim_type.value}]: "
        f"score={composite:.0f}/100 | strength={strength.value} | "
        f"critical_gaps={critical_gaps} | submit={'YES' if submit_recommended else 'NO'} | "
        f"risk={submission_risk}"
    )

    return EvidenceQualityReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        composite_score=composite,
        strength=strength,
        components=components,
        gaps=gaps,
        critical_gaps=critical_gaps,
        major_gaps=major_gaps,
        submit_recommended=submit_recommended,
        submission_risk=submission_risk,
        top_actions=top_actions,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases (older test suites use these names)
# ---------------------------------------------------------------------------
score_evidence_quality_legacy = score_evidence_quality


# ---------------------------------------------------------------------------
# Wave15 / test-API — enum-based EvidenceItem, EvidencePackage, EvidenceTier
# ---------------------------------------------------------------------------
from enum import Enum as _Enum2
from dataclasses import dataclass as _dc2, field as _f2


class EvidenceTier(str, _Enum2):
    EXCELLENT    = "EXCELLENT"
    GOOD         = "GOOD"
    ADEQUATE     = "ADEQUATE"
    POOR         = "POOR"
    INSUFFICIENT = "INSUFFICIENT"


@_dc2
class EvidencePackage:
    dispute_id: str
    claim_type: str = "INR"
    items: list = _f2(default_factory=list)


# Per-claim-type item weights (higher = more relevant to this claim)
_ITEM_WEIGHTS: dict = {
    "INR": {
        EvidenceItem.TRACKING_SHOWS_DELIVERED: 30,
        EvidenceItem.TRACKING_NUMBER: 20,
        EvidenceItem.SIGNATURE_CONFIRMATION: 20,
        EvidenceItem.DELIVERY_PHOTO: 15,
        EvidenceItem.CARRIER_AFFIDAVIT: 15,
        EvidenceItem.BUYER_MESSAGES_LOG: 10,
        EvidenceItem.SHIPPING_LABEL: 8,
    },
    "SNAD": {
        EvidenceItem.PRODUCT_PHOTOS: 30,
        EvidenceItem.LISTING_SCREENSHOT: 25,
        EvidenceItem.BUYER_MESSAGES_LOG: 15,
        EvidenceItem.RETURN_POLICY_DOC: 15,
        EvidenceItem.COMMUNICATION_LOG: 10,
        EvidenceItem.TRACKING_NUMBER: 5,
    },
    "UNAUTHORIZED": {
        EvidenceItem.MATCHING_IP_ADDRESS: 30,
        EvidenceItem.MATCHING_DEVICE: 25,
        EvidenceItem.PRIOR_SUCCESSFUL_ORDERS: 20,
        EvidenceItem.BUYER_MESSAGES_LOG: 10,
        EvidenceItem.SIGNATURE_CONFIRMATION: 15,
    },
    "SUBSCRIPTION": {
        EvidenceItem.TOS_ACCEPTANCE: 35,
        EvidenceItem.CANCELLATION_POLICY: 25,
        EvidenceItem.BUYER_MESSAGES_LOG: 20,
        EvidenceItem.COMMUNICATION_LOG: 15,
        EvidenceItem.INVOICE: 5,
    },
}

_CRITICAL_REQUIRED: dict = {
    "INR": [EvidenceItem.TRACKING_SHOWS_DELIVERED, EvidenceItem.TRACKING_NUMBER],
    "SNAD": [EvidenceItem.PRODUCT_PHOTOS, EvidenceItem.LISTING_SCREENSHOT],
    "UNAUTHORIZED": [EvidenceItem.MATCHING_IP_ADDRESS, EvidenceItem.MATCHING_DEVICE],
    "SUBSCRIPTION": [EvidenceItem.TOS_ACCEPTANCE, EvidenceItem.CANCELLATION_POLICY],
}


@_dc2
class _EvidenceGapV2:
    item: "EvidenceItem"
    severity: str
    description: str


@_dc2
class _EvidenceQualityReportV2:
    dispute_id: str
    tier: EvidenceTier
    normalized_score: float
    win_rate_boost: float
    critical_gaps: list     # list[EvidenceItem] — items missing that are critical
    gaps: list              # list[_EvidenceGapV2]
    items_present_count: int
    top_evidence_items: list  # list[tuple[EvidenceItem, float]] sorted desc by score
    collection_priority: list  # list[EvidenceItem] — recommended items to collect
    summary: str


def score_evidence(pkg: EvidencePackage) -> _EvidenceQualityReportV2:
    """Score an evidence package for a dispute (wave15 test-API)."""
    claim = (pkg.claim_type or "INR").upper()
    present_set = set(pkg.items)
    n_present = len(present_set)

    weights = _ITEM_WEIGHTS.get(claim, _ITEM_WEIGHTS["INR"])
    required = _CRITICAL_REQUIRED.get(claim, [])

    # Score each item by its weight for this claim type
    item_scores: list = []
    for item, w in weights.items():
        if item in present_set:
            item_scores.append((item, float(w)))

    # Normalized score: sum of present item weights / max possible (top 5 items)
    all_weights = sorted(weights.values(), reverse=True)
    max_score = sum(all_weights[:5]) if all_weights else 100.0
    raw = sum(s for _, s in item_scores)
    normalized = min(100.0, round(raw / max_score * 100.0, 1)) if max_score > 0 else 0.0

    # Tier
    if normalized >= 80:
        tier = EvidenceTier.EXCELLENT
    elif normalized >= 60:
        tier = EvidenceTier.GOOD
    elif normalized >= 40:
        tier = EvidenceTier.ADEQUATE
    elif normalized >= 15:
        tier = EvidenceTier.POOR
    else:
        tier = EvidenceTier.INSUFFICIENT

    # Win rate boost
    _TIER_BOOST = {
        EvidenceTier.EXCELLENT:    +0.12,
        EvidenceTier.GOOD:         +0.07,
        EvidenceTier.ADEQUATE:     +0.02,
        EvidenceTier.POOR:         -0.08,
        EvidenceTier.INSUFFICIENT: -0.15,
    }
    win_rate_boost = _TIER_BOOST[tier]

    # Critical gaps: required items not in present set
    critical_gaps = [item for item in required if item not in present_set]

    # All gaps: missing items from weight map
    gaps = []
    for item in weights:
        if item not in present_set:
            sev = "CRITICAL" if item in required else "MAJOR"
            gaps.append(_EvidenceGapV2(
                item=item,
                severity=sev,
                description=f"Missing {item.value if hasattr(item, 'value') else item}",
            ))

    # Collection priority: critical first, then by weight descending
    collection_priority = (
        [item for item in required if item not in present_set] +
        [item for item, _ in sorted(weights.items(), key=lambda x: x[1], reverse=True)
         if item not in present_set and item not in required]
    )

    top_evidence_items = sorted(item_scores, key=lambda x: x[1], reverse=True)

    summary = (
        f"Evidence {pkg.dispute_id} [{claim}]: "
        f"score={normalized:.0f}/100 | tier={tier.value} | "
        f"items={n_present} | critical_gaps={len(critical_gaps)}"
    )

    return _EvidenceQualityReportV2(
        dispute_id=pkg.dispute_id,
        tier=tier,
        normalized_score=normalized,
        win_rate_boost=win_rate_boost,
        critical_gaps=critical_gaps,
        gaps=gaps,
        items_present_count=n_present,
        top_evidence_items=top_evidence_items,
        collection_priority=collection_priority,
        summary=summary,
    )
