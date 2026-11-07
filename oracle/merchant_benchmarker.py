"""
Merchant Dispute Benchmarker — compare a merchant's dispute metrics to peers.

PayPal processes millions of disputes and has anonymized benchmark data by:
  - Merchant category (electronics, apparel, software, travel, etc.)
  - Merchant size segment (ENTERPRISE / MID_MARKET / SMALL_BUSINESS / MICRO)
  - Dispute reason code distribution norms

This module simulates that benchmark layer. Given a merchant's observed metrics,
it returns percentile rankings, risk flags, and coaching recommendations.

Key metrics benchmarked:
  1. dispute_rate        (disputes / orders) — primary health indicator
  2. win_rate            (disputes won / disputes fought)
  3. avg_response_days   (average days to submit response)
  4. refund_rate         (preemptive refunds / disputes)
  5. chargeback_escalation_rate  (escalations / total disputes)
  6. inr_share           (INR / total disputes) — fulfillment health proxy
  7. unauthorized_share  (UNAUTHORIZED / total) — fraud exposure proxy

PayPal benefit:
  - Identifies merchants approaching Visa/Mastercard chargeback thresholds
    (1% for standard, 1.5% for excessive) before they cross them
  - Routes high-dispute-rate merchants to PayPal seller tools proactively
  - Surfaces fulfillment quality problems (high INR → shipping issues)
  - Provides PayPal account managers with merchant health scorecards
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MerchantSegment(str, Enum):
    ENTERPRISE    = "ENTERPRISE"
    MID_MARKET    = "MID_MARKET"
    SMALL_BUSINESS = "SMALL_BUSINESS"
    MICRO         = "MICRO"


class MerchantCategory(str, Enum):
    ELECTRONICS   = "ELECTRONICS"
    APPAREL       = "APPAREL"
    SOFTWARE      = "SOFTWARE"
    TRAVEL        = "TRAVEL"
    HOME_GARDEN   = "HOME_GARDEN"
    HEALTH_BEAUTY = "HEALTH_BEAUTY"
    SERVICES      = "SERVICES"
    GENERAL       = "GENERAL"


class BenchmarkTier(str, Enum):
    EXCELLENT  = "EXCELLENT"   # top 25% of peers
    GOOD       = "GOOD"        # 25th–50th percentile
    AVERAGE    = "AVERAGE"     # 50th–75th percentile
    POOR       = "POOR"        # 75th–90th percentile
    CRITICAL   = "CRITICAL"    # bottom 10% — intervention needed


class RiskFlag(str, Enum):
    APPROACHING_CHARGEBACK_THRESHOLD = "APPROACHING_CHARGEBACK_THRESHOLD"
    HIGH_UNAUTHORIZED_RATE           = "HIGH_UNAUTHORIZED_RATE"
    HIGH_INR_RATE                    = "HIGH_INR_RATE"
    LOW_WIN_RATE                     = "LOW_WIN_RATE"
    SLOW_RESPONSE_TIME               = "SLOW_RESPONSE_TIME"
    HIGH_ESCALATION_RATE             = "HIGH_ESCALATION_RATE"


# ─── Anonymized benchmark tables ─────────────────────────────────────────────
# Format: { (category, segment): { metric: (p25, p50, p75, p90) } }
# Source: synthetic industry norms derived from public PayPal/Visa/MC disclosures

_BENCHMARKS: dict[tuple, dict[str, tuple]] = {}

def _add(cat: str, seg: str, **metrics):
    _BENCHMARKS[(cat, seg)] = metrics

# Electronics
_add("ELECTRONICS", "ENTERPRISE",    dispute_rate=(0.003, 0.005, 0.009, 0.015), win_rate=(0.75, 0.68, 0.60, 0.50), avg_response_days=(1.5, 2.5, 4.0, 7.0), inr_share=(0.35, 0.45, 0.55, 0.65), unauthorized_share=(0.15, 0.22, 0.30, 0.40), chargeback_escalation_rate=(0.02, 0.04, 0.07, 0.12))
_add("ELECTRONICS", "MID_MARKET",    dispute_rate=(0.005, 0.008, 0.013, 0.020), win_rate=(0.70, 0.62, 0.52, 0.42), avg_response_days=(2.0, 3.5, 5.5, 9.0), inr_share=(0.38, 0.48, 0.58, 0.68), unauthorized_share=(0.18, 0.26, 0.34, 0.44), chargeback_escalation_rate=(0.03, 0.06, 0.10, 0.16))
_add("ELECTRONICS", "SMALL_BUSINESS",dispute_rate=(0.007, 0.012, 0.020, 0.030), win_rate=(0.62, 0.53, 0.43, 0.33), avg_response_days=(2.5, 4.5, 7.0, 12.0), inr_share=(0.40, 0.52, 0.62, 0.72), unauthorized_share=(0.20, 0.28, 0.38, 0.50), chargeback_escalation_rate=(0.04, 0.08, 0.14, 0.22))
_add("ELECTRONICS", "MICRO",         dispute_rate=(0.010, 0.018, 0.030, 0.045), win_rate=(0.50, 0.40, 0.30, 0.20), avg_response_days=(3.0, 6.0, 10.0, 16.0), inr_share=(0.42, 0.55, 0.65, 0.75), unauthorized_share=(0.22, 0.32, 0.44, 0.58), chargeback_escalation_rate=(0.06, 0.12, 0.20, 0.30))

# Apparel (higher INR due to wrong-size returns masquerading as INR)
_add("APPAREL", "ENTERPRISE",    dispute_rate=(0.004, 0.007, 0.012, 0.018), win_rate=(0.68, 0.60, 0.50, 0.40), avg_response_days=(1.5, 3.0, 5.0, 8.0), inr_share=(0.45, 0.55, 0.65, 0.75), unauthorized_share=(0.10, 0.16, 0.24, 0.32), chargeback_escalation_rate=(0.02, 0.05, 0.09, 0.14))
_add("APPAREL", "SMALL_BUSINESS",dispute_rate=(0.009, 0.015, 0.025, 0.038), win_rate=(0.58, 0.48, 0.38, 0.28), avg_response_days=(3.0, 5.0, 8.0, 14.0), inr_share=(0.48, 0.60, 0.70, 0.80), unauthorized_share=(0.12, 0.20, 0.30, 0.42), chargeback_escalation_rate=(0.05, 0.10, 0.17, 0.26))
_add("APPAREL", "MICRO",         dispute_rate=(0.012, 0.020, 0.033, 0.050), win_rate=(0.45, 0.35, 0.25, 0.15), avg_response_days=(4.0, 7.0, 12.0, 18.0), inr_share=(0.50, 0.63, 0.73, 0.83), unauthorized_share=(0.14, 0.24, 0.36, 0.48), chargeback_escalation_rate=(0.07, 0.14, 0.23, 0.35))

# Software / Digital (low INR, higher SUBSCRIPTION)
_add("SOFTWARE", "ENTERPRISE",    dispute_rate=(0.002, 0.004, 0.007, 0.012), win_rate=(0.78, 0.70, 0.60, 0.50), avg_response_days=(1.0, 2.0, 3.5, 6.0), inr_share=(0.10, 0.18, 0.28, 0.38), unauthorized_share=(0.20, 0.28, 0.38, 0.50), chargeback_escalation_rate=(0.02, 0.04, 0.07, 0.11))
_add("SOFTWARE", "SMALL_BUSINESS",dispute_rate=(0.005, 0.009, 0.015, 0.025), win_rate=(0.65, 0.55, 0.44, 0.33), avg_response_days=(2.0, 4.0, 6.5, 11.0), inr_share=(0.12, 0.22, 0.32, 0.44), unauthorized_share=(0.22, 0.32, 0.44, 0.56), chargeback_escalation_rate=(0.04, 0.08, 0.13, 0.20))

# General fallback
_add("GENERAL", "ENTERPRISE",    dispute_rate=(0.004, 0.007, 0.011, 0.017), win_rate=(0.70, 0.62, 0.52, 0.42), avg_response_days=(2.0, 3.5, 5.5, 9.0), inr_share=(0.35, 0.45, 0.55, 0.65), unauthorized_share=(0.15, 0.23, 0.32, 0.43), chargeback_escalation_rate=(0.03, 0.06, 0.10, 0.16))
_add("GENERAL", "MID_MARKET",    dispute_rate=(0.006, 0.010, 0.016, 0.024), win_rate=(0.65, 0.57, 0.47, 0.37), avg_response_days=(2.5, 4.0, 6.5, 11.0), inr_share=(0.37, 0.48, 0.58, 0.68), unauthorized_share=(0.17, 0.26, 0.36, 0.47), chargeback_escalation_rate=(0.04, 0.07, 0.12, 0.19))
_add("GENERAL", "SMALL_BUSINESS",dispute_rate=(0.008, 0.013, 0.021, 0.032), win_rate=(0.60, 0.51, 0.41, 0.31), avg_response_days=(3.0, 5.0, 8.0, 13.0), inr_share=(0.40, 0.52, 0.62, 0.72), unauthorized_share=(0.19, 0.28, 0.40, 0.52), chargeback_escalation_rate=(0.05, 0.09, 0.15, 0.24))
_add("GENERAL", "MICRO",         dispute_rate=(0.011, 0.019, 0.031, 0.047), win_rate=(0.48, 0.38, 0.28, 0.18), avg_response_days=(3.5, 6.5, 11.0, 17.0), inr_share=(0.42, 0.55, 0.65, 0.75), unauthorized_share=(0.21, 0.31, 0.44, 0.57), chargeback_escalation_rate=(0.06, 0.12, 0.20, 0.31))


def _get_benchmarks(cat: MerchantCategory, seg: MerchantSegment) -> dict:
    key = (cat.value, seg.value)
    if key in _BENCHMARKS:
        return _BENCHMARKS[key]
    # Fallback to GENERAL
    fallback = ("GENERAL", seg.value)
    return _BENCHMARKS.get(fallback, _BENCHMARKS[("GENERAL", "MICRO")])


def _percentile(value: float, thresholds: tuple, lower_is_better: bool = True) -> int:
    """
    Map a value onto a percentile bucket (0=best 25%, 100=worst 10%).
    thresholds = (p25, p50, p75, p90)
    """
    p25, p50, p75, p90 = thresholds
    if lower_is_better:
        if value <= p25:
            return 25   # excellent
        if value <= p50:
            return 50
        if value <= p75:
            return 75
        if value <= p90:
            return 90
        return 100
    else:
        # Higher is better (e.g. win_rate)
        if value >= p25:
            return 25
        if value >= p50:
            return 50
        if value >= p75:
            return 75
        if value >= p90:
            return 90
        return 100


def _tier(pct: int) -> BenchmarkTier:
    if pct <= 25:
        return BenchmarkTier.EXCELLENT
    if pct <= 50:
        return BenchmarkTier.GOOD
    if pct <= 75:
        return BenchmarkTier.AVERAGE
    if pct <= 90:
        return BenchmarkTier.POOR
    return BenchmarkTier.CRITICAL


@dataclass
class MetricBenchmark:
    metric: str
    merchant_value: float
    peer_p50: float              # median peer
    percentile_rank: int         # 25/50/75/90/100
    tier: BenchmarkTier
    lower_is_better: bool
    insight: str


@dataclass
class MerchantMetrics:
    merchant_id: str
    segment: MerchantSegment
    category: MerchantCategory

    total_orders: int
    total_disputes: int
    disputes_won: int
    disputes_fought: int
    disputes_escalated: int
    preemptive_refunds: int

    avg_response_days: float
    inr_disputes: int
    unauthorized_disputes: int


@dataclass
class BenchmarkReport:
    merchant_id: str
    segment: MerchantSegment
    category: MerchantCategory

    dispute_rate: float
    win_rate: float
    avg_response_days: float
    refund_rate: float
    chargeback_escalation_rate: float
    inr_share: float
    unauthorized_share: float

    benchmarks: list[MetricBenchmark]
    overall_health_tier: BenchmarkTier
    risk_flags: list[RiskFlag]

    paypal_signal: str
    merchant_coaching: str
    summary: str


def _benchmark(merchant_id: str, seg: MerchantSegment, cat: MerchantCategory,
               metrics: MerchantMetrics) -> BenchmarkReport:
    bm = _get_benchmarks(cat, seg)

    dr = metrics.total_disputes / max(1, metrics.total_orders)
    wr = metrics.disputes_won / max(1, metrics.disputes_fought)
    rr = metrics.preemptive_refunds / max(1, metrics.total_disputes)
    esc = metrics.disputes_escalated / max(1, metrics.total_disputes)
    inr_s = metrics.inr_disputes / max(1, metrics.total_disputes)
    unauth_s = metrics.unauthorized_disputes / max(1, metrics.total_disputes)

    bm_items = [
        MetricBenchmark(
            "dispute_rate", dr,
            peer_p50=bm["dispute_rate"][1],
            percentile_rank=_percentile(dr, bm["dispute_rate"], lower_is_better=True),
            tier=_tier(_percentile(dr, bm["dispute_rate"], lower_is_better=True)),
            lower_is_better=True,
            insight=_insight("dispute_rate", dr, bm["dispute_rate"][1]),
        ),
        MetricBenchmark(
            "win_rate", wr,
            peer_p50=bm["win_rate"][1],
            percentile_rank=_percentile(wr, bm["win_rate"], lower_is_better=False),
            tier=_tier(_percentile(wr, bm["win_rate"], lower_is_better=False)),
            lower_is_better=False,
            insight=_insight("win_rate", wr, bm["win_rate"][1], lower_is_better=False),
        ),
        MetricBenchmark(
            "avg_response_days", metrics.avg_response_days,
            peer_p50=bm["avg_response_days"][1],
            percentile_rank=_percentile(metrics.avg_response_days, bm["avg_response_days"], lower_is_better=True),
            tier=_tier(_percentile(metrics.avg_response_days, bm["avg_response_days"], lower_is_better=True)),
            lower_is_better=True,
            insight=_insight("avg_response_days", metrics.avg_response_days, bm["avg_response_days"][1]),
        ),
        MetricBenchmark(
            "chargeback_escalation_rate", esc,
            peer_p50=bm["chargeback_escalation_rate"][1],
            percentile_rank=_percentile(esc, bm["chargeback_escalation_rate"], lower_is_better=True),
            tier=_tier(_percentile(esc, bm["chargeback_escalation_rate"], lower_is_better=True)),
            lower_is_better=True,
            insight=_insight("chargeback_escalation_rate", esc, bm["chargeback_escalation_rate"][1]),
        ),
        MetricBenchmark(
            "inr_share", inr_s,
            peer_p50=bm["inr_share"][1],
            percentile_rank=_percentile(inr_s, bm["inr_share"], lower_is_better=True),
            tier=_tier(_percentile(inr_s, bm["inr_share"], lower_is_better=True)),
            lower_is_better=True,
            insight=_insight("inr_share", inr_s, bm["inr_share"][1]),
        ),
        MetricBenchmark(
            "unauthorized_share", unauth_s,
            peer_p50=bm["unauthorized_share"][1],
            percentile_rank=_percentile(unauth_s, bm["unauthorized_share"], lower_is_better=True),
            tier=_tier(_percentile(unauth_s, bm["unauthorized_share"], lower_is_better=True)),
            lower_is_better=True,
            insight=_insight("unauthorized_share", unauth_s, bm["unauthorized_share"][1]),
        ),
    ]

    # Overall health = worst tier across critical metrics
    critical_tiers = [
        b.tier for b in bm_items
        if b.metric in ("dispute_rate", "win_rate", "chargeback_escalation_rate")
    ]
    tier_order = [BenchmarkTier.EXCELLENT, BenchmarkTier.GOOD, BenchmarkTier.AVERAGE,
                  BenchmarkTier.POOR, BenchmarkTier.CRITICAL]
    overall = max(critical_tiers, key=lambda t: tier_order.index(t))

    # Risk flags
    flags: list[RiskFlag] = []
    if dr >= 0.010:
        flags.append(RiskFlag.APPROACHING_CHARGEBACK_THRESHOLD)
    if unauth_s > bm["unauthorized_share"][2]:
        flags.append(RiskFlag.HIGH_UNAUTHORIZED_RATE)
    if inr_s > bm["inr_share"][2]:
        flags.append(RiskFlag.HIGH_INR_RATE)
    if wr < bm["win_rate"][2]:
        flags.append(RiskFlag.LOW_WIN_RATE)
    if metrics.avg_response_days > bm["avg_response_days"][2]:
        flags.append(RiskFlag.SLOW_RESPONSE_TIME)
    if esc > bm["chargeback_escalation_rate"][2]:
        flags.append(RiskFlag.HIGH_ESCALATION_RATE)

    # PayPal signal
    if overall == BenchmarkTier.CRITICAL:
        paypal_signal = (
            f"CRITICAL: Merchant {merchant_id} ({cat.value}/{seg.value}) is in the "
            f"bottom 10% of peers. Dispute rate={dr:.2%} (peer median={bm['dispute_rate'][1]:.2%}). "
            f"Flags: {', '.join(f.value for f in flags)}. "
            f"Recommend immediate account manager outreach and seller tools enrollment."
        )
    elif flags:
        paypal_signal = (
            f"Merchant {merchant_id}: {overall.value} overall. "
            f"Active risk flags: {', '.join(f.value for f in flags)}. "
            f"Peer dispute rate median: {bm['dispute_rate'][1]:.2%}, merchant: {dr:.2%}."
        )
    else:
        paypal_signal = (
            f"Merchant {merchant_id}: {overall.value} health. "
            f"Dispute rate {dr:.2%} vs peer median {bm['dispute_rate'][1]:.2%}. No active flags."
        )

    # Merchant coaching
    top_issues = [b for b in bm_items if b.tier in (BenchmarkTier.POOR, BenchmarkTier.CRITICAL)]
    if top_issues:
        coaching_lines = []
        for b in top_issues[:3]:
            coaching_lines.append(
                f"• {b.metric}: {b.merchant_value:.2%} vs peer median {b.peer_p50:.2%} — {b.insight}"
            )
        merchant_coaching = "Areas needing improvement:\n" + "\n".join(coaching_lines)
    else:
        merchant_coaching = (
            f"Performance is {overall.value}. "
            f"Top opportunity: improve {min(bm_items, key=lambda b: b.percentile_rank if not b.lower_is_better else 100 - b.percentile_rank).metric}."
        )

    summary = (
        f"Merchant {merchant_id} | {cat.value}/{seg.value} | "
        f"dispute_rate={dr:.2%} | win_rate={wr:.2%} | "
        f"overall={overall.value} | flags={len(flags)}"
    )

    return BenchmarkReport(
        merchant_id=merchant_id,
        segment=seg,
        category=cat,
        dispute_rate=round(dr, 6),
        win_rate=round(wr, 6),
        avg_response_days=metrics.avg_response_days,
        refund_rate=round(rr, 6),
        chargeback_escalation_rate=round(esc, 6),
        inr_share=round(inr_s, 6),
        unauthorized_share=round(unauth_s, 6),
        benchmarks=bm_items,
        overall_health_tier=overall,
        risk_flags=flags,
        paypal_signal=paypal_signal,
        merchant_coaching=merchant_coaching,
        summary=summary,
    )


def _insight(metric: str, value: float, peer_median: float,
             lower_is_better: bool = True) -> str:
    pct_diff = abs(value - peer_median) / max(peer_median, 0.001) * 100
    direction = "above" if value > peer_median else "below"
    good = (value < peer_median) if lower_is_better else (value > peer_median)
    verb = "better than" if good else "worse than"
    return f"{pct_diff:.0f}% {direction} peer median — {verb} average {cat_insight(metric)}."


def cat_insight(metric: str) -> str:
    return {
        "dispute_rate":               "fulfillment quality",
        "win_rate":                   "evidence quality",
        "avg_response_days":          "response speed",
        "chargeback_escalation_rate": "dispute resolution quality",
        "inr_share":                  "shipping reliability",
        "unauthorized_share":         "fraud prevention",
    }.get(metric, "performance")


def benchmark_merchant(metrics: MerchantMetrics) -> BenchmarkReport:
    """Public entry point: benchmark a merchant's dispute metrics vs. peers."""
    return _benchmark(
        metrics.merchant_id,
        metrics.segment,
        metrics.category,
        metrics,
    )
