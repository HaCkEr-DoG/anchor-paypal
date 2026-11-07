"""
PayPal Ecosystem Reporter — generates intelligence that benefits PAYPAL,
not just the merchant.

The standard dispute tool worldview: merchant vs. buyer.
This module's worldview: PayPal as the network operator that must keep
all three parties healthy — merchants, buyers, and card networks.

What PayPal needs that no merchant tool currently provides:
  1. Dispute rate trend forecasting — will this merchant breach 1% threshold?
  2. Cross-dispute buyer fraud signals — same buyer defrauding many merchants
  3. Evidence quality scoring — does this submission meet PayPal's rubric?
  4. Buyer protection health — are PayPal's buyers getting fair outcomes?
  5. Network compliance score — is this merchant Visa/MC/Amex compliant?
  6. Resolution velocity — how fast are disputes resolving vs. SLA targets?

PayPal can embed this report in their Merchant Center or Risk Dashboard.
Merchants using Anchor automatically become better PayPal partners:
  - Lower dispute rates (prevention advisor)
  - Cleaner evidence packages (evidence quality scorer)
  - Fraud signals shared with PayPal (cross-merchant intel)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MerchantHealthTier(str, Enum):
    EXCELLENT   = "EXCELLENT"   # <0.5% dispute rate, low fraud, high win rate
    GOOD        = "GOOD"        # 0.5-0.75% dispute rate
    WATCH       = "WATCH"       # 0.75-1.0% dispute rate — approaching threshold
    AT_RISK     = "AT_RISK"     # >1.0% — PayPal intervention needed
    CRITICAL    = "CRITICAL"    # >2.0% — card network compliance breach


class BuyerProtectionScore(str, Enum):
    STRONG      = "STRONG"      # Buyers winning legitimate disputes quickly
    ADEQUATE    = "ADEQUATE"    # Normal outcomes
    WEAK        = "WEAK"        # Buyers losing too many legitimate disputes
    ABUSED      = "ABUSED"      # Buyer protection being gamed by fraud network


@dataclass
class EcosystemReportInput:
    merchant_id: str
    report_period_days: int = 90

    # Dispute volume
    total_transactions: int = 0
    total_disputes: int = 0
    disputes_won: int = 0
    disputes_lost: int = 0
    disputes_accepted_early: int = 0
    disputes_pending: int = 0

    # Reason code breakdown (counts)
    inr_count: int = 0
    snad_count: int = 0
    unauthorized_count: int = 0
    subscription_count: int = 0

    # Evidence quality signals
    avg_evidence_score: float = 0.0        # 0-100 from response_quality_scorer
    evidence_missing_rate: float = 0.0     # fraction of disputes with missing evidence

    # Buyer signals (cross-dispute)
    unique_buyers_disputing: int = 0
    repeat_disputers: int = 0              # buyers who disputed >1 time
    suspected_fraud_buyers: int = 0        # from fraud DNA profiler

    # Financial
    total_dispute_amount: float = 0.0
    total_recovered: float = 0.0           # won disputes
    total_lost: float = 0.0
    total_fees_paid: float = 0.0

    # Network compliance
    visa_disputes: int = 0
    mc_disputes: int = 0
    amex_disputes: int = 0
    paypal_disputes: int = 0

    # Resolution velocity
    avg_response_time_hours: float = 24.0
    disputes_responded_on_time: int = 0

    # Previous period for trend
    prev_period_dispute_rate: float = 0.0
    prev_period_win_rate: float = 0.0


@dataclass
class ComplianceFlag:
    code: str
    severity: str           # "WARNING" / "CRITICAL"
    description: str
    recommendation: str
    affects_paypal: bool    # True if this triggers PayPal's own obligations


@dataclass
class BuyerProtectionInsight:
    buyer_id: str
    dispute_count: int
    suspected_fraud: bool
    networks_involved: list[str]
    total_claimed: float
    description: str


@dataclass
class EcosystemReport:
    merchant_id: str
    report_period_days: int

    # Health scoring
    merchant_health_tier: MerchantHealthTier
    health_score: float             # 0-100
    dispute_rate: float             # disputes / transactions
    win_rate: float
    response_rate: float            # disputes responded to on time

    # Trend signals
    dispute_rate_trend: str         # "IMPROVING" / "STABLE" / "WORSENING"
    forecast_next_30d_rate: float   # predicted dispute rate next 30 days
    threshold_days_until_breach: Optional[int]  # days until 1% breach if worsening

    # PayPal-specific insights
    buyer_protection_score: BuyerProtectionScore
    compliance_flags: list[ComplianceFlag]
    fraud_buyer_signals: list[BuyerProtectionInsight]

    # Network breakdown
    network_dispute_rates: dict[str, float]  # {"VISA": 0.008, "PAYPAL": 0.012}
    highest_risk_network: str

    # Financial summary
    total_dispute_exposure: float
    total_recovered: float
    recovery_rate: float
    fee_efficiency: float           # fees paid / amount recovered ratio

    # Evidence quality (helps PayPal process faster)
    avg_evidence_score: float
    evidence_quality_tier: str      # "EXCELLENT" / "GOOD" / "POOR"
    paypal_processing_efficiency: float  # estimated % faster PayPal can resolve

    # Recommendations
    merchant_actions: list[str]     # what merchant should do
    paypal_actions: list[str]       # what PayPal should do (flags for Risk team)

    summary: str
    paypal_summary: str             # concise summary PayPal's risk team reads


def _dispute_rate(inp: EcosystemReportInput) -> float:
    if inp.total_transactions == 0:
        return 0.0
    return round(inp.total_disputes / inp.total_transactions, 5)


def _win_rate(inp: EcosystemReportInput) -> float:
    closed = inp.disputes_won + inp.disputes_lost + inp.disputes_accepted_early
    if closed == 0:
        return 0.0
    return round(inp.disputes_won / closed, 4)


def _response_rate(inp: EcosystemReportInput) -> float:
    if inp.total_disputes == 0:
        return 1.0
    return round(inp.disputes_responded_on_time / max(inp.total_disputes, 1), 4)


def _health_tier(rate: float, win_rate: float) -> MerchantHealthTier:
    if rate <= 0.005 and win_rate >= 0.55:
        return MerchantHealthTier.EXCELLENT
    if rate <= 0.0075:
        return MerchantHealthTier.GOOD
    if rate <= 0.010:
        return MerchantHealthTier.WATCH
    if rate <= 0.020:
        return MerchantHealthTier.AT_RISK
    return MerchantHealthTier.CRITICAL


def _health_score(tier: MerchantHealthTier, win_rate: float, evidence: float) -> float:
    base = {
        MerchantHealthTier.EXCELLENT: 90,
        MerchantHealthTier.GOOD: 75,
        MerchantHealthTier.WATCH: 55,
        MerchantHealthTier.AT_RISK: 35,
        MerchantHealthTier.CRITICAL: 15,
    }[tier]
    base += (win_rate - 0.5) * 10
    base += (evidence - 50) * 0.1
    return round(max(0, min(100, base)), 1)


def _build_compliance_flags(inp: EcosystemReportInput, rate: float) -> list[ComplianceFlag]:
    flags = []

    if rate >= 0.010:
        flags.append(ComplianceFlag(
            code="DISPUTE_RATE_THRESHOLD_BREACH",
            severity="CRITICAL",
            description=f"Dispute rate {rate:.2%} ≥ 1.0% — Visa/MC excessive chargeback threshold",
            recommendation="Immediate prevention program required; consider 30-day freeze on high-risk items",
            affects_paypal=True,
        ))
    elif rate >= 0.0075:
        flags.append(ComplianceFlag(
            code="DISPUTE_RATE_WATCH",
            severity="WARNING",
            description=f"Dispute rate {rate:.2%} approaching 1.0% threshold",
            recommendation="Enable Chargeback Prevention Advisor on all new orders",
            affects_paypal=True,
        ))

    if inp.suspected_fraud_buyers >= 3:
        flags.append(ComplianceFlag(
            code="FRAUD_NETWORK_SIGNAL",
            severity="CRITICAL",
            description=f"{inp.suspected_fraud_buyers} suspected professional fraudsters detected across disputes",
            recommendation="Share buyer IDs with PayPal Fraud team for cross-merchant analysis",
            affects_paypal=True,
        ))

    if inp.evidence_missing_rate >= 0.30:
        flags.append(ComplianceFlag(
            code="EVIDENCE_QUALITY_LOW",
            severity="WARNING",
            description=f"{inp.evidence_missing_rate:.0%} of dispute submissions missing required evidence",
            recommendation="Enable Evidence Vault integration to auto-collect from Drive and Shopify",
            affects_paypal=False,
        ))

    if inp.unauthorized_count / max(inp.total_disputes, 1) >= 0.40:
        flags.append(ComplianceFlag(
            code="HIGH_UNAUTHORIZED_RATE",
            severity="WARNING",
            description="40%+ of disputes are unauthorized — may indicate card-not-present fraud targeting",
            recommendation="Enable 3DS on all transactions; review IP velocity controls",
            affects_paypal=True,
        ))

    if inp.avg_response_time_hours > 48:
        flags.append(ComplianceFlag(
            code="SLOW_RESPONSE_VELOCITY",
            severity="WARNING",
            description=f"Average response time {inp.avg_response_time_hours:.0f}h exceeds 48h best practice",
            recommendation="Configure Anchor's APScheduler to auto-submit evidence within 24h",
            affects_paypal=False,
        ))

    return flags


def _buyer_protection_score(inp: EcosystemReportInput, win_rate: float) -> BuyerProtectionScore:
    fraud_ratio = inp.suspected_fraud_buyers / max(inp.unique_buyers_disputing, 1)
    if fraud_ratio >= 0.30:
        return BuyerProtectionScore.ABUSED
    if win_rate < 0.25:
        return BuyerProtectionScore.WEAK
    if win_rate >= 0.55 and fraud_ratio < 0.10:
        return BuyerProtectionScore.STRONG
    return BuyerProtectionScore.ADEQUATE


def _network_rates(inp: EcosystemReportInput) -> dict[str, float]:
    total_t = max(inp.total_transactions, 1)
    return {
        "VISA":   round(inp.visa_disputes / total_t, 5),
        "MC":     round(inp.mc_disputes / total_t, 5),
        "AMEX":   round(inp.amex_disputes / total_t, 5),
        "PAYPAL": round(inp.paypal_disputes / total_t, 5),
    }


def _forecast_rate(rate: float, prev_rate: float, days: int) -> float:
    if prev_rate == 0:
        return rate
    delta_per_day = (rate - prev_rate) / max(days, 1)
    forecast = rate + delta_per_day * 30
    return round(max(0, forecast), 5)


def _days_until_breach(rate: float, forecast: float, days: int) -> Optional[int]:
    if rate >= 0.010:
        return 0
    if forecast <= rate:
        return None
    daily_increase = (forecast - rate) / 30
    if daily_increase <= 0:
        return None
    days_to_breach = (0.010 - rate) / daily_increase
    return max(0, int(days_to_breach))


def generate_ecosystem_report(inp: EcosystemReportInput) -> EcosystemReport:
    """
    Generate a full ecosystem health report consumable by BOTH the merchant
    (operational dashboard) and PayPal (risk / compliance feed).
    """
    rate = _dispute_rate(inp)
    wr = _win_rate(inp)
    rr = _response_rate(inp)
    tier = _health_tier(rate, wr)
    score = _health_score(tier, wr, inp.avg_evidence_score)
    flags = _build_compliance_flags(inp, rate)
    bp_score = _buyer_protection_score(inp, wr)
    net_rates = _network_rates(inp)
    highest_risk = max(net_rates, key=lambda k: net_rates[k])

    forecast = _forecast_rate(rate, inp.prev_period_dispute_rate, inp.report_period_days)
    if rate > inp.prev_period_dispute_rate and inp.prev_period_dispute_rate > 0:
        trend = "WORSENING"
    elif rate < inp.prev_period_dispute_rate - 0.001:
        trend = "IMPROVING"
    else:
        trend = "STABLE"

    days_breach = _days_until_breach(rate, forecast, inp.report_period_days)

    recovery_rate = round(inp.total_recovered / max(inp.total_dispute_amount, 1), 4)
    fee_eff = round(inp.total_fees_paid / max(inp.total_recovered, 1), 4)

    # Evidence quality tier
    if inp.avg_evidence_score >= 80:
        ev_tier = "EXCELLENT"
        paypal_speedup = 0.35
    elif inp.avg_evidence_score >= 60:
        ev_tier = "GOOD"
        paypal_speedup = 0.15
    else:
        ev_tier = "POOR"
        paypal_speedup = 0.0

    # Merchant actions
    merchant_actions = []
    if tier in (MerchantHealthTier.WATCH, MerchantHealthTier.AT_RISK, MerchantHealthTier.CRITICAL):
        merchant_actions.append("Enable Chargeback Prevention Advisor on all new orders immediately")
    if inp.evidence_missing_rate >= 0.20:
        merchant_actions.append("Connect Google Drive Evidence Vault — auto-collect invoices and tracking")
    if inp.avg_response_time_hours > 24:
        merchant_actions.append("Configure 24h auto-response in Anchor scheduler")
    if inp.suspected_fraud_buyers >= 2:
        merchant_actions.append("Review Fraud DNA flagged buyers; consider requiring signature confirmation")
    if inp.unauthorized_count / max(inp.total_disputes, 1) >= 0.30:
        merchant_actions.append("Enable 3DS authentication on transactions >$100")

    if not merchant_actions:
        merchant_actions.append("Maintain current practices — dispute health is excellent")

    # PayPal actions
    paypal_actions = []
    if any(f.affects_paypal and f.severity == "CRITICAL" for f in flags):
        paypal_actions.append("RISK REVIEW: Merchant approaching/breaching Visa/MC dispute thresholds")
    if inp.suspected_fraud_buyers >= 3:
        paypal_actions.append(
            f"FRAUD SIGNAL: {inp.suspected_fraud_buyers} buyers with professional fraud DNA "
            "patterns — cross-reference against PayPal's buyer fraud database"
        )
    if bp_score == BuyerProtectionScore.ABUSED:
        paypal_actions.append(
            "BUYER PROTECTION ALERT: Fraud network appears to be gaming buyer protection — "
            "consider enhanced verification for future disputes from these buyer IDs"
        )
    if rate >= 0.010:
        paypal_actions.append(
            "COMPLIANCE: Merchant in excessive chargeback program territory — "
            "notify merchant and begin remediation monitoring per card network requirements"
        )
    if not paypal_actions:
        paypal_actions.append("No action required — merchant dispute health is within normal parameters")

    # Fraud buyer signals
    fraud_signals = []
    if inp.suspected_fraud_buyers > 0:
        fraud_signals.append(BuyerProtectionInsight(
            buyer_id="[see FraudDNA profiler logs]",
            dispute_count=inp.suspected_fraud_buyers,
            suspected_fraud=True,
            networks_involved=["PAYPAL"] + (["VISA"] if inp.visa_disputes > 0 else []),
            total_claimed=inp.snad_count * (inp.total_dispute_amount / max(inp.total_disputes, 1)),
            description="Professional chargeback filers detected via linguistic fingerprinting",
        ))

    summary = (
        f"Ecosystem {inp.merchant_id} [{inp.report_period_days}d]: "
        f"tier={tier.value} score={score:.0f}/100 "
        f"rate={rate:.2%} win={wr:.0%} "
        f"trend={trend} flags={len(flags)}"
    )

    paypal_summary = (
        f"Merchant {inp.merchant_id}: health={tier.value} dispute_rate={rate:.2%} "
        f"({trend}) compliance_flags={len([f for f in flags if f.affects_paypal])} "
        f"fraud_signals={inp.suspected_fraud_buyers} "
        f"buyer_protection={bp_score.value}"
    )

    return EcosystemReport(
        merchant_id=inp.merchant_id,
        report_period_days=inp.report_period_days,
        merchant_health_tier=tier,
        health_score=score,
        dispute_rate=rate,
        win_rate=wr,
        response_rate=rr,
        dispute_rate_trend=trend,
        forecast_next_30d_rate=forecast,
        threshold_days_until_breach=days_breach,
        buyer_protection_score=bp_score,
        compliance_flags=flags,
        fraud_buyer_signals=fraud_signals,
        network_dispute_rates=net_rates,
        highest_risk_network=highest_risk,
        total_dispute_exposure=round(inp.total_dispute_amount, 2),
        total_recovered=round(inp.total_recovered, 2),
        recovery_rate=recovery_rate,
        fee_efficiency=fee_eff,
        avg_evidence_score=inp.avg_evidence_score,
        evidence_quality_tier=ev_tier,
        paypal_processing_efficiency=paypal_speedup,
        merchant_actions=merchant_actions,
        paypal_actions=paypal_actions,
        summary=summary,
        paypal_summary=paypal_summary,
    )
