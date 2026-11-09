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


# ---------------------------------------------------------------------------
# Wave37 / test-API — BuyerMessage dataclass, CommunicationInput, new report fields
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dc4, field as _f4
from enum import Enum as _Enum4


@_dc4
class BuyerMessage:
    message_id: str = ""
    text: str = ""
    timestamp_epoch: int = 0
    is_from_buyer: bool = True


class HostilityLevel(str, _Enum4):
    COOPERATIVE = "COOPERATIVE"
    LOW         = "LOW"
    MODERATE    = "MODERATE"
    HIGH        = "HIGH"
    EXTREME     = "EXTREME"


class RecommendedTone(str, _Enum4):
    COOPERATIVE = "COOPERATIVE"
    FORMAL      = "FORMAL"
    FIRM        = "FIRM"
    LEGAL       = "LEGAL"


@_dc4
class CommunicationInput:
    dispute_id: str = "D-001"
    claim_type: str = "INR"
    messages: list = _f4(default_factory=list)   # list[BuyerMessage]
    is_repeat_disputer: bool = False
    buyer_prior_disputes: int = 0


_LEGAL_THREAT_KW   = ["lawyer", "attorney", "legal action", "sue", "court", "ftc",
                       "consumer protection", "fraud report", "file a complaint", "police"]
_SOCIAL_THREAT_KW  = ["bbb", "better business", "social media", "twitter", "facebook",
                       "instagram", "yelp", "trustpilot", "post about", "tell everyone"]
_HOSTILE_KW        = ["scam", "fraud", "criminal", "thief", "steal", "rip off",
                       "worst", "disgusting", "unacceptable"]
_URGENT_KW         = ["immediately", "asap", "right now", "today", "urgent", "immediately",
                       "demand", "must", "now"]
_SOPHIST_KW        = ["chargeback", "regulation e", "reg e", "provisional credit",
                       "consumer rights", "arbitration", "fdcpa", "cfpb", "done it before",
                       "done this before", "know the process", "know the rules"]
_COOPERATIVE_KW    = ["understand", "happy to", "willing to", "let me know", "please",
                       "thank you", "appreciate", "work together", "amicably", "open to",
                       "wait", "help"]
_RECEIPT_ACK_KW    = ["received the package", "got the package", "package arrived",
                       "received it", "got the item", "i received the", "got it"]
_NON_DELIVERY_KW   = ["never received", "did not receive", "not received", "never got",
                       "i never got", "item not received", "didn't receive"]
_BOILERPLATE_PHRASES = [
    "i never received", "i did not receive", "item not received",
    "never got", "tracking shows delivered but",
    "package was stolen", "not what i ordered",
]
_WANTS_RETURN_KW   = ["want to return", "return it", "doesn't fit", "wrong size",
                       "not what i expected", "changed my mind"]


def _has(text: str, phrases: list) -> bool:
    t = text.lower()
    return any(p in t for p in phrases)


def _extract(text: str, phrases: list) -> list:
    t = text.lower()
    return [p for p in phrases if p in t]


def analyze_buyer_communication(inp):
    """Analyze buyer communication (wave37 test-API)."""
    if isinstance(inp, BuyerCommunicationInput):
        # Legacy path — convert to new format
        messages = [
            BuyerMessage(
                text=m.content,
                timestamp_epoch=m.timestamp_epoch,
                is_from_buyer=(m.sender == MessageSender.BUYER),
            )
            for m in inp.messages
        ]
        ci = CommunicationInput(
            dispute_id=inp.dispute_id,
            claim_type=inp.dispute_reason_code,
            messages=messages,
            is_repeat_disputer=(inp.buyer_prior_disputes >= 3),
            buyer_prior_disputes=inp.buyer_prior_disputes,
        )
    else:
        ci = inp

    all_text = " ".join(m.text for m in ci.messages if m.is_from_buyer)
    n = len(ci.messages)

    # Signal counts
    threat_phrases = _extract(all_text, _LEGAL_THREAT_KW + _SOCIAL_THREAT_KW + _HOSTILE_KW)
    legal_threat = _has(all_text, _LEGAL_THREAT_KW)
    social_threat = _has(all_text, _SOCIAL_THREAT_KW)
    threat_count = len(threat_phrases)
    urgency_count = len(_extract(all_text, _URGENT_KW))
    sophistication_count = len(_extract(all_text, _SOPHIST_KW))
    cooperation_count = len(_extract(all_text, _COOPERATIVE_KW))
    buyer_ack = _has(all_text, _RECEIPT_ACK_KW) and not _has(all_text, _NON_DELIVERY_KW)

    # Inconsistencies: claims non-delivery AND explicitly acknowledges receipt or wants to return
    inconsistencies = []
    has_nondelivery = _has(all_text, _NON_DELIVERY_KW)
    has_return = _has(all_text, _WANTS_RETURN_KW)
    has_receipt = _has(all_text, _RECEIPT_ACK_KW)
    # Only flag if explicit return intent or explicit self-acknowledgment of receipt
    if has_nondelivery and has_return:
        inconsistencies.append("Claims non-delivery but also wants to return item")
    elif has_nondelivery and has_receipt:
        inconsistencies.append("Claims non-delivery but also acknowledges receiving item")

    # Boilerplate/script detection
    boilerplate_count = sum(1 for p in _BOILERPLATE_PHRASES if p in all_text.lower())

    # Fraud signal
    fraud_pts = 0
    if sophistication_count >= 1:
        fraud_pts += 2
    if legal_threat:
        fraud_pts += 1
    if has_receipt and has_nondelivery:
        fraud_pts += 2
    elif has_receipt:
        fraud_pts += 2  # INR claim but buyer admits receipt is a strong fraud signal
    if ci.is_repeat_disputer or ci.buyer_prior_disputes >= 3:
        fraud_pts += 2
    if boilerplate_count >= 2:
        fraud_pts += 2  # multiple stock fraud phrases
    if fraud_pts >= 5:
        fraud_level = FraudSignalLevel.VERY_HIGH
    elif fraud_pts >= 3:
        fraud_level = FraudSignalLevel.HIGH
    elif fraud_pts >= 2:
        fraud_level = FraudSignalLevel.MODERATE
    elif fraud_pts >= 1:
        fraud_level = FraudSignalLevel.LOW
    else:
        fraud_level = FraudSignalLevel.MINIMAL

    # Hostility
    if threat_count >= 3 or (legal_threat and social_threat):
        hostility = HostilityLevel.EXTREME
    elif threat_count >= 2 or legal_threat:
        hostility = HostilityLevel.HIGH
    elif threat_count >= 1 or urgency_count >= 2:
        hostility = HostilityLevel.MODERATE
    elif cooperation_count >= 2:
        hostility = HostilityLevel.COOPERATIVE
    elif n == 0:
        hostility = HostilityLevel.LOW
    else:
        hostility = HostilityLevel.LOW

    # Recommended tone
    if hostility in (HostilityLevel.EXTREME,) or (legal_threat and social_threat):
        tone = RecommendedTone.LEGAL
    elif hostility == HostilityLevel.HIGH or fraud_level in (FraudSignalLevel.HIGH, FraudSignalLevel.VERY_HIGH):
        tone = RecommendedTone.FIRM
    elif hostility in (HostilityLevel.COOPERATIVE, HostilityLevel.LOW) and fraud_pts == 0:
        tone = RecommendedTone.COOPERATIVE
    else:
        tone = RecommendedTone.FORMAL

    key_threat_phrases = threat_phrases[:5]

    action_note = (
        f"fraud={fraud_level.value} hostility={hostility.value} threats={threat_count} "
        f"sophistication={sophistication_count}"
    )
    summary = (
        f"Comms {ci.dispute_id} [{ci.claim_type}]: "
        f"hostility={hostility.value} fraud={fraud_level.value} "
        f"threats={threat_count} ack_receipt={buyer_ack}"
    )

    class _Report:
        pass

    r = _Report()
    r.dispute_id = ci.dispute_id
    r.fraud_signal_level = fraud_level
    r.hostility_level = hostility
    r.threat_count = threat_count
    r.urgency_count = urgency_count
    r.sophistication_count = sophistication_count
    r.cooperation_count = cooperation_count
    r.key_threat_phrases = key_threat_phrases
    r.buyer_acknowledged_receipt = buyer_ack
    r.inconsistencies = inconsistencies
    r.recommended_tone = tone
    r.action_note = action_note
    r.summary = summary
    # Legacy fields
    r.legal_threat_detected = legal_threat
    r.social_media_threat_detected = social_threat
    r.delivery_acknowledged_by_buyer = buyer_ack

    # Map hostility to buyer_tone
    if hostility in (HostilityLevel.EXTREME, HostilityLevel.HIGH):
        r.buyer_tone = BuyerTone.HOSTILE
    elif hostility == HostilityLevel.MODERATE:
        r.buyer_tone = BuyerTone.DEMANDING
    elif hostility == HostilityLevel.COOPERATIVE or cooperation_count >= 2:
        r.buyer_tone = BuyerTone.COOPERATIVE
    else:
        r.buyer_tone = BuyerTone.NEUTRAL

    # Map escalation risk
    if hostility == HostilityLevel.EXTREME or (legal_threat and r.buyer_tone == BuyerTone.HOSTILE):
        r.escalation_risk = EscalationRisk.IMMINENT
    elif hostility in (HostilityLevel.HIGH,) or social_threat:
        r.escalation_risk = EscalationRisk.HIGH
    elif hostility == HostilityLevel.MODERATE:
        r.escalation_risk = EscalationRisk.MODERATE
    else:
        r.escalation_risk = EscalationRisk.LOW

    # Build contradiction_flags (INR + delivery acknowledged)
    cf = []
    if buyer_ack and ci.claim_type == "INR":
        cf.append(ContradictionFlag(
            description="INR dispute: Buyer acknowledged delivery",
            evidence_a="Buyer acknowledged receiving the item",
            evidence_b="Dispute reason: Item Not Received (INR)",
        ))
    r.contradiction_flags = cf

    # Build signal_flags
    sf = []
    if legal_threat:
        sf.append(SignalFlag("LEGAL_THREAT", "HIGH", all_text[:100]))
    if social_threat:
        sf.append(SignalFlag("SOCIAL_MEDIA_THREAT", "MEDIUM", all_text[:100]))
    if ci.buyer_prior_disputes >= 3:
        sf.append(SignalFlag("REPEAT_DISPUTER", "HIGH", f"Buyer has {ci.buyer_prior_disputes} prior disputes"))
    if fraud_pts >= 3:
        sf.append(SignalFlag("FRAUD_SIGNAL", "HIGH", f"Fraud score {fraud_pts}"))
    r.signal_flags = sf

    r.total_messages = len(ci.messages)
    r.buyer_message_count = sum(1 for m in ci.messages if m.is_from_buyer)
    r.merchant_message_count = sum(1 for m in ci.messages if not m.is_from_buyer)
    r.negotiation_leverage = action_note
    r.suggested_response_tone = tone.value
    # Burst detection: 3+ buyer messages within 1 hour
    buyer_times = sorted(m.timestamp_epoch for m in ci.messages if m.is_from_buyer)
    r.message_burst_detected = any(
        buyer_times[i + 2] - buyer_times[i] <= 3600
        for i in range(len(buyer_times) - 2)
    )
    return r
