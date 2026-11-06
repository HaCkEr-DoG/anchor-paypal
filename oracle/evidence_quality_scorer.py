"""
Evidence Quality Scorer — assess how strong a merchant's dispute evidence is.

PayPal's dispute review team evaluates evidence on:
  1. Delivery proof strength (tracking, signature, photo)
  2. Product match (shipped item = ordered item)
  3. Buyer interaction (communication attempts)
  4. Policy compliance (ToS, description accuracy)
  5. Transaction legitimacy (device match, prior orders)

This module scores a merchant's evidence package (0-100) and predicts
how much it will boost the win probability for each claim type.

Evidence tiers:
  EXCELLENT (85-100): Signature confirmation + photo + tracking — near-certain win
  GOOD      (65-84):  Tracking shows delivered + communication logs
  ADEQUATE  (45-64):  Tracking only, or partial evidence
  WEAK      (25-44):  No delivery proof or only self-reported info
  INSUFFICIENT (0-24): No meaningful evidence

Claim-type weights (which evidence matters most per claim):
  INR:            Delivery proof (40%), tracking details (30%), photos (20%), comms (10%)
  SNAD:           Photos (35%), description accuracy (30%), return policy (20%), comms (15%)
  UNAUTHORIZED:   Transaction legitimacy (40%), device match (30%), prior orders (20%), comms (10%)
  SUBSCRIPTION:   ToS acceptance (45%), cancellation policy (35%), comms (20%)
  DUPLICATE:      Transaction records (60%), comms (40%)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceTier(str, Enum):
    EXCELLENT     = "EXCELLENT"
    GOOD          = "GOOD"
    ADEQUATE      = "ADEQUATE"
    WEAK          = "WEAK"
    INSUFFICIENT  = "INSUFFICIENT"


class EvidenceItem(str, Enum):
    # Delivery evidence
    TRACKING_NUMBER         = "TRACKING_NUMBER"
    TRACKING_SHOWS_DELIVERED = "TRACKING_SHOWS_DELIVERED"
    SIGNATURE_CONFIRMATION  = "SIGNATURE_CONFIRMATION"
    DELIVERY_PHOTO          = "DELIVERY_PHOTO"
    CARRIER_AFFIDAVIT       = "CARRIER_AFFIDAVIT"

    # Product match evidence
    PRODUCT_PHOTOS          = "PRODUCT_PHOTOS"
    LISTING_SCREENSHOT      = "LISTING_SCREENSHOT"
    PACKING_SLIP            = "PACKING_SLIP"
    WEIGHT_MANIFEST         = "WEIGHT_MANIFEST"

    # Buyer interaction
    BUYER_MESSAGES_LOG      = "BUYER_MESSAGES_LOG"
    RESOLUTION_ATTEMPT      = "RESOLUTION_ATTEMPT"
    BUYER_ACKNOWLEDGMENT    = "BUYER_ACKNOWLEDGMENT"

    # Transaction legitimacy
    MATCHING_IP_ADDRESS     = "MATCHING_IP_ADDRESS"
    MATCHING_DEVICE         = "MATCHING_DEVICE"
    PRIOR_SUCCESSFUL_ORDERS = "PRIOR_SUCCESSFUL_ORDERS"
    ADDRESS_VERIFICATION    = "ADDRESS_VERIFICATION"

    # Policy / ToS
    TOS_ACCEPTANCE          = "TOS_ACCEPTANCE"
    CANCELLATION_POLICY     = "CANCELLATION_POLICY"
    RETURN_POLICY           = "RETURN_POLICY"
    DESCRIPTION_ACCURACY    = "DESCRIPTION_ACCURACY"

    # Administrative
    INVOICE                 = "INVOICE"
    SHIPPING_LABEL          = "SHIPPING_LABEL"


# Base score contribution per evidence item (1-25)
_EVIDENCE_WEIGHTS: dict[EvidenceItem, float] = {
    EvidenceItem.TRACKING_NUMBER:           8.0,
    EvidenceItem.TRACKING_SHOWS_DELIVERED:  18.0,
    EvidenceItem.SIGNATURE_CONFIRMATION:    22.0,
    EvidenceItem.DELIVERY_PHOTO:            15.0,
    EvidenceItem.CARRIER_AFFIDAVIT:         20.0,

    EvidenceItem.PRODUCT_PHOTOS:            12.0,
    EvidenceItem.LISTING_SCREENSHOT:        10.0,
    EvidenceItem.PACKING_SLIP:              8.0,
    EvidenceItem.WEIGHT_MANIFEST:           10.0,

    EvidenceItem.BUYER_MESSAGES_LOG:        10.0,
    EvidenceItem.RESOLUTION_ATTEMPT:        8.0,
    EvidenceItem.BUYER_ACKNOWLEDGMENT:      14.0,

    EvidenceItem.MATCHING_IP_ADDRESS:       15.0,
    EvidenceItem.MATCHING_DEVICE:           18.0,
    EvidenceItem.PRIOR_SUCCESSFUL_ORDERS:   14.0,
    EvidenceItem.ADDRESS_VERIFICATION:      10.0,

    EvidenceItem.TOS_ACCEPTANCE:            20.0,
    EvidenceItem.CANCELLATION_POLICY:       15.0,
    EvidenceItem.RETURN_POLICY:             10.0,
    EvidenceItem.DESCRIPTION_ACCURACY:      12.0,

    EvidenceItem.INVOICE:                   8.0,
    EvidenceItem.SHIPPING_LABEL:            10.0,
}

# Per-claim-type relevance multiplier (1.0 = full weight, 0.3 = minimal relevance)
_CLAIM_RELEVANCE: dict[str, dict[EvidenceItem, float]] = {
    "INR": {
        EvidenceItem.TRACKING_SHOWS_DELIVERED:  1.5,
        EvidenceItem.SIGNATURE_CONFIRMATION:    1.5,
        EvidenceItem.DELIVERY_PHOTO:            1.3,
        EvidenceItem.CARRIER_AFFIDAVIT:         1.4,
        EvidenceItem.TRACKING_NUMBER:           1.2,
        EvidenceItem.SHIPPING_LABEL:            1.1,
        EvidenceItem.BUYER_MESSAGES_LOG:        0.8,
        EvidenceItem.TOS_ACCEPTANCE:            0.3,
        EvidenceItem.CANCELLATION_POLICY:       0.2,
    },
    "SNAD": {
        EvidenceItem.PRODUCT_PHOTOS:            1.5,
        EvidenceItem.LISTING_SCREENSHOT:        1.4,
        EvidenceItem.DESCRIPTION_ACCURACY:      1.4,
        EvidenceItem.RETURN_POLICY:             1.3,
        EvidenceItem.WEIGHT_MANIFEST:           1.2,
        EvidenceItem.BUYER_MESSAGES_LOG:        1.2,
        EvidenceItem.TRACKING_SHOWS_DELIVERED:  0.5,
        EvidenceItem.SIGNATURE_CONFIRMATION:    0.5,
    },
    "UNAUTHORIZED": {
        EvidenceItem.MATCHING_IP_ADDRESS:       1.5,
        EvidenceItem.MATCHING_DEVICE:           1.5,
        EvidenceItem.PRIOR_SUCCESSFUL_ORDERS:   1.4,
        EvidenceItem.ADDRESS_VERIFICATION:      1.3,
        EvidenceItem.TRACKING_SHOWS_DELIVERED:  1.0,
        EvidenceItem.SIGNATURE_CONFIRMATION:    1.0,
        EvidenceItem.BUYER_ACKNOWLEDGMENT:      0.6,
        EvidenceItem.TOS_ACCEPTANCE:            0.4,
    },
    "SUBSCRIPTION": {
        EvidenceItem.TOS_ACCEPTANCE:            1.5,
        EvidenceItem.CANCELLATION_POLICY:       1.5,
        EvidenceItem.BUYER_ACKNOWLEDGMENT:      1.4,
        EvidenceItem.BUYER_MESSAGES_LOG:        1.2,
        EvidenceItem.RESOLUTION_ATTEMPT:        1.0,
        EvidenceItem.TRACKING_SHOWS_DELIVERED:  0.2,
    },
    "DUPLICATE": {
        EvidenceItem.INVOICE:                   1.5,
        EvidenceItem.BUYER_MESSAGES_LOG:        1.3,
        EvidenceItem.RESOLUTION_ATTEMPT:        1.2,
        EvidenceItem.BUYER_ACKNOWLEDGMENT:      1.4,
        EvidenceItem.TRACKING_SHOWS_DELIVERED:  0.6,
    },
}

# Base win-rate boost per evidence tier (additive pp)
_TIER_WIN_BOOST: dict[EvidenceTier, float] = {
    EvidenceTier.EXCELLENT:    0.22,
    EvidenceTier.GOOD:         0.14,
    EvidenceTier.ADEQUATE:     0.07,
    EvidenceTier.WEAK:         0.02,
    EvidenceTier.INSUFFICIENT: -0.05,
}


@dataclass
class EvidencePackage:
    """Evidence a merchant has collected for one dispute."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / DUPLICATE
    items: list[EvidenceItem] = field(default_factory=list)
    notes: str = ""               # Free-form notes about evidence quality


@dataclass
class EvidenceGap:
    """An evidence item not present but recommended."""
    item: EvidenceItem
    importance: str               # HIGH / MEDIUM / LOW
    action: str                   # What to do to get it


@dataclass
class EvidenceScoreReport:
    """
    Complete evidence quality assessment for one dispute.
    """
    dispute_id: str
    claim_type: str

    # Score & tier
    raw_score: float              # 0-100 before normalization
    normalized_score: float       # 0-100 normalized to claim type max
    tier: EvidenceTier

    # Win-rate impact
    win_rate_boost: float         # Additive adjustment to base win rate

    # What merchant has
    items_present: list[EvidenceItem]
    items_present_count: int

    # What's missing
    gaps: list[EvidenceGap]
    critical_gaps: list[EvidenceItem]   # Missing items that heavily impact the case

    # Scoring breakdown
    top_evidence_items: list[tuple[EvidenceItem, float]]  # (item, weighted_score)

    # Guidance
    collection_priority: list[str]   # Ordered list of things to collect
    summary: str


def _tier_from_score(score: float) -> EvidenceTier:
    if score >= 85:
        return EvidenceTier.EXCELLENT
    if score >= 65:
        return EvidenceTier.GOOD
    if score >= 45:
        return EvidenceTier.ADEQUATE
    if score >= 25:
        return EvidenceTier.WEAK
    return EvidenceTier.INSUFFICIENT


def _critical_gap_items(claim_type: str) -> list[EvidenceItem]:
    """Items that are near-mandatory for each claim type."""
    critical = {
        "INR": [EvidenceItem.TRACKING_SHOWS_DELIVERED, EvidenceItem.TRACKING_NUMBER],
        "SNAD": [EvidenceItem.PRODUCT_PHOTOS, EvidenceItem.LISTING_SCREENSHOT],
        "UNAUTHORIZED": [EvidenceItem.MATCHING_IP_ADDRESS, EvidenceItem.MATCHING_DEVICE],
        "SUBSCRIPTION": [EvidenceItem.TOS_ACCEPTANCE, EvidenceItem.CANCELLATION_POLICY],
        "DUPLICATE": [EvidenceItem.INVOICE],
    }
    return critical.get(claim_type, [EvidenceItem.BUYER_MESSAGES_LOG])


def _recommended_items(claim_type: str) -> list[EvidenceItem]:
    """Full recommended list for a claim type, ordered by importance."""
    recs = {
        "INR": [
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.SIGNATURE_CONFIRMATION,
            EvidenceItem.DELIVERY_PHOTO,
            EvidenceItem.CARRIER_AFFIDAVIT,
            EvidenceItem.TRACKING_NUMBER,
            EvidenceItem.SHIPPING_LABEL,
            EvidenceItem.BUYER_MESSAGES_LOG,
            EvidenceItem.PACKING_SLIP,
        ],
        "SNAD": [
            EvidenceItem.PRODUCT_PHOTOS,
            EvidenceItem.LISTING_SCREENSHOT,
            EvidenceItem.DESCRIPTION_ACCURACY,
            EvidenceItem.RETURN_POLICY,
            EvidenceItem.WEIGHT_MANIFEST,
            EvidenceItem.BUYER_MESSAGES_LOG,
            EvidenceItem.RESOLUTION_ATTEMPT,
        ],
        "UNAUTHORIZED": [
            EvidenceItem.MATCHING_IP_ADDRESS,
            EvidenceItem.MATCHING_DEVICE,
            EvidenceItem.PRIOR_SUCCESSFUL_ORDERS,
            EvidenceItem.ADDRESS_VERIFICATION,
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.SIGNATURE_CONFIRMATION,
        ],
        "SUBSCRIPTION": [
            EvidenceItem.TOS_ACCEPTANCE,
            EvidenceItem.CANCELLATION_POLICY,
            EvidenceItem.BUYER_ACKNOWLEDGMENT,
            EvidenceItem.BUYER_MESSAGES_LOG,
            EvidenceItem.RESOLUTION_ATTEMPT,
        ],
        "DUPLICATE": [
            EvidenceItem.INVOICE,
            EvidenceItem.BUYER_ACKNOWLEDGMENT,
            EvidenceItem.BUYER_MESSAGES_LOG,
            EvidenceItem.RESOLUTION_ATTEMPT,
        ],
    }
    return recs.get(claim_type, list(EvidenceItem))


def score_evidence(pkg: EvidencePackage) -> EvidenceScoreReport:
    """
    Score the quality of a merchant's evidence package for one dispute.
    """
    relevance = _CLAIM_RELEVANCE.get(pkg.claim_type, {})
    present_set = set(pkg.items)

    # Compute weighted score per item
    item_scores: list[tuple[EvidenceItem, float]] = []
    for item in pkg.items:
        base = _EVIDENCE_WEIGHTS.get(item, 5.0)
        multiplier = relevance.get(item, 1.0)
        item_scores.append((item, round(base * multiplier, 2)))

    item_scores.sort(key=lambda x: -x[1])

    # Raw score: sum of item scores, capped at 100
    raw = round(min(100.0, sum(s for _, s in item_scores)), 1)

    # Max possible score for this claim type (top-N items)
    recommended = _recommended_items(pkg.claim_type)
    max_possible = min(100.0, sum(
        _EVIDENCE_WEIGHTS.get(i, 5.0) * relevance.get(i, 1.0)
        for i in recommended[:8]
    ))
    normalized = round(min(100.0, raw / max(max_possible, 1.0) * 100.0), 1)

    tier = _tier_from_score(normalized)
    win_boost = _TIER_WIN_BOOST[tier]

    # Gaps
    critical_items = _critical_gap_items(pkg.claim_type)
    critical_gaps = [i for i in critical_items if i not in present_set]

    all_recommended = recommended[:8]
    gaps: list[EvidenceGap] = []
    for item in all_recommended:
        if item not in present_set:
            is_critical = item in critical_items
            gap_actions = {
                EvidenceItem.TRACKING_SHOWS_DELIVERED: "Screenshot the tracking page showing 'Delivered' status",
                EvidenceItem.SIGNATURE_CONFIRMATION:   "Request proof of delivery with signature from carrier",
                EvidenceItem.DELIVERY_PHOTO:           "Contact carrier to retrieve delivery photo",
                EvidenceItem.PRODUCT_PHOTOS:           "Photograph the actual item vs listing photos side-by-side",
                EvidenceItem.LISTING_SCREENSHOT:       "Screenshot the original product listing at time of sale",
                EvidenceItem.MATCHING_IP_ADDRESS:      "Export transaction IP from your payment processor",
                EvidenceItem.MATCHING_DEVICE:          "Pull device fingerprint from your platform's fraud tools",
                EvidenceItem.TOS_ACCEPTANCE:           "Export ToS acceptance log with timestamp and IP",
                EvidenceItem.CANCELLATION_POLICY:      "Screenshot your subscription cancellation policy page",
                EvidenceItem.INVOICE:                  "Attach the original transaction invoice/receipt",
                EvidenceItem.BUYER_MESSAGES_LOG:       "Export the full PayPal messaging thread",
            }
            gaps.append(EvidenceGap(
                item=item,
                importance="HIGH" if is_critical else "MEDIUM",
                action=gap_actions.get(item, f"Collect {item.value.lower().replace('_', ' ')}"),
            ))

    # Collection priority
    priority = []
    if critical_gaps:
        priority.append(f"CRITICAL: Collect {', '.join(i.value for i in critical_gaps)}")
    for gap in gaps[:3]:
        priority.append(f"Collect {gap.item.value.lower().replace('_', ' ')} ({gap.importance})")
    if tier in (EvidenceTier.EXCELLENT, EvidenceTier.GOOD):
        priority.append("Evidence is strong — submit promptly before deadline")

    summary = (
        f"Evidence {pkg.dispute_id}: {pkg.claim_type} | "
        f"score={normalized:.0f}/100 ({tier.value}) | "
        f"items={len(pkg.items)} | "
        f"win_boost={win_boost:+.0%} | "
        f"critical_gaps={len(critical_gaps)}"
    )

    return EvidenceScoreReport(
        dispute_id=pkg.dispute_id,
        claim_type=pkg.claim_type,
        raw_score=raw,
        normalized_score=normalized,
        tier=tier,
        win_rate_boost=win_boost,
        items_present=list(pkg.items),
        items_present_count=len(pkg.items),
        gaps=gaps,
        critical_gaps=critical_gaps,
        top_evidence_items=item_scores[:5],
        collection_priority=priority,
        summary=summary,
    )
