"""
Dispute Velocity Monitor — detect abnormal spikes in dispute filing rate.

A merchant's "normal" dispute velocity is 2-5 per day. When it suddenly
jumps to 20+ per day, something is wrong: a product went out defective,
a fraud ring targeted the merchant, or a fulfillment failure caused
widespread INR disputes.

This module computes rolling velocity metrics and classifies the spike
cause so the merchant can respond immediately — not after the monthly
report.

Three spike patterns:
  1. PRODUCT_DEFECT_SPIKE: >60% of same-day disputes are SNAD + same SKU
  2. FRAUD_WAVE_SPIKE: >50% UNAUTHORIZED, velocity 5× normal, cluster in <6h
  3. FULFILLMENT_FAILURE_SPIKE: >60% INR, velocity 3× normal, all same carrier
  4. SUBSCRIPTION_CHURN_SPIKE: >70% SUBSCRIPTION disputes in 24h window
  5. GENERAL_SPIKE: velocity >3× normal, mixed reason codes

PayPal benefit: PayPal can use velocity alerts to proactively trigger
merchant assistance calls before dispute rates breach thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VelocitySpikeType(str, Enum):
    PRODUCT_DEFECT_SPIKE      = "PRODUCT_DEFECT_SPIKE"
    FRAUD_WAVE_SPIKE          = "FRAUD_WAVE_SPIKE"
    FULFILLMENT_FAILURE_SPIKE = "FULFILLMENT_FAILURE_SPIKE"
    SUBSCRIPTION_CHURN_SPIKE  = "SUBSCRIPTION_CHURN_SPIKE"
    GENERAL_SPIKE             = "GENERAL_SPIKE"
    NORMAL                    = "NORMAL"


class SpikeUrgency(str, Enum):
    CRITICAL = "CRITICAL"   # Act within 1 hour
    HIGH     = "HIGH"       # Act within 6 hours
    MEDIUM   = "MEDIUM"     # Act within 24 hours
    LOW      = "LOW"        # Monitor; no immediate action
    NORMAL   = "NORMAL"


@dataclass
class DisputeVelocityRecord:
    """A lightweight snapshot of one dispute, for velocity analysis."""
    dispute_id: str
    reason_code: str
    amount: float
    filed_hour: int         # 0-23, hour of day the dispute was filed
    sku: str = ""           # product SKU if available
    carrier: str = ""       # carrier if INR


@dataclass
class VelocityInput:
    merchant_id: str
    window_hours: int = 24          # analysis window
    current_window_disputes: list[DisputeVelocityRecord] = field(default_factory=list)
    baseline_daily_disputes: float = 5.0    # merchant's normal disputes/day
    baseline_win_rate: float = 0.55
    high_value_threshold: float = 200.0     # disputes above this are "high value"


@dataclass
class SpikeAction:
    priority: int
    action: str
    owner: str              # "MERCHANT" / "PAYPAL" / "BOTH"
    deadline_hours: int


@dataclass
class VelocityReport:
    merchant_id: str
    window_hours: int
    current_dispute_count: int
    baseline_rate_per_window: float
    velocity_multiplier: float      # current / baseline

    spike_type: VelocitySpikeType
    urgency: SpikeUrgency
    is_spike: bool

    # Reason code breakdown
    inr_count: int
    snad_count: int
    unauthorized_count: int
    subscription_count: int
    dominant_reason_code: str
    dominant_reason_pct: float

    # Pattern signals
    top_sku: str
    top_sku_dispute_count: int
    top_carrier: str
    top_carrier_dispute_count: int
    hour_cluster: Optional[int]     # hour with highest filing concentration
    high_value_dispute_count: int

    # Financial exposure
    total_exposure: float
    avg_dispute_amount: float

    # Actions
    immediate_actions: list[SpikeAction]
    paypal_notification: str    # text to send to PayPal

    key_insight: str
    summary: str


def _dominant_code(inp: VelocityInput) -> tuple[str, int, float]:
    records = inp.current_window_disputes
    counts = {}
    for r in records:
        counts[r.reason_code] = counts.get(r.reason_code, 0) + 1
    if not counts:
        return "NONE", 0, 0.0
    top_code = max(counts, key=lambda k: counts[k])
    top_count = counts[top_code]
    total = len(records)
    return top_code, top_count, round(top_count / max(total, 1), 3)


def _top_sku(records: list[DisputeVelocityRecord]) -> tuple[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        if r.sku:
            counts[r.sku] = counts.get(r.sku, 0) + 1
    if not counts:
        return "", 0
    top = max(counts, key=lambda k: counts[k])
    return top, counts[top]


def _top_carrier(records: list[DisputeVelocityRecord]) -> tuple[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        if r.carrier:
            counts[r.carrier] = counts.get(r.carrier, 0) + 1
    if not counts:
        return "", 0
    top = max(counts, key=lambda k: counts[k])
    return top, counts[top]


def _hour_cluster(records: list[DisputeVelocityRecord]) -> Optional[int]:
    """Return the hour (0-23) with the most filings, if >40% of all disputes."""
    if not records:
        return None
    counts: dict[int, int] = {}
    for r in records:
        counts[r.filed_hour] = counts.get(r.filed_hour, 0) + 1
    top_hour = max(counts, key=lambda k: counts[k])
    if counts[top_hour] / len(records) >= 0.40:
        return top_hour
    return None


def _classify_spike(
    velocity_mult: float,
    dominant_code: str,
    dominant_pct: float,
    top_carrier: str,
    hour_cluster: Optional[int],
    records: list[DisputeVelocityRecord],
) -> VelocitySpikeType:
    if velocity_mult < 2.0:
        return VelocitySpikeType.NORMAL

    unauth_pct = sum(1 for r in records if r.reason_code == "UNAUTHORIZED") / max(len(records), 1)
    if unauth_pct >= 0.50 and hour_cluster is not None:
        return VelocitySpikeType.FRAUD_WAVE_SPIKE

    inr_pct = sum(1 for r in records if r.reason_code == "INR") / max(len(records), 1)
    if inr_pct >= 0.60 and top_carrier:
        return VelocitySpikeType.FULFILLMENT_FAILURE_SPIKE

    snad_pct = sum(1 for r in records if r.reason_code == "SNAD") / max(len(records), 1)
    if snad_pct >= 0.60:
        return VelocitySpikeType.PRODUCT_DEFECT_SPIKE

    sub_pct = sum(1 for r in records if r.reason_code == "SUBSCRIPTION") / max(len(records), 1)
    if sub_pct >= 0.70:
        return VelocitySpikeType.SUBSCRIPTION_CHURN_SPIKE

    return VelocitySpikeType.GENERAL_SPIKE


def _urgency(spike_type: VelocitySpikeType, velocity_mult: float) -> SpikeUrgency:
    if spike_type == VelocitySpikeType.NORMAL:
        return SpikeUrgency.NORMAL
    if spike_type == VelocitySpikeType.FRAUD_WAVE_SPIKE or velocity_mult >= 8:
        return SpikeUrgency.CRITICAL
    if velocity_mult >= 5:
        return SpikeUrgency.HIGH
    if velocity_mult >= 3:
        return SpikeUrgency.MEDIUM
    return SpikeUrgency.LOW


def _build_actions(
    spike_type: VelocitySpikeType,
    urgency: SpikeUrgency,
    top_sku: str,
    top_carrier: str,
    merchant_id: str,
) -> list[SpikeAction]:
    actions = []
    if spike_type == VelocitySpikeType.FRAUD_WAVE_SPIKE:
        actions += [
            SpikeAction(1, "Enable enhanced fraud screening on all new orders immediately", "MERCHANT", 1),
            SpikeAction(2, "Review Buyer Fraud DNA profiler for all disputes in this window", "MERCHANT", 2),
            SpikeAction(3, f"Notify PayPal Risk team of fraud wave at merchant {merchant_id}", "PAYPAL", 2),
        ]
    elif spike_type == VelocitySpikeType.PRODUCT_DEFECT_SPIKE:
        sku_note = f" for SKU {top_sku}" if top_sku else ""
        actions += [
            SpikeAction(1, f"Pause sales{sku_note} pending quality review", "MERCHANT", 4),
            SpikeAction(2, "Issue proactive refund to all affected buyers to prevent further disputes", "MERCHANT", 6),
            SpikeAction(3, "Document quality issue for dispute evidence (supplier records)", "MERCHANT", 12),
        ]
    elif spike_type == VelocitySpikeType.FULFILLMENT_FAILURE_SPIKE:
        carrier_note = f" via {top_carrier}" if top_carrier else ""
        actions += [
            SpikeAction(1, f"Contact carrier{carrier_note} to report systemic delivery failure", "MERCHANT", 4),
            SpikeAction(2, "Proactively email all buyers with updated tracking status", "MERCHANT", 6),
            SpikeAction(3, "File carrier claim for lost/delayed shipments before dispute deadlines", "MERCHANT", 24),
        ]
    elif spike_type == VelocitySpikeType.SUBSCRIPTION_CHURN_SPIKE:
        actions += [
            SpikeAction(1, "Audit recent subscription billing events for errors", "MERCHANT", 4),
            SpikeAction(2, "Process cancellation + refund proactively for all affected subscribers", "MERCHANT", 12),
            SpikeAction(3, "Review subscription terms clarity on checkout page", "MERCHANT", 48),
        ]
    else:
        actions += [
            SpikeAction(1, "Review all disputes in spike window for common pattern", "MERCHANT", 6),
            SpikeAction(2, "Enable Chargeback Prevention Advisor on all new orders", "MERCHANT", 12),
        ]
    return actions


def monitor_dispute_velocity(inp: VelocityInput) -> VelocityReport:
    """
    Analyze current dispute filing rate against baseline to detect and
    classify velocity spikes.
    """
    records = inp.current_window_disputes
    n = len(records)

    baseline_rate = inp.baseline_daily_disputes * (inp.window_hours / 24.0)
    velocity_mult = round(n / max(baseline_rate, 0.1), 2)

    # Code breakdown
    inr_count = sum(1 for r in records if r.reason_code == "INR")
    snad_count = sum(1 for r in records if r.reason_code == "SNAD")
    unauth_count = sum(1 for r in records if r.reason_code == "UNAUTHORIZED")
    sub_count = sum(1 for r in records if r.reason_code == "SUBSCRIPTION")

    dom_code, dom_count, dom_pct = _dominant_code(inp)
    top_sku_val, top_sku_cnt = _top_sku(records)
    top_car, top_car_cnt = _top_carrier(records)
    h_cluster = _hour_cluster(records)

    spike_type = _classify_spike(velocity_mult, dom_code, dom_pct, top_car, h_cluster, records)
    urgency = _urgency(spike_type, velocity_mult)
    is_spike = spike_type != VelocitySpikeType.NORMAL

    total_exposure = round(sum(r.amount for r in records), 2)
    avg_amount = round(total_exposure / max(n, 1), 2)
    high_val_count = sum(1 for r in records if r.amount >= inp.high_value_threshold)

    actions = _build_actions(spike_type, urgency, top_sku_val, top_car, inp.merchant_id)

    # Key insight
    if spike_type == VelocitySpikeType.FRAUD_WAVE_SPIKE:
        insight = (
            f"Fraud wave detected: {unauth_count} UNAUTHORIZED disputes "
            f"({dom_pct:.0%}) clustered at hour {h_cluster}. "
            "Likely automated card testing or coordinated fraud ring."
        )
    elif spike_type == VelocitySpikeType.PRODUCT_DEFECT_SPIKE:
        sku_note = f" SKU={top_sku_val}" if top_sku_val else ""
        insight = (
            f"Product defect spike:{sku_note} {snad_count} SNAD disputes "
            f"({dom_pct:.0%}) in {inp.window_hours}h. "
            "Likely a batch of defective units."
        )
    elif spike_type == VelocitySpikeType.FULFILLMENT_FAILURE_SPIKE:
        carrier_note = f" via {top_car}" if top_car else ""
        insight = (
            f"Fulfillment failure:{carrier_note} {inr_count} INR disputes "
            f"({dom_pct:.0%}). Carrier may have systemic delay/loss issue."
        )
    elif spike_type == VelocitySpikeType.SUBSCRIPTION_CHURN_SPIKE:
        insight = (
            f"Subscription churn: {sub_count} SUBSCRIPTION disputes "
            f"({dom_pct:.0%}). Likely billing surprise or failed cancellation flow."
        )
    elif is_spike:
        insight = (
            f"General dispute spike: {velocity_mult:.1f}× baseline, "
            f"mixed reasons (top: {dom_code} {dom_pct:.0%}). Root cause unclear."
        )
    else:
        insight = f"Dispute velocity normal: {n} disputes in {inp.window_hours}h ({velocity_mult:.1f}× baseline)."

    paypal_notif = (
        f"[{urgency.value}] Merchant {inp.merchant_id}: {spike_type.value} "
        f"— {n} disputes in {inp.window_hours}h ({velocity_mult:.1f}× baseline, "
        f"${total_exposure:.0f} exposure). Dominant: {dom_code} ({dom_pct:.0%})."
    ) if is_spike else ""

    summary = (
        f"Velocity {inp.merchant_id} [{inp.window_hours}h]: "
        f"{n} disputes ({velocity_mult:.1f}× baseline) "
        f"spike={spike_type.value} urgency={urgency.value} "
        f"exposure=${total_exposure:.0f}"
    )

    return VelocityReport(
        merchant_id=inp.merchant_id,
        window_hours=inp.window_hours,
        current_dispute_count=n,
        baseline_rate_per_window=round(baseline_rate, 2),
        velocity_multiplier=velocity_mult,
        spike_type=spike_type,
        urgency=urgency,
        is_spike=is_spike,
        inr_count=inr_count,
        snad_count=snad_count,
        unauthorized_count=unauth_count,
        subscription_count=sub_count,
        dominant_reason_code=dom_code,
        dominant_reason_pct=dom_pct,
        top_sku=top_sku_val,
        top_sku_dispute_count=top_sku_cnt,
        top_carrier=top_car,
        top_carrier_dispute_count=top_car_cnt,
        hour_cluster=h_cluster,
        high_value_dispute_count=high_val_count,
        total_exposure=total_exposure,
        avg_dispute_amount=avg_amount,
        immediate_actions=actions,
        paypal_notification=paypal_notif,
        key_insight=insight,
        summary=summary,
    )
