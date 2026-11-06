"""
Dispute Outcome Tracker — maintain a rolling win/loss record and compute
running performance metrics for dispute management.

Given a list of historical dispute outcomes, this oracle computes:
  - Overall win rate, by network, by reason code
  - Expected vs. actual win rate deviation
  - Revenue recovered vs. lost
  - Performance trends (improving / stable / declining)
  - Recommendations for improvement areas

Useful for calibrating win-probability estimates in other oracles and
for generating merchant-facing dispute performance reports.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OutcomeResult(str, Enum):
    WON          = "WON"          # Merchant won the dispute
    LOST         = "LOST"         # Merchant lost / chargeback finalized
    ACCEPTED     = "ACCEPTED"     # Merchant accepted the chargeback
    PARTIAL_WIN  = "PARTIAL_WIN"  # Negotiated partial refund
    WITHDRAWN    = "WITHDRAWN"    # Buyer withdrew dispute


class PerformanceTrend(str, Enum):
    IMPROVING  = "IMPROVING"   # Win rate rising over last 30 days vs prior 30
    STABLE     = "STABLE"      # Win rate within ±5pp
    DECLINING  = "DECLINING"   # Win rate falling
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class DisputeOutcomeRecord:
    """A single resolved dispute outcome."""
    dispute_id: str
    network: str
    reason_code: str
    transaction_amount: float
    outcome: OutcomeResult
    resolved_epoch: int          # When the dispute was resolved
    predicted_win_probability: float = 0.0   # What the oracle predicted


@dataclass
class SegmentStats:
    """Win-rate and revenue stats for a segment (network or reason code)."""
    segment_key: str
    total_disputes: int
    won: int
    lost: int
    accepted: int
    partial_win: int
    win_rate: float              # won / (won + lost)
    revenue_recovered: float
    revenue_lost: float
    avg_predicted_win_prob: float
    prediction_accuracy_delta: float   # actual win_rate - avg_predicted


@dataclass
class DisputePerformanceReport:
    """Full dispute performance report."""
    total_disputes: int
    overall_win_rate: float
    revenue_recovered: float
    revenue_lost: float
    revenue_accepted: float

    by_network: list[SegmentStats]
    by_reason_code: list[SegmentStats]

    performance_trend: PerformanceTrend
    trend_win_rate_recent: float     # Last 30 days
    trend_win_rate_prior: float      # Prior 30 days

    top_loss_reason: str
    prediction_calibration_delta: float   # Avg(actual - predicted) across all disputes

    recommendations: list[str]
    summary: str


def _compute_segment_stats(key: str, records: list[DisputeOutcomeRecord]) -> SegmentStats:
    if not records:
        return SegmentStats(key, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    won       = sum(1 for r in records if r.outcome == OutcomeResult.WON)
    lost      = sum(1 for r in records if r.outcome == OutcomeResult.LOST)
    accepted  = sum(1 for r in records if r.outcome == OutcomeResult.ACCEPTED)
    partial   = sum(1 for r in records if r.outcome == OutcomeResult.PARTIAL_WIN)

    contested = won + lost
    win_rate  = won / contested if contested > 0 else 0.0

    revenue_recovered = sum(
        r.transaction_amount for r in records
        if r.outcome == OutcomeResult.WON
    )
    revenue_lost = sum(
        r.transaction_amount for r in records
        if r.outcome in (OutcomeResult.LOST, OutcomeResult.ACCEPTED)
    )

    pred_probs = [r.predicted_win_probability for r in records if r.predicted_win_probability > 0]
    avg_pred   = sum(pred_probs) / len(pred_probs) if pred_probs else 0.0
    cal_delta  = win_rate - avg_pred

    return SegmentStats(
        segment_key=key,
        total_disputes=len(records),
        won=won,
        lost=lost,
        accepted=accepted,
        partial_win=partial,
        win_rate=round(win_rate, 4),
        revenue_recovered=round(revenue_recovered, 2),
        revenue_lost=round(revenue_lost, 2),
        avg_predicted_win_prob=round(avg_pred, 4),
        prediction_accuracy_delta=round(cal_delta, 4),
    )


def compute_dispute_performance(
    records: list[DisputeOutcomeRecord],
    now_epoch: int,
) -> DisputePerformanceReport:
    """Compute dispute performance metrics from a list of resolved disputes."""
    if not records:
        return DisputePerformanceReport(
            total_disputes=0,
            overall_win_rate=0.0,
            revenue_recovered=0.0,
            revenue_lost=0.0,
            revenue_accepted=0.0,
            by_network=[],
            by_reason_code=[],
            performance_trend=PerformanceTrend.INSUFFICIENT_DATA,
            trend_win_rate_recent=0.0,
            trend_win_rate_prior=0.0,
            top_loss_reason="N/A",
            prediction_calibration_delta=0.0,
            recommendations=["No dispute history yet"],
            summary="No dispute history",
        )

    # Overall stats
    overall = _compute_segment_stats("ALL", records)

    # By network
    networks = sorted({r.network for r in records})
    by_network = [
        _compute_segment_stats(n, [r for r in records if r.network == n])
        for n in networks
    ]

    # By reason code
    codes = sorted({r.reason_code for r in records})
    by_code = [
        _compute_segment_stats(c, [r for r in records if r.reason_code == c])
        for c in codes
    ]

    # Trend: last 30 days vs prior 30 days
    day30 = 30 * 86400
    recent_cutoff = now_epoch - day30
    prior_cutoff  = now_epoch - 2 * day30
    recent_records = [r for r in records if r.resolved_epoch >= recent_cutoff]
    prior_records  = [r for r in records if prior_cutoff <= r.resolved_epoch < recent_cutoff]
    recent_stats = _compute_segment_stats("recent", recent_records)
    prior_stats  = _compute_segment_stats("prior", prior_records)

    if len(recent_records) < 3 or len(prior_records) < 3:
        trend = PerformanceTrend.INSUFFICIENT_DATA
    elif recent_stats.win_rate - prior_stats.win_rate > 0.05:
        trend = PerformanceTrend.IMPROVING
    elif prior_stats.win_rate - recent_stats.win_rate > 0.05:
        trend = PerformanceTrend.DECLINING
    else:
        trend = PerformanceTrend.STABLE

    # Top loss reason
    losses_by_code: dict[str, int] = {}
    for r in records:
        if r.outcome == OutcomeResult.LOST:
            losses_by_code[r.reason_code] = losses_by_code.get(r.reason_code, 0) + 1
    top_loss = max(losses_by_code, key=losses_by_code.get) if losses_by_code else "N/A"

    # Revenue metrics
    revenue_accepted = sum(
        r.transaction_amount for r in records if r.outcome == OutcomeResult.ACCEPTED
    )

    # Recommendations
    recs: list[str] = []
    if overall.win_rate < 0.40:
        recs.append(f"Win rate {overall.win_rate:.0%} is below 40% — review evidence quality")
    low_win_segments = [s for s in by_code if s.win_rate < 0.30 and s.total_disputes >= 3]
    for seg in low_win_segments:
        recs.append(f"Reason code {seg.segment_key} has {seg.win_rate:.0%} win rate — consider auto-accept")
    if overall.prediction_accuracy_delta < -0.10:
        recs.append("Oracle predictions are optimistic — recalibrate win probability inputs")
    elif overall.prediction_accuracy_delta > 0.10:
        recs.append("Oracle predictions are conservative — you may be accepting winnable disputes")
    if trend == PerformanceTrend.DECLINING:
        recs.append("Win rate declining — investigate recent changes in evidence quality")
    if not recs:
        recs.append("Performance on track — maintain current dispute handling approach")

    summary = (
        f"Dispute performance: {len(records)} resolved | "
        f"win_rate={overall.win_rate:.0%} | "
        f"recovered=${overall.revenue_recovered:.0f} lost=${overall.revenue_lost:.0f} | "
        f"trend={trend.value}"
    )

    return DisputePerformanceReport(
        total_disputes=len(records),
        overall_win_rate=overall.win_rate,
        revenue_recovered=overall.revenue_recovered,
        revenue_lost=overall.revenue_lost,
        revenue_accepted=round(revenue_accepted, 2),
        by_network=by_network,
        by_reason_code=by_code,
        performance_trend=trend,
        trend_win_rate_recent=recent_stats.win_rate,
        trend_win_rate_prior=prior_stats.win_rate,
        top_loss_reason=top_loss,
        prediction_calibration_delta=overall.prediction_accuracy_delta,
        recommendations=recs,
        summary=summary,
    )
