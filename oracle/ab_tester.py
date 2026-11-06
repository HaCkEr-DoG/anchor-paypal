"""
A/B Tester — framework for measuring dispute strategy win-rate lift.

The oracle modules recommend strategies, but which actually work for a given merchant?
The A/B tester tracks parallel experiments on dispute response strategies and
computes statistically significant win-rate differences between them.

Use cases:
  - Is adding signature confirmation to INR responses worth the carrier cost?
  - Does submitting early (day 3) vs. late (day 20) affect win rate?
  - Which evidence template wins more SNAD disputes?
  - Does buyer dispute prediction reduce dispute rate by X%?

Design:
  - Experiments are defined with a control (A) and one or more treatments (B, C, ...)
  - Disputes are assigned to arms by a deterministic hash of dispute_id
  - Outcomes are recorded as WIN / LOSS / PENDING
  - Statistical significance tested via two-proportion z-test
  - Effect size computed as absolute and relative win rate lift

This is a pure oracle module — no I/O, no persistence.
Persistence is handled by the Anchor scheduler that calls these functions.

Output:
  - ExperimentResult with winner (or "insufficient data"), effect size, p-value
  - RecommendationFromExperiment: when to adopt the winner system-wide
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeOutcome(str, Enum):
    WIN     = "WIN"
    LOSS    = "LOSS"
    PENDING = "PENDING"


class ExperimentStatus(str, Enum):
    RUNNING         = "RUNNING"
    SIGNIFICANT     = "SIGNIFICANT"      # p < 0.05, can decide
    INCONCLUSIVE    = "INCONCLUSIVE"     # Insufficient data
    COMPLETE        = "COMPLETE"         # Manually closed


class ArmLabel(str, Enum):
    CONTROL   = "CONTROL"
    TREATMENT = "TREATMENT"


@dataclass
class ExperimentArm:
    """One arm (strategy variant) in an A/B experiment."""
    name: str
    label: ArmLabel
    description: str
    wins: int = 0
    losses: int = 0

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.wins / self.total

    @property
    def has_enough_data(self) -> bool:
        return self.total >= 20


@dataclass
class DisputeAssignment:
    """Assignment of one dispute to an experiment arm."""
    dispute_id: str
    experiment_id: str
    arm_name: str
    assigned_at: Optional[str] = None  # ISO date string


@dataclass
class ExperimentResult:
    """
    Statistical analysis of one A/B experiment.
    """
    experiment_id: str
    status: ExperimentStatus

    # Arms
    control: ExperimentArm
    treatment: ExperimentArm

    # Statistics
    absolute_lift: float          # Treatment WR - Control WR (e.g. 0.08 = +8pp)
    relative_lift: float          # (Treatment WR - Control WR) / Control WR
    p_value: float                # Two-proportion z-test
    is_significant: bool          # p < 0.05
    confidence_level: float       # 1 - p_value, capped at 0.999

    # Decision
    winner: Optional[str]         # Arm name, or None if inconclusive
    recommendation: str
    minimum_detectable_effect: float  # Smallest lift detectable at current sample size

    summary: str


@dataclass
class Experiment:
    """
    An A/B experiment definition and its accumulated results.
    Pure data — no I/O.
    """
    experiment_id: str
    name: str
    description: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / ALL
    metric: str                   # "win_rate" (only metric supported currently)

    control_arm: ExperimentArm
    treatment_arm: ExperimentArm

    status: ExperimentStatus = ExperimentStatus.RUNNING
    min_sample_per_arm: int = 30  # Minimum disputes per arm before analysis
    significance_threshold: float = 0.05


def assign_dispute_to_arm(
    dispute_id: str,
    experiment_id: str,
    treatment_pct: float = 0.50,
) -> ArmLabel:
    """
    Deterministically assign a dispute to control or treatment.
    Uses SHA-256 hash of dispute_id + experiment_id for consistent assignment.
    treatment_pct: fraction of disputes going to treatment (default 50/50).
    """
    key = f"{experiment_id}:{dispute_id}"
    digest = hashlib.sha256(key.encode()).digest()
    # Use first 4 bytes as a uniform float in [0, 1)
    value = int.from_bytes(digest[:4], "big") / (2**32)
    return ArmLabel.TREATMENT if value < treatment_pct else ArmLabel.CONTROL


def record_outcome(
    experiment: Experiment,
    dispute_id: str,
    outcome: DisputeOutcome,
    treatment_pct: float = 0.50,
) -> Experiment:
    """
    Record a dispute outcome into the appropriate arm of an experiment.
    Returns a new Experiment with updated counts (immutable-style update).
    """
    if outcome == DisputeOutcome.PENDING:
        return experiment  # Not yet resolved

    arm_label = assign_dispute_to_arm(dispute_id, experiment.experiment_id, treatment_pct)
    is_win = outcome == DisputeOutcome.WIN

    if arm_label == ArmLabel.CONTROL:
        new_control = ExperimentArm(
            name=experiment.control_arm.name,
            label=ArmLabel.CONTROL,
            description=experiment.control_arm.description,
            wins=experiment.control_arm.wins + (1 if is_win else 0),
            losses=experiment.control_arm.losses + (0 if is_win else 1),
        )
        new_treatment = experiment.treatment_arm
    else:
        new_control = experiment.control_arm
        new_treatment = ExperimentArm(
            name=experiment.treatment_arm.name,
            label=ArmLabel.TREATMENT,
            description=experiment.treatment_arm.description,
            wins=experiment.treatment_arm.wins + (1 if is_win else 0),
            losses=experiment.treatment_arm.losses + (0 if is_win else 1),
        )

    return Experiment(
        experiment_id=experiment.experiment_id,
        name=experiment.name,
        description=experiment.description,
        claim_type=experiment.claim_type,
        metric=experiment.metric,
        control_arm=new_control,
        treatment_arm=new_treatment,
        status=experiment.status,
        min_sample_per_arm=experiment.min_sample_per_arm,
        significance_threshold=experiment.significance_threshold,
    )


def _two_prop_z_test(n1: int, p1: float, n2: int, p2: float) -> float:
    """
    Two-proportion z-test. Returns p-value (two-tailed).
    Returns 1.0 if insufficient data.
    """
    if n1 < 2 or n2 < 2:
        return 1.0
    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    if p_pool <= 0 or p_pool >= 1:
        return 1.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    if se == 0:
        return 1.0
    z = abs(p1 - p2) / se
    # Approximate p-value from z using error function
    p_value = 2 * (1 - _normal_cdf(z))
    return round(max(0.001, min(1.0, p_value)), 4)


def _normal_cdf(z: float) -> float:
    """Approximation of Φ(z) using Abramowitz & Stegun 7.1.26."""
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    phi = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z**2) * poly
    return phi if z >= 0 else 1.0 - phi


def _minimum_detectable_effect(n: int, baseline_rate: float, power: float = 0.80) -> float:
    """
    Minimum detectable effect (absolute) at given sample size and baseline rate.
    Approximation using z-scores for α=0.05 two-tailed, 80% power.
    """
    if n < 2:
        return 1.0
    z_alpha = 1.96   # α=0.05 two-tailed
    z_beta = 0.842   # 80% power
    p = baseline_rate
    mde = (z_alpha + z_beta) * math.sqrt(2 * p * (1 - p) / n)
    return round(min(1.0, mde), 4)


def analyze_experiment(experiment: Experiment) -> ExperimentResult:
    """
    Run statistical analysis on the experiment's current data.
    Returns ExperimentResult with significance test and winner recommendation.
    """
    ctrl = experiment.control_arm
    trt = experiment.treatment_arm

    # Check sample size
    min_n = experiment.min_sample_per_arm
    has_data = ctrl.total >= min_n and trt.total >= min_n

    absolute_lift = round(trt.win_rate - ctrl.win_rate, 4)
    relative_lift = round(
        (trt.win_rate - ctrl.win_rate) / ctrl.win_rate if ctrl.win_rate > 0 else 0.0,
        4
    )

    p_value = _two_prop_z_test(ctrl.total, ctrl.win_rate, trt.total, trt.win_rate)
    is_significant = p_value < experiment.significance_threshold
    confidence_level = round(min(0.999, 1 - p_value), 3)

    mde = _minimum_detectable_effect(
        min(ctrl.total, trt.total) if has_data else min_n,
        ctrl.win_rate if ctrl.win_rate > 0 else 0.65,
    )

    # Determine status
    if not has_data:
        status = ExperimentStatus.INCONCLUSIVE
        winner = None
        rec = (
            f"Insufficient data: {ctrl.total}/{min_n} control, {trt.total}/{min_n} treatment disputes resolved. "
            f"Continue running experiment."
        )
    elif is_significant:
        status = ExperimentStatus.SIGNIFICANT
        if absolute_lift > 0:
            winner = trt.name
            rec = (
                f"ADOPT {trt.name}: +{absolute_lift:.1%} absolute lift ({relative_lift:+.0%} relative) "
                f"at {confidence_level:.0%} confidence (p={p_value:.3f}). "
                f"Roll out to 100% of {experiment.claim_type} disputes."
            )
        elif absolute_lift < 0:
            winner = ctrl.name
            rec = (
                f"KEEP {ctrl.name}: treatment WORSE by {abs(absolute_lift):.1%} at {confidence_level:.0%} confidence. "
                f"Stop treatment arm immediately."
            )
        else:
            winner = None
            rec = "No difference detected — strategies equivalent at this confidence level."
    else:
        status = ExperimentStatus.INCONCLUSIVE
        winner = None
        if abs(absolute_lift) < mde:
            rec = (
                f"Results trend toward {trt.name if absolute_lift > 0 else ctrl.name} "
                f"but effect ({absolute_lift:+.1%}) is below minimum detectable effect ({mde:.1%}). "
                f"Need more data — keep running."
            )
        else:
            needed = round(min_n * 1.5)
            rec = (
                f"Promising lift of {absolute_lift:+.1%} but not yet significant (p={p_value:.3f}). "
                f"Target ~{needed} resolved disputes per arm for significance."
            )

    summary = (
        f"{experiment.name}: {ctrl.name} WR={ctrl.win_rate:.0%} (n={ctrl.total}) vs "
        f"{trt.name} WR={trt.win_rate:.0%} (n={trt.total}) | "
        f"lift={absolute_lift:+.1%} | p={p_value:.3f} | {status.value}"
    )

    return ExperimentResult(
        experiment_id=experiment.experiment_id,
        status=status,
        control=ctrl,
        treatment=trt,
        absolute_lift=absolute_lift,
        relative_lift=relative_lift,
        p_value=p_value,
        is_significant=is_significant,
        confidence_level=confidence_level,
        winner=winner,
        recommendation=rec,
        minimum_detectable_effect=mde,
        summary=summary,
    )


def create_experiment(
    experiment_id: str,
    name: str,
    description: str,
    claim_type: str,
    control_name: str,
    control_description: str,
    treatment_name: str,
    treatment_description: str,
    min_sample_per_arm: int = 30,
) -> Experiment:
    """Factory for creating a new experiment."""
    return Experiment(
        experiment_id=experiment_id,
        name=name,
        description=description,
        claim_type=claim_type,
        metric="win_rate",
        control_arm=ExperimentArm(control_name, ArmLabel.CONTROL, control_description),
        treatment_arm=ExperimentArm(treatment_name, ArmLabel.TREATMENT, treatment_description),
        min_sample_per_arm=min_sample_per_arm,
    )
