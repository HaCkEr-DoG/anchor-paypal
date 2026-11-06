"""
Duplicate Charge Detector — identify whether a dispute arises from a
genuine duplicate charge (same card, same amount, within a short time
window) versus a buyer who ordered the same item twice intentionally.

Duplicate disputes are almost always winnable when the merchant can show:
  - Only one transaction in the payment processor record
  - Matching order IDs
  - Single fulfillment event

But if two separate authorizations DID go through, the merchant usually
must refund the second charge even without a chargeback.

This module computes:
  - Likelihood that the dispute is a true duplicate (vs. legitimate second purchase)
  - Whether the merchant is at fault (double auth) or the buyer is confused
  - Evidence needed to defend or refund

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DuplicateVerdict(str, Enum):
    TRUE_DUPLICATE       = "TRUE_DUPLICATE"       # Merchant double-charged
    LIKELY_DUPLICATE     = "LIKELY_DUPLICATE"     # High suspicion, needs investigation
    AMBIGUOUS            = "AMBIGUOUS"             # Could go either way
    LIKELY_LEGITIMATE    = "LIKELY_LEGITIMATE"    # Buyer ordered twice
    CONFIRMED_LEGITIMATE = "CONFIRMED_LEGITIMATE" # Definitively two orders


class DuplicateResolution(str, Enum):
    REFUND_SECOND        = "REFUND_SECOND"         # Refund the duplicate charge
    INVESTIGATE          = "INVESTIGATE"           # Need more data before acting
    FIGHT                = "FIGHT"                 # Buyer ordered twice; defend
    PARTIAL_REFUND       = "PARTIAL_REFUND"        # Refund difference if amounts differ


@dataclass
class TransactionRecord:
    """One transaction from the payment processor."""
    transaction_id: str
    amount: float
    timestamp_epoch: int    # Unix seconds
    order_id: Optional[str] = None
    authorization_code: Optional[str] = None
    is_settled: bool = True


@dataclass
class DuplicateChargeInput:
    """Input for duplicate charge analysis."""
    dispute_id: str
    disputed_transaction_id: str
    disputed_amount: float
    card_last4: str

    # Other recent transactions for this card
    recent_transactions: list[TransactionRecord] = field(default_factory=list)

    # Merchant-side evidence
    unique_order_count: int = 1          # How many unique orders exist
    has_separate_shipments: bool = False  # Two orders → two shipments
    has_separate_fulfillment_logs: bool = False
    buyer_acknowledged_second_order: bool = False

    # Timing
    dispute_window_hours: int = 24       # Transactions within this window to compare

    # Context
    buyer_dispute_reason: str = ""       # Buyer's stated reason


def _time_diff_hours(t1: int, t2: int) -> float:
    return abs(t1 - t2) / 3600.0


def _find_potential_duplicates(
        disputed_tx_id: str,
        disputed_amount: float,
        transactions: list[TransactionRecord],
        window_hours: int) -> list[TransactionRecord]:
    """Find transactions that look like duplicates of the disputed one."""
    disputed = next((t for t in transactions if t.transaction_id == disputed_tx_id), None)
    if disputed is None:
        return []

    matches = []
    for tx in transactions:
        if tx.transaction_id == disputed_tx_id:
            continue
        same_amount = abs(tx.amount - disputed_amount) < 0.01
        within_window = _time_diff_hours(tx.timestamp_epoch, disputed.timestamp_epoch) <= window_hours
        if same_amount and within_window:
            matches.append(tx)
    return matches


def _assess_verdict(
        inp: DuplicateChargeInput,
        potential_dups: list[TransactionRecord]) -> DuplicateVerdict:

    if not potential_dups:
        # No matching transaction found — buyer may be confused
        if inp.unique_order_count <= 1:
            return DuplicateVerdict.CONFIRMED_LEGITIMATE  # Only one order
        return DuplicateVerdict.LIKELY_LEGITIMATE

    # Found at least one transaction with same amount in the window
    n_dups = len(potential_dups)

    if inp.has_separate_shipments and inp.has_separate_fulfillment_logs:
        return DuplicateVerdict.CONFIRMED_LEGITIMATE
    if inp.buyer_acknowledged_second_order:
        return DuplicateVerdict.CONFIRMED_LEGITIMATE
    if inp.has_separate_shipments:
        return DuplicateVerdict.LIKELY_LEGITIMATE

    if n_dups >= 1 and inp.unique_order_count <= 1:
        return DuplicateVerdict.TRUE_DUPLICATE  # Same order, two charges
    if n_dups >= 1 and inp.unique_order_count == 2:
        return DuplicateVerdict.AMBIGUOUS  # Two orders, same amount
    return DuplicateVerdict.LIKELY_DUPLICATE


def _resolution(verdict: DuplicateVerdict, inp: DuplicateChargeInput) -> DuplicateResolution:
    if verdict == DuplicateVerdict.TRUE_DUPLICATE:
        return DuplicateResolution.REFUND_SECOND
    if verdict == DuplicateVerdict.LIKELY_DUPLICATE:
        return DuplicateResolution.INVESTIGATE
    if verdict == DuplicateVerdict.AMBIGUOUS:
        return DuplicateResolution.INVESTIGATE
    # Legitimate
    return DuplicateResolution.FIGHT


def _win_probability(verdict: DuplicateVerdict) -> float:
    return {
        DuplicateVerdict.TRUE_DUPLICATE:       0.05,
        DuplicateVerdict.LIKELY_DUPLICATE:     0.25,
        DuplicateVerdict.AMBIGUOUS:            0.50,
        DuplicateVerdict.LIKELY_LEGITIMATE:    0.72,
        DuplicateVerdict.CONFIRMED_LEGITIMATE: 0.88,
    }[verdict]


@dataclass
class DuplicateChargeReport:
    """Analysis of a potential duplicate charge dispute."""
    dispute_id: str
    verdict: DuplicateVerdict
    resolution: DuplicateResolution
    win_probability: float
    is_merchant_fault: bool

    duplicate_transactions: list[str]   # transaction_ids of suspected duplicates
    total_duplicate_amount: float

    required_evidence: list[str]
    missing_evidence: list[str]
    immediate_actions: list[str]
    summary: str


def detect_duplicate_charge(inp: DuplicateChargeInput) -> DuplicateChargeReport:
    """Analyze whether a dispute is caused by a genuine duplicate charge."""
    potential_dups = _find_potential_duplicates(
        inp.disputed_transaction_id, inp.disputed_amount,
        inp.recent_transactions, inp.dispute_window_hours,
    )

    verdict = _assess_verdict(inp, potential_dups)
    resolution = _resolution(verdict, inp)
    p_win = _win_probability(verdict)
    is_fault = verdict in (DuplicateVerdict.TRUE_DUPLICATE, DuplicateVerdict.LIKELY_DUPLICATE)

    dup_ids = [t.transaction_id for t in potential_dups]
    total_dup = sum(t.amount for t in potential_dups)

    # Required evidence
    required: list[str] = ["payment_processor_transaction_log", "order_management_records"]
    if verdict in (DuplicateVerdict.TRUE_DUPLICATE, DuplicateVerdict.LIKELY_DUPLICATE):
        required.append("refund_confirmation_for_duplicate_charge")
    else:
        required += [
            "separate_order_confirmations_for_each_purchase",
            "fulfillment_records_per_order",
        ]

    missing: list[str] = []
    if not inp.has_separate_fulfillment_logs and resolution == DuplicateResolution.FIGHT:
        missing.append("fulfillment_logs")
    if not inp.has_separate_shipments and resolution == DuplicateResolution.FIGHT:
        missing.append("separate_shipment_records")

    # Immediate actions
    actions: list[str] = []
    if resolution == DuplicateResolution.REFUND_SECOND:
        actions.append(f"Issue immediate refund of ${inp.disputed_amount:.2f} for duplicate charge")
        actions.append("Audit payment processor to prevent future double-charges")
    elif resolution == DuplicateResolution.INVESTIGATE:
        actions.append("Pull payment processor logs to compare authorization codes")
        actions.append("Check order management system for unique order count")
    else:
        actions.append("Submit separate order confirmations and fulfillment records as evidence")

    summary = (
        f"DupCheck {inp.dispute_id}: "
        f"verdict={verdict.value} | p_win={p_win:.0%} | "
        f"dup_txns={len(dup_ids)} | action={resolution.value}"
    )

    return DuplicateChargeReport(
        dispute_id=inp.dispute_id,
        verdict=verdict,
        resolution=resolution,
        win_probability=p_win,
        is_merchant_fault=is_fault,
        duplicate_transactions=dup_ids,
        total_duplicate_amount=round(total_dup, 2),
        required_evidence=required,
        missing_evidence=missing,
        immediate_actions=actions,
        summary=summary,
    )
