"""
Buyer Behavior Profiler — analyze a buyer's transaction and dispute history
to classify their risk profile and inform dispute strategy decisions.

Given a buyer's purchase and dispute history, this oracle computes:
  - Dispute abuse risk score (0–100)
  - Risk classification: TRUSTED / NORMAL / ELEVATED / HIGH_RISK / FRAUDSTER
  - Behavioral pattern detection (serial returner, friendly fraud, etc.)
  - Recommended handling tier (standard / enhanced review / flag / block)
  - Per-dispute strategy modifier

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BuyerRiskClass(str, Enum):
    TRUSTED    = "TRUSTED"      # Long history, zero disputes, high spend
    NORMAL     = "NORMAL"       # Typical buyer, 1–2 disputes ever
    ELEVATED   = "ELEVATED"     # Above-average dispute rate
    HIGH_RISK  = "HIGH_RISK"    # Multiple disputes, pattern-matched
    FRAUDSTER  = "FRAUDSTER"    # Clear abuse pattern — block recommended


class BehaviorPattern(str, Enum):
    SERIAL_RETURNER       = "SERIAL_RETURNER"       # INR/SNAD repeat
    FRIENDLY_FRAUD        = "FRIENDLY_FRAUD"        # Keeps goods, disputes charge
    SUBSCRIPTION_ABUSER   = "SUBSCRIPTION_ABUSER"  # Disputes after consuming service
    UNAUTHORIZED_REPEAT   = "UNAUTHORIZED_REPEAT"  # Repeated "I didn't do it" claims
    AMOUNT_ESCALATOR      = "AMOUNT_ESCALATOR"      # Dispute amounts grow over time
    TIMING_PATTERN        = "TIMING_PATTERN"        # Always disputes near end of window
    CLEAN                 = "CLEAN"                 # No concerning patterns


class HandlingTier(str, Enum):
    STANDARD        = "STANDARD"         # Normal automated processing
    ENHANCED_REVIEW = "ENHANCED_REVIEW"  # Require manual review before action
    FLAG_ACCOUNT    = "FLAG_ACCOUNT"     # Tag for fraud team attention
    BLOCK_SALES     = "BLOCK_SALES"      # Refuse new orders from this buyer


@dataclass
class DisputeHistoryEntry:
    """One historical dispute from this buyer."""
    dispute_id: str
    reason_code: str
    amount: float
    outcome: str           # WON / LOST / ACCEPTED
    days_after_purchase: int   # How many days after purchase was it filed
    epoch: int


@dataclass
class BuyerProfileInput:
    """Input for buyer behavior profiling."""
    buyer_id: str

    # Purchase history
    total_orders: int
    total_spend: float
    account_age_days: int
    avg_order_value: float = 0.0

    # Dispute history
    dispute_history: list[DisputeHistoryEntry] = field(default_factory=list)
    total_disputes: int = 0

    # Current dispute context
    current_dispute_reason: str = ""
    current_dispute_amount: float = 0.0
    current_dispute_days_after_purchase: int = 0


@dataclass
class PatternSignal:
    """A detected behavioral pattern with confidence and evidence."""
    pattern: BehaviorPattern
    confidence: float          # 0–1
    evidence: str


@dataclass
class BuyerProfileReport:
    """Full buyer behavior profile."""
    buyer_id: str

    risk_score: int              # 0 = pristine, 100 = definite fraudster
    risk_class: BuyerRiskClass

    dispute_rate: float          # disputes / orders
    patterns_detected: list[PatternSignal]
    primary_pattern: BehaviorPattern

    handling_tier: HandlingTier
    win_rate_modifier: float     # Adjust dispute win prob by this amount
    strategy_note: str           # One-line guidance for this dispute

    trust_indicators: list[str]
    risk_indicators: list[str]

    summary: str


def _dispute_rate(total_orders: int, total_disputes: int) -> float:
    return total_disputes / max(total_orders, 1)


def _detect_patterns(inp: BuyerProfileInput) -> list[PatternSignal]:
    signals: list[PatternSignal] = []
    history = inp.dispute_history

    if not history:
        return [PatternSignal(BehaviorPattern.CLEAN, 1.0, "No prior disputes")]

    # Serial returner: 2+ INR or SNAD disputes
    inr_snad = [d for d in history if d.reason_code in ("INR", "SNAD", "13.1", "13.3", "4853")]
    if len(inr_snad) >= 2:
        signals.append(PatternSignal(
            BehaviorPattern.SERIAL_RETURNER,
            min(1.0, len(inr_snad) * 0.35),
            f"{len(inr_snad)} INR/SNAD disputes on record",
        ))

    # Friendly fraud: disputes after long delay (> 25 days) while buying again
    late_disputes = [d for d in history if d.days_after_purchase > 25]
    if len(late_disputes) >= 2:
        signals.append(PatternSignal(
            BehaviorPattern.FRIENDLY_FRAUD,
            min(1.0, len(late_disputes) * 0.40),
            f"{len(late_disputes)} disputes filed > 25 days after purchase",
        ))

    # Subscription abuser: 2+ subscription disputes
    sub_disputes = [d for d in history if d.reason_code in ("SUBSCRIPTION", "12.6")]
    if len(sub_disputes) >= 2:
        signals.append(PatternSignal(
            BehaviorPattern.SUBSCRIPTION_ABUSER,
            min(1.0, len(sub_disputes) * 0.45),
            f"{len(sub_disputes)} subscription disputes",
        ))

    # Unauthorized repeat: 2+ unauthorized disputes
    unauth = [d for d in history if d.reason_code in ("UNAUTHORIZED", "10.4", "4807")]
    if len(unauth) >= 2:
        signals.append(PatternSignal(
            BehaviorPattern.UNAUTHORIZED_REPEAT,
            min(1.0, len(unauth) * 0.50),
            f"{len(unauth)} unauthorized disputes — statistically suspicious",
        ))

    # Amount escalator: dispute amounts trending up
    if len(history) >= 3:
        amounts = [d.amount for d in sorted(history, key=lambda x: x.epoch)]
        if amounts[-1] > amounts[0] * 1.5 and amounts[-1] > amounts[-2]:
            signals.append(PatternSignal(
                BehaviorPattern.AMOUNT_ESCALATOR,
                0.60,
                f"Dispute amounts escalating: ${amounts[0]:.0f} → ${amounts[-1]:.0f}",
            ))

    # Timing pattern: always files near end of dispute window (25-30 days after purchase)
    end_window = [d for d in history if 25 <= d.days_after_purchase <= 30]
    if len(end_window) >= 2 and len(end_window) / max(len(history), 1) >= 0.60:
        signals.append(PatternSignal(
            BehaviorPattern.TIMING_PATTERN,
            0.70,
            f"{len(end_window)}/{len(history)} disputes filed in last 5 days of window",
        ))

    if not signals:
        signals.append(PatternSignal(BehaviorPattern.CLEAN, 1.0, "No concerning patterns found"))

    return sorted(signals, key=lambda s: s.confidence, reverse=True)


def profile_buyer(inp: BuyerProfileInput) -> BuyerProfileReport:
    """Profile buyer behavior and produce dispute strategy guidance."""
    dispute_rate = _dispute_rate(inp.total_orders, inp.total_disputes)
    patterns = _detect_patterns(inp)
    primary = patterns[0].pattern if patterns else BehaviorPattern.CLEAN

    # Base risk score
    score = 0

    # Dispute rate contribution (0–40 pts)
    if dispute_rate >= 0.20:
        score += 40
    elif dispute_rate >= 0.10:
        score += 30
    elif dispute_rate >= 0.05:
        score += 20
    elif dispute_rate >= 0.02:
        score += 10

    # Pattern contribution (0–40 pts)
    pattern_weights = {
        BehaviorPattern.UNAUTHORIZED_REPEAT:  35,
        BehaviorPattern.FRIENDLY_FRAUD:       30,
        BehaviorPattern.SUBSCRIPTION_ABUSER:  25,
        BehaviorPattern.AMOUNT_ESCALATOR:     25,
        BehaviorPattern.SERIAL_RETURNER:      20,
        BehaviorPattern.TIMING_PATTERN:       15,
        BehaviorPattern.CLEAN:                 0,
    }
    top_pattern_score = max(
        (int(sig.confidence * pattern_weights.get(sig.pattern, 0)) for sig in patterns),
        default=0,
    )
    score += min(40, top_pattern_score)

    # Raw dispute count contribution (0–20 pts)
    if inp.total_disputes >= 10:
        score += 20
    elif inp.total_disputes >= 5:
        score += 15
    elif inp.total_disputes >= 3:
        score += 10
    elif inp.total_disputes >= 2:
        score += 5

    score = max(0, min(100, score))

    # Trust indicators
    trust: list[str] = []
    risk: list[str] = []

    if inp.account_age_days >= 365:
        trust.append(f"Account {inp.account_age_days} days old — established buyer")
        score = max(0, score - 5)
    if inp.total_orders >= 20 and inp.total_disputes == 0:
        trust.append(f"{inp.total_orders} orders with zero disputes")
        score = max(0, score - 10)
    if inp.total_spend >= 1000:
        trust.append(f"${inp.total_spend:.0f} lifetime spend — high-value customer")
        score = max(0, score - 3)

    if dispute_rate >= 0.10:
        risk.append(f"Dispute rate {dispute_rate:.1%} — 10× normal")
    if inp.total_disputes >= 5:
        risk.append(f"{inp.total_disputes} lifetime disputes on record")
    if primary != BehaviorPattern.CLEAN:
        risk.append(f"Pattern detected: {primary.value.replace('_', ' ').title()}")

    # Classify
    if score >= 80:
        risk_class = BuyerRiskClass.FRAUDSTER
    elif score >= 60:
        risk_class = BuyerRiskClass.HIGH_RISK
    elif score >= 35:
        risk_class = BuyerRiskClass.ELEVATED
    elif score >= 10:
        risk_class = BuyerRiskClass.NORMAL
    else:
        risk_class = BuyerRiskClass.TRUSTED

    # Handling tier and win modifier
    tier_map = {
        BuyerRiskClass.TRUSTED:   (HandlingTier.STANDARD,        +0.05),
        BuyerRiskClass.NORMAL:    (HandlingTier.STANDARD,         0.00),
        BuyerRiskClass.ELEVATED:  (HandlingTier.ENHANCED_REVIEW, -0.05),
        BuyerRiskClass.HIGH_RISK: (HandlingTier.FLAG_ACCOUNT,    +0.08),  # Fight harder
        BuyerRiskClass.FRAUDSTER: (HandlingTier.BLOCK_SALES,     +0.12),  # Always fight
    }
    handling, win_mod = tier_map[risk_class]

    strategy_map = {
        BuyerRiskClass.TRUSTED:   "Standard processing — no adjustments needed",
        BuyerRiskClass.NORMAL:    "Review evidence normally before deciding strategy",
        BuyerRiskClass.ELEVATED:  "Flag for manual review; request more evidence from buyer",
        BuyerRiskClass.HIGH_RISK: "Fight disputes assertively; document all interactions",
        BuyerRiskClass.FRAUDSTER: "Fight every dispute; escalate to fraud team; block sales",
    }
    strategy_note = strategy_map[risk_class]

    summary = (
        f"Buyer {inp.buyer_id}: "
        f"score={score}/100 ({risk_class.value}) | "
        f"dispute_rate={dispute_rate:.2%} | "
        f"pattern={primary.value} | tier={handling.value}"
    )

    return BuyerProfileReport(
        buyer_id=inp.buyer_id,
        risk_score=score,
        risk_class=risk_class,
        dispute_rate=round(dispute_rate, 6),
        patterns_detected=patterns,
        primary_pattern=primary,
        handling_tier=handling,
        win_rate_modifier=win_mod,
        strategy_note=strategy_note,
        trust_indicators=trust,
        risk_indicators=risk,
        summary=summary,
    )
