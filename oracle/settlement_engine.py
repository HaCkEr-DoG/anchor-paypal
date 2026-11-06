"""
Settlement Engine — smart pre-dispute and in-dispute settlement optimization.

The underappreciated insight: sometimes the highest-EV move is not to fight
OR accept, but to offer the buyer a partial refund BEFORE they escalate.

Why this matters:
  - A $200 dispute that becomes a chargeback hurts HDR + costs $20 to fight
  - A $60 partial refund that closes the dispute preserves HDR + saves $140
  - The HDR impact is BIGGER than the dispute amount for borderline merchants

Settlement windows:
  1. PRE-DISPUTE: Buyer contacts support → offer partial before they escalate
  2. OPEN_DISPUTE: PayPal opens dispute → still possible to resolve bilaterally
  3. PAYPAL_REVIEW: Harder, but PayPal often accepts merchant-initiated resolution

Settlement calculator:
  offer_ev = amount_saved - partial_payout - hdr_impact_avoided
  fight_ev  = P(win) * amount - fight_cost - hdr_impact_risk

Recommend settlement when: offer_ev > fight_ev

Settlement strategy tiers:
  - FULL_REFUND: Maximize HDR protection, accept total loss on this dispute
  - PARTIAL_REFUND_HIGH: 60-80% — typical "let's find a middle ground" offer
  - PARTIAL_REFUND_LOW: 20-40% — for strong-evidence fights where you have leverage
  - REPLACEMENT: Ship replacement + tracking — often cheaper than refund
  - NO_SETTLE: Fight is better EV than any settlement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SettlementStrategy(str, Enum):
    FULL_REFUND         = "FULL_REFUND"          # 100% — accept total loss
    PARTIAL_HIGH        = "PARTIAL_REFUND_HIGH"  # 60–80% of dispute amount
    PARTIAL_MID         = "PARTIAL_REFUND_MID"   # 40–60%
    PARTIAL_LOW         = "PARTIAL_REFUND_LOW"   # 20–40%
    REPLACEMENT         = "REPLACEMENT"          # Ship replacement item
    STORE_CREDIT        = "STORE_CREDIT"         # Offer store credit instead of cash
    NO_SETTLE           = "NO_SETTLE"            # Fight is better EV
    ESCALATE_FIRST      = "ESCALATE_FIRST"       # Need more info before deciding


class DisputeStage(str, Enum):
    PRE_DISPUTE     = "PRE_DISPUTE"      # Customer complained but hasn't filed yet
    OPEN_DISPUTE    = "OPEN_DISPUTE"     # Dispute filed, response window open
    PAYPAL_REVIEW   = "PAYPAL_REVIEW"   # Under PayPal review


@dataclass
class SettlementOffer:
    """One settlement offer option."""
    strategy: SettlementStrategy
    offer_amount: float              # Dollar amount of offer
    offer_pct: float                 # Percentage of dispute amount
    offer_ev: float                  # EV of making this offer
    probability_accepted: float      # Estimated probability buyer accepts
    expected_outcome: float          # offer_pct * probability_accepted
    rationale: str


@dataclass
class SettlementAnalysis:
    """
    Settlement vs. fight analysis for one dispute.
    """
    dispute_id: str
    dispute_amount: float
    claim_type: str
    stage: DisputeStage

    # EV comparison
    fight_ev: float
    best_settlement_ev: float
    settle_vs_fight_advantage: float    # Positive = settle is better

    # Recommendation
    recommended_strategy: SettlementStrategy
    recommended_offer_amount: float
    recommended_offer_pct: float

    # All options ranked
    ranked_offers: list[SettlementOffer]

    # HDR analysis
    hdr_current_rate: float
    hdr_at_risk: bool                   # Would this dispute push HDR into elevated zone?
    hdr_protection_value: float         # Dollar value of avoiding HDR escalation

    # Buyer leverage assessment
    buyer_has_leverage: bool            # Does buyer have evidence we don't?
    merchant_has_leverage: bool         # Do we have evidence buyer doesn't?

    action_script: str                  # What to say to the buyer
    reasoning: list[str] = field(default_factory=list)


# HDR threshold values used for impact calculation
_HDR_THRESHOLDS = [
    (0.0050, "ELEVATED",  0.0,     "monitoring"),
    (0.0100, "LIMITED",   500.0,   "limited processing"),
    (0.0150, "FEE_TIER",  30.0,    "$30/dispute fee"),    # $30/dispute above 1.5%
    (0.0300, "SEVERE",    5000.0,  "severe restrictions"),
    (0.0500, "TERMINAL",  50000.0, "account suspension"),
]


def analyze_settlement(
    dispute_id: str,
    dispute_amount: float,
    claim_type: str,
    stage: DisputeStage,
    base_win_probability: float,
    fight_cost: float = 10.0,
    current_hdr: float = 0.0,
    monthly_transaction_volume: int = 1000,
    hdr_impact_per_dispute: Optional[float] = None,
    item_cost: float = 0.0,         # Merchant's cost of goods (for replacement EV)
    buyer_has_evidence: bool = False,
    merchant_has_tracking: bool = False,
    merchant_has_delivery_confirmation: bool = False,
    is_repeat_buyer: bool = False,
    buyer_risk_tier: str = "TIER_1_NORMAL",
) -> SettlementAnalysis:
    """
    Compute optimal settlement strategy vs. fight/accept for one dispute.

    Key insight: HDR impact isn't just risk — it has a DOLLAR value.
    If you're at 0.90% HDR and this dispute pushes you to 1.00%, you're
    entering LIMITED processing. That's worth real money to avoid.
    """
    reasoning: list[str] = []

    # ── Fight EV ──────────────────────────────────────────────────────────
    fight_ev = base_win_probability * dispute_amount - fight_cost
    reasoning.append(
        f"Fight EV: {base_win_probability:.0%} × ${dispute_amount:.0f} - ${fight_cost:.0f} = ${fight_ev:.0f}"
    )

    # ── HDR impact analysis ────────────────────────────────────────────────
    hdr_protection_value = 0.0
    hdr_at_risk = False

    if hdr_impact_per_dispute is None:
        # Approximate: each dispute adds ~1/volume to HDR rate
        hdr_impact_per_dispute = 1.0 / max(monthly_transaction_volume, 1)

    projected_hdr = current_hdr + hdr_impact_per_dispute

    for threshold, label, penalty_base, description in _HDR_THRESHOLDS:
        if current_hdr < threshold <= projected_hdr:
            # This dispute crosses a threshold
            hdr_at_risk = True
            if label == "FEE_TIER":
                # $30/dispute on all disputes above 1.5% — ongoing cost
                hdr_protection_value = penalty_base * 10  # 10 future disputes at risk
            else:
                hdr_protection_value = penalty_base
            reasoning.append(
                f"HDR ALERT: This dispute crosses {label} threshold ({threshold:.2%}). "
                f"Protection value: ${hdr_protection_value:.0f}"
            )
            break

    # ── Buyer/merchant leverage ────────────────────────────────────────────
    buyer_has_leverage = buyer_has_evidence or claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    merchant_has_leverage = (
        merchant_has_tracking or
        merchant_has_delivery_confirmation or
        base_win_probability >= 0.70
    )

    # ── Build settlement offer options ─────────────────────────────────────
    offers: list[SettlementOffer] = []

    # Full refund
    full_refund_ev = -dispute_amount + hdr_protection_value
    p_accepted_full = 0.95
    offers.append(SettlementOffer(
        strategy=SettlementStrategy.FULL_REFUND,
        offer_amount=dispute_amount,
        offer_pct=1.0,
        offer_ev=full_refund_ev,
        probability_accepted=p_accepted_full,
        expected_outcome=p_accepted_full * 1.0,
        rationale=f"Full refund: EV=${full_refund_ev:.0f} (includes ${hdr_protection_value:.0f} HDR protection)",
    ))

    # Partial offers at different tiers
    for pct, label, strategy in [
        (0.70, "PARTIAL_HIGH", SettlementStrategy.PARTIAL_HIGH),
        (0.50, "PARTIAL_MID",  SettlementStrategy.PARTIAL_MID),
        (0.30, "PARTIAL_LOW",  SettlementStrategy.PARTIAL_LOW),
    ]:
        offer_amt = dispute_amount * pct
        # Buyer acceptance probability depends on how much leverage each side has
        if merchant_has_leverage and not buyer_has_leverage:
            p_accepted = pct * 0.75   # Buyer likely to hold out for more
        elif buyer_has_leverage and not merchant_has_leverage:
            p_accepted = pct * 0.95   # Buyer has strong case, may accept lower
        else:
            p_accepted = pct * 0.85   # Balanced

        # EV: probability_accepted * (dispute_amount saved) + hdr_protection
        ev = p_accepted * (dispute_amount - offer_amt) - (1 - p_accepted) * fight_cost + hdr_protection_value * p_accepted
        offers.append(SettlementOffer(
            strategy=strategy,
            offer_amount=offer_amt,
            offer_pct=pct,
            offer_ev=round(ev, 2),
            probability_accepted=round(p_accepted, 3),
            expected_outcome=round(p_accepted * pct, 3),
            rationale=f"{label}: offer ${offer_amt:.0f} ({pct:.0%}) | EV=${ev:.0f}",
        ))

    # Replacement option (if item_cost known and meaningful)
    if item_cost > 0 and claim_type == "ITEM_NOT_RECEIVED":
        replacement_cost = item_cost * 1.2  # Include shipping
        p_accepted_repl = 0.60  # Many buyers prefer cash
        repl_ev = p_accepted_repl * dispute_amount - replacement_cost + hdr_protection_value * p_accepted_repl
        offers.append(SettlementOffer(
            strategy=SettlementStrategy.REPLACEMENT,
            offer_amount=replacement_cost,
            offer_pct=replacement_cost / max(dispute_amount, 1),
            offer_ev=round(repl_ev, 2),
            probability_accepted=p_accepted_repl,
            expected_outcome=round(p_accepted_repl * 0.8, 3),
            rationale=f"Replacement ship: cost ${replacement_cost:.0f} | EV=${repl_ev:.0f}",
        ))

    # No-settle option
    offers.append(SettlementOffer(
        strategy=SettlementStrategy.NO_SETTLE,
        offer_amount=0,
        offer_pct=0,
        offer_ev=fight_ev,
        probability_accepted=1.0,  # This always "succeeds" — it's the fight path
        expected_outcome=fight_ev / max(dispute_amount, 1),
        rationale=f"Fight: EV=${fight_ev:.0f} ({base_win_probability:.0%} win rate)",
    ))

    # Sort by EV descending
    offers.sort(key=lambda o: o.offer_ev, reverse=True)

    best = offers[0]
    best_settlement = max(
        (o for o in offers if o.strategy != SettlementStrategy.NO_SETTLE),
        key=lambda o: o.offer_ev,
        default=offers[-1],
    )

    settle_advantage = best_settlement.offer_ev - fight_ev

    # Determine recommendation
    if best.strategy == SettlementStrategy.NO_SETTLE or settle_advantage < -20:
        recommended = SettlementStrategy.NO_SETTLE
        recommended_amount = 0.0
        recommended_pct = 0.0
    else:
        recommended = best_settlement.strategy
        recommended_amount = best_settlement.offer_amount
        recommended_pct = best_settlement.offer_pct

    # Action script
    if recommended == SettlementStrategy.NO_SETTLE:
        action_script = (
            "No settlement recommended. Fight with available evidence. "
            f"Win probability {base_win_probability:.0%} justifies the fight."
        )
    elif recommended == SettlementStrategy.FULL_REFUND:
        action_script = (
            f"Offer full refund of ${dispute_amount:.0f}. "
            "HDR protection value exceeds dispute amount."
        )
    else:
        action_script = (
            f"Message buyer: 'We want to make this right. We can offer ${recommended_amount:.0f} "
            f"({recommended_pct:.0%} of your order) as a resolution. "
            "Would that work for you?'"
        )

    reasoning.append(
        f"Best settlement: {recommended.value} @ ${recommended_amount:.0f} "
        f"(EV=${best_settlement.offer_ev:.0f} vs fight EV=${fight_ev:.0f})"
    )

    return SettlementAnalysis(
        dispute_id=dispute_id,
        dispute_amount=dispute_amount,
        claim_type=claim_type,
        stage=stage,
        fight_ev=round(fight_ev, 2),
        best_settlement_ev=round(best_settlement.offer_ev, 2),
        settle_vs_fight_advantage=round(settle_advantage, 2),
        recommended_strategy=recommended,
        recommended_offer_amount=round(recommended_amount, 2),
        recommended_offer_pct=round(recommended_pct, 3),
        ranked_offers=offers,
        hdr_current_rate=current_hdr,
        hdr_at_risk=hdr_at_risk,
        hdr_protection_value=round(hdr_protection_value, 2),
        buyer_has_leverage=buyer_has_leverage,
        merchant_has_leverage=merchant_has_leverage,
        action_script=action_script,
        reasoning=reasoning,
    )
