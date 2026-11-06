"""
Buyer Communication Analyzer — analyze buyer-to-merchant message history
to surface fraud signals, escalation risk, and negotiation leverage.

Signals extracted:
  - Contradiction detection (claimed non-receipt vs. prior acknowledgment)
  - Threat patterns (legal threats, social media threats, refund escalation)
  - Communication velocity (sudden burst of messages)
  - Tone classification (hostile / demanding / reasonable / cooperative)
  - Fraud signal markers (too-common dispute phrases, copy-paste patterns)

Output feeds into win_rate_estimator and dispute_outcome_predictor.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageSender(str, Enum):
    BUYER    = "BUYER"
    MERCHANT = "MERCHANT"
    SYSTEM   = "SYSTEM"


class BuyerTone(str, Enum):
    HOSTILE      = "HOSTILE"       # Threatening, aggressive
    DEMANDING    = "DEMANDING"     # Firm, entitled
    NEUTRAL      = "NEUTRAL"
    COOPERATIVE  = "COOPERATIVE"   # Willing to resolve
    APOLOGETIC   = "APOLOGETIC"    # Possibly legitimate mistake


class FraudSignalLevel(str, Enum):
    VERY_HIGH = "VERY_HIGH"   # Multiple strong fraud markers
    HIGH      = "HIGH"
    MODERATE  = "MODERATE"
    LOW       = "LOW"
    MINIMAL   = "MINIMAL"     # No fraud indicators


class EscalationRisk(str, Enum):
    IMMINENT  = "IMMINENT"    # Will escalate within 24h if unresolved
    HIGH      = "HIGH"
    MODERATE  = "MODERATE"
    LOW       = "LOW"


@dataclass
class MessageRecord:
    """A single message in the conversation thread."""
    sender: MessageSender
    content: str
    timestamp_epoch: int   # Unix timestamp
    message_index: int = 0


@dataclass
class BuyerCommunicationInput:
    """Input: the full message thread for a dispute."""
    dispute_id: str
    messages: list[MessageRecord]

    # Contextual flags (from other oracles or database)
    buyer_prior_disputes: int = 0
    buyer_won_prior: int = 0
    dispute_reason_code: str = "INR"
    transaction_amount: float = 0.0


# ── Signal detection patterns ─────────────────────────────────────

_LEGAL_THREAT_PHRASES = [
    "lawyer", "attorney", "legal action", "sue", "court", "ftc", "bbb",
    "consumer protection", "fraud report", "file a complaint",
]
_SOCIAL_THREAT_PHRASES = [
    "social media", "twitter", "facebook", "instagram", "review",
    "yelp", "trustpilot", "post about", "tell everyone",
]
_DELIVERY_ACK_PHRASES = [
    "received", "got it", "package arrived", "delivered", "picked it up",
    "got the item", "got the package",
]
_FRAUD_SCRIPT_PHRASES = [
    "i never received", "i did not receive", "item not received",
    "never got", "tracking shows delivered but",
    "package was stolen", "not what i ordered",
    "completely different",
]
_HOSTILE_PHRASES = [
    "scam", "fraud", "criminal", "thief", "steal", "rip off",
    "worst company", "disgusting", "unacceptable",
]
_COOPERATIVE_PHRASES = [
    "understand", "happy to", "willing to", "let me know", "please",
    "thank you", "appreciate", "work together",
]


def _contains_any(text: str, phrases: list[str]) -> bool:
    t = text.lower()
    return any(p in t for p in phrases)


def _count_buyer_messages(messages: list[MessageRecord]) -> int:
    return sum(1 for m in messages if m.sender == MessageSender.BUYER)


def _message_burst(messages: list[MessageRecord], window_seconds: int = 3600) -> bool:
    """True if buyer sent 3+ messages within window_seconds."""
    buyer_times = sorted(
        m.timestamp_epoch for m in messages if m.sender == MessageSender.BUYER
    )
    for i in range(len(buyer_times) - 2):
        if buyer_times[i + 2] - buyer_times[i] <= window_seconds:
            return True
    return False


@dataclass
class ContradictionFlag:
    description: str
    evidence_a: str
    evidence_b: str


@dataclass
class SignalFlag:
    signal_type: str
    severity: str    # HIGH / MEDIUM / LOW
    excerpt: str


@dataclass
class BuyerCommunicationReport:
    """Analysis of buyer communication for a dispute."""
    dispute_id: str

    buyer_tone: BuyerTone
    fraud_signal_level: FraudSignalLevel
    escalation_risk: EscalationRisk

    # Specific signals
    legal_threat_detected: bool
    social_media_threat_detected: bool
    delivery_acknowledged_by_buyer: bool
    message_burst_detected: bool

    contradiction_flags: list[ContradictionFlag]
    signal_flags: list[SignalFlag]

    # Counts
    total_messages: int
    buyer_message_count: int
    merchant_message_count: int

    # Negotiation guidance
    negotiation_leverage: str   # What leverage the merchant has
    suggested_response_tone: str

    summary: str


def analyze_buyer_communication(inp: BuyerCommunicationInput) -> BuyerCommunicationReport:
    """Analyze buyer communication for signals and fraud indicators."""
    messages = inp.messages
    buyer_msgs = [m for m in messages if m.sender == MessageSender.BUYER]
    merchant_msgs = [m for m in messages if m.sender == MessageSender.MERCHANT]

    buyer_text_all = " ".join(m.content for m in buyer_msgs)

    # ── Threat detection ─────────────────────────────────────────
    legal_threat = _contains_any(buyer_text_all, _LEGAL_THREAT_PHRASES)
    social_threat = _contains_any(buyer_text_all, _SOCIAL_THREAT_PHRASES)
    delivery_ack = _contains_any(buyer_text_all, _DELIVERY_ACK_PHRASES)
    burst = _message_burst(messages)

    # ── Contradiction detection ───────────────────────────────────
    contradictions: list[ContradictionFlag] = []

    if delivery_ack and inp.dispute_reason_code == "INR":
        # Buyer acknowledged delivery but filed INR — contradiction
        ack_msg = next(
            (m.content[:120] for m in buyer_msgs
             if _contains_any(m.content, _DELIVERY_ACK_PHRASES)),
            "acknowledged delivery in a prior message"
        )
        contradictions.append(ContradictionFlag(
            description="Buyer acknowledged delivery but filed INR dispute",
            evidence_a=ack_msg,
            evidence_b="Dispute reason: Item Not Received (INR)",
        ))

    # ── Signal flags ─────────────────────────────────────────────
    signal_flags: list[SignalFlag] = []

    if legal_threat:
        # Find excerpt
        lt_msg = next(
            (m.content[:100] for m in buyer_msgs
             if _contains_any(m.content, _LEGAL_THREAT_PHRASES)),
            buyer_text_all[:100]
        )
        signal_flags.append(SignalFlag("LEGAL_THREAT", "HIGH", lt_msg))

    if social_threat:
        st_msg = next(
            (m.content[:100] for m in buyer_msgs
             if _contains_any(m.content, _SOCIAL_THREAT_PHRASES)),
            buyer_text_all[:100]
        )
        signal_flags.append(SignalFlag("SOCIAL_MEDIA_THREAT", "MEDIUM", st_msg))

    # Fraud script pattern — buyer using boilerplate dispute phrasing
    fraud_script_count = sum(
        1 for phrase in _FRAUD_SCRIPT_PHRASES
        if phrase in buyer_text_all.lower()
    )
    if fraud_script_count >= 2:
        signal_flags.append(SignalFlag(
            "BOILERPLATE_FRAUD_SCRIPT", "HIGH",
            f"Buyer used {fraud_script_count} common fraud-dispute phrases",
        ))

    if burst:
        signal_flags.append(SignalFlag(
            "MESSAGE_BURST", "MEDIUM",
            "3+ buyer messages within 1 hour — escalation pressure tactic",
        ))

    if inp.buyer_prior_disputes >= 3:
        signal_flags.append(SignalFlag(
            "REPEAT_DISPUTER", "HIGH",
            f"Buyer has {inp.buyer_prior_disputes} prior disputes",
        ))

    if inp.buyer_won_prior >= 2:
        signal_flags.append(SignalFlag(
            "SERIAL_WINNER", "MODERATE",
            f"Buyer has won {inp.buyer_won_prior} prior disputes — may be pattern",
        ))

    # ── Tone classification ───────────────────────────────────────
    hostile_count = sum(
        1 for phrase in _HOSTILE_PHRASES if phrase in buyer_text_all.lower()
    )
    cooperative_count = sum(
        1 for phrase in _COOPERATIVE_PHRASES if phrase in buyer_text_all.lower()
    )

    if hostile_count >= 2 or legal_threat:
        tone = BuyerTone.HOSTILE
    elif hostile_count == 1 or social_threat:
        tone = BuyerTone.DEMANDING
    elif cooperative_count >= 2:
        tone = BuyerTone.COOPERATIVE
    elif len(buyer_msgs) == 0:
        tone = BuyerTone.NEUTRAL
    else:
        tone = BuyerTone.NEUTRAL

    # ── Fraud signal level ────────────────────────────────────────
    high_signals = sum(1 for f in signal_flags if f.severity == "HIGH")
    med_signals  = sum(1 for f in signal_flags if f.severity in ("MEDIUM", "MODERATE"))

    if high_signals >= 2 or len(contradictions) >= 1:
        fraud_level = FraudSignalLevel.VERY_HIGH
    elif high_signals == 1:
        fraud_level = FraudSignalLevel.HIGH
    elif med_signals >= 2:
        fraud_level = FraudSignalLevel.MODERATE
    elif med_signals == 1:
        fraud_level = FraudSignalLevel.LOW
    else:
        fraud_level = FraudSignalLevel.MINIMAL

    # ── Escalation risk ───────────────────────────────────────────
    if legal_threat and tone == BuyerTone.HOSTILE:
        esc_risk = EscalationRisk.IMMINENT
    elif burst or social_threat:
        esc_risk = EscalationRisk.HIGH
    elif tone == BuyerTone.DEMANDING:
        esc_risk = EscalationRisk.MODERATE
    else:
        esc_risk = EscalationRisk.LOW

    # ── Negotiation leverage ──────────────────────────────────────
    leverage_parts = []
    if delivery_ack:
        leverage_parts.append("Buyer previously acknowledged delivery in writing")
    if legal_threat:
        leverage_parts.append("Legal threats often signal awareness of a weak claim")
    if contradictions:
        leverage_parts.append(f"{len(contradictions)} contradiction(s) in buyer messages")
    if inp.buyer_prior_disputes >= 3:
        leverage_parts.append("Buyer dispute history pattern supports fraud flag")
    if not leverage_parts:
        leverage_parts.append("No strong leverage — focus on evidence quality")
    negotiation_leverage = "; ".join(leverage_parts)

    # ── Suggested response tone ───────────────────────────────────
    if tone == BuyerTone.COOPERATIVE:
        suggested = "Acknowledge buyer's concern, present evidence factually, offer resolution"
    elif tone in (BuyerTone.DEMANDING, BuyerTone.HOSTILE):
        suggested = "Professional and factual — do not engage with threats, cite evidence only"
    else:
        suggested = "Professional and factual"

    summary = (
        f"Comms {inp.dispute_id}: "
        f"tone={tone.value} | fraud={fraud_level.value} | "
        f"esc={esc_risk.value} | contradictions={len(contradictions)}"
    )

    return BuyerCommunicationReport(
        dispute_id=inp.dispute_id,
        buyer_tone=tone,
        fraud_signal_level=fraud_level,
        escalation_risk=esc_risk,
        legal_threat_detected=legal_threat,
        social_media_threat_detected=social_threat,
        delivery_acknowledged_by_buyer=delivery_ack,
        message_burst_detected=burst,
        contradiction_flags=contradictions,
        signal_flags=signal_flags,
        total_messages=len(messages),
        buyer_message_count=len(buyer_msgs),
        merchant_message_count=len(merchant_msgs),
        negotiation_leverage=negotiation_leverage,
        suggested_response_tone=suggested,
        summary=summary,
    )
