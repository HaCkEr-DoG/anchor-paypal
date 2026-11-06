"""
Pre-Chargeback Alert Interceptor — Ethoca/Verifi parity, automated.

The gap this closes: Ethoca and Verifi send card-network alerts BEFORE a formal
chargeback is filed. Merchants who respond within the alert window (~24–72h) can
refund and prevent the chargeback from hitting their count at all. Missing this
window costs: (a) the chargeback itself, (b) the $20–$100 alert fee, and
(c) one chargeback count toward the dispute rate threshold.

No competitor automates this decision. Chargebacks911 has humans review alerts.
Ethoca/Verifi just relay the alert — they don't tell you what to do.

Anchor's decision engine:
  - If EV(refund_now) > EV(fight_later): REFUND_NOW + void the alert
  - If EV(fight_later) > EV(refund_now): IGNORE + prepare evidence now
  - If signals indicate friendly fraud: ESCALATE (flag + fight)

Key math:
  EV(refund_now)   = -(dispute_amount) but: saves alert_fee + chargeback_count_cost
  EV(fight_later)  = P(win) × amount − dispute_fee − fight_cost
  Count_cost       = HDR risk contribution (amortized across rate thresholds)

Alert networks supported:
  - Ethoca (Mastercard) — "RDR" (Rapid Dispute Resolution) alerts
  - Verifi (Visa)       — "CDRN" (Compelling Evidence Dispute Resolution Network)
  - Direct bank alerts  — some issuers send their own pre-dispute signals

This module is pure computation — no I/O. The integration layer (agents/watcher.py
or a webhook handler) calls intercept_alert() with the parsed payload and executes
the returned action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AlertNetwork(str, Enum):
    ETHOCA    = "ETHOCA"    # Mastercard RDR
    VERIFI    = "VERIFI"    # Visa CDRN
    DIRECT    = "DIRECT"    # Direct issuer alert
    UNKNOWN   = "UNKNOWN"


class AlertReasonCode(str, Enum):
    ITEM_NOT_RECEIVED          = "ITEM_NOT_RECEIVED"
    NOT_AS_DESCRIBED           = "NOT_AS_DESCRIBED"
    UNAUTHORIZED               = "UNAUTHORIZED"
    CREDIT_NOT_PROCESSED       = "CREDIT_NOT_PROCESSED"
    DUPLICATE_TRANSACTION      = "DUPLICATE_TRANSACTION"
    GENERAL_DISPUTE            = "GENERAL_DISPUTE"


class AlertAction(str, Enum):
    REFUND_NOW     = "REFUND_NOW"      # Refund immediately, void the chargeback
    FIGHT_LATER    = "FIGHT_LATER"     # Ignore alert, fight if chargeback is filed
    PARTIAL_REFUND = "PARTIAL_REFUND"  # Offer partial to prevent escalation
    ESCALATE       = "ESCALATE"        # Fraud signal — flag buyer, fight aggressively


@dataclass
class AlertPayload:
    """Incoming pre-chargeback alert from Ethoca, Verifi, or a bank."""
    alert_id: str
    network: AlertNetwork
    reason_code: AlertReasonCode
    dispute_amount: float
    transaction_id: str                # Original PayPal transaction ID
    card_last4: str = ""
    response_deadline_hours: int = 24  # Hours to respond before it escalates to chargeback
    # Context signals (enriched by watcher before calling this module)
    has_delivery_confirmation: bool = False
    item_delivered: bool = False       # Carrier confirmed delivery
    buyer_dispute_rate: float = 0.0    # From buyer_intelligence
    buyer_risk_tier: str = "TIER_1_NORMAL"
    dispute_filed_before: bool = False # Same buyer has filed before with us
    is_digital_goods: bool = False
    digital_goods_accessed: bool = False
    merchant_dispute_rate: float = 0.0  # Current merchant HDR rate (0.0–0.05)
    alert_fee: float = 25.0            # Fee for receiving this alert (Ethoca/Verifi charge)


@dataclass
class AlertInterceptionResult:
    """Decision and execution instructions for one pre-chargeback alert."""
    alert_id: str
    action: AlertAction
    refund_amount: float               # 0 if FIGHT_LATER; full or partial amount
    should_void_transaction: bool      # True only for REFUND_NOW + same-day transactions
    priority: str                      # "URGENT" / "HIGH" / "NORMAL"
    deadline_hours: int                # Time left to act
    ev_refund_now: float               # EV of refunding (negative = cost)
    ev_fight_later: float              # EV of fighting the eventual chargeback
    net_saving_vs_fight: float         # How much refunding now saves vs fighting later
    evidence_to_gather: list[str]      # If FIGHT_LATER: gather these NOW
    response_message: str              # What to send back to the alert network
    reasoning: list[str] = field(default_factory=list)


# HDR rate thresholds — crossing these triggers PayPal program restrictions
_HDR_THRESHOLDS = [
    (0.0150, 30.0),   # 1.50%: $30/dispute HDR fee kicks in
    (0.0100, 10.0),   # 1.00%: Limited processing
    (0.0050, 5.0),    # 0.50%: Elevated monitoring
]

# Base win probabilities when a chargeback is eventually filed (conservative vs Oracle)
_BASE_WIN_RATES = {
    AlertReasonCode.ITEM_NOT_RECEIVED:    0.65,
    AlertReasonCode.NOT_AS_DESCRIBED:     0.35,
    AlertReasonCode.UNAUTHORIZED:         0.08,
    AlertReasonCode.CREDIT_NOT_PROCESSED: 0.70,
    AlertReasonCode.DUPLICATE_TRANSACTION: 0.80,
    AlertReasonCode.GENERAL_DISPUTE:      0.45,
}

_CHARGEBACK_FEE = 20.0   # Average chargeback processing fee (on top of dispute amount)
_FIGHT_COST     = 75.0   # Merchant time + evidence preparation


def intercept_alert(alert: AlertPayload) -> AlertInterceptionResult:
    """
    Decide the optimal response to a pre-chargeback alert.

    Decision: REFUND_NOW if EV(refund) > EV(fight), otherwise FIGHT_LATER.
    Special cases: delivery confirmed → FIGHT_LATER regardless of EV;
                   fraud signals → ESCALATE regardless of EV.
    """
    reasoning: list[str] = []

    # ── 1. Fraud fast-path ────────────────────────────────────────────────────
    if alert.buyer_risk_tier in ("TIER_4_SERIAL", "TIER_5_FRAUD"):
        return _build_result(
            alert, AlertAction.ESCALATE,
            refund=0.0,
            ev_refund=-alert.dispute_amount,
            ev_fight=_ev_fight(alert, 0.80),
            reasoning=[
                f"Buyer risk tier {alert.buyer_risk_tier} — escalate, do not refund",
                "Serial/fraud buyers should not receive refunds as it rewards the behavior",
            ],
        )

    if alert.is_digital_goods and alert.digital_goods_accessed:
        return _build_result(
            alert, AlertAction.ESCALATE,
            refund=0.0,
            ev_refund=-alert.dispute_amount,
            ev_fight=_ev_fight(alert, 0.85),
            reasoning=[
                "Digital goods accessed before alert — friendly fraud signal",
                "Fight with access logs as evidence",
            ],
        )

    # ── 2. Delivery confirmed + INR → fight ───────────────────────────────────
    if alert.item_delivered and alert.has_delivery_confirmation and \
       alert.reason_code == AlertReasonCode.ITEM_NOT_RECEIVED:
        ev_f = _ev_fight(alert, 0.80)
        return _build_result(
            alert, AlertAction.FIGHT_LATER,
            refund=0.0,
            ev_refund=-alert.dispute_amount,
            ev_fight=ev_f,
            reasoning=[
                "Item confirmed delivered with carrier proof — fight the INR claim",
                "Delivery confirmation gives 80%+ win probability",
                "Evidence: carrier delivery scan + tracking URL",
            ],
            evidence=[
                "Save carrier delivery confirmation screenshot NOW",
                "Save full tracking history (all scan events)",
                "Save PayPal transaction detail + buyer's confirmed shipping address",
            ],
        )

    # ── 3. EV calculation ─────────────────────────────────────────────────────
    base_win_prob = _BASE_WIN_RATES.get(alert.reason_code, 0.45)

    # Adjust win probability for buyer signals
    if alert.has_delivery_confirmation:
        base_win_prob = min(base_win_prob + 0.20, 0.90)
        reasoning.append(f"Win prob +20% for delivery confirmation → {base_win_prob:.0%}")
    if alert.buyer_dispute_rate > 0.08:
        base_win_prob = min(base_win_prob + 0.15, 0.90)
        reasoning.append(f"Win prob +15% for high buyer dispute rate ({alert.buyer_dispute_rate:.0%})")
    if alert.dispute_filed_before:
        base_win_prob = min(base_win_prob + 0.08, 0.90)
        reasoning.append("Win prob +8% for repeat disputer")

    ev_fight_val = _ev_fight(alert, base_win_prob)

    # EV(refund_now): cost is dispute_amount, but we save:
    #   - alert fee avoidance IF we refund (some networks void the fee on refund)
    #   - chargeback count cost (HDR rate impact)
    #   - chargeback fee
    hdr_count_cost = _hdr_count_cost(alert.merchant_dispute_rate, alert.dispute_amount)
    ev_refund = -(alert.dispute_amount) + alert.alert_fee + _CHARGEBACK_FEE + hdr_count_cost
    reasoning.append(
        f"EV(refund_now) = -${alert.dispute_amount:.2f} + ${alert.alert_fee:.0f}(alert_fee_saved) "
        f"+ ${_CHARGEBACK_FEE:.0f}(chargeback_fee_saved) + ${hdr_count_cost:.2f}(rate_impact_saved) "
        f"= ${ev_refund:.2f}"
    )
    reasoning.append(
        f"EV(fight_later) = {base_win_prob:.0%} × ${alert.dispute_amount:.2f} "
        f"− ${_CHARGEBACK_FEE:.0f} − ${_FIGHT_COST:.0f} = ${ev_fight_val:.2f}"
    )

    net_saving = ev_refund - ev_fight_val
    reasoning.append(f"Net saving from refunding now vs fighting: ${net_saving:.2f}")

    # ── 4. Decision ───────────────────────────────────────────────────────────
    if ev_refund > ev_fight_val:
        # Also check: is a partial refund better than full?
        if alert.reason_code == AlertReasonCode.NOT_AS_DESCRIBED and alert.dispute_amount > 50:
            partial_amount = alert.dispute_amount * 0.50
            partial_ev = -(partial_amount) + alert.alert_fee * 0.5 + _CHARGEBACK_FEE
            if partial_ev > ev_fight_val:
                action = AlertAction.PARTIAL_REFUND
                refund = round(partial_amount, 2)
                reasoning.append(
                    f"SNAD: partial refund (50% = ${refund:.2f}) is EV-better than full refund"
                )
            else:
                action = AlertAction.REFUND_NOW
                refund = alert.dispute_amount
        else:
            action = AlertAction.REFUND_NOW
            refund = alert.dispute_amount
            reasoning.append("Full refund is EV-optimal: saves more in fees + rate impact than fighting costs")

        evidence = []

    else:
        action = AlertAction.FIGHT_LATER
        refund = 0.0
        reasoning.append("Fighting expected to yield better EV than refunding now")
        evidence = _evidence_for_reason(alert.reason_code)

    return _build_result(
        alert, action,
        refund=refund,
        ev_refund=ev_refund,
        ev_fight=ev_fight_val,
        reasoning=reasoning,
        evidence=evidence,
    )


def _ev_fight(alert: AlertPayload, win_prob: float) -> float:
    return round(win_prob * alert.dispute_amount - _CHARGEBACK_FEE - _FIGHT_COST, 2)


def _hdr_count_cost(current_rate: float, dispute_amount: float) -> float:
    """
    Amortized cost of one more chargeback count toward HDR thresholds.

    If merchant is near a threshold, each additional chargeback has amplified cost
    (e.g., crossing 1.50% adds $30/dispute on ALL disputes, not just the new one).
    We model this as: how much does crossing the next threshold cost amortized
    over the next 100 disputes?
    """
    for threshold, fee_per_dispute in _HDR_THRESHOLDS:
        if current_rate >= threshold * 0.85:  # Within 15% of threshold
            # This dispute might push us over — model cost as: fee × 100 / 100 = fee
            # (simplified: one count amortized over a 100-dispute forward window)
            return fee_per_dispute * 2.5  # Buffer multiplier for threshold proximity
    return 5.0  # Baseline: even non-HDR disputes have soft rate cost


def _evidence_for_reason(reason: AlertReasonCode) -> list[str]:
    base = ["Save full transaction history with timestamps", "Save order confirmation email"]
    if reason == AlertReasonCode.ITEM_NOT_RECEIVED:
        return base + [
            "Capture carrier tracking with ALL scan events",
            "Screenshot delivery confirmation (timestamp visible)",
            "Save PayPal buyer's confirmed shipping address",
        ]
    elif reason == AlertReasonCode.NOT_AS_DESCRIBED:
        return base + [
            "Save original product listing / description screenshots",
            "Save packing photos (item + label)",
            "Screenshot any buyer messages acknowledging receipt",
        ]
    elif reason == AlertReasonCode.UNAUTHORIZED:
        return base + [
            "Save IP address and device fingerprint for original transaction",
            "Save 3DS authentication confirmation if available",
            "Check for account-takeover signals in login history",
        ]
    return base


def _build_result(
    alert: AlertPayload,
    action: AlertAction,
    *,
    refund: float,
    ev_refund: float,
    ev_fight: float,
    reasoning: list[str],
    evidence: list[str] | None = None,
) -> AlertInterceptionResult:
    net_saving = round(ev_refund - ev_fight, 2)

    priority = "URGENT" if alert.response_deadline_hours <= 12 else \
               "HIGH"   if alert.response_deadline_hours <= 24 else "NORMAL"

    if action == AlertAction.REFUND_NOW:
        response_msg = (
            f"Merchant acknowledges alert {alert.alert_id}. "
            f"Full refund of ${refund:.2f} is being processed immediately."
        )
    elif action == AlertAction.PARTIAL_REFUND:
        response_msg = (
            f"Merchant acknowledges alert {alert.alert_id}. "
            f"Partial refund of ${refund:.2f} (50%) is being processed."
        )
    elif action == AlertAction.FIGHT_LATER:
        response_msg = (
            f"Merchant acknowledges alert {alert.alert_id}. "
            f"Transaction is under review. No refund will be issued at this time."
        )
    else:
        response_msg = (
            f"Merchant acknowledges alert {alert.alert_id}. "
            f"This alert has been flagged for fraud review."
        )

    return AlertInterceptionResult(
        alert_id=alert.alert_id,
        action=action,
        refund_amount=round(refund, 2),
        should_void_transaction=(action == AlertAction.REFUND_NOW and refund > 0),
        priority=priority,
        deadline_hours=alert.response_deadline_hours,
        ev_refund_now=round(ev_refund, 2),
        ev_fight_later=round(ev_fight, 2),
        net_saving_vs_fight=net_saving,
        evidence_to_gather=evidence or [],
        response_message=response_msg,
        reasoning=reasoning,
    )


if __name__ == "__main__":
    # Scenario 1: $85 INR, no delivery proof, low-risk buyer, merchant near HDR threshold
    alert = AlertPayload(
        alert_id="ETH-2024-001",
        network=AlertNetwork.ETHOCA,
        reason_code=AlertReasonCode.ITEM_NOT_RECEIVED,
        dispute_amount=85.0,
        transaction_id="TXID-123456",
        has_delivery_confirmation=False,
        item_delivered=False,
        buyer_risk_tier="TIER_1_NORMAL",
        merchant_dispute_rate=0.013,  # Near 1.5% HDR threshold
        alert_fee=25.0,
        response_deadline_hours=20,
    )
    result = intercept_alert(alert)
    print(f"Alert: {result.alert_id}")
    print(f"Action: {result.action.value}  |  Refund: ${result.refund_amount:.2f}")
    print(f"Priority: {result.priority}  |  Deadline: {result.deadline_hours}h")
    print(f"EV(refund_now): ${result.ev_refund_now:.2f}")
    print(f"EV(fight_later): ${result.ev_fight_later:.2f}")
    print(f"Net saving by refunding: ${result.net_saving_vs_fight:.2f}")
    print(f"\nResponse: {result.response_message}")
    print("\nReasoning:")
    for r in result.reasoning:
        print(f"  → {r}")
