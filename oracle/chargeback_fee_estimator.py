"""
Chargeback Fee Estimator — estimate all fees associated with a chargeback
across PayPal, Visa, Mastercard, Amex, and Discover.

Covers:
  - Standard chargeback processing fee (per network)
  - HDR tier-based additional dispute fees ($30/dispute at FEE_TIER/SEVERE)
  - Arbitration fees (when escalated)
  - Reversal fees (when merchant loses pre-arbitration)
  - Monthly monitoring program fee (Visa VDMP / MC MDMAC)
  - Pro-forma annual exposure given current dispute rate

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL    = "PAYPAL"
    VISA      = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX      = "AMEX"
    DISCOVER  = "DISCOVER"


class HDRTier(str, Enum):
    NORMAL   = "NORMAL"
    ELEVATED = "ELEVATED"
    LIMITED  = "LIMITED"
    FEE_TIER = "FEE_TIER"
    SEVERE   = "SEVERE"


class DisputeOutcome(str, Enum):
    MERCHANT_WIN  = "MERCHANT_WIN"
    MERCHANT_LOSE = "MERCHANT_LOSE"
    ARBITRATION   = "ARBITRATION"
    PRE_ARBITRATION_LOSS = "PRE_ARBITRATION_LOSS"


# ── Per-network standard chargeback processing fees (USD) ─────────
# These are representative industry figures — actual fees vary by processor.
_CB_FEE: dict[Network, float] = {
    Network.PAYPAL:     20.0,
    Network.VISA:       15.0,
    Network.MASTERCARD: 15.0,
    Network.AMEX:       25.0,
    Network.DISCOVER:   15.0,
}

# Arbitration / pre-arb escalation fees
_ARBITRATION_FEE: dict[Network, float] = {
    Network.PAYPAL:     0.0,     # PayPal does not have card-network arbitration
    Network.VISA:       500.0,
    Network.MASTERCARD: 500.0,
    Network.AMEX:       0.0,     # Amex handles internally
    Network.DISCOVER:   250.0,
}

# Pre-arbitration loss reversal fees (applied on top of CB fee)
_PRE_ARB_REVERSAL_FEE: dict[Network, float] = {
    Network.PAYPAL:     0.0,
    Network.VISA:       150.0,
    Network.MASTERCARD: 150.0,
    Network.AMEX:       0.0,
    Network.DISCOVER:   100.0,
}

# VDMP / MDMAC monthly monitoring surcharge (per dispute in the program)
_MONITORING_FEE: dict[Network, float] = {
    Network.PAYPAL:     0.0,
    Network.VISA:       10.0,    # VDMP standard surcharge per dispute
    Network.MASTERCARD: 10.0,    # MDMAC standard surcharge
    Network.AMEX:       0.0,
    Network.DISCOVER:   0.0,
}

_HDR_TIER_FEE: dict[HDRTier, float] = {
    HDRTier.NORMAL:   0.0,
    HDRTier.ELEVATED: 0.0,
    HDRTier.LIMITED:  0.0,
    HDRTier.FEE_TIER: 30.0,
    HDRTier.SEVERE:   30.0,
}


@dataclass
class FeeEstimatorInput:
    """Input for chargeback fee estimation."""
    dispute_id: str
    network: Network
    transaction_amount: float

    # Outcome assumed for fee projection
    assumed_outcome: DisputeOutcome = DisputeOutcome.MERCHANT_LOSE

    # HDR context
    hdr_tier: HDRTier = HDRTier.NORMAL

    # Whether merchant is in a network monitoring program
    in_monitoring_program: bool = False

    # Whether this escalated to arbitration
    escalated_to_arbitration: bool = False

    # Number of disputes in the last 90 days (for annual projection)
    disputes_last_90_days: int = 1

    # Gateway / processing context
    gateway_fee_pct: float = 0.029
    gateway_fee_flat: float = 0.30
    gateway_refunds_fee: bool = False    # Whether gateway refunds processing fee

    # Operational context
    fight_cost: float = 15.0


@dataclass
class FeeLineItem:
    """A single fee component."""
    name: str
    amount: float
    conditional: bool   # True if this fee only applies in certain conditions
    condition_description: str


@dataclass
class FeeEstimatorReport:
    """Full fee breakdown for a chargeback."""
    dispute_id: str
    network: Network
    transaction_amount: float
    assumed_outcome: DisputeOutcome

    # Individual fee components
    fee_items: list[FeeLineItem]

    # Totals
    total_fees: float           # All fees excluding transaction loss
    total_cost_with_loss: float # Fees + transaction amount lost
    total_cost_win: float       # Fees even if merchant wins

    # Annual projection
    annual_dispute_volume: int  # Projected from 90-day rate
    annual_fee_exposure: float  # If current rate continues

    recommendation: str
    summary: str


def estimate_chargeback_fees(inp: FeeEstimatorInput) -> FeeEstimatorReport:
    """Estimate all fees associated with a chargeback dispute."""
    network = inp.network
    amount = inp.transaction_amount
    outcome = inp.assumed_outcome

    fee_items: list[FeeLineItem] = []

    # ── Processing fee ───────────────────────────────────────────
    cb_fee = _CB_FEE[network]
    fee_items.append(FeeLineItem(
        name=f"{network.value} chargeback processing fee",
        amount=cb_fee,
        conditional=False,
        condition_description="Applied on every chargeback regardless of outcome",
    ))

    # ── Fight / response cost ────────────────────────────────────
    if outcome != DisputeOutcome.MERCHANT_LOSE:
        fee_items.append(FeeLineItem(
            name="Dispute response labor cost",
            amount=inp.fight_cost,
            conditional=True,
            condition_description="Applied when merchant files a response",
        ))

    # ── Gateway fee (not refunded) ────────────────────────────────
    gw_fee = round(amount * inp.gateway_fee_pct + inp.gateway_fee_flat, 2)
    if not inp.gateway_refunds_fee:
        fee_items.append(FeeLineItem(
            name="Gateway processing fee (non-refundable)",
            amount=gw_fee,
            conditional=False,
            condition_description="Most processors keep the processing fee on chargebacks",
        ))

    # ── HDR tier additional fee ──────────────────────────────────
    hdr_fee = _HDR_TIER_FEE[inp.hdr_tier]
    if hdr_fee > 0:
        fee_items.append(FeeLineItem(
            name=f"HDR {inp.hdr_tier.value} tier surcharge",
            amount=hdr_fee,
            conditional=True,
            condition_description=f"Applied when HDR tier is {inp.hdr_tier.value}",
        ))

    # ── Monitoring program fee ────────────────────────────────────
    monitoring_fee = _MONITORING_FEE[network] if inp.in_monitoring_program else 0.0
    if monitoring_fee > 0:
        fee_items.append(FeeLineItem(
            name=f"{network.value} monitoring program surcharge",
            amount=monitoring_fee,
            conditional=True,
            condition_description="Per-dispute surcharge while enrolled in monitoring program",
        ))

    # ── Arbitration fees ─────────────────────────────────────────
    arb_fee = 0.0
    if inp.escalated_to_arbitration:
        arb_fee = _ARBITRATION_FEE[network]
        if arb_fee > 0:
            fee_items.append(FeeLineItem(
                name=f"{network.value} arbitration filing fee",
                amount=arb_fee,
                conditional=True,
                condition_description="Charged when dispute is escalated to network arbitration",
            ))

    # ── Pre-arbitration reversal fee ──────────────────────────────
    pre_arb_fee = 0.0
    if outcome == DisputeOutcome.PRE_ARBITRATION_LOSS:
        pre_arb_fee = _PRE_ARB_REVERSAL_FEE[network]
        if pre_arb_fee > 0:
            fee_items.append(FeeLineItem(
                name=f"{network.value} pre-arbitration reversal fee",
                amount=pre_arb_fee,
                conditional=True,
                condition_description="Charged when merchant loses pre-arbitration review",
            ))

    # ── Totals ────────────────────────────────────────────────────
    all_fees = sum(f.amount for f in fee_items)
    total_with_loss = round(all_fees + amount, 2)

    # Fees even if merchant wins: just fight cost + gateway
    win_fees = round(inp.fight_cost + (gw_fee if not inp.gateway_refunds_fee else 0.0), 2)

    # Annual projection (90-day rate × 4)
    annual_vol = inp.disputes_last_90_days * 4
    # Use per-dispute fees (excluding the one-time arb) for annual
    per_dispute_recurring = cb_fee + hdr_fee + monitoring_fee
    annual_exposure = round(annual_vol * (per_dispute_recurring + gw_fee), 2)

    # Recommendation
    if inp.hdr_tier in (HDRTier.FEE_TIER, HDRTier.SEVERE):
        rec = (f"HDR at {inp.hdr_tier.value}: each additional loss costs ${all_fees:.0f}+ — "
               "accept borderline disputes to control tier")
    elif inp.escalated_to_arbitration and arb_fee > 100:
        rec = (f"Arbitration filing adds ${arb_fee:.0f} — "
               "only escalate if win probability is very high (>75%)")
    elif outcome == DisputeOutcome.MERCHANT_WIN:
        rec = f"Win path costs ${win_fees:.0f} in irrecoverable fees — still worthwhile if EV is positive"
    else:
        rec = (f"Total cost of losing: ${total_with_loss:.0f}. "
               f"Fight only if EV(fight) > ${all_fees:.0f}")

    summary = (
        f"Fees {inp.dispute_id} [{network.value}]: "
        f"cb_fee=${cb_fee:.0f} | hdr=${hdr_fee:.0f} | "
        f"total_loss=${total_with_loss:.0f} | annual=${annual_exposure:.0f}"
    )

    return FeeEstimatorReport(
        dispute_id=inp.dispute_id,
        network=network,
        transaction_amount=amount,
        assumed_outcome=outcome,
        fee_items=fee_items,
        total_fees=round(all_fees, 2),
        total_cost_with_loss=total_with_loss,
        total_cost_win=win_fees,
        annual_dispute_volume=annual_vol,
        annual_fee_exposure=annual_exposure,
        recommendation=rec,
        summary=summary,
    )
