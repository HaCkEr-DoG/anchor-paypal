"""
Fee Impact Calculator — compute the full fee cost of a dispute across all
applicable PayPal and card network fee categories.

PayPal disputes trigger multiple overlapping fees depending on the dispute
outcome, the merchant's HDR status, and the card network involved. This
module calculates the total financial impact of each possible outcome and
produces a fee breakdown to inform the fight-or-refund decision.

Fee categories:
  TRANSACTION_FEE     — Non-refundable PayPal processing fee on original sale
  CHARGEBACK_FEE      — $20 per lost chargeback (waived with Seller Protection)
  HDR_SURCHARGE       — $30/dispute when HDR rate ≥ 1.5% (accumulates per dispute)
  REFUND_FEE          — Partial return of transaction fee on full refunds
  DISPUTE_RESOLUTION  — Internal labor cost of resolving the dispute

HDR surcharge rules:
  < 0.50%  — No surcharge
  0.50–1.00% — Elevated (no fee yet, monitoring)
  1.00–1.50% — Limited (no surcharge yet)
  ≥ 1.50%  — $30/dispute surcharge active
  ≥ 3.00%  — Severe (same $30 + potential additional restrictions)

Seller Protection (SP) impact:
  When SP is applicable and the merchant meets requirements:
  - Chargeback fee is fully waived
  - HDR surcharge still applies if rate ≥ 1.5%

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FeeCategory(str, Enum):
    TRANSACTION_FEE     = "TRANSACTION_FEE"
    CHARGEBACK_FEE      = "CHARGEBACK_FEE"
    HDR_SURCHARGE       = "HDR_SURCHARGE"
    REFUND_FEE_RECOVERY = "REFUND_FEE_RECOVERY"  # Partial fee returned on refund
    DISPUTE_LABOR       = "DISPUTE_LABOR"


class DisputeOutcome(str, Enum):
    WIN          = "WIN"           # Merchant wins → no refund
    LOSE         = "LOSE"          # Merchant loses → full chargeback
    REFUND_NOW   = "REFUND_NOW"    # Merchant refunds proactively
    PARTIAL      = "PARTIAL"       # Partial settlement


@dataclass
class FeeItem:
    """One fee line item."""
    category: FeeCategory
    amount: float
    description: str
    is_waived: bool = False
    waiver_reason: str = ""


@dataclass
class FeeImpactInput:
    """Input for fee impact computation."""
    dispute_id: str
    transaction_amount: float
    paypal_rate_pct: float = 3.49    # PayPal transaction fee %
    paypal_fixed_fee: float = 0.49   # PayPal transaction fixed fee

    # HDR context
    hdr_rate_pct: float = 0.40        # Current rolling 90d rate
    sp_applicable: bool = False       # Seller Protection eligible

    # Dispute context
    fight_hours: float = 1.0          # Staff hours to work this dispute
    hourly_rate: float = 25.0         # Internal cost per labor hour

    # Outcome modifier
    partial_refund_pct: float = 0.50  # For PARTIAL outcome


@dataclass
class OutcomeFeeBreakdown:
    """Fee breakdown for one possible outcome."""
    outcome: DisputeOutcome
    fee_items: list[FeeItem]
    total_fees: float
    net_loss: float              # total_fees + (refunded amount if applicable)
    effective_loss_pct: float    # Net loss as % of transaction amount


@dataclass
class FeeImpactReport:
    """Complete fee impact analysis across all outcomes."""
    dispute_id: str
    transaction_amount: float

    # Per-outcome analysis
    outcome_win: OutcomeFeeBreakdown
    outcome_lose: OutcomeFeeBreakdown
    outcome_refund_now: OutcomeFeeBreakdown
    outcome_partial: OutcomeFeeBreakdown

    # Computed breakeven
    breakeven_p_win: float       # Min win probability to make fighting worthwhile
    fee_delta_win_vs_lose: float  # Fee difference: lose - win

    # HDR context
    hdr_surcharge_active: bool
    hdr_surcharge_per_dispute: float

    summary: str


def _transaction_fee(inp: FeeImpactInput) -> FeeItem:
    fee = round(inp.transaction_amount * (inp.paypal_rate_pct / 100) + inp.paypal_fixed_fee, 2)
    return FeeItem(
        category=FeeCategory.TRANSACTION_FEE,
        amount=fee,
        description=f"PayPal processing fee ({inp.paypal_rate_pct}% + ${inp.paypal_fixed_fee:.2f})",
    )


def _chargeback_fee(inp: FeeImpactInput) -> FeeItem:
    if inp.sp_applicable:
        return FeeItem(
            category=FeeCategory.CHARGEBACK_FEE,
            amount=20.0,
            description="Chargeback fee ($20) — WAIVED by Seller Protection",
            is_waived=True,
            waiver_reason="Seller Protection coverage",
        )
    return FeeItem(
        category=FeeCategory.CHARGEBACK_FEE,
        amount=20.0,
        description="Chargeback fee ($20) — applied on lost chargeback",
    )


def _hdr_surcharge(inp: FeeImpactInput) -> Optional[FeeItem]:
    if inp.hdr_rate_pct < 1.50:
        return None
    surcharge = 30.0
    return FeeItem(
        category=FeeCategory.HDR_SURCHARGE,
        amount=surcharge,
        description=f"HDR surcharge ($30) — rate {inp.hdr_rate_pct:.2f}% ≥ 1.50% threshold",
    )


def _refund_fee_recovery(inp: FeeImpactInput, refund_pct: float = 1.0) -> FeeItem:
    # PayPal returns the fixed fee portion on full refunds, but NOT the percentage
    if refund_pct >= 1.0:
        recovered = inp.paypal_fixed_fee
        return FeeItem(
            category=FeeCategory.REFUND_FEE_RECOVERY,
            amount=-recovered,  # Negative = money back
            description=f"Fixed fee recovery on full refund (${recovered:.2f} returned)",
        )
    return FeeItem(
        category=FeeCategory.REFUND_FEE_RECOVERY,
        amount=0.0,
        description="No fee recovery on partial refund",
    )


def _labor_fee(inp: FeeImpactInput) -> FeeItem:
    cost = round(inp.fight_hours * inp.hourly_rate, 2)
    return FeeItem(
        category=FeeCategory.DISPUTE_LABOR,
        amount=cost,
        description=f"Dispute labor ({inp.fight_hours:.1f}h × ${inp.hourly_rate:.0f}/h)",
    )


def _build_outcome(inp: FeeImpactInput, outcome: DisputeOutcome) -> OutcomeFeeBreakdown:
    fees: list[FeeItem] = []
    refund_amount = 0.0

    tx_fee = _transaction_fee(inp)
    fees.append(tx_fee)

    if outcome == DisputeOutcome.WIN:
        fees.append(_labor_fee(inp))
        # No refund, no chargeback fee — just labor and transaction fee already paid

    elif outcome == DisputeOutcome.LOSE:
        cb_fee = _chargeback_fee(inp)
        fees.append(cb_fee)
        hdr = _hdr_surcharge(inp)
        if hdr:
            fees.append(hdr)
        fees.append(_labor_fee(inp))
        refund_amount = inp.transaction_amount

    elif outcome == DisputeOutcome.REFUND_NOW:
        fees.append(_refund_fee_recovery(inp, 1.0))
        refund_amount = inp.transaction_amount
        # No chargeback fee, no labor cost (proactive refund is fast)

    elif outcome == DisputeOutcome.PARTIAL:
        fees.append(_refund_fee_recovery(inp, inp.partial_refund_pct))
        refund_amount = round(inp.transaction_amount * inp.partial_refund_pct, 2)

    active_fees = [f for f in fees if not f.is_waived]
    total_fees = round(sum(f.amount for f in active_fees), 2)
    net_loss = round(total_fees + refund_amount, 2)
    effective_pct = round(net_loss / inp.transaction_amount, 4) if inp.transaction_amount > 0 else 0.0

    return OutcomeFeeBreakdown(
        outcome=outcome,
        fee_items=fees,
        total_fees=total_fees,
        net_loss=net_loss,
        effective_loss_pct=effective_pct,
    )


def calculate_fee_impact(inp: FeeImpactInput) -> FeeImpactReport:
    """
    Calculate the full fee impact for all possible dispute outcomes.
    """
    win    = _build_outcome(inp, DisputeOutcome.WIN)
    lose   = _build_outcome(inp, DisputeOutcome.LOSE)
    refund = _build_outcome(inp, DisputeOutcome.REFUND_NOW)
    partial = _build_outcome(inp, DisputeOutcome.PARTIAL)

    # Breakeven: EV(fight) = p * win_loss + (1-p) * lose_loss = refund_loss
    # p * win_loss + lose_loss - p * lose_loss = refund_loss
    # p * (win_loss - lose_loss) = refund_loss - lose_loss
    # p = (refund_loss - lose_loss) / (win_loss - lose_loss)
    denom = win.net_loss - lose.net_loss
    if abs(denom) < 0.01:
        breakeven_p = 0.5
    else:
        breakeven_p = round((refund.net_loss - lose.net_loss) / denom, 4)
    breakeven_p = max(0.0, min(1.0, breakeven_p))

    hdr_active = inp.hdr_rate_pct >= 1.50
    hdr_surcharge_amt = 30.0 if hdr_active else 0.0

    summary = (
        f"Fees {inp.dispute_id}: amt=${inp.transaction_amount:.2f} | "
        f"win=${win.net_loss:.0f} | lose=${lose.net_loss:.0f} | "
        f"refund_now=${refund.net_loss:.0f} | breakeven_p={breakeven_p:.0%} | "
        f"hdr_surcharge={'YES' if hdr_active else 'NO'}"
    )

    return FeeImpactReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        outcome_win=win,
        outcome_lose=lose,
        outcome_refund_now=refund,
        outcome_partial=partial,
        breakeven_p_win=breakeven_p,
        fee_delta_win_vs_lose=round(lose.net_loss - win.net_loss, 2),
        hdr_surcharge_active=hdr_active,
        hdr_surcharge_per_dispute=hdr_surcharge_amt,
        summary=summary,
    )
