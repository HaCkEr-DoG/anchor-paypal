"""
Win/Loss Analyzer — post-dispute learning engine.

The best dispute management system gets smarter after every fight.
This module extracts patterns from dispute outcomes and feeds them
back into the oracle stack as calibration signals.

What it learns:
  1. Which evidence combinations win most often by claim type
  2. Which carriers cause the most repeat disputes
  3. Which ZIP codes / buyer profiles are highest-risk
  4. Whether fighting or settling was better for specific scenarios
  5. How response timing correlates with win rate

How it feeds back:
  → calibrator: win rate updates by evidence quality tier
  → carrier_intel: observed win rates vs. database priors
  → timeline_optimizer: validates optimal timing assumptions
  → dispute_DNA: adds new winning patterns to the database

The compound effect: each dispute outcome improves the next 100 decisions.
Competitors that don't do this are locked into static rule sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date


class OutcomeType(str, Enum):
    WON_INITIAL     = "WON_INITIAL"      # Won at first response
    WON_APPEAL      = "WON_APPEAL"       # Won after appeal
    LOST_INITIAL    = "LOST_INITIAL"     # Lost, did not appeal
    LOST_APPEAL     = "LOST_APPEAL"      # Lost, appeal also failed
    SETTLED         = "SETTLED"          # Reached mutual agreement
    ACCEPTED        = "ACCEPTED"         # Merchant voluntarily accepted
    WITHDRAWN       = "WITHDRAWN"        # Buyer withdrew dispute
    TIMED_OUT       = "TIMED_OUT"        # Auto-closed (merchant didn't respond)


@dataclass
class DisputeOutcomeRecord:
    """
    One completed dispute outcome to learn from.
    The richer this record, the more the system learns.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float
    outcome: OutcomeType
    # Evidence used
    had_tracking: bool = False
    had_delivery_confirmation: bool = False
    had_buyer_signature: bool = False
    had_product_photos: bool = False
    had_communication_history: bool = False
    evidence_quality_tier: str = "MODERATE"   # "STRONG" / "MODERATE" / "WEAK"
    # Timing
    response_day: Optional[int] = None         # Day of response window (1-20)
    was_appealed: bool = False
    # Context
    carrier: Optional[str] = None
    buyer_zip: Optional[str] = None
    buyer_risk_tier: str = "TIER_1_NORMAL"
    dispute_date: Optional[date] = None
    resolution_date: Optional[date] = None
    # Settlement
    was_settled: bool = False
    settlement_amount: Optional[float] = None
    # Scoring at time
    fight_score_at_decision: Optional[float] = None


@dataclass
class WinRateStat:
    """Win rate for a specific segment."""
    segment_name: str
    segment_value: str
    wins: int
    sample_size: int
    win_rate: float
    avg_dispute_amount: float
    sample_size_note: str    # "small sample" / "reliable" / "high confidence"


@dataclass
class LearningInsight:
    """One actionable insight extracted from outcome data."""
    insight_type: str           # "evidence_impact" / "timing_impact" / "carrier_impact" etc.
    finding: str                # What the data shows
    impact: float               # How much this factor changes win rate (+/-)
    confidence: float           # 0–1 based on sample size
    sample_size: int
    recommendation: str         # What to do with this insight


@dataclass
class WinLossReport:
    """
    Complete win/loss analysis from a batch of outcomes.
    Contains both statistics and actionable insights.
    """
    generated_date: date
    total_outcomes: int
    total_wins: int
    total_losses: int
    total_settled: int
    overall_win_rate: float

    # Segmented win rates
    win_rate_by_claim_type: list[WinRateStat]
    win_rate_by_evidence_quality: list[WinRateStat]
    win_rate_by_response_timing: list[WinRateStat]
    win_rate_by_carrier: list[WinRateStat]
    win_rate_by_buyer_risk_tier: list[WinRateStat]

    # Insights
    insights: list[LearningInsight]

    # Calibration updates to feed back into the oracle stack
    calibration_updates: dict[str, float]   # {segment_key: updated_win_rate}

    # Settlement analysis
    settled_vs_fought_comparison: str       # Narrative on settle vs fight outcomes

    summary: str


def analyze_outcomes(
    outcomes: list[DisputeOutcomeRecord],
    analysis_date: Optional[date] = None,
) -> WinLossReport:
    """
    Analyze a batch of dispute outcomes and extract learning signals.

    Returns win rates by segment plus actionable insights for
    improving the oracle stack's decision quality.
    """
    if analysis_date is None:
        analysis_date = date.today()

    if not outcomes:
        return WinLossReport(
            generated_date=analysis_date,
            total_outcomes=0,
            total_wins=0,
            total_losses=0,
            total_settled=0,
            overall_win_rate=0.0,
            win_rate_by_claim_type=[],
            win_rate_by_evidence_quality=[],
            win_rate_by_response_timing=[],
            win_rate_by_carrier=[],
            win_rate_by_buyer_risk_tier=[],
            insights=[],
            calibration_updates={},
            settled_vs_fought_comparison="No data",
            summary="No outcomes to analyze",
        )

    # Categorize outcomes
    wins = [o for o in outcomes if o.outcome in (OutcomeType.WON_INITIAL, OutcomeType.WON_APPEAL, OutcomeType.WITHDRAWN)]
    losses = [o for o in outcomes if o.outcome in (OutcomeType.LOST_INITIAL, OutcomeType.LOST_APPEAL, OutcomeType.TIMED_OUT)]
    settled = [o for o in outcomes if o.outcome in (OutcomeType.SETTLED, OutcomeType.ACCEPTED)]

    overall_win_rate = len(wins) / len(outcomes) if outcomes else 0.0

    # ── Win rates by claim type ────────────────────────────────────────────
    ct_stats = _win_rates_by_field(outcomes, "claim_type", wins)

    # ── Win rates by evidence quality ─────────────────────────────────────
    ev_stats = _win_rates_by_field(outcomes, "evidence_quality_tier", wins)

    # ── Win rates by response timing bucket ───────────────────────────────
    timed_outcomes = [o for o in outcomes if o.response_day is not None]
    timing_buckets = {
        "day_1_2":    [o for o in timed_outcomes if o.response_day and o.response_day <= 2],
        "day_3_5":    [o for o in timed_outcomes if o.response_day and 3 <= o.response_day <= 5],
        "day_6_10":   [o for o in timed_outcomes if o.response_day and 6 <= o.response_day <= 10],
        "day_11_15":  [o for o in timed_outcomes if o.response_day and 11 <= o.response_day <= 15],
        "day_16_plus":[o for o in timed_outcomes if o.response_day and o.response_day >= 16],
    }
    timing_stats = []
    for bucket_name, bucket_outcomes in timing_buckets.items():
        if not bucket_outcomes:
            continue
        bucket_wins = [o for o in bucket_outcomes if o.outcome in (OutcomeType.WON_INITIAL, OutcomeType.WON_APPEAL, OutcomeType.WITHDRAWN)]
        stat = _make_stat(bucket_name, bucket_name, bucket_wins, bucket_outcomes)
        timing_stats.append(stat)

    # ── Win rates by carrier ───────────────────────────────────────────────
    carrier_stats = _win_rates_by_field(
        [o for o in outcomes if o.carrier], "carrier", wins
    )

    # ── Win rates by buyer risk tier ───────────────────────────────────────
    risk_stats = _win_rates_by_field(outcomes, "buyer_risk_tier", wins)

    # ── Extract insights ───────────────────────────────────────────────────
    insights: list[LearningInsight] = []

    # Evidence quality insight
    strong_ev = [o for o in outcomes if o.evidence_quality_tier == "STRONG"]
    weak_ev = [o for o in outcomes if o.evidence_quality_tier == "WEAK"]
    if len(strong_ev) >= 3 and len(weak_ev) >= 3:
        strong_wins = [o for o in strong_ev if o in wins]
        weak_wins = [o for o in weak_ev if o in wins]
        strong_wr = len(strong_wins) / len(strong_ev)
        weak_wr = len(weak_wins) / len(weak_ev)
        impact = strong_wr - weak_wr
        insights.append(LearningInsight(
            insight_type="evidence_impact",
            finding=f"Strong evidence wins {strong_wr:.0%} vs weak evidence {weak_wr:.0%}",
            impact=round(impact, 3),
            confidence=min(0.95, 0.60 + min(len(strong_ev), len(weak_ev)) * 0.01),
            sample_size=len(strong_ev) + len(weak_ev),
            recommendation=(
                f"Each improvement in evidence quality adds ~{impact:.0%} win rate. "
                "Prioritize evidence gathering before submitting."
            ),
        ))

    # Timing insight
    if len(timing_stats) >= 2:
        optimal_stat = next((s for s in timing_stats if s.segment_value == "day_3_5"), None)
        early_stat = next((s for s in timing_stats if s.segment_value == "day_1_2"), None)
        if optimal_stat and early_stat:
            timing_impact = optimal_stat.win_rate - early_stat.win_rate
            insights.append(LearningInsight(
                insight_type="timing_impact",
                finding=(
                    f"Responding day 3-5 wins {optimal_stat.win_rate:.0%} "
                    f"vs day 1-2 wins {early_stat.win_rate:.0%}"
                ),
                impact=round(timing_impact, 3),
                confidence=min(0.90, 0.55 + (optimal_stat.sample_size + early_stat.sample_size) * 0.005),
                sample_size=optimal_stat.sample_size + early_stat.sample_size,
                recommendation=(
                    f"Waiting until day 3-5 adds {timing_impact:.0%} win rate. "
                    "Do not rush to respond on day 1."
                ),
            ))

    # Carrier insight
    for stat in carrier_stats:
        if stat.win_rate < 0.50 and stat.sample_size >= 3:
            insights.append(LearningInsight(
                insight_type="carrier_risk",
                finding=f"Carrier {stat.segment_value} has only {stat.win_rate:.0%} win rate",
                impact=round(0.68 - stat.win_rate, 3),  # vs average INR win rate
                confidence=min(0.85, 0.50 + stat.sample_size * 0.02),
                sample_size=stat.sample_size,
                recommendation=(
                    f"Avoid {stat.segment_value} for high-value orders or for buyers in "
                    "high-dispute ZIP codes. Switch to FedEx/USPS Priority."
                ),
            ))

    # Appeal insight
    appealed = [o for o in outcomes if o.was_appealed]
    if len(appealed) >= 3:
        appeal_wins = [o for o in appealed if o.outcome == OutcomeType.WON_APPEAL]
        appeal_wr = len(appeal_wins) / len(appealed)
        insights.append(LearningInsight(
            insight_type="appeal_effectiveness",
            finding=f"Appeals succeed {appeal_wr:.0%} of the time ({len(appeal_wins)}/{len(appealed)})",
            impact=round(appeal_wr, 3),
            confidence=min(0.85, 0.50 + len(appealed) * 0.02),
            sample_size=len(appealed),
            recommendation=(
                f"Appeal win rate {appeal_wr:.0%} — always appeal losses when you have new evidence. "
                "Never resubmit same evidence; include something new."
                if appeal_wr >= 0.30
                else "Appeal win rate is low — prioritize getting it right the first time."
            ),
        ))

    # ── Calibration updates ────────────────────────────────────────────────
    calibration_updates: dict[str, float] = {}
    for stat in ct_stats:
        if stat.sample_size >= 5:
            calibration_updates[f"claim_type:{stat.segment_value}"] = round(stat.win_rate, 3)
    for stat in ev_stats:
        if stat.sample_size >= 3:
            calibration_updates[f"evidence_quality:{stat.segment_value}"] = round(stat.win_rate, 3)

    # ── Settlement comparison ──────────────────────────────────────────────
    if settled:
        avg_settlement_pct = sum(
            (o.settlement_amount or 0) / max(o.dispute_amount, 1)
            for o in settled
        ) / len(settled)
        total_settled_amount = sum(o.settlement_amount or 0 for o in settled)
        total_lost_amount = sum(o.dispute_amount for o in losses)
        settled_vs_fought = (
            f"Settled {len(settled)} disputes at avg {avg_settlement_pct:.0%} of dispute amount "
            f"(${total_settled_amount:.0f} paid out). "
            f"Lost {len(losses)} fights (${total_lost_amount:.0f} lost). "
            + (
                "Settlement strategy saved money compared to fought-and-lost disputes."
                if avg_settlement_pct < (1 - overall_win_rate)
                else "Fighting produced better outcomes than settling for most cases."
            )
        )
    else:
        settled_vs_fought = "No settled disputes in this window — all disputes fought or accepted."

    summary = (
        f"{len(outcomes)} outcomes | "
        f"win rate: {overall_win_rate:.0%} ({len(wins)}W / {len(losses)}L / {len(settled)}S) | "
        f"{len(insights)} insights | "
        f"{len(calibration_updates)} calibration updates"
    )

    return WinLossReport(
        generated_date=analysis_date,
        total_outcomes=len(outcomes),
        total_wins=len(wins),
        total_losses=len(losses),
        total_settled=len(settled),
        overall_win_rate=round(overall_win_rate, 3),
        win_rate_by_claim_type=ct_stats,
        win_rate_by_evidence_quality=ev_stats,
        win_rate_by_response_timing=timing_stats,
        win_rate_by_carrier=carrier_stats,
        win_rate_by_buyer_risk_tier=risk_stats,
        insights=insights,
        calibration_updates=calibration_updates,
        settled_vs_fought_comparison=settled_vs_fought,
        summary=summary,
    )


def _win_rates_by_field(
    outcomes: list[DisputeOutcomeRecord],
    field_name: str,
    wins: list[DisputeOutcomeRecord],
) -> list[WinRateStat]:
    """Group outcomes by a field and compute win rates per group."""
    win_set = set(o.dispute_id for o in wins)
    groups: dict[str, list[DisputeOutcomeRecord]] = {}
    for o in outcomes:
        val = str(getattr(o, field_name, "UNKNOWN") or "UNKNOWN")
        groups.setdefault(val, []).append(o)

    stats: list[WinRateStat] = []
    for val, group in sorted(groups.items()):
        group_wins = [o for o in group if o.dispute_id in win_set]
        stat = _make_stat(field_name, val, group_wins, group)
        stats.append(stat)
    return stats


def _make_stat(
    segment_name: str,
    segment_value: str,
    wins: list[DisputeOutcomeRecord],
    total: list[DisputeOutcomeRecord],
) -> WinRateStat:
    n = len(total)
    w = len(wins)
    win_rate = w / n if n else 0.0
    avg_amount = sum(o.dispute_amount for o in total) / n if n else 0.0
    note = "small sample" if n < 5 else ("reliable" if n < 20 else "high confidence")
    return WinRateStat(
        segment_name=segment_name,
        segment_value=segment_value,
        wins=w,
        sample_size=n,
        win_rate=round(win_rate, 3),
        avg_dispute_amount=round(avg_amount, 2),
        sample_size_note=note,
    )
