"""
Cross-Merchant Network Intelligence — the signal no single merchant can generate alone.

The fundamental asymmetry in dispute management: a fraudster who files the same
INR claim against 200 merchants in a week is identifiable at the NETWORK level.
Any single merchant sees one transaction and no pattern. The network sees all 200.

What this module does:
  - Aggregates dispute signals across a merchant network (anonymized)
  - Identifies: serial fraudsters, fraud rings, carrier fraud patterns, ZIP clusters
  - Provides a "Network Risk Score" for any buyer/transaction fingerprint
  - Surfaces patterns before they become your dispute

Competitor gap:
  - Ethoca/Verifi relay alerts but don't provide cross-merchant behavioral intelligence
  - Chargebacks911 uses "representment databases" but only within their client base
  - Midigator has dispute analytics but no real-time network signal API
  - NONE expose a queryable per-buyer network score at decision time

This module simulates network signal computation from aggregated data. In production,
the network data feed would come from Anchor's multi-merchant aggregation layer
(opt-in, anonymized — merchants contribute signals, receive signals).

The value: a buyer with 0 disputes in YOUR history but 47 disputes across the
network this month is a known bad actor. Fight or block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class NetworkSignalType(str, Enum):
    SERIAL_FRAUDSTER       = "SERIAL_FRAUDSTER"       # Buyer has N disputes across merchants
    FRAUD_RING             = "FRAUD_RING"              # Device/IP shared with known fraud ring
    CARRIER_EXPLOIT        = "CARRIER_EXPLOIT"         # Carrier repeatedly fails delivery in this area
    ZIP_CLUSTER            = "ZIP_CLUSTER"             # Unusual dispute concentration in zip/region
    VELOCITY_SPIKE         = "VELOCITY_SPIKE"          # Buyer filed N disputes in last 30 days
    DEVICE_FINGERPRINT     = "DEVICE_FINGERPRINT"      # Device ID seen on multiple fraudulent accounts
    EMAIL_PATTERN          = "EMAIL_PATTERN"           # Email matches known synthetic/fraud pattern
    REFUND_PATTERN         = "REFUND_PATTERN"          # Buyer has high pre-dispute refund rate
    CLEAN_BUYER            = "CLEAN_BUYER"             # Buyer has long, clean cross-merchant history
    INSUFFICIENT_DATA      = "INSUFFICIENT_DATA"       # Not enough network data for this buyer


class NetworkRiskLevel(str, Enum):
    CRITICAL  = "CRITICAL"   # Confirmed bad actor — do not fulfill; fight/escalate
    HIGH      = "HIGH"       # Strong fraud signal — elevated scrutiny
    ELEVATED  = "ELEVATED"   # Some signals — proceed with extra evidence
    NORMAL    = "NORMAL"     # Average buyer profile
    TRUSTED   = "TRUSTED"    # Long clean history — reduce friction


@dataclass
class NetworkSignal:
    """One signal from the cross-merchant network."""
    signal_type: NetworkSignalType
    confidence: float              # 0.0–1.0
    network_dispute_count: int     # How many disputes across network
    network_merchant_count: int    # How many merchants affected
    days_lookback: int             # Window over which signal was observed
    win_rate_impact: float         # How this signal changes baseline win rate (+/-)
    description: str
    recommended_action: str


@dataclass
class NetworkIntelligenceReport:
    """
    Cross-merchant intelligence for one transaction/buyer fingerprint.
    Use this BEFORE making fight/accept decisions.
    """
    buyer_email_hash: str          # Hashed — never store raw PII
    device_id_hash: Optional[str]
    network_risk_level: NetworkRiskLevel
    network_risk_score: float      # 0–100 (100 = highest risk)
    signals: list[NetworkSignal]
    # Aggregate stats
    total_disputes_on_network: int # This buyer across all merchants in network
    total_merchants_seen: int
    network_win_rate: float        # Win rate across network for this buyer's claim type
    # Decision guidance
    fight_recommendation: str      # "FIGHT" / "ACCEPT" / "ESCALATE" / "STANDARD"
    win_probability_adjustment: float  # Additive adjustment to baseline win_prob
    alert_merchant: bool           # True if this buyer is flagged for alert
    reasoning: list[str] = field(default_factory=list)


@dataclass
class BuyerNetworkProfile:
    """
    Buyer's historical profile as seen across the merchant network.
    In production, this comes from the aggregation layer. Here it's the input.
    """
    email_hash: str
    device_id_hash: Optional[str] = None
    # Cross-network history
    total_transactions_on_network: int = 0
    total_disputes_on_network: int = 0
    total_merchants_transacted: int = 0
    total_merchants_disputed: int = 0
    # Recent activity (last 90 days)
    disputes_last_30d: int = 0
    disputes_last_90d: int = 0
    merchants_disputed_last_90d: int = 0
    # Outcomes
    merchant_wins_vs_buyer: int = 0   # Times merchant won against this buyer
    buyer_wins_vs_merchant: int = 0   # Times buyer won
    # Risk indicators
    shared_device_fraud_count: int = 0  # Other accounts on same device with fraud
    zip_code: Optional[str] = None
    dispute_claim_types: list[str] = field(default_factory=list)  # What they typically claim


def analyze_network_signals(
    profile: BuyerNetworkProfile,
    claim_type: str,
    dispute_amount: float,
    buyer_risk_tier: str = "TIER_1_NORMAL",
) -> NetworkIntelligenceReport:
    """
    Generate cross-merchant intelligence report for a dispute.

    This is the network effect: aggregate what the entire Anchor merchant
    network has seen about this buyer, then surface it at decision time.
    """
    reasoning: list[str] = []
    signals: list[NetworkSignal] = []

    # ── Signal 1: Serial Fraudster (high dispute count across network) ────────
    if profile.total_disputes_on_network >= 50:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.SERIAL_FRAUDSTER,
            confidence=0.95,
            network_dispute_count=profile.total_disputes_on_network,
            network_merchant_count=profile.total_merchants_disputed,
            days_lookback=365,
            win_rate_impact=+0.25,  # Very high win rate against serial fraudsters
            description=(
                f"Buyer has {profile.total_disputes_on_network} disputes across "
                f"{profile.total_merchants_disputed} merchants on network — serial fraudster pattern"
            ),
            recommended_action="FIGHT: network data strongly supports fraud classification",
        ))
        reasoning.append(
            f"CRITICAL: Serial fraudster — {profile.total_disputes_on_network} network disputes"
        )
    elif profile.total_disputes_on_network >= 10:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.SERIAL_FRAUDSTER,
            confidence=0.70,
            network_dispute_count=profile.total_disputes_on_network,
            network_merchant_count=profile.total_merchants_disputed,
            days_lookback=365,
            win_rate_impact=+0.15,
            description=f"Elevated dispute history: {profile.total_disputes_on_network} disputes across network",
            recommended_action="FIGHT with network dispute history as evidence",
        ))

    # ── Signal 2: Velocity Spike (many disputes recently) ─────────────────────
    if profile.disputes_last_30d >= 5:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.VELOCITY_SPIKE,
            confidence=0.90,
            network_dispute_count=profile.disputes_last_30d,
            network_merchant_count=profile.merchants_disputed_last_90d,
            days_lookback=30,
            win_rate_impact=+0.20,
            description=(
                f"VELOCITY SPIKE: {profile.disputes_last_30d} disputes in last 30 days "
                f"across {profile.merchants_disputed_last_90d} merchants — coordinated fraud pattern"
            ),
            recommended_action="ESCALATE: velocity pattern indicates organized fraud",
        ))
        reasoning.append(f"Velocity spike: {profile.disputes_last_30d} disputes in 30d")
    elif profile.disputes_last_30d >= 2:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.VELOCITY_SPIKE,
            confidence=0.55,
            network_dispute_count=profile.disputes_last_30d,
            network_merchant_count=profile.merchants_disputed_last_90d,
            days_lookback=30,
            win_rate_impact=+0.08,
            description=f"Moderate recent dispute activity: {profile.disputes_last_30d} in 30 days",
            recommended_action="FIGHT with extra documentation",
        ))

    # ── Signal 3: Device fingerprint fraud ────────────────────────────────────
    if profile.shared_device_fraud_count >= 3:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.DEVICE_FINGERPRINT,
            confidence=0.85,
            network_dispute_count=profile.shared_device_fraud_count,
            network_merchant_count=profile.shared_device_fraud_count,
            days_lookback=180,
            win_rate_impact=+0.18,
            description=(
                f"Device linked to {profile.shared_device_fraud_count} confirmed fraud accounts "
                "on the network — fraud ring indicator"
            ),
            recommended_action="ESCALATE: device fingerprint matches fraud ring",
        ))
        reasoning.append(f"Device fraud ring: {profile.shared_device_fraud_count} linked accounts")

    # ── Signal 4: Insufficient data ───────────────────────────────────────────
    if profile.total_transactions_on_network == 0:
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.INSUFFICIENT_DATA,
            confidence=1.0,
            network_dispute_count=0,
            network_merchant_count=0,
            days_lookback=0,
            win_rate_impact=0.0,
            description="Buyer has no history on the Anchor merchant network",
            recommended_action="Use local merchant history and claim-type priors",
        ))
        reasoning.append("New to network — no cross-merchant data")

    # ── Signal 5: Trusted buyer (clean long history) ──────────────────────────
    elif (
        profile.total_transactions_on_network >= 20
        and profile.total_disputes_on_network <= 1
        and profile.disputes_last_30d == 0
    ):
        signals.append(NetworkSignal(
            signal_type=NetworkSignalType.CLEAN_BUYER,
            confidence=0.80,
            network_dispute_count=profile.total_disputes_on_network,
            network_merchant_count=profile.total_merchants_transacted,
            days_lookback=365,
            win_rate_impact=-0.15,  # Lower win prob — buyer likely legitimate
            description=(
                f"Trusted buyer: {profile.total_transactions_on_network} transactions, "
                f"only {profile.total_disputes_on_network} disputes across network"
            ),
            recommended_action="REVIEW: trusted buyer — investigate before auto-fighting",
        ))
        reasoning.append("Trusted buyer — dispute may be legitimate")

    # ── Compute network risk level ─────────────────────────────────────────────
    risk_score = _compute_risk_score(profile, signals)

    if risk_score >= 80:
        risk_level = NetworkRiskLevel.CRITICAL
    elif risk_score >= 60:
        risk_level = NetworkRiskLevel.HIGH
    elif risk_score >= 40:
        risk_level = NetworkRiskLevel.ELEVATED
    elif risk_score >= 20:
        risk_level = NetworkRiskLevel.NORMAL
    else:
        risk_level = NetworkRiskLevel.TRUSTED

    # Win rate adjustment from signals
    win_prob_adjustment = sum(s.win_rate_impact for s in signals)
    win_prob_adjustment = max(-0.30, min(win_prob_adjustment, 0.40))

    # Network win rate for this buyer's claim type
    total_outcomes = profile.merchant_wins_vs_buyer + profile.buyer_wins_vs_merchant
    if total_outcomes >= 3:
        network_win_rate = profile.merchant_wins_vs_buyer / total_outcomes
        reasoning.append(
            f"Network win rate against this buyer: {network_win_rate:.0%} "
            f"({profile.merchant_wins_vs_buyer}W / {profile.buyer_wins_vs_merchant}L)"
        )
    else:
        network_win_rate = 0.5  # Unknown

    # Fight recommendation
    if risk_level in (NetworkRiskLevel.CRITICAL, NetworkRiskLevel.HIGH):
        fight_recommendation = "FIGHT"
        alert_merchant = True
    elif risk_level == NetworkRiskLevel.TRUSTED:
        fight_recommendation = "STANDARD"  # Let claim-type logic decide
        alert_merchant = False
    elif risk_level == NetworkRiskLevel.ELEVATED:
        fight_recommendation = "FIGHT"
        alert_merchant = True
    else:
        fight_recommendation = "STANDARD"
        alert_merchant = False

    reasoning.append(f"Network risk score: {risk_score:.0f}/100 → {risk_level.value}")

    return NetworkIntelligenceReport(
        buyer_email_hash=profile.email_hash,
        device_id_hash=profile.device_id_hash,
        network_risk_level=risk_level,
        network_risk_score=round(risk_score, 1),
        signals=signals,
        total_disputes_on_network=profile.total_disputes_on_network,
        total_merchants_seen=profile.total_merchants_transacted,
        network_win_rate=round(network_win_rate, 3),
        fight_recommendation=fight_recommendation,
        win_probability_adjustment=round(win_prob_adjustment, 3),
        alert_merchant=alert_merchant,
        reasoning=reasoning,
    )


def _compute_risk_score(profile: BuyerNetworkProfile, signals: list[NetworkSignal]) -> float:
    """Composite risk score 0–100 from profile and signals."""
    score = 0.0

    # Dispute rate across network (most predictive single feature)
    if profile.total_transactions_on_network > 0:
        network_dispute_rate = profile.total_disputes_on_network / profile.total_transactions_on_network
        score += min(network_dispute_rate * 500, 40)  # Up to 40 points

    # Recent velocity (last 30d) — highest weight
    score += min(profile.disputes_last_30d * 8, 30)  # Up to 30 points

    # Device fraud links
    score += min(profile.shared_device_fraud_count * 5, 20)  # Up to 20 points

    # Merchant breadth of disputes
    if profile.merchants_disputed_last_90d >= 3:
        score += 10

    # Trust deduction for clean long-term buyers
    if profile.total_transactions_on_network >= 20 and profile.total_disputes_on_network <= 1:
        score = max(0, score - 20)

    return min(score, 100.0)


def network_summary(report: NetworkIntelligenceReport) -> str:
    """One-line dashboard summary of network intelligence."""
    flags = []
    for s in report.signals:
        if s.signal_type != NetworkSignalType.INSUFFICIENT_DATA:
            flags.append(s.signal_type.value)
    flag_str = ", ".join(flags) if flags else "none"
    return (
        f"Network: {report.network_risk_level.value} "
        f"(score={report.network_risk_score:.0f}/100) | "
        f"signals=[{flag_str}] | "
        f"win_adj={report.win_probability_adjustment:+.0%} | "
        f"action={report.fight_recommendation}"
    )
