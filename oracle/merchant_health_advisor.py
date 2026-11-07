"""
Merchant Health Advisor — strategic analysis of a merchant's dispute history
to surface root causes, improvement recommendations, and PayPal-facing signals.

Most oracle modules are tactical: decide one dispute.
This module is strategic: review ALL disputes and produce a 30-day action plan.

Outputs:
  1. Health score (0-100) — a single number PayPal can trend over time
  2. Top 3 root causes — why disputes are happening
  3. Prevention recommendations — specific actions to reduce rate
  4. Projected impact — what dispute rate will look like if actions are taken
  5. PayPal risk signal — a concise summary for PayPal's merchant risk team

Health tiers:
  EXCELLENT (85-100): dispute rate well below threshold, strong win rate
  GOOD      (70-84):  healthy metrics, minor improvement areas
  FAIR      (50-69):  approaching risk threshold, intervention recommended
  POOR      (25-49):  threshold risk in next 90 days, PayPal should engage
  CRITICAL  (0-24):   threshold breached or imminent, immediate intervention

PayPal benefit:
  - Trending health scores across all Anchor-using merchants = portfolio view
  - Poor/Critical merchants routed to proactive merchant success calls
  - EXCELLENT merchants are upsell candidates for premium services
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HealthTier(str, Enum):
    EXCELLENT = "EXCELLENT"
    GOOD      = "GOOD"
    FAIR      = "FAIR"
    POOR      = "POOR"
    CRITICAL  = "CRITICAL"


class DisputeRootCause(str, Enum):
    FULFILLMENT_DELAYS      = "FULFILLMENT_DELAYS"      # carrier / shipping issues
    PRODUCT_QUALITY         = "PRODUCT_QUALITY"         # SNAD, defective goods
    FRAUD_EXPOSURE          = "FRAUD_EXPOSURE"          # UNAUTHORIZED transactions
    SUBSCRIPTION_CONFUSION  = "SUBSCRIPTION_CONFUSION"  # unclear subscription terms
    EVIDENCE_WEAKNESS       = "EVIDENCE_WEAKNESS"       # low win rate despite valid disputes
    RESPONSE_LATENCY        = "RESPONSE_LATENCY"        # missing deadlines → auto-losses
    PRICING_CONFUSION       = "PRICING_CONFUSION"       # buyer didn't recognize charge
    COMMUNICATION_GAP       = "COMMUNICATION_GAP"       # buyer contacted PayPal instead of merchant
    UNKNOWN                 = "UNKNOWN"


class RecommendationPriority(str, Enum):
    URGENT   = "URGENT"    # implement within 1 week
    HIGH     = "HIGH"      # implement within 2 weeks
    MEDIUM   = "MEDIUM"    # implement within 30 days
    LOW      = "LOW"       # nice-to-have, next quarter


@dataclass
class HistoricalDispute:
    """Lightweight snapshot of one past dispute for health analysis."""
    dispute_id: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    amount: float
    outcome: str                # WON / LOST / REFUNDED / PENDING
    days_to_respond: int        # 0 = same day; >10 = slow
    had_tracking: bool
    had_invoice: bool
    had_buyer_communication: bool
    carrier: str = ""           # for INR disputes
    sku: str = ""               # for SNAD disputes
    dispute_month: str = ""     # YYYY-MM, for trend analysis


@dataclass
class MerchantHealthInput:
    merchant_id: str
    disputes: list[HistoricalDispute]
    total_orders_in_period: int         # denominator for dispute rate
    baseline_industry_dispute_rate: float = 0.01   # 1% industry avg
    paypal_threshold: float = 0.0075   # 0.75% triggers review


@dataclass
class HealthRecommendation:
    priority: RecommendationPriority
    action: str
    expected_impact: str        # e.g., "Reduce INR disputes by ~30%"
    effort: str                 # "LOW" / "MEDIUM" / "HIGH"
    root_cause_addressed: DisputeRootCause


@dataclass
class MerchantHealthReport:
    merchant_id: str

    # Score
    health_score: float          # 0-100
    health_tier: HealthTier
    current_dispute_rate: float  # fraction of total_orders
    dispute_rate_vs_threshold: float   # 1.0 = at threshold, >1.0 = over

    # Breakdown
    total_disputes: int
    won_count: int
    lost_count: int
    refunded_count: int
    win_rate: float

    # Reason code breakdown
    inr_pct: float
    snad_pct: float
    unauthorized_pct: float
    subscription_pct: float

    # Root causes (top 3)
    root_causes: list[DisputeRootCause]

    # Recommendations
    recommendations: list[HealthRecommendation]

    # Projections
    projected_dispute_rate_if_actions_taken: float
    projected_health_score_if_actions_taken: float

    # PayPal signal
    paypal_risk_signal: str         # one-paragraph summary for PayPal Risk
    paypal_opportunity_signal: str  # one-line upsell/support signal for PayPal

    key_insight: str
    summary: str


# ─── Score computation ─────────────────────────────────────────────────────────

def _health_score(
    dispute_rate: float,
    win_rate: float,
    avg_response_days: float,
    threshold: float,
) -> float:
    """
    Composite health score 0-100:
      - 50 pts: dispute rate component (0 = at/above threshold, 50 = zero disputes)
      - 30 pts: win rate component (0 = 0% win rate, 30 = 100% win rate)
      - 20 pts: response speed component (0 = >10 days, 20 = same-day)
    """
    # Dispute rate component: 50 pts, linear from 0 → threshold (0 rate = 50 pts)
    dr_ratio = dispute_rate / max(threshold, 0.0001)
    dr_score = max(0.0, 50.0 * (1.0 - dr_ratio))

    # Win rate: 30 pts
    wr_score = 30.0 * win_rate

    # Response speed: 20 pts, capped at 10 days
    speed_score = max(0.0, 20.0 * (1.0 - avg_response_days / 10.0))

    return round(min(100.0, dr_score + wr_score + speed_score), 1)


def _tier(score: float) -> HealthTier:
    if score >= 85:
        return HealthTier.EXCELLENT
    if score >= 70:
        return HealthTier.GOOD
    if score >= 50:
        return HealthTier.FAIR
    if score >= 25:
        return HealthTier.POOR
    return HealthTier.CRITICAL


def _detect_root_causes(
    disputes: list[HistoricalDispute],
    win_rate: float,
    avg_response_days: float,
    inr_pct: float,
    snad_pct: float,
    unauth_pct: float,
    sub_pct: float,
) -> list[DisputeRootCause]:
    causes: list[tuple[float, DisputeRootCause]] = []

    if inr_pct >= 0.40:
        causes.append((inr_pct, DisputeRootCause.FULFILLMENT_DELAYS))

    if snad_pct >= 0.30:
        causes.append((snad_pct, DisputeRootCause.PRODUCT_QUALITY))

    if unauth_pct >= 0.25:
        causes.append((unauth_pct, DisputeRootCause.FRAUD_EXPOSURE))

    if sub_pct >= 0.20:
        causes.append((sub_pct, DisputeRootCause.SUBSCRIPTION_CONFUSION))

    if win_rate < 0.40 and len(disputes) >= 3:
        causes.append((0.60 - win_rate, DisputeRootCause.EVIDENCE_WEAKNESS))

    if avg_response_days > 5:
        causes.append((avg_response_days / 10.0, DisputeRootCause.RESPONSE_LATENCY))

    no_tracking_pct = sum(
        1 for d in disputes if d.reason_code == "INR" and not d.had_tracking
    ) / max(len(disputes), 1)
    if no_tracking_pct >= 0.20:
        causes.append((no_tracking_pct, DisputeRootCause.COMMUNICATION_GAP))

    # Sort by weight, deduplicate, return top 3
    causes.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    result = []
    for _, cause in causes:
        if cause not in seen:
            seen.add(cause)
            result.append(cause)
        if len(result) == 3:
            break

    return result if result else [DisputeRootCause.UNKNOWN]


def _build_recommendations(
    root_causes: list[DisputeRootCause],
    win_rate: float,
    avg_response_days: float,
    inr_pct: float,
    snad_pct: float,
    unauth_pct: float,
    health_tier: HealthTier,
) -> list[HealthRecommendation]:
    recs = []
    urgent = health_tier in (HealthTier.POOR, HealthTier.CRITICAL)

    for cause in root_causes:
        if cause == DisputeRootCause.FULFILLMENT_DELAYS:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.URGENT if urgent else RecommendationPriority.HIGH,
                action=(
                    "Enable Firecrawl carrier tracking on all orders and auto-send tracking "
                    "updates to buyers at dispatch + delivery confirmation. Add tracking "
                    "numbers to all shipments before marking as 'shipped'."
                ),
                expected_impact="Reduce INR disputes by 30-50%",
                effort="LOW",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.PRODUCT_QUALITY:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.URGENT if urgent else RecommendationPriority.HIGH,
                action=(
                    "Audit top 3 SKUs by SNAD dispute rate. Run a product quality review "
                    "for those SKUs and add detailed 'accurate product representation' "
                    "photos + descriptions to listings. Consider a no-questions-asked "
                    "30-day return policy to intercept SNAD buyers before they escalate."
                ),
                expected_impact="Reduce SNAD disputes by 20-40%",
                effort="MEDIUM",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.FRAUD_EXPOSURE:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.URGENT,
                action=(
                    "Enable transaction risk scoring on all new orders. Require 3DS "
                    "authentication for orders over $200. Set up velocity rules blocking "
                    ">3 orders per 24h from same buyer IP/email. Review recent "
                    "UNAUTHORIZED disputes for buyer ID patterns."
                ),
                expected_impact="Reduce UNAUTHORIZED chargebacks by 40-60%",
                effort="MEDIUM",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.SUBSCRIPTION_CONFUSION:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.HIGH,
                action=(
                    "Add a clear cancellation link to every subscription invoice email. "
                    "Send a 7-day 'Your subscription renews soon' reminder. Add "
                    "conspicuous recurring billing disclosure at checkout in bold. "
                    "Process cancellation requests within 24h."
                ),
                expected_impact="Reduce SUBSCRIPTION disputes by 50-70%",
                effort="LOW",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.EVIDENCE_WEAKNESS:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.HIGH,
                action=(
                    "Use Google Drive Evidence Vault to store proof of delivery, "
                    "signed delivery confirmations, and buyer communication logs "
                    "for every order. Run evidence completeness scoring before "
                    "submitting dispute responses. Use Gmail prevention outreach "
                    "to contact buyers before they escalate to PayPal."
                ),
                expected_impact="Increase win rate by 15-25 percentage points",
                effort="MEDIUM",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.RESPONSE_LATENCY:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.URGENT,
                action=(
                    "Enable Anchor's automated deadline tracker and set up a daily "
                    "digest of disputes expiring within 48h. Assign a primary contact "
                    "who checks Anchor every morning. Consider enabling automated "
                    "evidence submission for disputes with strong evidence packages."
                ),
                expected_impact="Reduce auto-lost disputes by 60-80%",
                effort="LOW",
                root_cause_addressed=cause,
            ))

        elif cause == DisputeRootCause.COMMUNICATION_GAP:
            recs.append(HealthRecommendation(
                priority=RecommendationPriority.MEDIUM,
                action=(
                    "Add a prominent 'Contact Us' button on all order confirmation "
                    "and tracking pages. Enable Gmail buyer outreach for all INR "
                    "disputes that have been open >48h without tracking events. "
                    "Make it easier for buyers to reach you than to reach PayPal."
                ),
                expected_impact="Reduce disputes reaching PayPal by 20-35%",
                effort="MEDIUM",
                root_cause_addressed=cause,
            ))

    return recs


def _project_improvement(
    current_rate: float,
    root_causes: list[DisputeRootCause],
    recommendations: list[HealthRecommendation],
) -> float:
    """Estimate dispute rate after all HIGH+ priority recommendations are implemented."""
    impact_per_cause = {
        DisputeRootCause.FULFILLMENT_DELAYS:     0.40,
        DisputeRootCause.PRODUCT_QUALITY:        0.30,
        DisputeRootCause.FRAUD_EXPOSURE:         0.50,
        DisputeRootCause.SUBSCRIPTION_CONFUSION: 0.60,
        DisputeRootCause.EVIDENCE_WEAKNESS:      0.00,  # improves win rate, not rate
        DisputeRootCause.RESPONSE_LATENCY:       0.10,
        DisputeRootCause.COMMUNICATION_GAP:      0.25,
        DisputeRootCause.UNKNOWN:                0.05,
    }
    combined_reduction = 1.0
    for cause in root_causes:
        combined_reduction *= (1.0 - impact_per_cause.get(cause, 0.05))
    projected = round(current_rate * combined_reduction, 6)
    return projected


# ─── Main function ─────────────────────────────────────────────────────────────

def analyze_merchant_health(inp: MerchantHealthInput) -> MerchantHealthReport:
    """
    Analyze a merchant's full dispute history and produce a 30-day action plan.
    """
    disputes = inp.disputes
    n = len(disputes)
    total_orders = max(inp.total_orders_in_period, 1)

    dispute_rate = n / total_orders

    won = sum(1 for d in disputes if d.outcome == "WON")
    lost = sum(1 for d in disputes if d.outcome == "LOST")
    refunded = sum(1 for d in disputes if d.outcome == "REFUNDED")
    win_rate = won / max(n, 1)

    inr_count = sum(1 for d in disputes if d.reason_code == "INR")
    snad_count = sum(1 for d in disputes if d.reason_code == "SNAD")
    unauth_count = sum(1 for d in disputes if d.reason_code == "UNAUTHORIZED")
    sub_count = sum(1 for d in disputes if d.reason_code == "SUBSCRIPTION")

    inr_pct = inr_count / max(n, 1)
    snad_pct = snad_count / max(n, 1)
    unauth_pct = unauth_count / max(n, 1)
    sub_pct = sub_count / max(n, 1)

    avg_response_days = (
        sum(d.days_to_respond for d in disputes) / max(n, 1)
    )

    score = _health_score(dispute_rate, win_rate, avg_response_days, inp.paypal_threshold)
    tier = _tier(score)

    root_causes = _detect_root_causes(
        disputes, win_rate, avg_response_days,
        inr_pct, snad_pct, unauth_pct, sub_pct,
    )
    recs = _build_recommendations(
        root_causes, win_rate, avg_response_days,
        inr_pct, snad_pct, unauth_pct, tier,
    )

    projected_rate = _project_improvement(dispute_rate, root_causes, recs)
    projected_score = _health_score(projected_rate, min(win_rate + 0.10, 1.0),
                                    max(avg_response_days - 2.0, 1.0), inp.paypal_threshold)

    dr_ratio = dispute_rate / max(inp.paypal_threshold, 0.0001)

    # Key insight
    primary_cause = root_causes[0] if root_causes else DisputeRootCause.UNKNOWN
    if tier in (HealthTier.POOR, HealthTier.CRITICAL):
        key_insight = (
            f"Dispute rate {dispute_rate:.2%} is {dr_ratio:.1f}× the PayPal threshold. "
            f"Primary driver: {primary_cause.value.replace('_', ' ')}. "
            f"Immediate action required."
        )
    elif tier == HealthTier.FAIR:
        key_insight = (
            f"Dispute rate {dispute_rate:.2%} is approaching PayPal threshold. "
            f"Top cause: {primary_cause.value.replace('_', ' ')}. "
            f"Proactive improvement recommended."
        )
    else:
        key_insight = (
            f"Dispute rate {dispute_rate:.2%} is healthy. "
            f"Win rate {win_rate:.0%}. "
            f"Top opportunity: {primary_cause.value.replace('_', ' ')}."
        )

    paypal_risk = (
        f"Merchant {inp.merchant_id}: health score {score:.0f}/100 ({tier.value}), "
        f"dispute rate {dispute_rate:.3%} ({dr_ratio:.1f}× threshold), "
        f"win rate {win_rate:.0%}, avg response {avg_response_days:.1f} days. "
        f"Top driver: {primary_cause.value}. "
        f"{'ACTION REQUIRED — threshold risk.' if tier in (HealthTier.POOR, HealthTier.CRITICAL) else 'Monitoring.'}"
    )

    paypal_opportunity = (
        f"Merchant {inp.merchant_id} ({tier.value}): "
        + ("candidate for proactive merchant success call" if tier in (HealthTier.POOR, HealthTier.CRITICAL)
           else "eligible for dispute prevention product upsell" if tier == HealthTier.FAIR
           else "healthy — eligible for premium tier / case study")
    )

    summary = (
        f"Health {inp.merchant_id}: score={score:.0f}/100 tier={tier.value} "
        f"rate={dispute_rate:.3%} win_rate={win_rate:.0%} "
        f"causes={','.join(c.value for c in root_causes)} "
        f"recs={len(recs)} projected_rate={projected_rate:.3%}"
    )

    return MerchantHealthReport(
        merchant_id=inp.merchant_id,
        health_score=score,
        health_tier=tier,
        current_dispute_rate=round(dispute_rate, 6),
        dispute_rate_vs_threshold=round(dr_ratio, 3),
        total_disputes=n,
        won_count=won,
        lost_count=lost,
        refunded_count=refunded,
        win_rate=round(win_rate, 4),
        inr_pct=round(inr_pct, 4),
        snad_pct=round(snad_pct, 4),
        unauthorized_pct=round(unauth_pct, 4),
        subscription_pct=round(sub_pct, 4),
        root_causes=root_causes,
        recommendations=recs,
        projected_dispute_rate_if_actions_taken=projected_rate,
        projected_health_score_if_actions_taken=round(projected_score, 1),
        paypal_risk_signal=paypal_risk,
        paypal_opportunity_signal=paypal_opportunity,
        key_insight=key_insight,
        summary=summary,
    )
