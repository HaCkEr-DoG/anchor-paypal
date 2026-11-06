"""
Buyer Communication Analyzer — analyze buyer messages in a dispute thread
to detect fraud signals, assess hostility, and recommend response tone.

Signal categories:
  1. Urgency / threat signals (refund threats, BBB/bank threats, social media)
  2. Inconsistency signals (story changes, contradictions)
  3. Sophistication signals (knows dispute process well — possible serial disputer)
  4. Cooperation signals (willing to resolve, acknowledges receipt)

Output:
  - FraudSignalLevel: how strongly the messages suggest friendly fraud
  - HostilityLevel: threat level and recommended tone
  - RecommendedTone: FORMAL / FIRM / COOPERATIVE / DE_ESCALATE
  - Key phrases extracted, inconsistency flags, action recommendation

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FraudSignalLevel(str, Enum):
    VERY_HIGH = "VERY_HIGH"   # Multiple strong fraud signals
    HIGH      = "HIGH"        # Clear fraud indicators
    MODERATE  = "MODERATE"    # Mixed — some fraud signals
    LOW       = "LOW"         # Mostly legitimate-sounding
    MINIMAL   = "MINIMAL"     # No fraud signals detected


class HostilityLevel(str, Enum):
    EXTREME      = "EXTREME"     # Explicit threats, abusive language
    HIGH         = "HIGH"        # Demands, ultimatums
    MODERATE     = "MODERATE"    # Frustrated but civil
    LOW          = "LOW"         # Mildly dissatisfied
    COOPERATIVE  = "COOPERATIVE" # Polite, willing to resolve


class RecommendedTone(str, Enum):
    FORMAL       = "FORMAL"       # Neutral, professional distance
    FIRM         = "FIRM"         # Assert position clearly, no apology
    COOPERATIVE  = "COOPERATIVE"  # Meet halfway, offer solutions
    DE_ESCALATE  = "DE_ESCALATE"  # Calm extreme hostility first
    LEGAL        = "LEGAL"        # Threats warrant formal legal tone


# Keyword lists for signal detection (lowercase, partial match)
_URGENCY_KEYWORDS = [
    "immediately", "right now", "within 24 hours", "today",
    "or else", "last chance", "final warning",
]

_THREAT_KEYWORDS = [
    "lawyer", "attorney", "legal action", "sue", "lawsuit",
    "bbb", "better business bureau", "consumer protection",
    "social media", "twitter", "facebook", "post everywhere",
    "bank dispute", "credit card company", "police",
    "fraud report", "ftc", "chargebacks forever",
]

_INCONSISTENCY_SIGNALS = [
    # Phrases that contradict "never received"
    ("never received", "returned it"),
    ("never received", "want to return"),
    ("item broken", "never arrived"),
    ("wrong item", "never received"),
    ("cancelled", "never placed"),
]

_SOPHISTICATION_KEYWORDS = [
    "chargeback", "dispute process", "regulation e", "reg e",
    "provisional credit", "arbitration", "compelling evidence",
    "visa rules", "mastercard rules", "nacha",
    "i know my rights", "i've done this before",
]

_COOPERATION_KEYWORDS = [
    "i understand", "willing to", "can we", "would you",
    "please help", "i appreciate", "i'd like to resolve",
    "open to", "let me know", "happy to provide",
]

_RECEIPT_ACKNOWLEDGEMENT = [
    "i received", "i got the", "it arrived", "it was delivered",
    "i have the item", "i opened it", "i used it",
]


def _count_keywords(text: str, keywords: list[str]) -> int:
    lower = text.lower()
    return sum(1 for kw in keywords if kw in lower)


def _detect_inconsistencies(messages: list[str]) -> list[str]:
    combined = " ".join(m.lower() for m in messages)
    found = []
    for a, b in _INCONSISTENCY_SIGNALS:
        if a in combined and b in combined:
            found.append(f"Contradicting claims: '{a}' vs '{b}'")
    return found


def _score_fraud(
        threat_count: int,
        sophistication_count: int,
        inconsistencies: list[str],
        receipt_ack: bool,
        is_repeat_disputer: bool) -> FraudSignalLevel:
    score = 0
    score += min(threat_count * 5, 25)
    score += min(sophistication_count * 10, 30)
    score += len(inconsistencies) * 15
    if receipt_ack:
        score += 25
    if is_repeat_disputer:
        score += 20

    if score >= 55:
        return FraudSignalLevel.VERY_HIGH
    if score >= 35:
        return FraudSignalLevel.HIGH
    if score >= 15:
        return FraudSignalLevel.MODERATE
    if score >= 5:
        return FraudSignalLevel.LOW
    return FraudSignalLevel.MINIMAL


def _score_hostility(
        threat_count: int,
        urgency_count: int,
        cooperation_count: int) -> HostilityLevel:
    if threat_count >= 3:
        return HostilityLevel.EXTREME
    if threat_count >= 1 or urgency_count >= 3:
        return HostilityLevel.HIGH
    if urgency_count >= 1:
        return HostilityLevel.MODERATE
    if cooperation_count >= 2:
        return HostilityLevel.COOPERATIVE
    return HostilityLevel.LOW


def _recommend_tone(
        hostility: HostilityLevel,
        fraud: FraudSignalLevel) -> RecommendedTone:
    if hostility == HostilityLevel.EXTREME:
        return RecommendedTone.LEGAL
    if hostility == HostilityLevel.HIGH and fraud in (
            FraudSignalLevel.VERY_HIGH, FraudSignalLevel.HIGH):
        return RecommendedTone.FIRM
    if hostility == HostilityLevel.HIGH:
        return RecommendedTone.DE_ESCALATE
    if hostility == HostilityLevel.MODERATE:
        return RecommendedTone.FORMAL
    if hostility == HostilityLevel.COOPERATIVE:
        return RecommendedTone.COOPERATIVE
    # LOW hostility
    if fraud in (FraudSignalLevel.VERY_HIGH, FraudSignalLevel.HIGH):
        return RecommendedTone.FIRM
    return RecommendedTone.COOPERATIVE


@dataclass
class BuyerMessage:
    """A single buyer message in the dispute thread."""
    message_id: str
    text: str
    timestamp_epoch: int
    is_from_buyer: bool = True


@dataclass
class CommunicationInput:
    """Input for buyer communication analysis."""
    dispute_id: str
    messages: list[BuyerMessage] = field(default_factory=list)

    # Context
    is_repeat_disputer: bool = False
    prior_dispute_count: int = 0
    dispute_resolved_prior: bool = False   # Had a prior dispute resolved in buyer's favor
    claim_type: str = "INR"


@dataclass
class CommunicationReport:
    """Full analysis of buyer communications."""
    dispute_id: str
    fraud_signal_level: FraudSignalLevel
    hostility_level: HostilityLevel
    recommended_tone: RecommendedTone

    threat_count: int
    urgency_count: int
    sophistication_count: int
    cooperation_count: int

    buyer_acknowledged_receipt: bool
    inconsistencies: list[str]

    key_fraud_phrases: list[str]
    key_threat_phrases: list[str]

    action_note: str
    summary: str


def analyze_buyer_communication(inp: CommunicationInput) -> CommunicationReport:
    """Analyze all buyer messages and return a fraud/hostility profile."""
    buyer_texts = [m.text for m in inp.messages if m.is_from_buyer]
    combined = " ".join(buyer_texts)
    lower = combined.lower()

    threat_count     = _count_keywords(combined, _THREAT_KEYWORDS)
    urgency_count    = _count_keywords(combined, _URGENCY_KEYWORDS)
    soph_count       = _count_keywords(combined, _SOPHISTICATION_KEYWORDS)
    coop_count       = _count_keywords(combined, _COOPERATION_KEYWORDS)
    receipt_ack      = _count_keywords(combined, _RECEIPT_ACKNOWLEDGEMENT) > 0

    inconsistencies  = _detect_inconsistencies(buyer_texts)

    fraud_level    = _score_fraud(
        threat_count, soph_count, inconsistencies, receipt_ack,
        inp.is_repeat_disputer)
    hostility_level = _score_hostility(threat_count, urgency_count, coop_count)
    tone           = _recommend_tone(hostility_level, fraud_level)

    # Extract notable phrases
    fraud_phrases  = [kw for kw in _SOPHISTICATION_KEYWORDS + _RECEIPT_ACKNOWLEDGEMENT
                      if kw in lower]
    threat_phrases = [kw for kw in _THREAT_KEYWORDS if kw in lower]

    # Action note
    if fraud_level in (FraudSignalLevel.VERY_HIGH, FraudSignalLevel.HIGH):
        action = "Strong fraud signals — escalate to manual review; document all contradictions"
    elif hostility_level in (HostilityLevel.EXTREME, HostilityLevel.HIGH):
        action = "Buyer is hostile — use firm/legal tone; do not concede under pressure"
    elif fraud_level == FraudSignalLevel.MODERATE:
        action = "Some fraud signals — proceed cautiously; request additional evidence from buyer"
    else:
        action = "Routine dispute — standard response tone appropriate"

    summary = (
        f"Comms {inp.dispute_id}: "
        f"fraud={fraud_level.value} | hostility={hostility_level.value} | "
        f"tone={tone.value} | msgs={len(buyer_texts)}"
    )

    return CommunicationReport(
        dispute_id=inp.dispute_id,
        fraud_signal_level=fraud_level,
        hostility_level=hostility_level,
        recommended_tone=tone,
        threat_count=threat_count,
        urgency_count=urgency_count,
        sophistication_count=soph_count,
        cooperation_count=coop_count,
        buyer_acknowledged_receipt=receipt_ack,
        inconsistencies=inconsistencies,
        key_fraud_phrases=fraud_phrases,
        key_threat_phrases=threat_phrases,
        action_note=action,
        summary=summary,
    )
