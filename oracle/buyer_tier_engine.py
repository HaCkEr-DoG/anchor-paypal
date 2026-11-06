"""
Buyer Tier Engine — buyer risk/value classification for dispute decision weighting.

PayPal merchants deal with two types of dispute filers:
  1. Legitimate buyers: purchased in good faith, genuine problems
  2. Bad actors: serial fraudsters, organized ring members, opportunistic abusers

This module classifies buyers into tiers based on their history and assigns
per-tier policy overrides for how disputes from them should be handled.

Tiers:
  GOLD_CUSTOMER    — high-value, low-dispute, long tenure. Fight rarely; settle generously.
  STANDARD_BUYER   — typical buyer with no red flags.
  ELEVATED_WATCH   — some signals but not conclusive (1-2 disputes, new account).
  HIGH_RISK_BUYER  — multiple disputes, refund pattern, possible churner.
  FRAUDSTER        — confirmed bad actor (3+ disputes, address cluster, card test).
  INTERNAL_REVIEW  — edge case requiring human review (high-value account + signals).

Tier policy overrides:
  - Win threshold adjustment (± pp on fight threshold)
  - Settlement generosity multiplier
  - Auto-refund without fight on small amounts
  - Escalate to fraud team flag
  - Block future orders flag

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BuyerTier(str, Enum):
    GOLD_CUSTOMER   = "GOLD_CUSTOMER"
    STANDARD_BUYER  = "STANDARD_BUYER"
    ELEVATED_WATCH  = "ELEVATED_WATCH"
    HIGH_RISK_BUYER = "HIGH_RISK_BUYER"
    FRAUDSTER       = "FRAUDSTER"
    INTERNAL_REVIEW = "INTERNAL_REVIEW"


class BuyerSignalFlag(str, Enum):
    HIGH_DISPUTE_RATE       = "HIGH_DISPUTE_RATE"       # Dispute rate > 15%
    RAPID_ACCOUNT           = "RAPID_ACCOUNT"           # Account < 90 days old
    REPEAT_CLAIMANT         = "REPEAT_CLAIMANT"         # 2+ disputes in 90d
    SERIAL_FRAUDSTER        = "SERIAL_FRAUDSTER"        # 3+ disputes in 90d
    ADDRESS_MATCH           = "ADDRESS_MATCH"           # Shares address with other disputers
    HIGH_LIFETIME_VALUE     = "HIGH_LIFETIME_VALUE"     # LTV ≥ $1,000
    LOYAL_CUSTOMER          = "LOYAL_CUSTOMER"          # 10+ purchases
    ZERO_DISPUTE_HISTORY    = "ZERO_DISPUTE_HISTORY"    # First ever dispute
    ESCALATING_AMOUNTS      = "ESCALATING_AMOUNTS"      # Each dispute larger than previous
    PATTERN_CLAIMANT        = "PATTERN_CLAIMANT"        # Always claims same reason (INR/SNAD)


@dataclass
class BuyerProfile:
    """
    Buyer's known history at the time of a dispute decision.
    """
    buyer_id: str
    email: str

    # History
    total_purchases: int = 0
    total_purchases_last_90d: int = 0
    total_lifetime_spend: float = 0.0
    account_age_days: int = 365

    # Dispute history
    dispute_count_90d: int = 0
    dispute_count_lifetime: int = 0
    dispute_amounts_90d: list[float] = field(default_factory=list)
    dispute_reasons_90d: list[str] = field(default_factory=list)  # INR/SNAD/UNAUTHORIZED
    past_disputes_won_by_merchant: int = 0

    # Network signals
    shares_address_with_disputers: bool = False
    is_in_fraud_network: bool = False


@dataclass
class BuyerTierPolicy:
    """
    Policy overrides that apply when processing disputes from buyers in this tier.
    """
    tier: BuyerTier

    # Fight/settle threshold adjustments (additive to base thresholds)
    fight_threshold_adjustment: float = 0.0   # +pp = easier to fight; -pp = easier to settle
    min_ev_to_fight_adjustment: float = 0.0   # Additive to base min EV threshold

    # Settlement generosity
    settlement_offer_multiplier: float = 1.0   # < 1 = offer less; > 1 = offer more

    # Routing overrides
    auto_refund_below: float = 0.0            # $ — auto-refund disputes below this
    escalate_to_fraud: bool = False
    block_future_orders: bool = False
    require_human_review: bool = False

    # EV impact
    ev_confidence_multiplier: float = 1.0     # < 1 = distrust win probability (fraudsters game system)

    description: str = ""


_TIER_POLICIES: dict[BuyerTier, BuyerTierPolicy] = {
    BuyerTier.GOLD_CUSTOMER: BuyerTierPolicy(
        tier=BuyerTier.GOLD_CUSTOMER,
        fight_threshold_adjustment=-15.0,     # Much less willing to fight gold customers
        min_ev_to_fight_adjustment=+20.0,     # Need higher EV before fighting
        settlement_offer_multiplier=1.3,      # Offer 30% more generously
        auto_refund_below=100.0,              # Auto-refund up to $100 without question
        description="High-value loyal customer — preserve relationship over winning.",
    ),
    BuyerTier.STANDARD_BUYER: BuyerTierPolicy(
        tier=BuyerTier.STANDARD_BUYER,
        description="Typical buyer — use baseline thresholds.",
    ),
    BuyerTier.ELEVATED_WATCH: BuyerTierPolicy(
        tier=BuyerTier.ELEVATED_WATCH,
        fight_threshold_adjustment=+5.0,      # Slightly more willing to fight
        min_ev_to_fight_adjustment=-5.0,      # Lower EV needed to justify fight
        settlement_offer_multiplier=0.9,      # Slightly less generous
        auto_refund_below=0.0,
        description="Some risk signals — monitor closely, modest fight preference.",
    ),
    BuyerTier.HIGH_RISK_BUYER: BuyerTierPolicy(
        tier=BuyerTier.HIGH_RISK_BUYER,
        fight_threshold_adjustment=+10.0,     # More willing to fight
        min_ev_to_fight_adjustment=-10.0,
        settlement_offer_multiplier=0.75,     # 25% less generous
        auto_refund_below=0.0,
        description="Multiple disputes — fight more aggressively; settle less.",
    ),
    BuyerTier.FRAUDSTER: BuyerTierPolicy(
        tier=BuyerTier.FRAUDSTER,
        fight_threshold_adjustment=+20.0,     # Always fight
        min_ev_to_fight_adjustment=-25.0,     # Fight even at negative EV
        settlement_offer_multiplier=0.0,      # Never settle
        escalate_to_fraud=True,
        block_future_orders=True,
        ev_confidence_multiplier=0.7,         # Distrust win-rate (fraudsters game evidence)
        description="Confirmed fraudster — fight, escalate, block.",
    ),
    BuyerTier.INTERNAL_REVIEW: BuyerTierPolicy(
        tier=BuyerTier.INTERNAL_REVIEW,
        require_human_review=True,
        description="Edge case — human review required before action.",
    ),
}


@dataclass
class BuyerTierReport:
    """
    Complete buyer tier classification for one dispute decision.
    """
    buyer_id: str
    tier: BuyerTier
    tier_score: float          # 0-100 (higher = more risk)
    policy: BuyerTierPolicy
    flags: list[BuyerSignalFlag]
    signal_count: int

    # Applied adjustments (ready to pass to claim router)
    adjusted_fight_threshold: float    # Base 72.0 + adjustment
    adjusted_min_ev: float             # Base $20 + adjustment
    adjusted_settlement_multiplier: float
    should_escalate: bool
    should_block: bool

    rationale: str
    summary: str


def _detect_flags(buyer: BuyerProfile) -> list[BuyerSignalFlag]:
    flags: list[BuyerSignalFlag] = []

    # Dispute rate flag
    if buyer.total_purchases_last_90d > 0:
        dispute_rate = buyer.dispute_count_90d / buyer.total_purchases_last_90d
        if dispute_rate > 0.15:
            flags.append(BuyerSignalFlag.HIGH_DISPUTE_RATE)

    # New account
    if buyer.account_age_days < 90:
        flags.append(BuyerSignalFlag.RAPID_ACCOUNT)

    # Dispute history
    if buyer.dispute_count_90d >= 3:
        flags.append(BuyerSignalFlag.SERIAL_FRAUDSTER)
    elif buyer.dispute_count_90d == 2:
        flags.append(BuyerSignalFlag.REPEAT_CLAIMANT)

    # Network
    if buyer.shares_address_with_disputers:
        flags.append(BuyerSignalFlag.ADDRESS_MATCH)

    # Value signals (positive)
    if buyer.total_lifetime_spend >= 1000.0:
        flags.append(BuyerSignalFlag.HIGH_LIFETIME_VALUE)
    if buyer.total_purchases >= 10:
        flags.append(BuyerSignalFlag.LOYAL_CUSTOMER)
    if buyer.dispute_count_lifetime == 0 or (
        buyer.dispute_count_lifetime == 1 and buyer.dispute_count_90d == 1
    ):
        if buyer.total_purchases >= 2:
            flags.append(BuyerSignalFlag.ZERO_DISPUTE_HISTORY)

    # Escalating dispute amounts
    if len(buyer.dispute_amounts_90d) >= 2:
        sorted_amts = sorted(buyer.dispute_amounts_90d)
        if sorted_amts[-1] > sorted_amts[0] * 1.5:
            flags.append(BuyerSignalFlag.ESCALATING_AMOUNTS)

    # Pattern claimant (always same reason)
    if len(buyer.dispute_reasons_90d) >= 2:
        if len(set(buyer.dispute_reasons_90d)) == 1:
            flags.append(BuyerSignalFlag.PATTERN_CLAIMANT)

    return flags


def _compute_tier(buyer: BuyerProfile, flags: list[BuyerSignalFlag]) -> tuple[BuyerTier, float]:
    """
    Classify buyer tier based on flags and history.
    Returns (tier, risk_score).
    """
    # Risk score components
    risk = 0.0
    value = 0.0

    # Risk signals
    if BuyerSignalFlag.SERIAL_FRAUDSTER in flags:
        risk += 50.0
    if BuyerSignalFlag.ADDRESS_MATCH in flags:
        risk += 20.0
    if BuyerSignalFlag.REPEAT_CLAIMANT in flags:
        risk += 20.0
    if BuyerSignalFlag.HIGH_DISPUTE_RATE in flags:
        risk += 15.0
    if BuyerSignalFlag.ESCALATING_AMOUNTS in flags:
        risk += 10.0
    if BuyerSignalFlag.PATTERN_CLAIMANT in flags:
        risk += 10.0
    if BuyerSignalFlag.RAPID_ACCOUNT in flags:
        risk += 10.0
    if buyer.is_in_fraud_network:
        risk += 25.0

    # Value signals (reduce effective risk)
    if BuyerSignalFlag.HIGH_LIFETIME_VALUE in flags:
        value += 30.0
    if BuyerSignalFlag.LOYAL_CUSTOMER in flags:
        value += 20.0
    if BuyerSignalFlag.ZERO_DISPUTE_HISTORY in flags:
        value += 15.0

    net_risk = max(0.0, min(100.0, risk - value * 0.5))

    # Tier classification
    if BuyerSignalFlag.SERIAL_FRAUDSTER in flags or buyer.is_in_fraud_network:
        return BuyerTier.FRAUDSTER, min(100.0, net_risk)

    # High-value customer with some signals → INTERNAL_REVIEW
    if value >= 30.0 and risk >= 20.0:
        return BuyerTier.INTERNAL_REVIEW, net_risk

    # Gold customer
    if value >= 40.0 and net_risk < 20.0:
        return BuyerTier.GOLD_CUSTOMER, net_risk

    # High risk
    if net_risk >= 50.0:
        return BuyerTier.HIGH_RISK_BUYER, net_risk

    # Elevated watch
    if net_risk >= 20.0:
        return BuyerTier.ELEVATED_WATCH, net_risk

    return BuyerTier.STANDARD_BUYER, net_risk


_BASE_FIGHT_THRESHOLD = 72.0
_BASE_MIN_EV = 20.0


def classify_buyer(buyer: BuyerProfile) -> BuyerTierReport:
    """
    Classify a buyer into a risk/value tier and return policy overrides.
    """
    flags = _detect_flags(buyer)
    tier, risk_score = _compute_tier(buyer, flags)
    policy = _TIER_POLICIES[tier]

    adj_fight_threshold = round(_BASE_FIGHT_THRESHOLD + policy.fight_threshold_adjustment, 1)
    adj_min_ev = round(_BASE_MIN_EV + policy.min_ev_to_fight_adjustment, 1)

    positive_flags = {BuyerSignalFlag.HIGH_LIFETIME_VALUE, BuyerSignalFlag.LOYAL_CUSTOMER, BuyerSignalFlag.ZERO_DISPUTE_HISTORY}
    risk_flags = [f for f in flags if f not in positive_flags]
    value_flags = [f for f in flags if f in positive_flags]

    rationale_parts = []
    if risk_flags:
        rationale_parts.append(f"Risk signals: {', '.join(f.value for f in risk_flags)}")
    if value_flags:
        rationale_parts.append(f"Value signals: {', '.join(f.value for f in value_flags)}")
    rationale_parts.append(f"Policy: {policy.description}")
    rationale = ". ".join(rationale_parts)

    summary = (
        f"Buyer {buyer.buyer_id}: tier={tier.value} "
        f"(risk_score={risk_score:.0f}) | "
        f"fight_thr={adj_fight_threshold:.0f} | "
        f"min_ev=${adj_min_ev:.0f} | "
        f"escalate={policy.escalate_to_fraud}"
    )

    return BuyerTierReport(
        buyer_id=buyer.buyer_id,
        tier=tier,
        tier_score=risk_score,
        policy=policy,
        flags=flags,
        signal_count=len(flags),
        adjusted_fight_threshold=adj_fight_threshold,
        adjusted_min_ev=adj_min_ev,
        adjusted_settlement_multiplier=policy.settlement_offer_multiplier,
        should_escalate=policy.escalate_to_fraud,
        should_block=policy.block_future_orders,
        rationale=rationale,
        summary=summary,
    )
