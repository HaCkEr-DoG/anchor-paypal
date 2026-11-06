"""
Dispute Escalation Predictor — estimate probability that a dispute escalates
from PayPal-managed to card-network chargeback, and recommend proactive steps.

Escalation path:
  PayPal dispute → escalated to PayPal claim → card network chargeback

Escalation raises stakes significantly:
  - Card network chargeback adds $20–$30 fee (often not covered by SP)
  - Chargeback count raises HDR rate → risk of program restrictions
  - Network chargebacks have stricter evidence requirements
  - Reversal timeline is longer (45 days vs. 10 days)

Escalation risk factors:
  - Buyer explicitly threatens chargeback (highest signal)
  - Communication tone deteriorating (hostile buyer)
  - Long open duration without resolution (frustration)
  - High dispute amount (more motivated to escalate)
  - Prior escalation history (pattern buyer)
  - No merchant response to PayPal dispute (triggers auto-escalation)
  - Buyer escalated within 24h (impatient)
  - Claim type: UNAUTHORIZED > INR > SNAD (escalation propensity)

Deescalation factors:
  - Merchant responded quickly (< 2 days)
  - Resolution offered (partial refund, replacement)
  - Buyer expressed satisfaction in messages
  - Delivery confirmed (reduces buyer's argument)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EscalationRisk(str, Enum):
    CRITICAL  = "CRITICAL"   # > 70% — intervene immediately
    HIGH      = "HIGH"       # 50–70% — act within 24h
    MODERATE  = "MODERATE"   # 30–49% — monitor closely
    LOW       = "LOW"        # < 30%  — standard handling


class EscalationDriver(str, Enum):
    CHARGEBACK_THREAT      = "CHARGEBACK_THREAT"
    HOSTILE_COMMUNICATION  = "HOSTILE_COMMUNICATION"
    LONG_OPEN_DURATION     = "LONG_OPEN_DURATION"
    HIGH_AMOUNT            = "HIGH_AMOUNT"
    PRIOR_ESCALATIONS      = "PRIOR_ESCALATIONS"
    NO_MERCHANT_RESPONSE   = "NO_MERCHANT_RESPONSE"
    RAPID_ESCALATION       = "RAPID_ESCALATION"
    UNAUTHORIZED_CLAIM     = "UNAUTHORIZED_CLAIM"
    BUYER_FRUSTRATION      = "BUYER_FRUSTRATION"


class EscalationMitigation(str, Enum):
    QUICK_RESPONSE         = "QUICK_RESPONSE"
    RESOLUTION_OFFERED     = "RESOLUTION_OFFERED"
    BUYER_SATISFIED        = "BUYER_SATISFIED"
    DELIVERY_CONFIRMED     = "DELIVERY_CONFIRMED"
    PARTIAL_RESOLUTION     = "PARTIAL_RESOLUTION"


@dataclass
class EscalationFactor:
    """One factor affecting escalation probability."""
    factor_type: str             # driver or mitigation
    is_active: bool
    probability_delta: float     # signed adjustment to base probability
    description: str


@dataclass
class DisputeEscalationInput:
    """Input for escalation probability estimation."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / DUPLICATE
    dispute_amount: float
    days_open: int                # How long the dispute has been open

    # Communication signals
    buyer_threatened_chargeback: bool = False
    buyer_tone_hostile: bool = False
    buyer_expressed_frustration: bool = False
    buyer_satisfied_with_progress: bool = False

    # Merchant response signals
    merchant_responded: bool = True
    days_until_merchant_responded: int = 1   # Days from open to first response
    resolution_offered: bool = False
    resolution_type: str = "none"            # "refund" / "partial" / "replacement" / "none"

    # History signals
    buyer_prior_escalations: int = 0
    buyer_escalated_within_24h: bool = False

    # Evidence signals
    has_delivery_confirmation: bool = False

    # Context
    base_escalation_probability: float = 0.15  # Platform baseline


_DRIVER_WEIGHTS: dict[EscalationDriver, float] = {
    EscalationDriver.CHARGEBACK_THREAT:     0.35,
    EscalationDriver.HOSTILE_COMMUNICATION: 0.15,
    EscalationDriver.LONG_OPEN_DURATION:    0.10,
    EscalationDriver.HIGH_AMOUNT:           0.10,
    EscalationDriver.PRIOR_ESCALATIONS:     0.12,
    EscalationDriver.NO_MERCHANT_RESPONSE:  0.20,
    EscalationDriver.RAPID_ESCALATION:      0.08,
    EscalationDriver.UNAUTHORIZED_CLAIM:    0.10,
    EscalationDriver.BUYER_FRUSTRATION:     0.08,
}

_MITIGATION_WEIGHTS: dict[EscalationMitigation, float] = {
    EscalationMitigation.QUICK_RESPONSE:     -0.12,
    EscalationMitigation.RESOLUTION_OFFERED: -0.18,
    EscalationMitigation.BUYER_SATISFIED:    -0.20,
    EscalationMitigation.DELIVERY_CONFIRMED: -0.08,
    EscalationMitigation.PARTIAL_RESOLUTION: -0.10,
}


def _build_factors(inp: DisputeEscalationInput) -> list[EscalationFactor]:
    factors: list[EscalationFactor] = []

    # Drivers
    w = _DRIVER_WEIGHTS[EscalationDriver.CHARGEBACK_THREAT]
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.CHARGEBACK_THREAT.value,
        is_active=inp.buyer_threatened_chargeback,
        probability_delta=w if inp.buyer_threatened_chargeback else 0.0,
        description="Buyer explicitly threatened to file a chargeback",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.HOSTILE_COMMUNICATION]
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.HOSTILE_COMMUNICATION.value,
        is_active=inp.buyer_tone_hostile,
        probability_delta=w if inp.buyer_tone_hostile else 0.0,
        description="Buyer communication tone is hostile or adversarial",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.LONG_OPEN_DURATION]
    is_long = inp.days_open >= 7
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.LONG_OPEN_DURATION.value,
        is_active=is_long,
        probability_delta=w if is_long else 0.0,
        description=f"Dispute open {inp.days_open} days (≥ 7 day threshold)",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.HIGH_AMOUNT]
    is_high = inp.dispute_amount >= 200.0
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.HIGH_AMOUNT.value,
        is_active=is_high,
        probability_delta=w if is_high else 0.0,
        description=f"High-value dispute (${inp.dispute_amount:.2f} ≥ $200)",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.PRIOR_ESCALATIONS]
    has_prior = inp.buyer_prior_escalations >= 1
    delta = min(w, inp.buyer_prior_escalations * 0.05) if has_prior else 0.0
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.PRIOR_ESCALATIONS.value,
        is_active=has_prior,
        probability_delta=delta,
        description=f"Buyer has {inp.buyer_prior_escalations} prior escalation(s)",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.NO_MERCHANT_RESPONSE]
    no_response = not inp.merchant_responded
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.NO_MERCHANT_RESPONSE.value,
        is_active=no_response,
        probability_delta=w if no_response else 0.0,
        description="Merchant has not responded to the dispute",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.RAPID_ESCALATION]
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.RAPID_ESCALATION.value,
        is_active=inp.buyer_escalated_within_24h,
        probability_delta=w if inp.buyer_escalated_within_24h else 0.0,
        description="Buyer escalated to PayPal within 24h without awaiting merchant",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.UNAUTHORIZED_CLAIM]
    is_unauthorized = inp.claim_type.upper() == "UNAUTHORIZED"
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.UNAUTHORIZED_CLAIM.value,
        is_active=is_unauthorized,
        probability_delta=w if is_unauthorized else 0.0,
        description="UNAUTHORIZED claims have highest card-network escalation propensity",
    ))

    w = _DRIVER_WEIGHTS[EscalationDriver.BUYER_FRUSTRATION]
    factors.append(EscalationFactor(
        factor_type=EscalationDriver.BUYER_FRUSTRATION.value,
        is_active=inp.buyer_expressed_frustration,
        probability_delta=w if inp.buyer_expressed_frustration else 0.0,
        description="Buyer has expressed strong frustration",
    ))

    # Mitigations
    w = _MITIGATION_WEIGHTS[EscalationMitigation.QUICK_RESPONSE]
    quick = inp.merchant_responded and inp.days_until_merchant_responded <= 1
    factors.append(EscalationFactor(
        factor_type=EscalationMitigation.QUICK_RESPONSE.value,
        is_active=quick,
        probability_delta=w if quick else 0.0,
        description="Merchant responded within 1 day (reduces escalation risk)",
    ))

    w = _MITIGATION_WEIGHTS[EscalationMitigation.RESOLUTION_OFFERED]
    resolution_full = inp.resolution_offered and inp.resolution_type in ("refund", "replacement")
    factors.append(EscalationFactor(
        factor_type=EscalationMitigation.RESOLUTION_OFFERED.value,
        is_active=resolution_full,
        probability_delta=w if resolution_full else 0.0,
        description="Full resolution (refund/replacement) offered to buyer",
    ))

    w = _MITIGATION_WEIGHTS[EscalationMitigation.BUYER_SATISFIED]
    factors.append(EscalationFactor(
        factor_type=EscalationMitigation.BUYER_SATISFIED.value,
        is_active=inp.buyer_satisfied_with_progress,
        probability_delta=w if inp.buyer_satisfied_with_progress else 0.0,
        description="Buyer expressed satisfaction with resolution progress",
    ))

    w = _MITIGATION_WEIGHTS[EscalationMitigation.DELIVERY_CONFIRMED]
    factors.append(EscalationFactor(
        factor_type=EscalationMitigation.DELIVERY_CONFIRMED.value,
        is_active=inp.has_delivery_confirmation,
        probability_delta=w if inp.has_delivery_confirmation else 0.0,
        description="Delivery confirmed — weakens buyer's escalation argument",
    ))

    w = _MITIGATION_WEIGHTS[EscalationMitigation.PARTIAL_RESOLUTION]
    partial = inp.resolution_offered and inp.resolution_type == "partial"
    factors.append(EscalationFactor(
        factor_type=EscalationMitigation.PARTIAL_RESOLUTION.value,
        is_active=partial,
        probability_delta=w if partial else 0.0,
        description="Partial resolution offered — reduces but does not eliminate risk",
    ))

    return factors


@dataclass
class EscalationPrediction:
    """Escalation probability prediction and guidance."""
    dispute_id: str

    # Core prediction
    escalation_probability: float    # 0.0–1.0
    risk_level: EscalationRisk

    # Factor breakdown
    factors: list[EscalationFactor]
    active_drivers: int
    active_mitigations: int

    # Guidance
    recommended_action: str
    action_deadline_hours: int        # Hours to act before escalation likely
    intervention_points: list[str]    # Specific steps to reduce escalation

    # Fee exposure
    additional_fee_if_escalated: float  # Incremental fee from chargeback
    expected_additional_loss: float     # Prob × fee exposure

    summary: str


def predict_escalation(inp: DisputeEscalationInput) -> EscalationPrediction:
    """
    Predict probability of dispute escalating to card-network chargeback.
    """
    factors = _build_factors(inp)

    total_delta = sum(f.probability_delta for f in factors)
    raw_prob = inp.base_escalation_probability + total_delta
    prob = round(max(0.02, min(0.97, raw_prob)), 4)

    if prob > 0.70:
        risk = EscalationRisk.CRITICAL
    elif prob > 0.50:
        risk = EscalationRisk.HIGH
    elif prob > 0.30:
        risk = EscalationRisk.MODERATE
    else:
        risk = EscalationRisk.LOW

    active_drivers = sum(1 for f in factors if f.is_active and f.probability_delta > 0)
    active_mits    = sum(1 for f in factors if f.is_active and f.probability_delta < 0)

    # Action deadline based on risk level
    deadline_map = {
        EscalationRisk.CRITICAL: 4,
        EscalationRisk.HIGH:     12,
        EscalationRisk.MODERATE: 24,
        EscalationRisk.LOW:      72,
    }
    deadline_hours = deadline_map[risk]

    # Build intervention points
    interventions: list[str] = []
    if not inp.merchant_responded:
        interventions.append("Respond to dispute immediately — no-response triggers auto-escalation")
    if not inp.resolution_offered and prob > 0.30:
        interventions.append("Offer proactive resolution (refund or replacement) to defuse escalation")
    if inp.buyer_threatened_chargeback:
        interventions.append("Acknowledge chargeback threat; emphasize PayPal dispute resolution path")
    if inp.buyer_tone_hostile:
        interventions.append("De-escalate communication tone; use professional, empathetic language")
    if inp.days_open >= 7:
        interventions.append("Provide status update — long open disputes increase frustration")
    if not interventions:
        interventions.append("Maintain current communication cadence; monitor for tone changes")

    # Fee exposure
    cb_fee = 20.0  # Base chargeback fee
    hdr_risk_fee = 10.0  # Approximate HDR impact per escalated dispute
    additional_fee = round(cb_fee + hdr_risk_fee, 2)
    expected_loss = round(prob * additional_fee, 2)

    if risk == EscalationRisk.CRITICAL:
        action = "IMMEDIATE INTERVENTION — offer full resolution within 4 hours"
    elif risk == EscalationRisk.HIGH:
        action = "URGENT — contact buyer and offer resolution within 12 hours"
    elif risk == EscalationRisk.MODERATE:
        action = "PROACTIVE — provide update and resolution offer within 24 hours"
    else:
        action = "STANDARD — continue normal dispute workflow"

    summary = (
        f"Escalation {inp.dispute_id}: "
        f"p={prob:.0%} | risk={risk.value} | "
        f"drivers={active_drivers} | mitigations={active_mits} | "
        f"action_by={deadline_hours}h | exp_fee=${expected_loss:.2f}"
    )

    return EscalationPrediction(
        dispute_id=inp.dispute_id,
        escalation_probability=prob,
        risk_level=risk,
        factors=factors,
        active_drivers=active_drivers,
        active_mitigations=active_mits,
        recommended_action=action,
        action_deadline_hours=deadline_hours,
        intervention_points=interventions,
        additional_fee_if_escalated=additional_fee,
        expected_additional_loss=expected_loss,
        summary=summary,
    )
