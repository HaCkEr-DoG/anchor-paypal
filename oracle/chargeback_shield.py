"""
Chargeback Shield — defense strategy engine for credit card chargebacks.

PayPal disputes and credit card chargebacks are different processes:
  - PayPal disputes: handled inside PayPal's Resolution Center
  - Credit card chargebacks: initiated by card issuer (Visa, Mastercard, Amex, Discover)
    and processed through PayPal's chargeback API

Key differences from PayPal disputes:
  1. Reason codes differ — Visa/MC have specific numeric codes (83, 4853, etc.)
  2. Compelling evidence rules are card-network-specific
  3. Chargeback representment requires network-specific documentation
  4. Win rates are LOWER — card networks favor cardholders more than PayPal does merchants
  5. Processing fees: $20-$30 per chargeback (separate from PayPal dispute fees)
  6. Second chargeback ("pre-arbitration") possible if merchant wins first round

Compelling evidence 3.0 (CE3.0) for Visa:
  - Undisputed prior transactions from same device/IP as the dispute
  - Shows buyer used card successfully before — undermines "I didn't do this" claim
  - Requires at least 2 prior non-disputed transactions within 120 days

This module provides:
  - Chargeback reason code classification and win-rate estimate
  - CE3.0 eligibility check for Visa unauthorized chargebacks
  - Required documentation checklist per reason code
  - Financial ROI of contesting vs. accepting

Output:
  - ChargebackDefenseReport with strategy, documentation checklist, expected outcome
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CardNetwork(str, Enum):
    VISA       = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX       = "AMEX"
    DISCOVER   = "DISCOVER"
    UNKNOWN    = "UNKNOWN"


class ChargebackReasonCategory(str, Enum):
    UNAUTHORIZED     = "UNAUTHORIZED"      # Fraud/unauthorized — hardest to win
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED"  # INR — good evidence wins
    SIGNIFICANTLY_NAS = "SIGNIFICANTLY_NAS"  # SNAD — hardest
    SUBSCRIPTION     = "SUBSCRIPTION"      # Cancelled before billing
    DUPLICATE        = "DUPLICATE"         # Duplicate charge — usually merchant error
    GENERAL_CREDIT   = "GENERAL_CREDIT"    # Credit not received
    PROCESSING_ERROR = "PROCESSING_ERROR"  # Technical/processing error


class DefenseStrategy(str, Enum):
    CONTEST         = "CONTEST"      # Submit representment
    ACCEPT          = "ACCEPT"       # Accept the chargeback
    PRE_ARBITRATION = "PRE_ARBITRATION"  # Escalate beyond first representment
    CE30_CONTEST    = "CE30_CONTEST"  # Visa CE3.0 compelling evidence strategy


# Base win rates by reason category (from industry data)
_BASE_WIN_RATES = {
    ChargebackReasonCategory.UNAUTHORIZED: 0.28,
    ChargebackReasonCategory.ITEM_NOT_RECEIVED: 0.52,
    ChargebackReasonCategory.SIGNIFICANTLY_NAS: 0.22,
    ChargebackReasonCategory.SUBSCRIPTION: 0.38,
    ChargebackReasonCategory.DUPLICATE: 0.65,
    ChargebackReasonCategory.GENERAL_CREDIT: 0.45,
    ChargebackReasonCategory.PROCESSING_ERROR: 0.60,
}

# Network-specific adjustments to base win rate
_NETWORK_WIN_RATE_ADJ = {
    CardNetwork.VISA: 0.0,
    CardNetwork.MASTERCARD: -0.03,  # Slightly harder
    CardNetwork.AMEX: -0.05,        # Amex strongly favors cardholders
    CardNetwork.DISCOVER: -0.02,
    CardNetwork.UNKNOWN: 0.0,
}

# Common Visa reason codes
_VISA_REASON_CODES = {
    "10.4": ChargebackReasonCategory.UNAUTHORIZED,      # Other Fraud - Card-Absent
    "10.5": ChargebackReasonCategory.UNAUTHORIZED,      # Visa Fraud Monitoring Program
    "12.5": ChargebackReasonCategory.ITEM_NOT_RECEIVED, # Incorrect Amount
    "13.1": ChargebackReasonCategory.ITEM_NOT_RECEIVED, # Merchandise/Services Not Received
    "13.3": ChargebackReasonCategory.SIGNIFICANTLY_NAS, # Not as Described
    "13.6": ChargebackReasonCategory.GENERAL_CREDIT,    # Credit Not Processed
    "13.7": ChargebackReasonCategory.SUBSCRIPTION,      # Cancelled Merchandise/Services
    "12.2": ChargebackReasonCategory.DUPLICATE,         # Incorrect Transaction Code
    "12.4": ChargebackReasonCategory.DUPLICATE,         # Incorrect Account Number
}

# Required documents by reason category
_REQUIRED_DOCS = {
    ChargebackReasonCategory.UNAUTHORIZED: [
        "PayPal transaction details showing buyer IP and device fingerprint",
        "Account login history showing buyer accessed account before order",
        "Order confirmation email sent to buyer's email address",
        "Signed delivery confirmation or proof of delivery",
        "Any buyer communications accepting the order",
    ],
    ChargebackReasonCategory.ITEM_NOT_RECEIVED: [
        "Tracking number with carrier delivery confirmation",
        "Signature confirmation (if order ≥ $750 or required by policy)",
        "Proof of shipment with matching shipping address",
        "Carrier's official delivery status screenshot",
        "Order details matching PayPal transaction amount and address",
    ],
    ChargebackReasonCategory.SIGNIFICANTLY_NAS: [
        "Original item description / product listing (screenshot)",
        "Photos of item as shipped vs. buyer's claim",
        "Return policy (must be clearly stated at checkout)",
        "Buyer communications and your responses",
        "Quality control documentation for the item",
    ],
    ChargebackReasonCategory.SUBSCRIPTION: [
        "Subscription terms clearly showing no-cancellation period or renewal terms",
        "Proof buyer accepted terms at checkout",
        "Cancellation policy disclosed prominently",
        "Evidence buyer did NOT cancel before billing period",
        "Service usage logs (if digital service)",
    ],
    ChargebackReasonCategory.DUPLICATE: [
        "Payment records showing only one charge to buyer",
        "Bank/PayPal reconciliation showing single transaction",
        "Order system logs showing one fulfillment",
    ],
    ChargebackReasonCategory.GENERAL_CREDIT: [
        "Refund processing record if credit was issued",
        "If no refund: explanation of why credit is not owed",
        "Any agreements with buyer about credit terms",
    ],
    ChargebackReasonCategory.PROCESSING_ERROR: [
        "Correct transaction details showing amount/currency were right",
        "Authorization records",
        "Any technical logs if applicable",
    ],
}


@dataclass
class PriorTransaction:
    """Prior transaction from same buyer for CE3.0 eligibility."""
    transaction_id: str
    amount: float
    is_disputed: bool = False
    days_before_chargeback: int = 0
    same_device: bool = False
    same_ip: bool = False
    same_billing_address: bool = True


@dataclass
class ChargebackInput:
    """State of a credit card chargeback at defense time."""
    chargeback_id: str
    order_id: str
    chargeback_amount: float

    # Card and reason
    card_network: CardNetwork = CardNetwork.VISA
    reason_code: str = "10.4"           # Network-specific code
    reason_category: Optional[ChargebackReasonCategory] = None  # Auto-derived if None

    # Evidence state
    has_tracking: bool = True
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_buyer_communications: bool = True
    has_account_login_history: bool = False

    # CE3.0 (Visa unauthorized only)
    prior_transactions: list[PriorTransaction] = field(default_factory=list)

    # Merchant context
    merchant_chargeback_win_rate: float = 0.40  # Merchant's historical CB win rate
    fight_cost: float = 25.0                    # Internal cost to contest


@dataclass
class CE30Assessment:
    """Visa Compelling Evidence 3.0 eligibility and strength."""
    eligible: bool
    qualifying_prior_transactions: int    # Count of non-disputed prior transactions
    same_device_matches: int
    same_ip_matches: int
    expected_win_rate_boost: float        # Additive to base win rate
    notes: list[str] = field(default_factory=list)


@dataclass
class ChargebackDefenseReport:
    """
    Complete chargeback defense strategy for one chargeback.
    """
    chargeback_id: str
    card_network: CardNetwork
    reason_category: ChargebackReasonCategory
    chargeback_amount: float

    # Win probability
    base_win_rate: float
    adjusted_win_rate: float              # After evidence adjustments
    expected_outcome: str                 # WIN_LIKELY / COIN_FLIP / LOSS_LIKELY

    # Strategy
    strategy: DefenseStrategy
    ev_contest: float                     # EV of contesting = win_rate × amount - fight_cost
    should_contest: bool

    # CE3.0 (Visa unauthorized only)
    ce30: Optional[CE30Assessment]

    # Documentation
    required_documents: list[str]
    missing_documents: list[str]
    document_completeness: float          # 0-1

    # Action items
    response_recommendation: str
    submission_notes: list[str]
    summary: str


def _resolve_reason_category(inp: ChargebackInput) -> ChargebackReasonCategory:
    if inp.reason_category is not None:
        return inp.reason_category
    if inp.card_network == CardNetwork.VISA and inp.reason_code in _VISA_REASON_CODES:
        return _VISA_REASON_CODES[inp.reason_code]
    # Default to unauthorized (most common, most fraud)
    return ChargebackReasonCategory.UNAUTHORIZED


def _assess_ce30(inp: ChargebackInput, category: ChargebackReasonCategory) -> Optional[CE30Assessment]:
    """Assess Visa CE3.0 eligibility for unauthorized chargebacks."""
    if inp.card_network != CardNetwork.VISA:
        return None
    if category != ChargebackReasonCategory.UNAUTHORIZED:
        return None

    # Need ≥2 non-disputed prior transactions within 120 days
    qualifying = [
        t for t in inp.prior_transactions
        if not t.is_disputed and t.days_before_chargeback <= 120
    ]
    same_device = sum(1 for t in qualifying if t.same_device)
    same_ip = sum(1 for t in qualifying if t.same_ip)

    eligible = len(qualifying) >= 2
    boost = 0.0
    notes: list[str] = []

    if eligible:
        boost = 0.15  # CE3.0 boosts win rate significantly
        notes.append(f"{len(qualifying)} qualifying prior transactions found (CE3.0 eligible)")
        if same_device >= 2:
            boost += 0.05
            notes.append(f"{same_device} transactions matched same device — strong CE3.0 evidence")
        if same_ip >= 2:
            boost += 0.03
            notes.append(f"{same_ip} transactions matched same IP address")
    else:
        if len(qualifying) == 1:
            notes.append("1 qualifying prior transaction found — need ≥2 for CE3.0 (need more data)")
        elif len(qualifying) == 0:
            notes.append("No qualifying prior transactions — CE3.0 not available")
        notes.append("Without CE3.0, unauthorized chargebacks have ~28% win rate")

    return CE30Assessment(
        eligible=eligible,
        qualifying_prior_transactions=len(qualifying),
        same_device_matches=same_device,
        same_ip_matches=same_ip,
        expected_win_rate_boost=round(boost, 3),
        notes=notes,
    )


def _adjusted_win_rate(
    base: float,
    category: ChargebackReasonCategory,
    inp: ChargebackInput,
    ce30: Optional[CE30Assessment],
) -> float:
    rate = base

    # Network adjustment
    rate += _NETWORK_WIN_RATE_ADJ.get(inp.card_network, 0.0)

    # Evidence bonuses
    if inp.has_delivery_confirmation:
        rate += 0.05
    if inp.has_signature_confirmation:
        rate += 0.08
    if inp.has_account_login_history and category == ChargebackReasonCategory.UNAUTHORIZED:
        rate += 0.07
    if inp.has_buyer_communications:
        rate += 0.03
    if not inp.has_tracking and category == ChargebackReasonCategory.ITEM_NOT_RECEIVED:
        rate -= 0.25  # Fatal for INR

    # CE3.0 boost
    if ce30 and ce30.eligible:
        rate += ce30.expected_win_rate_boost

    # Merchant experience
    if inp.merchant_chargeback_win_rate > 0.50:
        rate += 0.05  # Experienced merchant, knows how to fight
    elif inp.merchant_chargeback_win_rate < 0.25:
        rate -= 0.05

    return round(max(0.05, min(0.95, rate)), 3)


def _build_submission_notes(inp: ChargebackInput, category: ChargebackReasonCategory, strategy: DefenseStrategy) -> list[str]:
    notes: list[str] = []
    if strategy == DefenseStrategy.CE30_CONTEST:
        notes.append("SUBMIT CE3.0: Include all prior transactions from same device/IP as separate exhibit")
        notes.append("CE3.0 response must follow Visa's specific format — include transaction dates and amounts")
    if category == ChargebackReasonCategory.UNAUTHORIZED:
        notes.append("Include account login history screenshot from your platform")
    if not inp.has_signature_confirmation and inp.chargeback_amount >= 750:
        notes.append("WARNING: No signature confirmation for high-value order — this significantly weakens defense")
    notes.append("Submit response within 7-10 days (check network's specific deadline)")
    notes.append("Keep all evidence organized — second chargeback (pre-arb) may come if you win")
    return notes


def _missing_documents(category: ChargebackReasonCategory, inp: ChargebackInput) -> list[str]:
    required = _REQUIRED_DOCS.get(category, [])
    missing: list[str] = []
    if not inp.has_tracking and "Tracking number" in " ".join(required):
        missing.append("Tracking number with carrier delivery confirmation")
    if not inp.has_delivery_confirmation and "delivery confirmation" in " ".join(required).lower():
        missing.append("Carrier's official delivery confirmation")
    if not inp.has_signature_confirmation and inp.chargeback_amount >= 750:
        missing.append("Signature confirmation receipt (required for high-value orders)")
    if not inp.has_account_login_history and category == ChargebackReasonCategory.UNAUTHORIZED:
        missing.append("Account login history / device fingerprint logs")
    if not inp.has_buyer_communications:
        missing.append("Buyer communications and responses")
    return missing


def assess_chargeback_defense(inp: ChargebackInput) -> ChargebackDefenseReport:
    """
    Generate chargeback defense strategy for one credit card chargeback.
    """
    category = _resolve_reason_category(inp)
    base_win_rate = _BASE_WIN_RATES.get(category, 0.35)
    ce30 = _assess_ce30(inp, category)

    adj_win_rate = _adjusted_win_rate(base_win_rate, category, inp, ce30)

    ev_contest = round(adj_win_rate * inp.chargeback_amount - inp.fight_cost, 2)
    should_contest = ev_contest > 0

    # Strategy selection
    if not should_contest and category not in (ChargebackReasonCategory.DUPLICATE, ChargebackReasonCategory.PROCESSING_ERROR):
        strategy = DefenseStrategy.ACCEPT
    elif ce30 and ce30.eligible:
        strategy = DefenseStrategy.CE30_CONTEST
    else:
        strategy = DefenseStrategy.CONTEST

    # Expected outcome label
    if adj_win_rate >= 0.60:
        outcome = "WIN_LIKELY"
    elif adj_win_rate >= 0.40:
        outcome = "COIN_FLIP"
    else:
        outcome = "LOSS_LIKELY"

    required_docs = _REQUIRED_DOCS.get(category, [])
    missing_docs = _missing_documents(category, inp)
    completeness = round(1.0 - len(missing_docs) / max(1, len(required_docs)), 2)

    submission_notes = _build_submission_notes(inp, category, strategy)

    if strategy == DefenseStrategy.ACCEPT:
        rec = (
            f"Accept chargeback: EV(contest)=${ev_contest:.0f} — not worth fighting. "
            f"Focus on improving future evidence collection for similar orders."
        )
    elif strategy == DefenseStrategy.CE30_CONTEST:
        rec = (
            f"Use Visa CE3.0: prior transaction evidence boosts win rate to {adj_win_rate:.0%}. "
            f"Submit CE3.0 compelling evidence format with {ce30.qualifying_prior_transactions} qualifying transactions."
        )
    else:
        rec = (
            f"Contest chargeback: {adj_win_rate:.0%} win probability, EV=${ev_contest:+.0f}. "
            f"Submit {len(required_docs)} documents within deadline."
        )

    summary = (
        f"CB {inp.chargeback_id}: {inp.card_network.value} {inp.reason_code} "
        f"(${inp.chargeback_amount:.0f}) | {category.value} | "
        f"WR={adj_win_rate:.0%} | EV=${ev_contest:+.0f} | {strategy.value}"
    )

    return ChargebackDefenseReport(
        chargeback_id=inp.chargeback_id,
        card_network=inp.card_network,
        reason_category=category,
        chargeback_amount=inp.chargeback_amount,
        base_win_rate=base_win_rate,
        adjusted_win_rate=adj_win_rate,
        expected_outcome=outcome,
        strategy=strategy,
        ev_contest=ev_contest,
        should_contest=should_contest,
        ce30=ce30,
        required_documents=required_docs,
        missing_documents=missing_docs,
        document_completeness=completeness,
        response_recommendation=rec,
        submission_notes=submission_notes,
        summary=summary,
    )
