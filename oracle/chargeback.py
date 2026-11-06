"""
Chargeback Defense Engine — fight back when buyers go to their bank, not PayPal.

PayPal disputes and credit card chargebacks are DIFFERENT animals:
  - PayPal dispute: resolved inside PayPal, 10-day response window, SP applies
  - Chargeback: initiated at the card network (Visa/MC/Amex/Discover), 20-45 days
                to respond, different evidence requirements, different win conditions

When a buyer files a chargeback, the bank issues a "retrieval request" then a
"chargeback" with a network reason code. Each code has specific evidence
requirements. Responding with the wrong evidence (or the right evidence in the
wrong order) fails even when the merchant has a legitimate case.

Networks and their primary chargeback reason codes covered here:

Visa:
  10.4 — Other Fraud / Card Absent Environment (unauthorized)
  13.1 — Merchandise / Services Not Received
  13.2 — Cancelled Recurring Transaction
  13.3 — Not as Described / Defective
  13.5 — Misrepresentation
  13.6 — Credit Not Processed
  11.1 — Card Recovery Bulletin (card stolen)

Mastercard:
  4853 — Cardholder Dispute (not received / not as described)
  4863 — Cardholder Does Not Recognize (unauthorized)
  4855 — Goods or Services Not Provided
  4860 — Credit Not Processed
  4842 — Late Presentment

American Express:
  C02 — Credit Not Received
  C08 — Goods / Services Not Received
  C31 — Goods / Services Not As Described
  FR2 — Fraud (card present) / Unauthorized

Merchant win rates are significantly lower for chargebacks than PayPal disputes
because the card network is the arbiter — but are still typically 30-55% with
the right evidence package.

Key advantage: some chargebacks are automatically won (pre-arbitration) when
the merchant can show:
  - Transaction authentication (3DS/EMV)
  - Buyer's account credentials used (digital goods, subscription)
  - Delivery to billing address with signature (card-present defense)
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


class ChargebackCategory(str, Enum):
    FRAUD_UNAUTHORIZED  = "FRAUD_UNAUTHORIZED"   # Buyer claims card stolen
    ITEM_NOT_RECEIVED   = "ITEM_NOT_RECEIVED"
    NOT_AS_DESCRIBED    = "NOT_AS_DESCRIBED"
    CANCELLED_RECURRING = "CANCELLED_RECURRING"
    CREDIT_NOT_ISSUED   = "CREDIT_NOT_ISSUED"
    DUPLICATE_CHARGE    = "DUPLICATE_CHARGE"
    UNRECOGNIZED        = "UNRECOGNIZED"         # Buyer doesn't recognize the charge


class ChargebackStrength(str, Enum):
    AUTO_WIN   = "AUTO_WIN"    # 3DS/EMV auth present — network should auto-dismiss
    STRONG     = "STRONG"      # Clear winning evidence, high confidence
    MODERATE   = "MODERATE"    # Good evidence but some gaps
    WEAK       = "WEAK"        # Difficult case
    CONCEDE    = "CONCEDE"     # Concede — EV-negative to fight


@dataclass
class ChargebackRuling:
    category: ChargebackCategory
    network: CardNetwork
    reason_code: str
    strength: ChargebackStrength
    response_deadline_days: int           # Days from chargeback notice to respond
    required_evidence: list[str]          # Must-have for this code
    supporting_evidence: list[str]        # Strongly recommended
    missing_critical: list[str]           # What we don't have yet
    win_probability: float
    ev_fight: float                       # EV of fighting vs conceding
    rebuttal_letter: str                  # Ready-to-submit rebuttal
    auto_win_reason: str = ""             # Why this is auto-win (if applicable)
    reasoning: list[str] = field(default_factory=list)


# Network response deadlines (days from chargeback notice)
_NETWORK_DEADLINES = {
    CardNetwork.VISA:       30,
    CardNetwork.MASTERCARD: 45,
    CardNetwork.AMEX:       20,
    CardNetwork.DISCOVER:   30,
}

# Base win rates by category (without authentication)
_BASE_WIN_RATES = {
    ChargebackCategory.FRAUD_UNAUTHORIZED:  0.28,
    ChargebackCategory.ITEM_NOT_RECEIVED:   0.52,
    ChargebackCategory.NOT_AS_DESCRIBED:    0.38,
    ChargebackCategory.CANCELLED_RECURRING: 0.42,
    ChargebackCategory.CREDIT_NOT_ISSUED:   0.60,  # Easiest to prove
    ChargebackCategory.DUPLICATE_CHARGE:    0.65,
    ChargebackCategory.UNRECOGNIZED:        0.30,
}

# Network-specific reason codes
_REASON_CODES = {
    (CardNetwork.VISA,       ChargebackCategory.FRAUD_UNAUTHORIZED):  "10.4",
    (CardNetwork.VISA,       ChargebackCategory.ITEM_NOT_RECEIVED):   "13.1",
    (CardNetwork.VISA,       ChargebackCategory.CANCELLED_RECURRING): "13.2",
    (CardNetwork.VISA,       ChargebackCategory.NOT_AS_DESCRIBED):    "13.3",
    (CardNetwork.VISA,       ChargebackCategory.CREDIT_NOT_ISSUED):   "13.6",
    (CardNetwork.MASTERCARD, ChargebackCategory.FRAUD_UNAUTHORIZED):  "4863",
    (CardNetwork.MASTERCARD, ChargebackCategory.ITEM_NOT_RECEIVED):   "4855",
    (CardNetwork.MASTERCARD, ChargebackCategory.NOT_AS_DESCRIBED):    "4853",
    (CardNetwork.MASTERCARD, ChargebackCategory.CANCELLED_RECURRING): "4853",
    (CardNetwork.MASTERCARD, ChargebackCategory.CREDIT_NOT_ISSUED):   "4860",
    (CardNetwork.AMEX,       ChargebackCategory.ITEM_NOT_RECEIVED):   "C08",
    (CardNetwork.AMEX,       ChargebackCategory.NOT_AS_DESCRIBED):    "C31",
    (CardNetwork.AMEX,       ChargebackCategory.CREDIT_NOT_ISSUED):   "C02",
    (CardNetwork.AMEX,       ChargebackCategory.FRAUD_UNAUTHORIZED):  "FR2",
    (CardNetwork.DISCOVER,   ChargebackCategory.FRAUD_UNAUTHORIZED):  "UA02",
    (CardNetwork.DISCOVER,   ChargebackCategory.ITEM_NOT_RECEIVED):   "RG",
    (CardNetwork.DISCOVER,   ChargebackCategory.NOT_AS_DESCRIBED):    "RM",
}


def analyze_chargeback(
    category: ChargebackCategory,
    network: CardNetwork,
    dispute_amount: float,
    *,
    # Authentication signals (auto-win factors)
    has_3ds_authentication: bool = False,    # 3D Secure 2.0 auth at checkout
    has_emv_chip_data: bool = False,         # Card-present EMV transaction
    has_avs_match: bool = False,             # Address Verification System match
    has_cvv_match: bool = False,             # CVV matched at time of charge
    # Delivery evidence
    has_tracking_delivered: bool = False,
    shipped_to_billing_address: bool = False,
    has_signature_confirmation: bool = False,
    # Digital goods signals
    has_access_log: bool = False,
    has_login_from_buyer_ip: bool = False,
    # Communication evidence
    has_buyer_communication: bool = False,   # Email/chat showing awareness
    has_cancellation_policy_accepted: bool = False,
    has_no_cancellation_received: bool = False,
    # Refund signals
    already_issued_refund: bool = False,     # Concede immediately
    # Cost model
    dispute_fee: float = 15.0,
    merchant_hourly_rate: float = 50.0,
    response_hours: float = 2.0,
) -> ChargebackRuling:
    """
    Analyze a chargeback and produce an optimal defense strategy.

    Key insight: chargebacks have one killer fact that determines the case —
    for FRAUD it's 3DS auth, for INR it's carrier DELIVERED status, for
    CANCELLED_RECURRING it's proof no cancellation was received. Everything
    else is supporting. Lead with the killer fact in the rebuttal.
    """
    reasoning = []
    missing_critical = []

    deadline_days = _NETWORK_DEADLINES.get(network, 30)
    reason_code = _REASON_CODES.get((network, category), "varies")
    base_win = _BASE_WIN_RATES.get(category, 0.40)
    win_prob = base_win

    # ── Auto-win detection ────────────────────────────────────────────────────
    auto_win_reason = ""

    if has_3ds_authentication and category in (
        ChargebackCategory.FRAUD_UNAUTHORIZED,
        ChargebackCategory.UNRECOGNIZED,
    ):
        auto_win_reason = (
            f"3D Secure authentication shifts liability to the issuing bank. "
            f"Under {network.value} rules, authenticated transactions cannot be "
            f"successfully charged back for unauthorized use. Submit the 3DS auth "
            f"record and the network should dismiss pre-arbitration."
        )
        win_prob = 0.90
        reasoning.append("AUTO-WIN: 3DS auth present — liability shifts to issuer")

    elif has_emv_chip_data and category in (
        ChargebackCategory.FRAUD_UNAUTHORIZED,
        ChargebackCategory.UNRECOGNIZED,
    ):
        auto_win_reason = (
            "EMV chip transaction data shifts fraud liability to the card issuer. "
            "The chip cryptogram proves the physical card was present and authenticated."
        )
        win_prob = 0.85
        reasoning.append("AUTO-WIN candidate: EMV chip data shifts liability")

    # ── Evidence scoring by category ─────────────────────────────────────────
    required_evidence = []
    supporting_evidence = []

    if category == ChargebackCategory.FRAUD_UNAUTHORIZED:
        required_evidence = [
            "Transaction record with IP address and device fingerprint",
            "AVS/CVV match confirmation from payment processor",
            "Order confirmation email sent to buyer's address",
        ]
        supporting_evidence = [
            "Shipping tracking showing DELIVERED to billing address",
            "Buyer's login activity showing account access after purchase",
            "3DS authentication record (auto-wins the chargeback if present)",
        ]

        if has_3ds_authentication or has_emv_chip_data:
            win_prob = max(win_prob, 0.88)
        elif has_avs_match and has_cvv_match:
            win_prob += 0.10
            reasoning.append("AVS + CVV match: strong card-present signal")
        elif has_buyer_communication:
            win_prob += 0.08
            reasoning.append("Buyer communication post-purchase: aware of transaction")
        else:
            missing_critical.append("transaction_authentication_proof")
            win_prob = max(0.15, win_prob - 0.10)

        if has_tracking_delivered and shipped_to_billing_address:
            win_prob += 0.05

    elif category == ChargebackCategory.ITEM_NOT_RECEIVED:
        required_evidence = [
            "Carrier tracking showing DELIVERED status with delivery date",
            "Proof delivery address matches billing address on card",
        ]
        supporting_evidence = [
            "Signature confirmation (required for >$750 on most networks)",
            "GPS delivery photo from carrier (USPS, FedEx often include)",
            "Delivery notification email sent to buyer",
        ]

        if has_tracking_delivered:
            win_prob += 0.20
            reasoning.append("DELIVERED status: decisive for INR chargeback")
            if shipped_to_billing_address:
                win_prob += 0.05
                reasoning.append("Shipped to billing address: network requirement met")
            if has_signature_confirmation and dispute_amount > 500:
                win_prob += 0.05
                reasoning.append("Signature confirmation on high-value item")
        else:
            missing_critical.append("carrier_delivered_confirmation")
            win_prob = max(0.10, win_prob - 0.20)
            reasoning.append("No DELIVERED status: weak position for INR chargeback")

        if has_access_log:  # Digital goods
            win_prob += 0.10
            reasoning.append("Access log: buyer used product (digital INR)")

    elif category == ChargebackCategory.NOT_AS_DESCRIBED:
        required_evidence = [
            "Original product listing as it appeared at time of sale",
            "Photos of item shipped with measurements/specifications",
            "Side-by-side comparison: listing description vs. item shipped",
        ]
        supporting_evidence = [
            "Previous buyer reviews for identical item",
            "Return policy that was available but not used",
            "Buyer communication before dispute was filed",
        ]
        # Hardest to auto-win — subjective
        if has_buyer_communication:
            win_prob += 0.08
        else:
            missing_critical.append("original_listing_proof")

    elif category == ChargebackCategory.CANCELLED_RECURRING:
        required_evidence = [
            "Terms of Service with recurring billing disclosure buyer agreed to",
            "Record showing no cancellation request was received before charge",
            "Billing dates/amounts matching what buyer agreed to",
        ]
        supporting_evidence = [
            "Login activity during the cancelled billing period",
            "Service usage logs during the billing period",
            "Email confirmation of subscription sent to buyer",
        ]

        if has_cancellation_policy_accepted:
            win_prob += 0.12
            reasoning.append("Cancellation policy acceptance: buyer agreed to terms")
        else:
            missing_critical.append("tos_with_billing_acceptance")

        if has_no_cancellation_received:
            win_prob += 0.10
            reasoning.append("No cancellation on record before charge date")
        else:
            missing_critical.append("cancellation_log")

    elif category == ChargebackCategory.CREDIT_NOT_ISSUED:
        required_evidence = [
            "Proof refund was issued: processor confirmation with date and amount",
            "Refund transaction ID from your payment processor",
        ]
        supporting_evidence = [
            "Screenshot of refund in your merchant dashboard",
            "Email to buyer confirming refund was issued",
        ]
        if already_issued_refund:
            win_prob = 0.92
            reasoning.append("Refund already issued: credit_not_issued chargeback is frivolous")
        else:
            missing_critical.append("refund_processor_confirmation")

    elif category == ChargebackCategory.DUPLICATE_CHARGE:
        required_evidence = [
            "Transaction log showing one charge per order (not duplicated)",
            "Authorization and capture records for each transaction",
        ]
        supporting_evidence = ["Distinct order IDs for each charge if multiple"]
        win_prob = 0.70  # Usually easy to prove with processor records

    # ── EV calculation ────────────────────────────────────────────────────────
    if already_issued_refund and category != ChargebackCategory.CREDIT_NOT_ISSUED:
        # Already refunded — still need to fight chargeback fee
        ev_fight = -dispute_fee  # Can't win on amount, only avoid fee
        strength = ChargebackStrength.WEAK
        reasoning.append("Already refunded — fight only to avoid double-loss")
    else:
        response_cost = merchant_hourly_rate * response_hours
        ev_fight = (win_prob * dispute_amount) - response_cost - dispute_fee

    # Cap win probability
    win_prob = min(win_prob, 0.93)

    # ── Strength assessment ───────────────────────────────────────────────────
    if auto_win_reason:
        strength = ChargebackStrength.AUTO_WIN
    elif win_prob >= 0.65:
        strength = ChargebackStrength.STRONG
    elif win_prob >= 0.45:
        strength = ChargebackStrength.MODERATE
    elif win_prob >= 0.28:
        strength = ChargebackStrength.WEAK
    else:
        strength = ChargebackStrength.CONCEDE

    if ev_fight < -dispute_fee and strength == ChargebackStrength.WEAK:
        strength = ChargebackStrength.CONCEDE
        reasoning.append(f"EV({ev_fight:.2f}) < 0 — conceding is cheaper")

    # ── Build rebuttal letter ─────────────────────────────────────────────────
    rebuttal = _build_rebuttal(
        category, network, reason_code, dispute_amount,
        auto_win_reason, required_evidence, missing_critical,
        has_3ds_authentication, has_tracking_delivered,
        has_buyer_communication, already_issued_refund,
    )

    return ChargebackRuling(
        category=category,
        network=network,
        reason_code=reason_code,
        strength=strength,
        response_deadline_days=deadline_days,
        required_evidence=required_evidence,
        supporting_evidence=supporting_evidence,
        missing_critical=missing_critical,
        win_probability=round(win_prob, 3),
        ev_fight=round(ev_fight, 2),
        rebuttal_letter=rebuttal,
        auto_win_reason=auto_win_reason,
        reasoning=reasoning,
    )


def _build_rebuttal(
    category: ChargebackCategory,
    network: CardNetwork,
    reason_code: str,
    amount: float,
    auto_win_reason: str,
    required_evidence: list[str],
    missing_critical: list[str],
    has_3ds: bool,
    has_delivered: bool,
    has_communication: bool,
    already_refunded: bool,
) -> str:
    opener = (
        f"RE: Chargeback Rebuttal — {network.value} Reason Code {reason_code} — "
        f"${amount:.2f}\n\n"
        "We are responding to the above chargeback. Our records clearly demonstrate "
        "that this transaction was legitimate and fulfilled as agreed. "
        "The following evidence is attached in support of our position.\n\n"
    )

    if auto_win_reason:
        body = (
            f"AUTHENTICATION: {auto_win_reason}\n\n"
            "Under the applicable network rules, the presence of this authentication "
            "shifts chargeback liability to the issuing bank. We respectfully request "
            "that this chargeback be dismissed as a pre-arbitration matter."
        )
    elif category == ChargebackCategory.ITEM_NOT_RECEIVED and has_delivered:
        body = (
            "DELIVERY CONFIRMATION: The item was delivered to the cardholder's address. "
            "Attached is carrier tracking confirmation showing DELIVERED status, "
            "delivery date, and delivery address. The delivery address matches the "
            "billing address on the card at time of purchase."
        )
    elif category == ChargebackCategory.CREDIT_NOT_ISSUED and already_refunded:
        body = (
            "CREDIT ALREADY ISSUED: A full refund was processed prior to this chargeback. "
            "Attached is the processor confirmation showing the refund transaction ID, "
            "date, and amount. This chargeback is duplicative of a refund already received."
        )
    elif category == ChargebackCategory.CANCELLED_RECURRING:
        body = (
            "SUBSCRIPTION TERMS: The cardholder agreed to recurring billing terms at signup. "
            "No cancellation request was received prior to the disputed charge. "
            "Terms of service and billing schedule accepted by the cardholder are attached."
        )
    else:
        body = (
            "FULFILLMENT PROOF: The transaction was processed in good faith and the goods/"
            "services were delivered as described. Evidence of fulfillment is attached."
        )

    evidence_list = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(required_evidence))
    footer = (
        f"\n\nAttached evidence:\n{evidence_list}\n\n"
        "We respectfully request that this chargeback be reversed in our favor "
        "based on the evidence presented."
    )

    return opener + body + footer


if __name__ == "__main__":
    # Scenario: FRAUD chargeback with 3DS auth — should auto-win
    result = analyze_chargeback(
        ChargebackCategory.FRAUD_UNAUTHORIZED,
        CardNetwork.VISA,
        dispute_amount=320.0,
        has_3ds_authentication=True,
        has_avs_match=True,
        has_cvv_match=True,
    )
    print(f"Strength: {result.strength.value}")
    print(f"Win probability: {result.win_probability:.0%}")
    print(f"Reason code: {result.reason_code}")
    print(f"Auto-win: {result.auto_win_reason[:80] if result.auto_win_reason else 'No'}")
    for r in result.reasoning:
        print(f"  → {r}")
