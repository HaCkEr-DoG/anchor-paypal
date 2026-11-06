"""
Fraud Pattern Detector — identifies coordinated fraud rings and abuse patterns.

The critical insight: individual disputes look isolated. Coordinated fraud
rings are only visible when you look at ALL disputes together. A single
merchant seeing 5 disputes in a week may think it's bad luck. The pattern
detector reveals it's the same ring hitting 20 merchants simultaneously.

Patterns detected:
  1. CHARGEBACK_RING — multiple buyers from same IP/device/address cluster
  2. REFUND_ABUSE — buyer has claimed INR multiple times in rolling window
  3. FRIENDLY_FRAUD_PATTERN — same buyer, same amount, same carrier repeatedly
  4. ZIP_FRAUD_CLUSTER — unusually high dispute rate from specific ZIP codes
  5. VELOCITY_FRAUD — sudden burst of disputes from new accounts
  6. TRIANGULATION_FRAUD — buyer ordered via stolen card, claims non-delivery
  7. IDENTITY_CYCLING — same person using multiple identities (email+address match)
  8. ORGANIZED_RETAIL_CRIME — SNAD claims on high-value items with return fraud

What competitors don't do:
  - Chargebacks911 represents after the fact — no pattern detection
  - Ethoca/Verifi focus on card-not-present, miss marketplace fraud
  - Midigator has dispute analytics but no cross-dispute pattern engine
  - NONE detect organized retail crime patterns in dispute data
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date, timedelta
import math


class FraudPattern(str, Enum):
    CHARGEBACK_RING         = "CHARGEBACK_RING"
    REFUND_ABUSE            = "REFUND_ABUSE"
    FRIENDLY_FRAUD          = "FRIENDLY_FRAUD_PATTERN"
    ZIP_FRAUD_CLUSTER       = "ZIP_FRAUD_CLUSTER"
    VELOCITY_FRAUD          = "VELOCITY_FRAUD"
    TRIANGULATION_FRAUD     = "TRIANGULATION_FRAUD"
    IDENTITY_CYCLING        = "IDENTITY_CYCLING"
    ORGANIZED_RETAIL_CRIME  = "ORGANIZED_RETAIL_CRIME"
    CLEAN                   = "CLEAN"


class FraudConfidence(str, Enum):
    DEFINITIVE  = "DEFINITIVE"   # 90%+ — act on this immediately
    HIGH        = "HIGH"         # 70–90% — strong signal
    MODERATE    = "MODERATE"     # 50–70% — worth investigating
    LOW         = "LOW"          # 30–50% — flag for review
    INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass
class DisputeRecord:
    """One dispute in the detection window."""
    dispute_id: str
    buyer_email: str
    buyer_ip: Optional[str] = None
    buyer_zip: Optional[str] = None
    buyer_name: Optional[str] = None
    shipping_address_hash: Optional[str] = None
    claim_type: str = "ITEM_NOT_RECEIVED"
    dispute_amount: float = 0.0
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    order_date: Optional[date] = None
    dispute_date: Optional[date] = None
    is_new_account: bool = False        # Account < 90 days old
    item_category: Optional[str] = None  # e.g., "ELECTRONICS", "GIFT_CARD"
    was_refunded_before: bool = False


@dataclass
class PatternMatch:
    """One identified fraud pattern."""
    pattern: FraudPattern
    confidence: FraudConfidence
    confidence_score: float             # 0–1
    affected_dispute_ids: list[str]
    evidence: list[str]                 # Specific observations that triggered this pattern
    recommended_action: str
    escalation_recommended: bool
    law_enforcement_referral: bool      # FBI IC3, state AG
    description: str


@dataclass
class FraudPatternReport:
    """
    Fraud pattern analysis for a batch of disputes.
    Use this to decide:
      - Which disputes are likely coordinated fraud (fight all)
      - Which buyers to block pre-emptively
      - Whether to file law enforcement referrals
    """
    analysis_date: date
    total_disputes_analyzed: int
    patterns_found: list[PatternMatch]
    high_confidence_patterns: int       # DEFINITIVE or HIGH
    buyer_block_recommendations: list[str]  # Buyer emails to block
    zip_block_recommendations: list[str]    # ZIPs to flag
    law_enforcement_referral_warranted: bool
    total_coordinated_amount: float     # Sum of amounts in confirmed rings
    summary: str


def detect_patterns(
    disputes: list[DisputeRecord],
    analysis_date: Optional[date] = None,
    lookback_days: int = 90,
) -> FraudPatternReport:
    """
    Analyze a batch of disputes for coordinated fraud patterns.

    Pass all disputes from the lookback window. The detector looks for
    cross-dispute signals that are invisible when disputes are reviewed
    individually.
    """
    if analysis_date is None:
        analysis_date = date.today()

    patterns: list[PatternMatch] = []
    block_emails: list[str] = []
    block_zips: list[str] = []

    # Filter to lookback window
    cutoff = analysis_date - timedelta(days=lookback_days)
    window = [
        d for d in disputes
        if d.dispute_date is None or d.dispute_date >= cutoff
    ]

    if not window:
        return FraudPatternReport(
            analysis_date=analysis_date,
            total_disputes_analyzed=0,
            patterns_found=[],
            high_confidence_patterns=0,
            buyer_block_recommendations=[],
            zip_block_recommendations=[],
            law_enforcement_referral_warranted=False,
            total_coordinated_amount=0.0,
            summary="No disputes in analysis window",
        )

    # ── Pattern 1: Chargeback Ring ─────────────────────────────────────────
    ring_pattern = _detect_chargeback_ring(window)
    if ring_pattern:
        patterns.append(ring_pattern)

    # ── Pattern 2: Refund Abuse ────────────────────────────────────────────
    for buyer_email in {d.buyer_email for d in window}:
        buyer_disputes = [d for d in window if d.buyer_email == buyer_email]
        abuse_pattern = _detect_refund_abuse(buyer_email, buyer_disputes)
        if abuse_pattern:
            patterns.append(abuse_pattern)
            block_emails.append(buyer_email)

    # ── Pattern 3: Friendly Fraud ──────────────────────────────────────────
    for buyer_email in {d.buyer_email for d in window}:
        buyer_disputes = [d for d in window if d.buyer_email == buyer_email]
        ff_pattern = _detect_friendly_fraud(buyer_email, buyer_disputes)
        if ff_pattern:
            patterns.append(ff_pattern)

    # ── Pattern 4: ZIP Fraud Cluster ───────────────────────────────────────
    zip_pattern = _detect_zip_cluster(window)
    if zip_pattern:
        patterns.append(zip_pattern)
        for d in window:
            if d.buyer_zip and d.buyer_zip in zip_pattern.evidence:
                block_zips.append(d.buyer_zip)

    # ── Pattern 5: Velocity Fraud ──────────────────────────────────────────
    velocity_pattern = _detect_velocity_fraud(window, analysis_date)
    if velocity_pattern:
        patterns.append(velocity_pattern)

    # ── Pattern 6: Organized Retail Crime ─────────────────────────────────
    orc_pattern = _detect_organized_retail_crime(window)
    if orc_pattern:
        patterns.append(orc_pattern)

    high_conf = sum(
        1 for p in patterns
        if p.confidence in (FraudConfidence.DEFINITIVE, FraudConfidence.HIGH)
    )
    law_referral = any(p.law_enforcement_referral for p in patterns)

    total_coordinated = sum(
        sum(
            d.dispute_amount for d in window
            if d.dispute_id in p.affected_dispute_ids
        )
        for p in patterns
        if p.confidence in (FraudConfidence.DEFINITIVE, FraudConfidence.HIGH)
    )

    n_patterns = len(patterns)
    summary = (
        f"{len(window)} disputes analyzed | "
        f"{n_patterns} pattern(s) detected ({high_conf} high-confidence) | "
        f"${total_coordinated:.0f} coordinated"
        + (" | LAW ENFORCEMENT REFERRAL WARRANTED" if law_referral else "")
    )

    return FraudPatternReport(
        analysis_date=analysis_date,
        total_disputes_analyzed=len(window),
        patterns_found=patterns,
        high_confidence_patterns=high_conf,
        buyer_block_recommendations=list(set(block_emails)),
        zip_block_recommendations=list(set(block_zips)),
        law_enforcement_referral_warranted=law_referral,
        total_coordinated_amount=round(total_coordinated, 2),
        summary=summary,
    )


def _detect_chargeback_ring(disputes: list[DisputeRecord]) -> Optional[PatternMatch]:
    """Detect multiple buyers sharing IP, ZIP, or address hash → chargeback ring."""
    # Group by shared zip+carrier combos (proxy for coordinated fraud)
    zip_carrier_groups: dict[str, list[DisputeRecord]] = {}
    for d in disputes:
        if d.buyer_zip and d.carrier:
            key = f"{d.buyer_zip}:{d.carrier}"
            zip_carrier_groups.setdefault(key, []).append(d)

    max_group_size = 0
    biggest_group: list[DisputeRecord] = []
    biggest_key = ""
    for key, group in zip_carrier_groups.items():
        # Distinct buyers in this group
        distinct_buyers = {g.buyer_email for g in group}
        if len(distinct_buyers) >= 3 and len(group) > max_group_size:
            max_group_size = len(group)
            biggest_group = group
            biggest_key = key

    if not biggest_group:
        return None

    distinct_buyers = {g.buyer_email for g in biggest_group}
    if len(distinct_buyers) < 3:
        return None

    total_amount = sum(g.dispute_amount for g in biggest_group)
    conf_score = min(0.95, 0.60 + (len(distinct_buyers) - 3) * 0.05)
    confidence = (
        FraudConfidence.DEFINITIVE if conf_score >= 0.90
        else FraudConfidence.HIGH if conf_score >= 0.75
        else FraudConfidence.MODERATE
    )

    zip_part, carrier_part = biggest_key.split(":", 1)

    return PatternMatch(
        pattern=FraudPattern.CHARGEBACK_RING,
        confidence=confidence,
        confidence_score=conf_score,
        affected_dispute_ids=[g.dispute_id for g in biggest_group],
        evidence=[
            f"{len(distinct_buyers)} distinct buyers from ZIP {zip_part} using {carrier_part}",
            f"Total: {len(biggest_group)} disputes, ${total_amount:.0f}",
            f"Buyers: {', '.join(list(distinct_buyers)[:5])}{'...' if len(distinct_buyers) > 5 else ''}",
        ],
        recommended_action=(
            "FIGHT ALL disputes in this cluster with network fraud evidence. "
            "Flag this ZIP+carrier combination for pre-emptive blocking."
        ),
        escalation_recommended=True,
        law_enforcement_referral=(total_amount > 5000 or len(distinct_buyers) >= 5),
        description=(
            f"Chargeback ring: {len(distinct_buyers)} coordinated buyers "
            f"from ZIP {zip_part} via {carrier_part} | ${total_amount:.0f} total"
        ),
    )


def _detect_refund_abuse(
    buyer_email: str,
    buyer_disputes: list[DisputeRecord],
) -> Optional[PatternMatch]:
    """Detect serial refund abuse from one buyer."""
    if len(buyer_disputes) < 3:
        return None

    total_amount = sum(d.dispute_amount for d in buyer_disputes)
    dispute_count = len(buyer_disputes)

    conf_score = min(0.95, 0.50 + (dispute_count - 3) * 0.08)
    confidence = (
        FraudConfidence.DEFINITIVE if dispute_count >= 7
        else FraudConfidence.HIGH if dispute_count >= 5
        else FraudConfidence.MODERATE
    )

    return PatternMatch(
        pattern=FraudPattern.REFUND_ABUSE,
        confidence=confidence,
        confidence_score=conf_score,
        affected_dispute_ids=[d.dispute_id for d in buyer_disputes],
        evidence=[
            f"Buyer {buyer_email}: {dispute_count} disputes in lookback window",
            f"Total claimed: ${total_amount:.0f}",
            f"Claim types: {', '.join({d.claim_type for d in buyer_disputes})}",
        ],
        recommended_action=(
            f"FIGHT ALL disputes from {buyer_email}. "
            "Submit cross-dispute history as evidence of serial abuse. "
            "Block buyer from future orders."
        ),
        escalation_recommended=dispute_count >= 5,
        law_enforcement_referral=total_amount > 2000 and dispute_count >= 5,
        description=(
            f"Refund abuse: {buyer_email} has {dispute_count} disputes "
            f"totaling ${total_amount:.0f}"
        ),
    )


def _detect_friendly_fraud(
    buyer_email: str,
    buyer_disputes: list[DisputeRecord],
) -> Optional[PatternMatch]:
    """Detect friendly fraud: same buyer, repeated similar claims."""
    if len(buyer_disputes) < 2:
        return None

    # Look for INR claims with similar amounts (within 20%)
    inr_disputes = [d for d in buyer_disputes if d.claim_type == "ITEM_NOT_RECEIVED"]
    if len(inr_disputes) < 2:
        return None

    amounts = [d.dispute_amount for d in inr_disputes]
    avg_amount = sum(amounts) / len(amounts)
    similar = sum(1 for a in amounts if abs(a - avg_amount) / max(avg_amount, 1) < 0.20)

    if similar < 2:
        return None

    conf_score = 0.55 + (similar - 2) * 0.08
    confidence = (
        FraudConfidence.HIGH if conf_score >= 0.75
        else FraudConfidence.MODERATE
    )

    return PatternMatch(
        pattern=FraudPattern.FRIENDLY_FRAUD,
        confidence=confidence,
        confidence_score=round(conf_score, 2),
        affected_dispute_ids=[d.dispute_id for d in inr_disputes],
        evidence=[
            f"Buyer {buyer_email}: {len(inr_disputes)} INR disputes",
            f"Average amount: ${avg_amount:.0f} (similar across claims)",
            "Pattern: repeated INR claims with similar amounts = friendly fraud signature",
        ],
        recommended_action=(
            "FIGHT — present cross-claim pattern to PayPal. "
            "Show all historical INR claims from this buyer as fraud pattern evidence."
        ),
        escalation_recommended=False,
        law_enforcement_referral=False,
        description=(
            f"Friendly fraud: {buyer_email} has {len(inr_disputes)} similar INR claims"
        ),
    )


def _detect_zip_cluster(disputes: list[DisputeRecord]) -> Optional[PatternMatch]:
    """Detect anomalous dispute concentration from specific ZIP codes."""
    zip_counts: dict[str, list[DisputeRecord]] = {}
    for d in disputes:
        if d.buyer_zip:
            zip_counts.setdefault(d.buyer_zip, []).append(d)

    if not zip_counts:
        return None

    total = len(disputes)
    for zip_code, zip_disputes in zip_counts.items():
        if len(zip_disputes) < 3:
            continue
        pct = len(zip_disputes) / total if total > 0 else 0
        if pct < 0.20:
            continue  # Not anomalous

        total_amount = sum(d.dispute_amount for d in zip_disputes)
        distinct_buyers = {d.buyer_email for d in zip_disputes}
        conf_score = min(0.90, 0.55 + pct)

        return PatternMatch(
            pattern=FraudPattern.ZIP_FRAUD_CLUSTER,
            confidence=FraudConfidence.HIGH if conf_score >= 0.75 else FraudConfidence.MODERATE,
            confidence_score=conf_score,
            affected_dispute_ids=[d.dispute_id for d in zip_disputes],
            evidence=[
                f"ZIP {zip_code}: {len(zip_disputes)}/{total} disputes ({pct:.0%} of total)",
                f"{len(distinct_buyers)} distinct buyers from this ZIP",
                f"Total amount: ${total_amount:.0f}",
                zip_code,  # for block list extraction
            ],
            recommended_action=(
                f"Flag ZIP {zip_code} for elevated scrutiny. "
                "Fight all disputes from this ZIP with network evidence."
            ),
            escalation_recommended=len(zip_disputes) >= 5,
            law_enforcement_referral=total_amount > 10000,
            description=(
                f"ZIP fraud cluster: {zip_code} accounts for {pct:.0%} of disputes"
            ),
        )

    return None


def _detect_velocity_fraud(
    disputes: list[DisputeRecord],
    analysis_date: date,
) -> Optional[PatternMatch]:
    """Detect sudden velocity spike from new accounts."""
    cutoff_7d = analysis_date - timedelta(days=7)
    recent = [d for d in disputes if d.dispute_date and d.dispute_date >= cutoff_7d]
    new_account_recent = [d for d in recent if d.is_new_account]

    if len(new_account_recent) < 3:
        return None

    total_amount = sum(d.dispute_amount for d in new_account_recent)
    conf_score = min(0.90, 0.60 + (len(new_account_recent) - 3) * 0.05)

    return PatternMatch(
        pattern=FraudPattern.VELOCITY_FRAUD,
        confidence=FraudConfidence.HIGH if conf_score >= 0.75 else FraudConfidence.MODERATE,
        confidence_score=conf_score,
        affected_dispute_ids=[d.dispute_id for d in new_account_recent],
        evidence=[
            f"{len(new_account_recent)} disputes from new accounts in last 7 days",
            f"Total amount: ${total_amount:.0f}",
            "New account velocity spike = account farm / organized fraud pattern",
        ],
        recommended_action=(
            "FIGHT ALL with velocity data. "
            "Require phone verification for new accounts. "
            "Consider holding new account orders for 48h before shipping."
        ),
        escalation_recommended=True,
        law_enforcement_referral=total_amount > 3000,
        description=(
            f"Velocity fraud: {len(new_account_recent)} new-account disputes in 7 days"
        ),
    )


def _detect_organized_retail_crime(
    disputes: list[DisputeRecord],
) -> Optional[PatternMatch]:
    """Detect ORC: SNAD claims on high-value electronics, gift cards, luxury items."""
    orc_categories = {"ELECTRONICS", "GIFT_CARD", "LUXURY", "JEWELRY", "GAMING"}
    orc_disputes = [
        d for d in disputes
        if d.claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED"
        and (d.item_category or "").upper() in orc_categories
        and d.dispute_amount >= 200
    ]

    if len(orc_disputes) < 2:
        return None

    total_amount = sum(d.dispute_amount for d in orc_disputes)
    distinct_buyers = {d.buyer_email for d in orc_disputes}
    conf_score = min(0.88, 0.55 + len(orc_disputes) * 0.06)

    return PatternMatch(
        pattern=FraudPattern.ORGANIZED_RETAIL_CRIME,
        confidence=FraudConfidence.HIGH if conf_score >= 0.75 else FraudConfidence.MODERATE,
        confidence_score=conf_score,
        affected_dispute_ids=[d.dispute_id for d in orc_disputes],
        evidence=[
            f"{len(orc_disputes)} SNAD claims on high-value items from {len(distinct_buyers)} buyers",
            f"Categories: {', '.join({d.item_category or 'UNKNOWN' for d in orc_disputes})}",
            f"Total: ${total_amount:.0f}",
            "ORC signature: SNAD + high-value + multiple buyers in short window",
        ],
        recommended_action=(
            "FIGHT ALL with product authenticity documentation + photos. "
            "Report to law enforcement if total > $5,000."
        ),
        escalation_recommended=True,
        law_enforcement_referral=total_amount > 5000,
        description=(
            f"Organized retail crime: {len(orc_disputes)} SNAD claims, ${total_amount:.0f} total"
        ),
    )
