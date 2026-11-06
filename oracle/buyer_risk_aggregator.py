"""
Buyer Risk Aggregator — combine signals from velocity, communication,
fulfillment, chargeback scoring, and dispute history into a single
comprehensive buyer risk profile.

Inputs: individual signal reports (all optional) plus buyer metadata.
Output: a unified BuyerRiskProfile with an overall risk tier, a
recommendation, and a breakdown explaining each contributing signal.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OverallRiskTier(str, Enum):
    CRITICAL  = "CRITICAL"   # Immediate action — block or manual review
    HIGH      = "HIGH"       # Elevated — flag for review before shipping
    MODERATE  = "MODERATE"   # Monitor — normal processing with alerts
    LOW       = "LOW"        # Standard — proceed normally
    TRUSTED   = "TRUSTED"    # Established buyer with positive history


class BuyerAction(str, Enum):
    BLOCK             = "BLOCK"           # Refuse transaction
    MANUAL_REVIEW     = "MANUAL_REVIEW"   # Human eyes required
    ENHANCED_VERIFY   = "ENHANCED_VERIFY" # Extra authentication
    FLAG_AND_PROCEED  = "FLAG_AND_PROCEED"
    PROCEED_NORMAL    = "PROCEED_NORMAL"
    TRUSTED_FAST_PASS = "TRUSTED_FAST_PASS"


@dataclass
class SignalContribution:
    """One signal's contribution to the overall risk score."""
    signal_name: str
    raw_value: str      # Human-readable summary of what was observed
    score_added: float  # Points added to composite (can be negative)
    weight: float       # Relative importance (0–1)
    detail: str


@dataclass
class BuyerRiskInput:
    """Aggregated input from all available oracle modules."""
    buyer_id: str

    # From chargeback_risk_scorer (0–100 raw score)
    chargeback_risk_score: Optional[float] = None

    # From transaction_velocity_checker
    velocity_risk_level: Optional[str] = None   # "CRITICAL"/"HIGH"/etc.
    velocity_trigger_count: int = 0

    # From buyer_communication_analyzer
    fraud_signal_level: Optional[str] = None    # "VERY_HIGH"/"HIGH"/etc.
    hostility_level: Optional[str] = None       # "EXTREME"/"HIGH"/etc.
    threat_count: int = 0
    sophistication_count: int = 0

    # From order_fulfillment_tracker
    fulfillment_strength: Optional[str] = None  # "DEFINITIVE"/"STRONG"/etc.
    has_returned_shipments: bool = False

    # From dispute history (raw counters)
    total_disputes_lifetime: int = 0
    disputes_last_90_days: int = 0
    disputes_won_by_buyer: int = 0   # Buyer won = we lost
    disputes_ruled_for_merchant: int = 0

    # From refund_policy_advisor
    policy_defensibility: Optional[str] = None  # "STRONG"/"ADEQUATE"/etc.

    # Buyer metadata
    account_age_days: int = 365
    lifetime_order_count: int = 0
    lifetime_spend: float = 0.0
    is_verified_identity: bool = False


# Score tables
_VELOCITY_SCORES: dict[str, float] = {
    "CRITICAL": 35.0,
    "HIGH":     20.0,
    "MODERATE": 10.0,
    "LOW":       3.0,
    "NORMAL":    0.0,
}

_FRAUD_SIGNAL_SCORES: dict[str, float] = {
    "VERY_HIGH": 30.0,
    "HIGH":      18.0,
    "MODERATE":  10.0,
    "LOW":        4.0,
    "MINIMAL":    0.0,
}

_HOSTILITY_SCORES: dict[str, float] = {
    "EXTREME":     15.0,
    "HIGH":        10.0,
    "MODERATE":     5.0,
    "LOW":          2.0,
    "COOPERATIVE":  0.0,
}

_FULFILLMENT_SCORES: dict[str, float] = {
    "DEFINITIVE":  -5.0,  # Strong evidence reduces risk
    "STRONG":      -3.0,
    "MODERATE":     0.0,
    "WEAK":         5.0,
    "MISSING":     10.0,
}


def _tier_from_score(score: float) -> OverallRiskTier:
    if score >= 75:
        return OverallRiskTier.CRITICAL
    if score >= 55:
        return OverallRiskTier.HIGH
    if score >= 35:
        return OverallRiskTier.MODERATE
    if score >= 15:
        return OverallRiskTier.LOW
    return OverallRiskTier.TRUSTED


def _action_from_tier(tier: OverallRiskTier) -> BuyerAction:
    return {
        OverallRiskTier.CRITICAL:  BuyerAction.BLOCK,
        OverallRiskTier.HIGH:      BuyerAction.MANUAL_REVIEW,
        OverallRiskTier.MODERATE:  BuyerAction.FLAG_AND_PROCEED,
        OverallRiskTier.LOW:       BuyerAction.PROCEED_NORMAL,
        OverallRiskTier.TRUSTED:   BuyerAction.TRUSTED_FAST_PASS,
    }[tier]


@dataclass
class BuyerRiskProfile:
    """Unified buyer risk profile."""
    buyer_id: str
    composite_score: float          # 0–100
    overall_risk_tier: OverallRiskTier
    recommended_action: BuyerAction

    signal_contributions: list[SignalContribution]
    top_risk_factors: list[str]     # 1-3 sentence explanation
    mitigating_factors: list[str]

    dispute_rate: float             # disputes / total orders (0–1)
    is_repeat_disputer: bool
    summary: str


def aggregate_buyer_risk(inp: BuyerRiskInput) -> BuyerRiskProfile:
    """Aggregate all available risk signals into a buyer risk profile."""
    score = 0.0
    contributions: list[SignalContribution] = []
    risk_factors: list[str] = []
    mitigating: list[str] = []

    # ── Chargeback risk score (0–100 → scale to 0–30 contribution) ──
    if inp.chargeback_risk_score is not None:
        cb_contribution = inp.chargeback_risk_score * 0.30
        score += cb_contribution
        contributions.append(SignalContribution(
            signal_name="Chargeback Risk Score",
            raw_value=f"{inp.chargeback_risk_score:.0f}/100",
            score_added=cb_contribution,
            weight=0.30,
            detail=f"Pre-transaction risk score {inp.chargeback_risk_score:.0f}",
        ))
        if inp.chargeback_risk_score >= 65:
            risk_factors.append(f"High pre-transaction risk score ({inp.chargeback_risk_score:.0f}/100)")

    # ── Velocity ────────────────────────────────────────────────────
    vel_score = _VELOCITY_SCORES.get(inp.velocity_risk_level or "NORMAL", 0.0)
    if inp.velocity_trigger_count > 1:
        vel_score += inp.velocity_trigger_count * 2.0
    score += vel_score
    if vel_score > 0:
        contributions.append(SignalContribution(
            signal_name="Transaction Velocity",
            raw_value=inp.velocity_risk_level or "NORMAL",
            score_added=vel_score,
            weight=0.20,
            detail=f"{inp.velocity_trigger_count} velocity triggers",
        ))
        if inp.velocity_risk_level in ("CRITICAL", "HIGH"):
            risk_factors.append(f"Velocity anomaly ({inp.velocity_risk_level})")

    # ── Communication fraud signals ─────────────────────────────────
    fraud_score = _FRAUD_SIGNAL_SCORES.get(inp.fraud_signal_level or "MINIMAL", 0.0)
    hostility_score = _HOSTILITY_SCORES.get(inp.hostility_level or "COOPERATIVE", 0.0)
    comm_score = fraud_score + hostility_score
    if inp.threat_count > 0:
        comm_score += inp.threat_count * 2.0
    if comm_score > 0:
        score += comm_score
        contributions.append(SignalContribution(
            signal_name="Communication Analysis",
            raw_value=f"fraud={inp.fraud_signal_level or 'MINIMAL'}, hostility={inp.hostility_level or 'COOPERATIVE'}",
            score_added=comm_score,
            weight=0.15,
            detail=f"{inp.threat_count} threats, {inp.sophistication_count} sophistication signals",
        ))
        if inp.fraud_signal_level in ("VERY_HIGH", "HIGH"):
            risk_factors.append(f"Fraud-pattern language in buyer messages ({inp.fraud_signal_level})")
        if inp.hostility_level in ("EXTREME", "HIGH"):
            risk_factors.append(f"Buyer hostility level: {inp.hostility_level}")

    # ── Fulfillment evidence ────────────────────────────────────────
    fulfil_score = _FULFILLMENT_SCORES.get(inp.fulfillment_strength or "MODERATE", 0.0)
    if inp.has_returned_shipments:
        fulfil_score += 8.0
    score += fulfil_score
    if fulfil_score != 0:
        contributions.append(SignalContribution(
            signal_name="Fulfillment Evidence",
            raw_value=inp.fulfillment_strength or "MODERATE",
            score_added=fulfil_score,
            weight=0.10,
            detail="Returned shipments" if inp.has_returned_shipments else inp.fulfillment_strength or "MODERATE",
        ))
        if fulfil_score < 0:
            mitigating.append(f"Strong fulfillment evidence ({inp.fulfillment_strength})")
        elif inp.has_returned_shipments:
            risk_factors.append("Package returned to sender")

    # ── Dispute history ─────────────────────────────────────────────
    if inp.total_disputes_lifetime > 0:
        dispute_history_score = 0.0
        if inp.disputes_last_90_days >= 3:
            dispute_history_score += 25.0
            risk_factors.append(f"{inp.disputes_last_90_days} disputes in past 90 days")
        elif inp.disputes_last_90_days >= 1:
            dispute_history_score += 10.0
        if inp.disputes_won_by_buyer >= 2:
            dispute_history_score += 10.0
            risk_factors.append(f"Buyer won {inp.disputes_won_by_buyer} prior disputes")
        score += dispute_history_score
        contributions.append(SignalContribution(
            signal_name="Dispute History",
            raw_value=f"lifetime={inp.total_disputes_lifetime}, 90d={inp.disputes_last_90_days}",
            score_added=dispute_history_score,
            weight=0.15,
            detail=f"Buyer won {inp.disputes_won_by_buyer}, merchant won {inp.disputes_ruled_for_merchant}",
        ))

    # ── Account maturity (mitigating) ───────────────────────────────
    maturity_score = 0.0
    if inp.account_age_days < 30:
        maturity_score += 15.0
        risk_factors.append("Very new account (< 30 days)")
    elif inp.account_age_days < 90:
        maturity_score += 8.0
    elif inp.account_age_days > 365 and inp.lifetime_order_count > 10:
        maturity_score -= 5.0
        mitigating.append(f"Established account ({inp.account_age_days}d, {inp.lifetime_order_count} orders)")
    score += maturity_score
    if maturity_score != 0:
        contributions.append(SignalContribution(
            signal_name="Account Maturity",
            raw_value=f"{inp.account_age_days}d, {inp.lifetime_order_count} orders",
            score_added=maturity_score,
            weight=0.10,
            detail=f"Lifetime spend ${inp.lifetime_spend:.2f}",
        ))

    if inp.is_verified_identity:
        score -= 5.0
        mitigating.append("Verified identity on file")

    score = max(0.0, min(100.0, score))

    dispute_rate = (
        inp.total_disputes_lifetime / inp.lifetime_order_count
        if inp.lifetime_order_count > 0 else 0.0
    )
    is_repeat = inp.total_disputes_lifetime >= 2

    tier = _tier_from_score(score)
    action = _action_from_tier(tier)

    summary = (
        f"BuyerRisk {inp.buyer_id}: "
        f"score={score:.0f} | tier={tier.value} | action={action.value} | "
        f"disputes={inp.total_disputes_lifetime}"
    )

    return BuyerRiskProfile(
        buyer_id=inp.buyer_id,
        composite_score=round(score, 1),
        overall_risk_tier=tier,
        recommended_action=action,
        signal_contributions=contributions,
        top_risk_factors=risk_factors[:5],
        mitigating_factors=mitigating,
        dispute_rate=round(dispute_rate, 4),
        is_repeat_disputer=is_repeat,
        summary=summary,
    )
