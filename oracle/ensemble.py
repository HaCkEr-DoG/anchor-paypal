"""
Multi-Signal Ensemble Scorer — fuses every Anchor signal into one composite win probability.

The Settlement Oracle alone gives a good estimate. With the full signal stack:
  - Oracle base probability (evidence-adjusted)
  - Buyer risk profile boost (fraud detector)
  - Adversarial pattern penalties (adversarial detector)
  - Historical calibration adjustments (oracle feedback)
  - Carrier timing signals (friendly fraud detection)

...we get a materially more accurate estimate that PayPal's AI cannot match,
because it only sees the dispute side. We see the carrier side, the buyer's
network-wide history, and the adversarial patterns.

Output: EnsembleDecision with a final composite win probability and a full
reasoning trace showing how each signal contributed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from oracle.decision import (
    OracleDecision,
    Recommendation,
    ClaimType,
    calculate as oracle_calculate,
    Evidence,
)
from oracle.adversarial import AdversarialReport, analyze as adversarial_analyze
from oracle.paypal_rubric import RubricScore


@dataclass
class SignalContribution:
    source: str
    raw_value: float       # The signal's own value (e.g. base prob, penalty amount)
    contribution: float    # How much it shifted the final probability
    description: str


@dataclass
class EnsembleDecision:
    """
    Final decision after fusing all available signals.

    Fields
    ------
    composite_win_probability : final blended win probability (0.0–0.95)
    oracle_win_probability    : what the Oracle alone said
    recommendation            : FIGHT | ACCEPT | NEGOTIATE based on composite
    ev_fight                  : expected value using composite probability
    confidence                : confidence in the composite estimate
    signal_contributions      : how each signal shifted the probability
    adversarial_flags         : detected adversarial patterns (if any)
    counter_strategy          : recommended PayPal response strategy
    reasoning                 : full human-readable trace
    """
    composite_win_probability: float
    oracle_win_probability: float
    recommendation: Recommendation
    ev_fight: float
    ev_accept: float = 0.0
    confidence: float = 0.0
    signal_contributions: list[SignalContribution] = field(default_factory=list)
    adversarial_flags: list[str] = field(default_factory=list)
    counter_strategy: str = ""
    reasoning: list[str] = field(default_factory=list)
    computed_at: str = ""

    def __post_init__(self):
        if not self.computed_at:
            self.computed_at = datetime.now(timezone.utc).isoformat()


def score(
    oracle_decision: OracleDecision,
    dispute_amount: float,
    claim_type: ClaimType,
    *,
    # Buyer risk signals
    buyer_risk_level: str = "low",          # from fraud_detector
    buyer_disputes_reversed_90d: int = 0,
    is_fraud_network_suspect: bool = False,
    oracle_win_prob_boost: float = 0.0,     # pre-computed from fraud_detector.win_probability_boost
    # Adversarial signals
    adversarial_report: Optional[AdversarialReport] = None,
    # PayPal rubric alignment
    rubric_score: Optional[RubricScore] = None,  # from oracle.paypal_rubric.score_rubric
    # Carrier timing
    carrier_timing_risk: str = "",          # from carrier.delivery_timing_risk
    # Historical calibration
    calibration_adjustment: float = 0.0,   # from oracle.feedback.recalibration_signal
    # Fee tier
    high_volume_tier: bool = False,
    dispute_fee: float = 15.0,
    seller_hourly_rate: float = 50.0,
    hours_to_fight: float = 1.5,
) -> EnsembleDecision:
    """
    Fuse all signals into a composite win probability and final decision.

    The ensemble uses an additive model (not multiplicative) to prevent any
    single signal from dominating. Each signal has a capped contribution so
    that extreme single signals don't produce absurd final probabilities.

    Signal contributions (additive, clamped per signal):
      Oracle base probability        : anchor point
      Buyer risk boost               : +0.0 to +0.25
      Adversarial penalties          : variable, typically -0.05 to -0.40
      Carrier timing signal          : -0.10 to +0.08
      Historical calibration         : ±0.15 max
    """
    contributions: list[SignalContribution] = []
    reasoning: list[str] = []

    # ── 1. Oracle anchor ────────────────────────────────────────────────────
    p = oracle_decision.win_probability
    contributions.append(SignalContribution(
        source="oracle",
        raw_value=p,
        contribution=0.0,  # anchor, no shift
        description=f"Oracle base probability: {p:.0%}",
    ))
    reasoning.append(f"Oracle: {p:.0%} win probability (EV=${oracle_decision.ev_fight:.2f})")

    # ── 2. Buyer risk boost ─────────────────────────────────────────────────
    boost = min(oracle_win_prob_boost, 0.25)  # cap at 25%
    if boost > 0:
        p += boost
        contributions.append(SignalContribution(
            source="buyer_risk_profiler",
            raw_value=oracle_win_prob_boost,
            contribution=boost,
            description=(
                f"Buyer risk boost +{boost:.0%}: "
                + ("fraud network suspect" if is_fraud_network_suspect else f"{buyer_risk_level} risk")
                + (f" with {buyer_disputes_reversed_90d} reversed dispute(s)" if buyer_disputes_reversed_90d else "")
            ),
        ))
        reasoning.append(
            f"Buyer risk boost: +{boost:.0%} "
            f"({'network suspect' if is_fraud_network_suspect else buyer_risk_level})"
        )

    # ── 3. Adversarial signal adjustment ───────────────────────────────────
    if adversarial_report is not None:
        adj = adversarial_report.total_adjustment
        adj_clamped = max(-0.40, min(0.20, adj))  # floor -40%, ceiling +20%
        if abs(adj_clamped) > 0.001:
            p += adj_clamped
            flag_names = [s.name for s in adversarial_report.signals if s.penalty < 0]
            contributions.append(SignalContribution(
                source="adversarial_detector",
                raw_value=adj,
                contribution=adj_clamped,
                description=f"Adversarial adjustment {adj_clamped:+.0%}: {', '.join(flag_names[:3])}",
            ))
            reasoning.append(
                f"Adversarial signals: {adj_clamped:+.0%} "
                f"({len(adversarial_report.signals)} signal(s), "
                f"{'FRAUD PATTERN' if adversarial_report.is_fraud_pattern else 'monitoring'})"
            )
            for sig in adversarial_report.signals:
                if sig.severity in ("high", "critical"):
                    reasoning.append(f"  ↳ {sig.severity.upper()}: {sig.description}")

    # ── 3.5. PayPal rubric alignment ────────────────────────────────────────
    if rubric_score is not None:
        rubric_adj = max(-0.15, min(0.25, rubric_score.win_probability_boost))
        if abs(rubric_adj) > 0.001:
            p += rubric_adj
            sp_status = rubric_score.seller_protection.status.value
            contributions.append(SignalContribution(
                source="paypal_rubric",
                raw_value=rubric_score.score,
                contribution=rubric_adj,
                description=(
                    f"PayPal rubric alignment {rubric_score.score:.0%}: "
                    f"SP={sp_status}, boost={rubric_adj:+.0%}"
                ),
            ))
            reasoning.append(
                f"PayPal rubric: {rubric_score.score:.0%} alignment "
                f"(SP: {sp_status}) → {rubric_adj:+.0%}"
            )
            if rubric_score.missing_critical:
                reasoning.append(
                    f"  ↳ Missing critical evidence: {', '.join(rubric_score.missing_critical)}"
                )

    # ── 4. Carrier timing signal ────────────────────────────────────────────
    if carrier_timing_risk:
        timing_shift = _carrier_timing_shift(carrier_timing_risk)
        if abs(timing_shift) > 0.001:
            p += timing_shift
            contributions.append(SignalContribution(
                source="carrier_timing",
                raw_value=timing_shift,
                contribution=timing_shift,
                description=f"Carrier timing: {timing_shift:+.0%} — {carrier_timing_risk[:60]}",
            ))
            reasoning.append(f"Carrier timing: {timing_shift:+.0%}")

    # ── 5. Historical calibration ───────────────────────────────────────────
    cal_adj = max(-0.15, min(0.15, calibration_adjustment))
    if abs(cal_adj) > 0.001:
        p += cal_adj
        contributions.append(SignalContribution(
            source="oracle_calibration",
            raw_value=calibration_adjustment,
            contribution=cal_adj,
            description=f"Calibration correction: {cal_adj:+.0%}",
        ))
        reasoning.append(f"Oracle calibration correction: {cal_adj:+.0%}")

    # ── Clamp final probability ─────────────────────────────────────────────
    p_final = max(0.02, min(0.95, p))

    # ── Re-compute EV with composite probability ────────────────────────────
    time_cost = seller_hourly_rate * hours_to_fight
    if high_volume_tier:
        effective_cost = time_cost + dispute_fee
    else:
        effective_cost = time_cost + (1 - p_final) * dispute_fee
    ev_fight = round(p_final * dispute_amount - effective_cost, 2)

    # ── Decision ────────────────────────────────────────────────────────────
    if high_volume_tier:
        break_even = (time_cost + dispute_fee) / max(dispute_amount, 0.01)
    else:
        break_even = (time_cost + dispute_fee) / max(dispute_amount + dispute_fee, 0.01)

    if ev_fight > 0 and p_final >= break_even:
        recommendation = Recommendation.FIGHT
    elif p_final >= 0.35 and dispute_amount >= 30:
        recommendation = Recommendation.FIGHT if ev_fight > 0 else Recommendation.ACCEPT
    else:
        recommendation = Recommendation.ACCEPT

    confidence = min(abs(p_final - break_even) / 0.3, 1.0)

    reasoning.append(
        f"Composite: {p_final:.0%} win prob | EV=${ev_fight} | "
        f"Break-even={break_even:.0%} | Recommendation: {recommendation.value}"
    )

    adversarial_flags = (
        [s.name for s in adversarial_report.signals if s.severity in ("high", "critical")]
        if adversarial_report else []
    )
    counter_strategy = adversarial_report.counter_strategy if adversarial_report else ""

    return EnsembleDecision(
        composite_win_probability=round(p_final, 3),
        oracle_win_probability=oracle_decision.win_probability,
        recommendation=recommendation,
        ev_fight=ev_fight,
        ev_accept=0.0,
        confidence=round(confidence, 3),
        signal_contributions=contributions,
        adversarial_flags=adversarial_flags,
        counter_strategy=counter_strategy,
        reasoning=reasoning,
    )


def _carrier_timing_shift(timing_risk: str) -> float:
    """Convert carrier timing risk string to a probability adjustment."""
    timing_lower = timing_risk.lower()
    if "suspicious" in timing_lower or "friendly fraud" in timing_lower:
        return +0.18  # strong boost — delivery-confirmed + rapid dispute = fraud
    if "1." in timing_lower or "2." in timing_lower or "3." in timing_lower:
        # e.g. "1.5h after" → moderate boost
        return +0.10
    if "days after" in timing_lower or "day after" in timing_lower:
        return +0.06
    if "in transit" in timing_lower:
        return -0.05  # item not yet delivered — slightly weakens merchant position
    return 0.0
