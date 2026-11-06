"""
Master Claim Scorer — the crown jewel of Anchor's oracle stack.

Every other module produces one signal. This module integrates ALL of them
into a single, explainable fight/accept/escalate verdict with confidence score.

What it replaces:
  - A human reviewing 10 different signals inconsistently
  - Rule-based systems that miss signal interactions
  - Single-signal decisions that ignore 90% of available information

The scoring architecture:
  1. Gather signals from all available oracle modules (lazy — only computes what's available)
  2. Weight signals by reliability and claim type
  3. Compute composite fight_score (0–100)
  4. Apply threshold logic with confidence bands
  5. Generate explainable verdict with per-signal attribution

Thresholds:
  fight_score >= 75  → FIGHT (high confidence)
  fight_score >= 55  → FIGHT (moderate confidence, review recommended)
  fight_score >= 40  → ESCALATE (borderline — escalate before fighting)
  fight_score < 40   → ACCEPT (insufficient evidence to fight profitably)

The critical insight competitors miss: a 65% base win rate with strong network
intelligence (serial fraudster) is a BETTER fight than an 80% base win rate
with zero supporting signals. This module captures that interaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FightVerdict(str, Enum):
    FIGHT_HIGH_CONFIDENCE   = "FIGHT_HIGH_CONFIDENCE"    # Score >= 75
    FIGHT_MODERATE          = "FIGHT_MODERATE"           # Score 55–75
    ESCALATE_FIRST          = "ESCALATE_FIRST"           # Score 40–55 — get more before fighting
    ACCEPT                  = "ACCEPT"                   # Score < 40
    INSUFFICIENT_DATA       = "INSUFFICIENT_DATA"        # Can't score — missing critical inputs


class SignalWeight(float, Enum):
    """Relative importance of each signal in the composite score."""
    BASE_WIN_RATE        = 35.0   # Calibrated win rate from historical data
    EVIDENCE_QUALITY     = 20.0   # Evidence package completeness
    NETWORK_INTEL        = 15.0   # Cross-merchant buyer signals
    DISPUTE_AMOUNT       = 10.0   # EV anchor: bigger amount = more worth fighting
    SELLER_PROTECTION    = 10.0   # SP coverage certainty
    CARRIER_TRUST        = 5.0    # Carrier reliability for INR claims
    TIMELINE             = 3.0    # Whether response timing is optimal
    LEGAL_ESCALATION     = 2.0    # Availability of legal backstop


@dataclass
class SignalContribution:
    """One signal's contribution to the composite score."""
    signal_name: str
    raw_value: float            # The actual signal value (0–1 or 0–100 depending on signal)
    contribution: float         # How many points this signal adds to the 0-100 score
    weight: float               # Weight used in computation
    description: str            # Human-readable explanation


@dataclass
class ClaimScore:
    """
    Composite fight/accept scoring for one dispute.
    Integrates all available oracle signals.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float

    # Composite result
    fight_score: float                  # 0–100
    fight_verdict: FightVerdict
    confidence: float                   # 0–1 confidence in the verdict

    # Signal breakdown
    signal_contributions: list[SignalContribution]
    missing_signals: list[str]          # Signals not available for this dispute

    # EV calculation
    estimated_ev: float                 # Expected value of fighting: P(win) * amount - cost
    base_win_probability: float         # Win probability before signal adjustments
    adjusted_win_probability: float     # Win probability after all signal adjustments
    fight_cost_estimate: float          # Time and resource cost of fighting

    # Action guidance
    primary_action: str
    priority_evidence: list[str]        # What to gather first
    reasoning: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DisputeSignals:
    """
    Container for all oracle module outputs relevant to one dispute.
    Pass whatever you have — missing signals are handled gracefully.
    """
    # Required
    claim_type: str
    dispute_amount: float
    # From calibrator
    calibrated_win_rate: Optional[float] = None          # 0–1
    calibration_confidence: Optional[float] = None       # 0–1
    # From evidence builder
    evidence_completeness: Optional[float] = None        # 0–1
    evidence_quality_tier: Optional[str] = None          # "STRONG" / "MODERATE" / "WEAK"
    # From network intelligence
    network_risk_score: Optional[float] = None           # 0–100
    network_win_probability_adjustment: Optional[float] = None  # additive
    network_risk_level: Optional[str] = None             # "CRITICAL" / "HIGH" / etc.
    # From seller protection
    sp_covered: Optional[bool] = None
    sp_score: Optional[float] = None                     # 0–100
    sp_has_blocking_issues: Optional[bool] = None
    # From carrier intel (INR claims)
    carrier_trust_level: Optional[str] = None            # "PLATINUM" / "GOLD" / etc.
    carrier_inr_win_rate: Optional[float] = None         # 0–1
    # From timeline optimizer
    response_in_optimal_window: Optional[bool] = None
    days_remaining: Optional[int] = None
    # From legal escalation
    legal_escalation_available: Optional[bool] = None
    legal_escalation_ev: Optional[float] = None
    # From dispute DNA (pattern match)
    dna_pattern_win_rate: Optional[float] = None
    dna_match_confidence: Optional[float] = None
    # From order risk
    order_risk_score: Optional[float] = None             # 0–1 (higher = riskier order)
    # From buyer intelligence
    buyer_risk_tier: Optional[str] = None                # "TIER_1_NORMAL" / "TIER_2_ELEVATED" etc.


# Base win rates by claim type (before calibration)
_BASE_WIN_RATES: dict[str, float] = {
    "ITEM_NOT_RECEIVED":              0.68,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": 0.52,
    "UNAUTHORIZED":                   0.45,
    "BILLING_ISSUE":                  0.60,
    "DUPLICATE_CHARGE":               0.72,
}

# Fight cost estimates (time + processing) by claim type
_FIGHT_COSTS: dict[str, float] = {
    "ITEM_NOT_RECEIVED":              8.0,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": 15.0,
    "UNAUTHORIZED":                   12.0,
    "BILLING_ISSUE":                  6.0,
    "DUPLICATE_CHARGE":               5.0,
}

# Trust level to win rate multiplier
_CARRIER_TRUST_MULTIPLIERS: dict[str, float] = {
    "PLATINUM": 1.15,
    "GOLD":     1.05,
    "SILVER":   1.00,
    "BRONZE":   0.85,
    "RISKY":    0.70,
}

# Network risk to win rate adjustment
_NETWORK_RISK_ADJUSTMENTS: dict[str, float] = {
    "CRITICAL":  +0.20,
    "HIGH":      +0.12,
    "ELEVATED":  +0.06,
    "NORMAL":     0.00,
    "TRUSTED":   -0.10,
}


def score_claim(
    dispute_id: str,
    signals: DisputeSignals,
) -> ClaimScore:
    """
    Compute composite fight/accept score integrating all available signals.

    Returns a fully explainable ClaimScore with per-signal attribution
    and actionable guidance.
    """
    reasoning: list[str] = []
    warnings: list[str] = []
    contributions: list[SignalContribution] = []
    missing: list[str] = []

    claim_type = signals.claim_type
    amount = signals.dispute_amount

    # ── 1. Base win rate (calibrated if available) ─────────────────────────
    base_rate = _BASE_WIN_RATES.get(claim_type, 0.60)
    if signals.calibrated_win_rate is not None:
        base_rate = signals.calibrated_win_rate
        reasoning.append(f"Calibrated win rate: {base_rate:.0%} (merchant-specific history)")
    else:
        missing.append("calibrated_win_rate")
        reasoning.append(f"Base win rate: {base_rate:.0%} (industry prior — no merchant history)")

    base_contribution = (base_rate / 1.0) * SignalWeight.BASE_WIN_RATE.value
    contributions.append(SignalContribution(
        signal_name="base_win_rate",
        raw_value=base_rate,
        contribution=base_contribution,
        weight=SignalWeight.BASE_WIN_RATE.value,
        description=f"{'Calibrated' if signals.calibrated_win_rate else 'Industry'} win rate: {base_rate:.0%}",
    ))

    # Accumulate win probability adjustments
    win_prob_adjustments: float = 0.0

    # ── 2. Evidence quality ────────────────────────────────────────────────
    if signals.evidence_completeness is not None:
        ev_score = signals.evidence_completeness
        ev_contribution = ev_score * SignalWeight.EVIDENCE_QUALITY.value
        tier = signals.evidence_quality_tier or "?"
        contributions.append(SignalContribution(
            signal_name="evidence_quality",
            raw_value=ev_score,
            contribution=ev_contribution,
            weight=SignalWeight.EVIDENCE_QUALITY.value,
            description=f"Evidence package: {tier} ({ev_score:.0%} complete)",
        ))
        # Strong evidence boosts win prob
        if ev_score >= 0.80:
            win_prob_adjustments += 0.08
        elif ev_score < 0.40:
            win_prob_adjustments -= 0.10
            warnings.append("Evidence package is weak — gather more before fighting")
        reasoning.append(f"Evidence completeness: {ev_score:.0%}")
    else:
        missing.append("evidence_completeness")
        # Neutral assumption
        contributions.append(SignalContribution(
            signal_name="evidence_quality",
            raw_value=0.5,
            contribution=SignalWeight.EVIDENCE_QUALITY.value * 0.5,
            weight=SignalWeight.EVIDENCE_QUALITY.value,
            description="Evidence quality: unknown (assumed 50%)",
        ))

    # ── 3. Network intelligence ────────────────────────────────────────────
    if signals.network_risk_level is not None:
        network_adjustment = _NETWORK_RISK_ADJUSTMENTS.get(signals.network_risk_level, 0.0)
        win_prob_adjustments += network_adjustment
        # Score contribution from network: normalize risk score to 0-1
        net_score = (signals.network_risk_score or 0) / 100.0
        if signals.network_risk_level in ("CRITICAL", "HIGH", "ELEVATED"):
            net_contribution = net_score * SignalWeight.NETWORK_INTEL.value
        else:
            # Trusted buyers reduce our score
            net_contribution = (1 - net_score) * SignalWeight.NETWORK_INTEL.value * 0.3
        contributions.append(SignalContribution(
            signal_name="network_intel",
            raw_value=signals.network_risk_score or 0,
            contribution=net_contribution,
            weight=SignalWeight.NETWORK_INTEL.value,
            description=f"Network: {signals.network_risk_level} (score={signals.network_risk_score:.0f}/100)",
        ))
        reasoning.append(
            f"Network signal: {signals.network_risk_level} → win_prob {network_adjustment:+.0%}"
        )
    else:
        missing.append("network_intel")
        contributions.append(SignalContribution(
            signal_name="network_intel",
            raw_value=0,
            contribution=0,
            weight=SignalWeight.NETWORK_INTEL.value,
            description="Network intel: not available",
        ))

    # ── 4. Dispute amount (EV anchor) ──────────────────────────────────────
    # Higher amounts are more worth fighting (pure EV calculation)
    amount_normalized = min(amount / 1000.0, 1.0)  # Cap at $1000
    amount_contribution = amount_normalized * SignalWeight.DISPUTE_AMOUNT.value
    contributions.append(SignalContribution(
        signal_name="dispute_amount",
        raw_value=amount,
        contribution=amount_contribution,
        weight=SignalWeight.DISPUTE_AMOUNT.value,
        description=f"Dispute amount: ${amount:.0f}",
    ))
    if amount < 25:
        warnings.append(f"Dispute amount ${amount:.0f} may not justify fighting cost")

    # ── 5. Seller Protection coverage ─────────────────────────────────────
    if signals.sp_covered is not None:
        if signals.sp_covered and not signals.sp_has_blocking_issues:
            sp_norm = (signals.sp_score or 80) / 100.0
            sp_contribution = sp_norm * SignalWeight.SELLER_PROTECTION.value
            win_prob_adjustments += 0.10
            reasoning.append("Seller Protection coverage confirmed — significant win rate boost")
        elif signals.sp_has_blocking_issues:
            sp_contribution = 0.2 * SignalWeight.SELLER_PROTECTION.value
            win_prob_adjustments -= 0.05
            warnings.append("Seller Protection has blocking issues — coverage uncertain")
        else:
            # SNAD or not covered
            sp_contribution = 0.0
            warnings.append("No Seller Protection coverage for this claim type")
        contributions.append(SignalContribution(
            signal_name="seller_protection",
            raw_value=signals.sp_score or 0,
            contribution=sp_contribution,
            weight=SignalWeight.SELLER_PROTECTION.value,
            description=(
                f"Seller Protection: {'covered' if signals.sp_covered else 'not covered'}"
                + (f" (SP score={signals.sp_score:.0f})" if signals.sp_score else "")
            ),
        ))
    else:
        missing.append("seller_protection")
        contributions.append(SignalContribution(
            signal_name="seller_protection",
            raw_value=0,
            contribution=0,
            weight=SignalWeight.SELLER_PROTECTION.value,
            description="Seller Protection: not assessed",
        ))

    # ── 6. Carrier trust (INR only) ────────────────────────────────────────
    if claim_type == "ITEM_NOT_RECEIVED" and signals.carrier_trust_level is not None:
        multiplier = _CARRIER_TRUST_MULTIPLIERS.get(signals.carrier_trust_level, 1.0)
        carrier_norm = (multiplier - 0.70) / (1.15 - 0.70)  # normalize 0.70–1.15 to 0–1
        carrier_contribution = carrier_norm * SignalWeight.CARRIER_TRUST.value
        win_prob_adjustments += (multiplier - 1.0) * 0.5  # small adjustment
        contributions.append(SignalContribution(
            signal_name="carrier_trust",
            raw_value=multiplier,
            contribution=carrier_contribution,
            weight=SignalWeight.CARRIER_TRUST.value,
            description=f"Carrier trust: {signals.carrier_trust_level} ({signals.carrier_inr_win_rate:.0%} INR win rate)" if signals.carrier_inr_win_rate else f"Carrier: {signals.carrier_trust_level}",
        ))
        if signals.carrier_trust_level in ("BRONZE", "RISKY"):
            warnings.append(f"Carrier is {signals.carrier_trust_level} — consider upgrade for future orders")
    else:
        if claim_type == "ITEM_NOT_RECEIVED":
            missing.append("carrier_intel")
        contributions.append(SignalContribution(
            signal_name="carrier_trust",
            raw_value=0,
            contribution=0,
            weight=SignalWeight.CARRIER_TRUST.value,
            description="Carrier intel: not applicable" if claim_type != "ITEM_NOT_RECEIVED" else "Carrier intel: not available",
        ))

    # ── 7. Timeline ────────────────────────────────────────────────────────
    if signals.response_in_optimal_window is not None:
        if signals.response_in_optimal_window:
            timeline_contribution = SignalWeight.TIMELINE.value
            reasoning.append("Response timing is OPTIMAL — in algorithmic sweet spot")
        else:
            timeline_contribution = SignalWeight.TIMELINE.value * 0.3
        contributions.append(SignalContribution(
            signal_name="timeline",
            raw_value=1.0 if signals.response_in_optimal_window else 0.3,
            contribution=timeline_contribution,
            weight=SignalWeight.TIMELINE.value,
            description=f"Response timing: {'OPTIMAL' if signals.response_in_optimal_window else 'suboptimal'}"
            + (f" ({signals.days_remaining}d remaining)" if signals.days_remaining is not None else ""),
        ))
        if signals.days_remaining is not None and signals.days_remaining <= 1:
            warnings.append("CRITICAL: < 24 hours remaining in response window")
    else:
        missing.append("timeline")
        contributions.append(SignalContribution(
            signal_name="timeline",
            raw_value=0.5,
            contribution=SignalWeight.TIMELINE.value * 0.5,
            weight=SignalWeight.TIMELINE.value,
            description="Timeline: not assessed",
        ))

    # ── 8. Legal escalation backstop ──────────────────────────────────────
    if signals.legal_escalation_available is not None:
        if signals.legal_escalation_available and (signals.legal_escalation_ev or 0) > 0:
            legal_contribution = SignalWeight.LEGAL_ESCALATION.value
            reasoning.append(
                f"Legal escalation available as backstop (EV=${signals.legal_escalation_ev:.0f})"
            )
        else:
            legal_contribution = 0.0
        contributions.append(SignalContribution(
            signal_name="legal_escalation",
            raw_value=1.0 if signals.legal_escalation_available else 0,
            contribution=legal_contribution,
            weight=SignalWeight.LEGAL_ESCALATION.value,
            description=f"Legal backstop: {'available' if signals.legal_escalation_available else 'not applicable'}",
        ))
    else:
        missing.append("legal_escalation")
        contributions.append(SignalContribution(
            signal_name="legal_escalation",
            raw_value=0,
            contribution=0,
            weight=SignalWeight.LEGAL_ESCALATION.value,
            description="Legal escalation: not assessed",
        ))

    # ── Composite score ────────────────────────────────────────────────────
    fight_score = sum(c.contribution for c in contributions)
    fight_score = max(0.0, min(100.0, fight_score))

    # ── Adjusted win probability ───────────────────────────────────────────
    adjusted_win_prob = max(0.0, min(1.0, base_rate + win_prob_adjustments))
    fight_cost = _FIGHT_COSTS.get(claim_type, 10.0)
    estimated_ev = adjusted_win_prob * amount - fight_cost

    # ── Verdict ────────────────────────────────────────────────────────────
    if len(missing) >= 5 and fight_score < 30:
        verdict = FightVerdict.INSUFFICIENT_DATA
        confidence = 0.30
    elif fight_score >= 75:
        verdict = FightVerdict.FIGHT_HIGH_CONFIDENCE
        confidence = min(0.95, 0.70 + (fight_score - 75) / 100)
    elif fight_score >= 55:
        verdict = FightVerdict.FIGHT_MODERATE
        confidence = 0.60 + (fight_score - 55) / 100
    elif fight_score >= 40:
        verdict = FightVerdict.ESCALATE_FIRST
        confidence = 0.50
    else:
        verdict = FightVerdict.ACCEPT
        confidence = max(0.50, 0.90 - fight_score / 80)

    # ── Priority evidence ──────────────────────────────────────────────────
    priority_evidence: list[str] = []
    if signals.evidence_completeness is not None and signals.evidence_completeness < 0.70:
        priority_evidence.append("Gather stronger evidence before submitting")
    if "carrier_intel" in missing and claim_type == "ITEM_NOT_RECEIVED":
        priority_evidence.append("Add carrier name and tracking number")
    if "network_intel" in missing:
        priority_evidence.append("Run network intelligence check on buyer")
    if "seller_protection" in missing:
        priority_evidence.append("Assess Seller Protection coverage for this order")

    # ── Primary action ─────────────────────────────────────────────────────
    if verdict == FightVerdict.FIGHT_HIGH_CONFIDENCE:
        primary_action = f"FIGHT — submit evidence package now (EV=${estimated_ev:.0f})"
    elif verdict == FightVerdict.FIGHT_MODERATE:
        primary_action = f"FIGHT — moderate confidence (EV=${estimated_ev:.0f}), review evidence first"
    elif verdict == FightVerdict.ESCALATE_FIRST:
        primary_action = "ESCALATE — gather more signals before committing to fight/accept"
    elif verdict == FightVerdict.INSUFFICIENT_DATA:
        primary_action = "GATHER DATA — missing critical signals; cannot score reliably"
    else:
        primary_action = f"ACCEPT — EV=${estimated_ev:.0f} does not justify fighting"

    reasoning.append(
        f"Fight score: {fight_score:.0f}/100 → {verdict.value} (confidence={confidence:.0%})"
    )

    return ClaimScore(
        dispute_id=dispute_id,
        claim_type=claim_type,
        dispute_amount=amount,
        fight_score=round(fight_score, 1),
        fight_verdict=verdict,
        confidence=round(confidence, 3),
        signal_contributions=contributions,
        missing_signals=missing,
        estimated_ev=round(estimated_ev, 2),
        base_win_probability=round(base_rate, 3),
        adjusted_win_probability=round(adjusted_win_prob, 3),
        fight_cost_estimate=fight_cost,
        primary_action=primary_action,
        priority_evidence=priority_evidence,
        reasoning=reasoning,
        warnings=warnings,
    )


def score_summary(score: ClaimScore) -> str:
    """One-line summary for dashboards and logs."""
    return (
        f"[{score.fight_verdict.value}] {score.dispute_id} ${score.dispute_amount:.0f} "
        f"| score={score.fight_score:.0f}/100 | "
        f"win={score.adjusted_win_probability:.0%} | "
        f"EV=${score.estimated_ev:.0f} | "
        f"conf={score.confidence:.0%}"
    )
