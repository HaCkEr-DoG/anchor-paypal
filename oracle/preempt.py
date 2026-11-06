"""
Pre-Dispute Interceptor — stop disputes before they're filed.

A dispute that was never filed costs $0. A dispute that was refunded before
filing costs the item amount but no $15 fee, no dispute rate impact, and no
win/loss history effect on the account.

PayPal's research shows that ~40% of disputes begin with the buyer contacting
the merchant directly first. If those contacts are detected and handled
correctly, nearly half of all potential disputes never happen.

This module scans buyer messages for pre-dispute signals and recommends
the optimal response: ignore, respond proactively, offer partial refund,
or trigger a full refund — whichever has the best expected value given
the dispute rate exposure.

Signals detected:
  - Explicit refund requests
  - Item complaints ("not what I expected", "wrong item", "broken")
  - Escalation threats ("I'll contact PayPal", "file a claim", "charge back")
  - INR signals ("hasn't arrived", "where is my order", "tracking shows nothing")
  - Confusion signals that often precede disputes
  - Satisfaction signals (positive intent, confirming receipt)

Output: PreemptResult with risk score, action recommendation, response script,
and EV analysis of each option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PreemptAction(str, Enum):
    IGNORE              = "IGNORE"              # Low risk, no action needed
    RESPOND             = "RESPOND"             # Proactive communication to prevent escalation
    OFFER_PARTIAL       = "OFFER_PARTIAL"       # Partial refund is EV-optimal
    FULL_REFUND         = "FULL_REFUND"         # Full refund cheaper than dispute
    GATHER_TRACKING     = "GATHER_TRACKING"     # Pull tracking and send to buyer immediately
    ESCALATE_INTERNALLY = "ESCALATE_INTERNALLY" # Flag for human review


class SignalType(str, Enum):
    REFUND_REQUEST   = "REFUND_REQUEST"    # Explicit refund ask
    ITEM_COMPLAINT   = "ITEM_COMPLAINT"    # Item not as expected
    INR_SIGNAL       = "INR_SIGNAL"        # Item not received
    ESCALATION_THREAT= "ESCALATION_THREAT" # Threat to involve PayPal/bank
    CONFUSION        = "CONFUSION"         # Confusion that often precedes disputes
    SATISFACTION     = "SATISFACTION"      # Positive signal


@dataclass
class DetectedSignal:
    signal_type: SignalType
    matched_phrase: str
    severity: str           # "low" | "medium" | "high" | "critical"
    weight: float           # How much this increases dispute probability


@dataclass
class PreemptResult:
    dispute_probability: float          # Estimated probability this leads to a dispute
    action: PreemptAction
    detected_signals: list[DetectedSignal] = field(default_factory=list)
    response_script: str = ""           # Ready-to-send response text
    ev_analysis: dict[str, float] = field(default_factory=dict)  # EV of each option
    reasoning: list[str] = field(default_factory=list)
    urgency_hours: int = 72             # How many hours to respond within


# ── Signal patterns ───────────────────────────────────────────────────────────

_SIGNAL_PATTERNS: list[tuple[SignalType, str, float, str]] = [
    # (signal_type, regex_pattern, weight, severity)

    # Critical: explicit escalation threats
    (SignalType.ESCALATION_THREAT, r"\bfile\s+a\s+(dispute|claim|chargeback)\b", 0.45, "critical"),
    (SignalType.ESCALATION_THREAT, r"\bopen\s+a\s+(dispute|case|claim)\b",        0.45, "critical"),
    (SignalType.ESCALATION_THREAT, r"\bcontact\s+paypal\b",                        0.40, "critical"),
    (SignalType.ESCALATION_THREAT, r"\bchargeback\b",                              0.50, "critical"),
    (SignalType.ESCALATION_THREAT, r"\bdispute\s+(this|the|my)\s+(charge|payment|transaction)\b", 0.45, "critical"),
    (SignalType.ESCALATION_THREAT, r"\bmy\s+bank\b",                               0.40, "high"),
    (SignalType.ESCALATION_THREAT, r"\breport\s+(you|this|the\s+seller)\b",        0.35, "high"),

    # High: explicit refund requests
    (SignalType.REFUND_REQUEST, r"\bwant\s+(a\s+)?(full\s+)?refund\b",             0.35, "high"),
    (SignalType.REFUND_REQUEST, r"\bgive\s+me\s+my\s+money\s+back\b",              0.40, "high"),
    (SignalType.REFUND_REQUEST, r"\brefund\s+(me|my|this|the)\b",                  0.30, "high"),
    (SignalType.REFUND_REQUEST, r"\bask(ing)?\s+for\s+a\s+refund\b",              0.30, "high"),
    (SignalType.REFUND_REQUEST, r"\bget\s+my\s+money\s+back\b",                    0.35, "high"),
    (SignalType.REFUND_REQUEST, r"\bwant\s+to\s+return\b",                         0.20, "medium"),

    # High: INR signals
    (SignalType.INR_SIGNAL, r"\bnever\s+(arrived|received|came|got here)\b",        0.35, "high"),
    (SignalType.INR_SIGNAL, r"\bhasn.?t\s+(arrived|come|shown up)\b",              0.30, "high"),
    (SignalType.INR_SIGNAL, r"\bwhere\s+is\s+my\s+(order|package|item)\b",         0.25, "high"),
    (SignalType.INR_SIGNAL, r"\btracking\s+(shows|says|indicates)\s+(nothing|no movement|stuck)\b", 0.30, "high"),
    (SignalType.INR_SIGNAL, r"\b(still|not)\s+(received|delivered)\b",             0.25, "medium"),
    (SignalType.INR_SIGNAL, r"\b(package|order|item)\s+(is\s+)?lost\b",            0.35, "high"),
    (SignalType.INR_SIGNAL, r"\bno\s+tracking\s+update\b",                          0.20, "medium"),

    # Medium: item complaints
    (SignalType.ITEM_COMPLAINT, r"\bnot\s+what\s+i\s+(ordered|expected|paid for)\b", 0.25, "medium"),
    (SignalType.ITEM_COMPLAINT, r"\bwrong\s+(item|product|color|size|model)\b",      0.30, "medium"),
    (SignalType.ITEM_COMPLAINT, r"\b(broken|damaged|defective|not\s+working)\b",     0.25, "medium"),
    (SignalType.ITEM_COMPLAINT, r"\bdoes\s+not\s+match\s+(the\s+)?(listing|description|photo)\b", 0.30, "high"),
    (SignalType.ITEM_COMPLAINT, r"\bcompletely\s+different\b",                       0.30, "high"),
    (SignalType.ITEM_COMPLAINT, r"\bnot\s+as\s+described\b",                         0.30, "high"),
    (SignalType.ITEM_COMPLAINT, r"\bfake\b|\bcounterfeit\b",                         0.40, "high"),

    # Low: confusion (may or may not escalate)
    (SignalType.CONFUSION, r"\bwhen\s+will\s+(it|my\s+(order|package))\s+(arrive|come|ship)\b", 0.10, "low"),
    (SignalType.CONFUSION, r"\bstill\s+waiting\b",                                  0.12, "low"),
    (SignalType.CONFUSION, r"\bany\s+update\b",                                      0.10, "low"),

    # Satisfaction signals (reduce dispute probability)
    (SignalType.SATISFACTION, r"\bthank\s+you\b|\bthanks\b",                        -0.10, "low"),
    (SignalType.SATISFACTION, r"\breceived\s+(it|the\s+(order|package|item))\b",    -0.15, "low"),
    (SignalType.SATISFACTION, r"\blooks\s+(great|good|perfect|amazing)\b",          -0.12, "low"),
    (SignalType.SATISFACTION, r"\bhappy\s+with\b",                                   -0.10, "low"),
]


def scan_message(
    message_text: str,
    *,
    order_amount: float = 0.0,
    days_since_purchase: int = 0,
    buyer_prior_disputes: int = 0,
    dispute_rate_current: float = 0.0,  # Current account dispute rate (0-1)
    dispute_rate_next_threshold: float = 0.01,  # Next PayPal threshold
    seller_hourly_rate: float = 50.0,
    dispute_fee: float = 15.0,
) -> PreemptResult:
    """
    Scan a buyer message for pre-dispute signals and recommend action.

    The EV analysis compares:
      EV(ignore)         = p_dispute × (−dispute_fee − expected_loss)
      EV(respond)        = reduce p_dispute by ~40%; cost = 15 min labor
      EV(partial_refund) = partial_amount + p_dispute_if_partial × loss
      EV(full_refund)    = order_amount (but no fee, no dispute rate impact)
    """
    text_lower = message_text.lower()
    detected: list[DetectedSignal] = []
    reasoning: list[str] = []

    # ── Signal detection ──────────────────────────────────────────────────────
    base_dispute_prob = 0.05  # 5% baseline (some buyers contact without filing)

    # Time context: disputes filed after day 7–9 are often gaming PayPal's window
    if 7 <= days_since_purchase <= 9:
        base_dispute_prob += 0.10
        reasoning.append(f"Day {days_since_purchase}: in peak dispute-filing window (+10%)")

    if buyer_prior_disputes > 0:
        base_dispute_prob += min(buyer_prior_disputes * 0.08, 0.25)
        reasoning.append(f"Buyer has {buyer_prior_disputes} prior dispute(s)")

    total_weight = base_dispute_prob
    for (signal_type, pattern, weight, severity) in _SIGNAL_PATTERNS:
        matches = re.findall(pattern, text_lower, re.IGNORECASE)
        if matches:
            detected.append(DetectedSignal(
                signal_type=signal_type,
                matched_phrase=matches[0] if isinstance(matches[0], str) else " ".join(matches[0]),
                severity=severity,
                weight=weight,
            ))
            total_weight += weight

    dispute_probability = max(0.0, min(0.98, total_weight))

    if detected:
        signal_summary = ", ".join(
            f"{s.signal_type.value}({s.severity})" for s in detected[:5]
        )
        reasoning.append(f"Detected signals: {signal_summary}")

    # ── EV analysis ───────────────────────────────────────────────────────────
    p = dispute_probability
    response_time_cost = seller_hourly_rate * 0.25  # 15 min to respond

    # Dispute rate impact — crossing a threshold is worth modeling
    at_threshold_risk = dispute_rate_current >= (dispute_rate_next_threshold * 0.90)
    threshold_premium = order_amount * 0.08 if at_threshold_risk else 0.0  # 8% revenue held on crossing

    # EV of doing nothing (dispute files at estimated probability)
    expected_dispute_cost = p * (order_amount + dispute_fee + threshold_premium)
    ev_ignore = -expected_dispute_cost

    # EV of proactive response (reduces dispute prob by 40%, costs 15 min)
    p_after_response = p * 0.60
    ev_respond = -(p_after_response * (order_amount + dispute_fee + threshold_premium)) - response_time_cost

    # EV of partial refund (~30% of amount, reduces dispute prob by 70%)
    partial_amount = order_amount * 0.30
    p_after_partial = p * 0.30
    ev_partial = -(partial_amount + p_after_partial * (order_amount + dispute_fee + threshold_premium))

    # EV of full refund (lose the amount, but no fee, no dispute rate damage)
    ev_full_refund = -(order_amount + threshold_premium * 0.3)  # Some threshold risk avoided

    ev_analysis = {
        "ignore": round(ev_ignore, 2),
        "respond": round(ev_respond, 2),
        "partial_refund": round(ev_partial, 2),
        "full_refund": round(ev_full_refund, 2),
    }

    # ── Action recommendation ─────────────────────────────────────────────────
    has_escalation_threat = any(
        s.signal_type == SignalType.ESCALATION_THREAT for s in detected
    )
    has_inr = any(s.signal_type == SignalType.INR_SIGNAL for s in detected)
    has_complaint = any(s.signal_type == SignalType.ITEM_COMPLAINT for s in detected)
    is_satisfied = all(s.signal_type == SignalType.SATISFACTION for s in detected)

    if is_satisfied and not detected:
        action = PreemptAction.IGNORE
        reasoning.append("No risk signals — no action needed")
        urgency_hours = 168  # 7 days
    elif has_escalation_threat:
        # Escalation threat: respond immediately with tracking or refund offer
        if has_inr and order_amount > 0:
            action = PreemptAction.GATHER_TRACKING
            reasoning.append("Escalation threat + INR signal: send tracking immediately")
        elif ev_full_refund > ev_respond and order_amount < 50:
            action = PreemptAction.FULL_REFUND
            reasoning.append(f"Full refund EV={ev_full_refund:.2f} > respond EV={ev_respond:.2f} for small order")
        else:
            action = PreemptAction.RESPOND
            reasoning.append("Escalation threat: proactive response required within 2 hours")
        urgency_hours = 2
    elif has_inr:
        action = PreemptAction.GATHER_TRACKING
        reasoning.append("INR signal: send tracking update immediately")
        urgency_hours = 4
    elif has_complaint:
        if ev_partial > ev_respond:
            action = PreemptAction.OFFER_PARTIAL
            reasoning.append(f"Item complaint: partial refund EV={ev_partial:.2f} > respond EV={ev_respond:.2f}")
        else:
            action = PreemptAction.RESPOND
            reasoning.append("Item complaint: proactive response recommended")
        urgency_hours = 24
    elif dispute_probability > 0.40:
        action = PreemptAction.RESPOND
        reasoning.append(f"High dispute probability {dispute_probability:.0%}: respond proactively")
        urgency_hours = 12
    elif dispute_probability > 0.20:
        action = PreemptAction.RESPOND
        reasoning.append(f"Moderate dispute probability {dispute_probability:.0%}: respond within 24h")
        urgency_hours = 24
    else:
        action = PreemptAction.IGNORE
        reasoning.append(f"Low dispute probability {dispute_probability:.0%}: monitor only")
        urgency_hours = 72

    # ── Response script ───────────────────────────────────────────────────────
    response_script = _build_response_script(action, has_inr, has_complaint, order_amount)

    reasoning.append(
        f"Dispute probability: {dispute_probability:.0%} | "
        f"Best action: {action.value} | "
        f"Respond within: {urgency_hours}h"
    )

    return PreemptResult(
        dispute_probability=round(dispute_probability, 3),
        action=action,
        detected_signals=detected,
        response_script=response_script,
        ev_analysis=ev_analysis,
        reasoning=reasoning,
        urgency_hours=urgency_hours,
    )


def _build_response_script(
    action: PreemptAction,
    has_inr: bool,
    has_complaint: bool,
    order_amount: float,
) -> str:
    if action == PreemptAction.IGNORE:
        return ""

    if action == PreemptAction.GATHER_TRACKING:
        return (
            "Hi [BUYER_NAME],\n\n"
            "Thank you for reaching out. I've looked into your order and here's the "
            "latest tracking information:\n\n"
            "Carrier: [CARRIER]\nTracking: [TRACKING_NUMBER]\nStatus: [TRACKING_STATUS]\n"
            "Tracking link: [TRACKING_URL]\n\n"
            "If the tracking shows the item was delivered but you haven't received it, "
            "please check with neighbors and your local post office, as carriers "
            "sometimes leave packages in an unexpected location.\n\n"
            "Please don't hesitate to reach back out and I'll make sure we get this resolved."
        )

    if action == PreemptAction.OFFER_PARTIAL:
        partial = order_amount * 0.30
        return (
            f"Hi [BUYER_NAME],\n\n"
            f"I'm sorry to hear the item didn't meet your expectations. "
            f"I want to make this right for you.\n\n"
            f"I'd like to offer you a partial refund of ${partial:.2f} as a goodwill "
            f"gesture. If you'd like to return the item, I'm also happy to offer a "
            f"full refund once received.\n\n"
            f"Please let me know which option works best for you."
        )

    if action == PreemptAction.FULL_REFUND:
        return (
            f"Hi [BUYER_NAME],\n\n"
            f"I'm sorry your experience didn't meet expectations. "
            f"I've issued a full refund of ${order_amount:.2f} to your original payment method. "
            f"It typically appears within 3–5 business days.\n\n"
            f"I hope we can serve you again in the future."
        )

    # Default: proactive response
    if has_complaint:
        return (
            "Hi [BUYER_NAME],\n\n"
            "Thank you for letting me know. I want to resolve this for you right away.\n\n"
            "Could you share a photo of the issue so I can fully understand what happened? "
            "Once I see it, I'll get a resolution to you as quickly as possible — "
            "whether that's a replacement, partial refund, or full refund.\n\n"
            "I appreciate your patience and want to make sure you're completely satisfied."
        )

    return (
        "Hi [BUYER_NAME],\n\n"
        "Thank you for reaching out. I want to make sure everything is resolved to your "
        "satisfaction.\n\n"
        "Could you let me know more about the situation? I'm here to help and will "
        "get back to you as quickly as possible.\n\n"
        "Thank you for your patience."
    )


if __name__ == "__main__":
    # Scenario: buyer sends escalation threat
    result = scan_message(
        "I haven't received my order and if I don't hear back I'm going to file a dispute with PayPal",
        order_amount=85.0,
        days_since_purchase=8,
        dispute_rate_current=0.009,
        dispute_rate_next_threshold=0.01,
    )
    print(f"Dispute probability: {result.dispute_probability:.0%}")
    print(f"Action: {result.action.value}")
    print(f"Urgency: respond within {result.urgency_hours}h")
    print(f"EV analysis: {result.ev_analysis}")
    for r in result.reasoning:
        print(f"  → {r}")
    print(f"\nResponse script:\n{result.response_script}")
