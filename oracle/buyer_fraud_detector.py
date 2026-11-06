"""
Buyer Fraud Signal Detector — assess whether a dispute shows signs of
buyer-initiated fraud or abuse.

Buyer fraud patterns in PayPal disputes fall into several categories:
  1. Serial disputer    — buyer files disputes on a large fraction of orders
  2. Friendly fraud     — buyer claims non-delivery but address matches delivery
  3. Item-flip fraud    — buyer returns counterfeit/damaged item instead of original
  4. Triangulation      — buyer receives goods paid for by stolen card
  5. Chargeback abuse   — buyer files card chargeback after receiving item
  6. Timing anomaly     — dispute filed immediately after delivery (no good-faith attempt)

This module scores these signals individually and produces a composite
fraud risk score. The score feeds into the EV model by adjusting the
effective win probability upward (fraud signals = stronger case to fight).

Signal weights:
  SERIAL_DISPUTER        — highest weight (most reliable predictor)
  IMMEDIATE_DISPUTE      — high weight (no resolution attempt)
  ADDRESS_MISMATCH       — high weight (delivery confirms receipt)
  RAPID_ESCALATION       — medium weight
  INCONSISTENT_STORY     — medium weight
  HIGH_VALUE_PATTERN     — medium weight
  NEW_ACCOUNT            — low weight (new ≠ fraud, but correlated)
  MULTIPLE_NETWORKS      — low weight

Score bands:
  HIGH_RISK   ≥ 70 — Strong fraud signals; fight the dispute
  MEDIUM_RISK 40–69 — Mixed signals; fight with strong evidence
  LOW_RISK    < 40  — Likely legitimate; consider accommodation

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FraudRiskLevel(str, Enum):
    HIGH_RISK   = "HIGH_RISK"
    MEDIUM_RISK = "MEDIUM_RISK"
    LOW_RISK    = "LOW_RISK"


class FraudSignalType(str, Enum):
    SERIAL_DISPUTER        = "SERIAL_DISPUTER"
    IMMEDIATE_DISPUTE      = "IMMEDIATE_DISPUTE"
    ADDRESS_MISMATCH       = "ADDRESS_MISMATCH"
    RAPID_ESCALATION       = "RAPID_ESCALATION"
    INCONSISTENT_STORY     = "INCONSISTENT_STORY"
    HIGH_VALUE_PATTERN     = "HIGH_VALUE_PATTERN"
    NEW_ACCOUNT            = "NEW_ACCOUNT"
    MULTIPLE_NETWORKS      = "MULTIPLE_NETWORKS"
    PRIOR_FRAUD_FLAG       = "PRIOR_FRAUD_FLAG"
    DELIVERY_CONFIRMED     = "DELIVERY_CONFIRMED"   # Anti-fraud (reduces score)
    LONG_CUSTOMER          = "LONG_CUSTOMER"        # Anti-fraud


@dataclass
class FraudSignal:
    """One fraud signal present or absent."""
    signal_type: FraudSignalType
    is_present: bool
    weight: float            # 0.0–1.0, contribution when present
    score_contribution: float  # Actual contribution (can be negative for anti-fraud)
    description: str


@dataclass
class BuyerFraudInput:
    """Input for buyer fraud detection."""
    dispute_id: str
    buyer_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / etc.
    dispute_amount: float

    # Buyer history
    total_lifetime_orders: int
    total_lifetime_disputes: int
    months_as_buyer: int          # Account age
    has_prior_fraud_flag: bool = False

    # This dispute signals
    hours_until_dispute_filed: int = 72    # Time from delivery to dispute
    has_delivery_confirmation: bool = False
    delivery_address_matches_account: bool = True
    buyer_story_changed: bool = False      # Did buyer change their account of events?
    escalated_within_24h: bool = False     # Escalated without giving merchant time

    # Pattern signals
    dispute_rate_last_12m: float = 0.0    # Fraction of orders disputed in last year
    orders_above_threshold: int = 0       # Orders ≥ $200 ever disputed
    networks_used: int = 1                # Number of different payment networks used

    # Context
    is_first_order_with_merchant: bool = False


_WEIGHTS: dict[FraudSignalType, float] = {
    FraudSignalType.SERIAL_DISPUTER:    0.25,
    FraudSignalType.IMMEDIATE_DISPUTE:  0.20,
    FraudSignalType.ADDRESS_MISMATCH:   0.20,
    FraudSignalType.RAPID_ESCALATION:   0.10,
    FraudSignalType.INCONSISTENT_STORY: 0.10,
    FraudSignalType.HIGH_VALUE_PATTERN: 0.07,
    FraudSignalType.PRIOR_FRAUD_FLAG:   0.15,
    FraudSignalType.NEW_ACCOUNT:        0.05,
    FraudSignalType.MULTIPLE_NETWORKS:  0.03,
    FraudSignalType.DELIVERY_CONFIRMED: 0.20,   # Fraud signal: delivery confirmed + INR claim = lying
    FraudSignalType.LONG_CUSTOMER:      -0.10,  # Anti-fraud: long history = less likely
}


def _build_signals(inp: BuyerFraudInput) -> list[FraudSignal]:
    signals: list[FraudSignal] = []

    # SERIAL_DISPUTER — ≥ 15% lifetime dispute rate OR ≥ 25% last 12m
    lifetime_rate = (inp.total_lifetime_disputes / inp.total_lifetime_orders
                     if inp.total_lifetime_orders > 0 else 0.0)
    is_serial = lifetime_rate >= 0.15 or inp.dispute_rate_last_12m >= 0.25
    w = _WEIGHTS[FraudSignalType.SERIAL_DISPUTER]
    signals.append(FraudSignal(
        signal_type=FraudSignalType.SERIAL_DISPUTER,
        is_present=is_serial,
        weight=w,
        score_contribution=w * 100 if is_serial else 0.0,
        description=(f"Lifetime dispute rate {lifetime_rate:.0%} "
                     f"(last 12m: {inp.dispute_rate_last_12m:.0%})"),
    ))

    # IMMEDIATE_DISPUTE — filed within 24h of delivery
    is_immediate = inp.hours_until_dispute_filed <= 24
    w = _WEIGHTS[FraudSignalType.IMMEDIATE_DISPUTE]
    signals.append(FraudSignal(
        signal_type=FraudSignalType.IMMEDIATE_DISPUTE,
        is_present=is_immediate,
        weight=w,
        score_contribution=w * 100 if is_immediate else 0.0,
        description=f"Dispute filed {inp.hours_until_dispute_filed}h after delivery",
    ))

    # ADDRESS_MISMATCH — delivery address does not match account
    w = _WEIGHTS[FraudSignalType.ADDRESS_MISMATCH]
    is_mismatch = not inp.delivery_address_matches_account
    signals.append(FraudSignal(
        signal_type=FraudSignalType.ADDRESS_MISMATCH,
        is_present=is_mismatch,
        weight=w,
        score_contribution=w * 100 if is_mismatch else 0.0,
        description="Delivery address does not match PayPal account address",
    ))

    # RAPID_ESCALATION — escalated within 24h without giving merchant time
    w = _WEIGHTS[FraudSignalType.RAPID_ESCALATION]
    signals.append(FraudSignal(
        signal_type=FraudSignalType.RAPID_ESCALATION,
        is_present=inp.escalated_within_24h,
        weight=w,
        score_contribution=w * 100 if inp.escalated_within_24h else 0.0,
        description="Buyer escalated to PayPal within 24h without awaiting merchant response",
    ))

    # INCONSISTENT_STORY
    w = _WEIGHTS[FraudSignalType.INCONSISTENT_STORY]
    signals.append(FraudSignal(
        signal_type=FraudSignalType.INCONSISTENT_STORY,
        is_present=inp.buyer_story_changed,
        weight=w,
        score_contribution=w * 100 if inp.buyer_story_changed else 0.0,
        description="Buyer changed account of events during dispute",
    ))

    # HIGH_VALUE_PATTERN — repeated high-value disputes
    w = _WEIGHTS[FraudSignalType.HIGH_VALUE_PATTERN]
    is_hv_pattern = inp.orders_above_threshold >= 2
    signals.append(FraudSignal(
        signal_type=FraudSignalType.HIGH_VALUE_PATTERN,
        is_present=is_hv_pattern,
        weight=w,
        score_contribution=w * 100 if is_hv_pattern else 0.0,
        description=f"{inp.orders_above_threshold} prior high-value disputes",
    ))

    # PRIOR_FRAUD_FLAG
    w = _WEIGHTS[FraudSignalType.PRIOR_FRAUD_FLAG]
    signals.append(FraudSignal(
        signal_type=FraudSignalType.PRIOR_FRAUD_FLAG,
        is_present=inp.has_prior_fraud_flag,
        weight=w,
        score_contribution=w * 100 if inp.has_prior_fraud_flag else 0.0,
        description="Buyer has prior PayPal fraud flag on account",
    ))

    # NEW_ACCOUNT
    w = _WEIGHTS[FraudSignalType.NEW_ACCOUNT]
    is_new = inp.months_as_buyer <= 3
    signals.append(FraudSignal(
        signal_type=FraudSignalType.NEW_ACCOUNT,
        is_present=is_new,
        weight=w,
        score_contribution=w * 100 if is_new else 0.0,
        description=f"Account age: {inp.months_as_buyer} months",
    ))

    # MULTIPLE_NETWORKS
    w = _WEIGHTS[FraudSignalType.MULTIPLE_NETWORKS]
    is_multi = inp.networks_used >= 3
    signals.append(FraudSignal(
        signal_type=FraudSignalType.MULTIPLE_NETWORKS,
        is_present=is_multi,
        weight=w,
        score_contribution=w * 100 if is_multi else 0.0,
        description=f"{inp.networks_used} different payment networks used",
    ))

    # DELIVERY_CONFIRMED (anti-fraud: if claiming INR but delivery confirmed)
    w = _WEIGHTS[FraudSignalType.DELIVERY_CONFIRMED]
    is_anti = (inp.has_delivery_confirmation
               and inp.claim_type.upper() == "INR")
    signals.append(FraudSignal(
        signal_type=FraudSignalType.DELIVERY_CONFIRMED,
        is_present=is_anti,
        weight=abs(w),
        score_contribution=w * 100 if is_anti else 0.0,  # Negative contribution
        description="Delivery confirmed + INR claim → strong fraud indicator",
    ))

    # LONG_CUSTOMER (anti-fraud: loyal buyers are less likely to defraud)
    w = _WEIGHTS[FraudSignalType.LONG_CUSTOMER]
    is_loyal = inp.months_as_buyer >= 24 and lifetime_rate < 0.05
    signals.append(FraudSignal(
        signal_type=FraudSignalType.LONG_CUSTOMER,
        is_present=is_loyal,
        weight=abs(w),
        score_contribution=w * 100 if is_loyal else 0.0,  # Negative contribution
        description=f"Long-standing customer ({inp.months_as_buyer}mo) with low dispute rate",
    ))

    return signals


@dataclass
class BuyerFraudReport:
    """Buyer fraud risk assessment."""
    dispute_id: str
    buyer_id: str

    fraud_score: float              # 0–100 (higher = more suspicious)
    risk_level: FraudRiskLevel

    signals: list[FraudSignal]
    active_signals: int             # Count of positive fraud signals
    anti_fraud_signals: int         # Count of exonerating signals

    # Win probability adjustment
    base_p_win: float = 0.0
    adjusted_p_win: float = 0.0    # Increased if fraud signals present
    p_win_boost: float = 0.0

    rationale: str = ""
    recommendation: str = ""
    summary: str = ""


def detect_buyer_fraud(inp: BuyerFraudInput,
                       base_p_win: float = 0.55) -> BuyerFraudReport:
    """
    Detect buyer fraud signals and produce a risk-adjusted win probability.
    """
    signals = _build_signals(inp)

    raw_score = sum(s.score_contribution for s in signals)
    fraud_score = round(max(0.0, min(100.0, raw_score)), 1)

    if fraud_score >= 70:
        risk_level = FraudRiskLevel.HIGH_RISK
    elif fraud_score >= 40:
        risk_level = FraudRiskLevel.MEDIUM_RISK
    else:
        risk_level = FraudRiskLevel.LOW_RISK

    active = sum(1 for s in signals
                 if s.is_present and s.score_contribution > 0)
    anti   = sum(1 for s in signals
                 if s.is_present and s.score_contribution < 0)

    # Adjust win probability: HIGH_RISK → +15pp, MEDIUM_RISK → +7pp, LOW_RISK → 0
    boost = {
        FraudRiskLevel.HIGH_RISK:   0.15,
        FraudRiskLevel.MEDIUM_RISK: 0.07,
        FraudRiskLevel.LOW_RISK:    0.00,
    }[risk_level]
    adjusted_p = round(min(0.97, base_p_win + boost), 4)

    if risk_level == FraudRiskLevel.HIGH_RISK:
        recommendation = "Fight the dispute — strong fraud signals detected"
        rationale = (f"Fraud score {fraud_score:.0f}/100; "
                     f"{active} positive signal(s) detected")
    elif risk_level == FraudRiskLevel.MEDIUM_RISK:
        recommendation = "Fight with strong evidence — mixed signals"
        rationale = (f"Fraud score {fraud_score:.0f}/100; "
                     f"mixed signals ({active} fraud, {anti} exonerating)")
    else:
        recommendation = "Consider accommodation — low fraud risk"
        rationale = f"Fraud score {fraud_score:.0f}/100; likely legitimate dispute"

    summary = (
        f"Fraud {inp.dispute_id} [{inp.buyer_id}]: "
        f"score={fraud_score:.0f} | risk={risk_level.value} | "
        f"signals={active} | anti={anti} | "
        f"p_win_base={base_p_win:.0%} → adj={adjusted_p:.0%}"
    )

    return BuyerFraudReport(
        dispute_id=inp.dispute_id,
        buyer_id=inp.buyer_id,
        fraud_score=fraud_score,
        risk_level=risk_level,
        signals=signals,
        active_signals=active,
        anti_fraud_signals=anti,
        base_p_win=base_p_win,
        adjusted_p_win=adjusted_p,
        p_win_boost=round(boost, 4),
        rationale=rationale,
        recommendation=recommendation,
        summary=summary,
    )
