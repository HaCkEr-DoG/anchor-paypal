"""
Buyer Pattern Analyzer — detect repeat-dispute and serial-claim behavior.

Given a buyer's dispute history, this oracle computes a risk score and
classifies the buyer's pattern so merchants can calibrate response strategy
and PayPal can flag accounts for enhanced review.

Signals analyzed:
  - dispute_rate:         disputes / total_transactions
  - win_rate:             merchant wins / total_disputes
  - avg_claim_amount:     mean disputed amount
  - escalation_rate:      escalated disputes / total_disputes
  - category_diversity:   distinct dispute reasons / total_disputes
  - recency_weight:       disputes in last 30 days weighted 2×

Risk score: weighted sum of normalized signals, clamped to [0, 1].

PayPal benefit:
  - Early identification of serial claimants reduces platform abuse
  - Accurate risk scores help PayPal's Resolution Center triage workload
  - Merchant risk signal feeds into smarter dispute routing
  - Shared pattern data (anonymized) strengthens buyer-side fraud models
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

_WEIGHT_DISPUTE_RATE  = 0.30
_WEIGHT_WIN_RATE      = 0.20   # inverted — high merchant win rate = low risk
_WEIGHT_ESCALATION    = 0.20
_WEIGHT_RECENCY       = 0.20
_WEIGHT_DIVERSITY     = 0.10

_HIGH_RISK_THRESHOLD  = 0.70
_MEDIUM_RISK_THRESHOLD = 0.40


class BuyerRiskLevel(str, Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"
    SERIAL = "SERIAL"   # extreme repeat offender (score > 0.85 AND rate > 0.30)


@dataclass
class DisputeRecord:
    dispute_id: str
    amount_usd: float
    reason: str                     # e.g. "INR", "SNAD", "UNAUTH"
    merchant_won: bool
    escalated: bool
    days_ago: int                   # how many days ago the dispute was filed


@dataclass
class BuyerPatternInput:
    buyer_id: str
    total_transactions: int
    dispute_history: List[DisputeRecord] = field(default_factory=list)
    # Optional enrichment from KERNEL community trust graph
    community_trust_score: Optional[float] = None  # 0=untrusted … 1=trusted


@dataclass
class BuyerPatternReport:
    buyer_id: str
    total_disputes: int
    dispute_rate: float
    merchant_win_rate: float
    escalation_rate: float
    recency_score: float            # fraction of disputes in last 30 days (recency-weighted)
    avg_claim_amount: float
    category_diversity: float
    risk_score: float               # 0–1
    risk_level: BuyerRiskLevel
    community_trust_adjusted: bool  # True if kernel score shifted the rating
    recommendation: str
    paypal_signal: str


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


def analyze_buyer_pattern(inp: BuyerPatternInput) -> BuyerPatternReport:
    history = inp.dispute_history
    n = len(history)
    total_tx = max(1, inp.total_transactions)

    dispute_rate = round(_safe_div(n, total_tx), 4)
    merchant_wins = sum(1 for d in history if d.merchant_won)
    merchant_win_rate = round(_safe_div(merchant_wins, n), 4)
    escalated_count = sum(1 for d in history if d.escalated)
    escalation_rate = round(_safe_div(escalated_count, n), 4)

    # Recency: disputes in last 30 days weighted 2×, then normalized
    recent = sum(1 for d in history if d.days_ago <= 30)
    recency_weighted = recent * 2 + (n - recent)
    recency_score = round(_safe_div(recency_weighted, n * 2), 4) if n > 0 else 0.0

    avg_amount = round(_safe_div(sum(d.amount_usd for d in history), n), 2) if n > 0 else 0.0

    reasons = {d.reason for d in history}
    category_diversity = round(_safe_div(len(reasons), n), 4) if n > 0 else 0.0

    # Short-circuit: no disputes → no risk signal
    if n == 0:
        return BuyerPatternReport(
            buyer_id=inp.buyer_id, total_disputes=0, dispute_rate=0.0,
            merchant_win_rate=0.0, escalation_rate=0.0, recency_score=0.0,
            avg_claim_amount=0.0, category_diversity=0.0, risk_score=0.0,
            risk_level=BuyerRiskLevel.LOW, community_trust_adjusted=False,
            recommendation=(
                f"No dispute history for buyer {inp.buyer_id}. "
                f"Standard fulfillment appropriate."
            ),
            paypal_signal=(
                f"Buyer {inp.buyer_id}: risk=LOW, score=0.00, "
                f"dispute_rate=0.0%, disputes=0."
            ),
        )

    # Raw risk score: high dispute_rate → high risk; high merchant_win_rate → low risk
    win_risk = 1.0 - merchant_win_rate   # invert: winning means less risk to merchant
    raw = (
        _WEIGHT_DISPUTE_RATE * min(dispute_rate / 0.30, 1.0)   # cap at 30% rate
        + _WEIGHT_WIN_RATE   * win_risk
        + _WEIGHT_ESCALATION * escalation_rate
        + _WEIGHT_RECENCY    * recency_score
        + _WEIGHT_DIVERSITY  * category_diversity
    )
    raw = round(min(1.0, max(0.0, raw)), 4)

    # Optional KERNEL community trust adjustment
    community_adjusted = False
    adjusted = raw
    if inp.community_trust_score is not None:
        trust = max(0.0, min(1.0, inp.community_trust_score))
        # Trusted buyers get a 20% score reduction; untrusted get 20% increase
        delta = (0.5 - trust) * 0.20
        adjusted = round(min(1.0, max(0.0, raw + delta)), 4)
        community_adjusted = True

    risk_score = adjusted

    # Classify
    if risk_score > 0.85 and dispute_rate > 0.30:
        risk_level = BuyerRiskLevel.SERIAL
    elif risk_score >= _HIGH_RISK_THRESHOLD:
        risk_level = BuyerRiskLevel.HIGH
    elif risk_score >= _MEDIUM_RISK_THRESHOLD:
        risk_level = BuyerRiskLevel.MEDIUM
    else:
        risk_level = BuyerRiskLevel.LOW

    # Recommendation
    if risk_level == BuyerRiskLevel.SERIAL:
        rec = (
            f"Buyer {inp.buyer_id} is a serial claimant: dispute rate {dispute_rate:.1%} "
            f"across {n} disputes (score={risk_score:.2f}). "
            f"Escalate to PayPal for account-level review. "
            f"Collect maximum evidence and counter all future disputes."
        )
    elif risk_level == BuyerRiskLevel.HIGH:
        rec = (
            f"High-risk buyer (score={risk_score:.2f}): dispute rate {dispute_rate:.1%}, "
            f"escalation rate {escalation_rate:.1%}. "
            f"Require shipment tracking and delivery confirmation for future orders. "
            f"Fight any new disputes with full evidence package."
        )
    elif risk_level == BuyerRiskLevel.MEDIUM:
        rec = (
            f"Medium-risk buyer (score={risk_score:.2f}): dispute rate {dispute_rate:.1%}. "
            f"Flag future orders for manual review before fulfillment. "
            f"Request signature confirmation on shipments."
        )
    else:
        rec = (
            f"Low-risk buyer (score={risk_score:.2f}): dispute rate {dispute_rate:.1%} "
            f"with {merchant_win_rate:.1%} merchant win rate. Standard fulfillment appropriate."
        )

    paypal_signal = (
        f"Buyer {inp.buyer_id}: risk={risk_level.value}, score={risk_score:.2f}, "
        f"dispute_rate={dispute_rate:.1%}, disputes={n}, "
        f"escalation_rate={escalation_rate:.1%}, "
        f"merchant_win_rate={merchant_win_rate:.1%}"
        + (f", kernel_adjusted=true" if community_adjusted else "")
        + "."
    )

    return BuyerPatternReport(
        buyer_id=inp.buyer_id,
        total_disputes=n,
        dispute_rate=dispute_rate,
        merchant_win_rate=merchant_win_rate,
        escalation_rate=escalation_rate,
        recency_score=recency_score,
        avg_claim_amount=avg_amount,
        category_diversity=category_diversity,
        risk_score=risk_score,
        risk_level=risk_level,
        community_trust_adjusted=community_adjusted,
        recommendation=rec,
        paypal_signal=paypal_signal,
    )
