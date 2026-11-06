"""
Communication Sentiment Analyzer — analyze buyer messages to assess
tone, intent, and dispute escalation risk.

Buyer communications contain signals that predict dispute escalation and
inform the optimal response strategy. Analyzing the sentiment and content
of messages helps the merchant choose between:
  - Immediate resolution (high-value buyer, one-time frustration)
  - Measured response (legitimate concern, standard handling)
  - Aggressive defense (hostile/abusive buyer, fraud indicators)
  - Escalation hold (waiting for more information)

Sentiment dimensions:
  TONE          — positive / neutral / negative / hostile
  URGENCY       — low / medium / high / extreme
  LEGITIMACY    — does the message reflect a genuine concern?
  THREAT_LEVEL  — are chargebacks, reviews, or legal action threatened?
  RESOLUTION_OPENNESS — is the buyer open to resolution?

Keyword categories:
  Positive: "thank", "appreciate", "happy", "resolved", "satisfied"
  Negative: "disappointed", "frustrated", "unacceptable", "terrible"
  Hostile: "scam", "fraud", "fake", "liar", "lawsuit", "BBB", "FTC"
  Threat: "chargeback", "dispute", "report", "attorney", "legal", "review"
  Urgency: "immediately", "asap", "urgent", "right now", "today"
  Resolution: "refund", "replace", "fix", "resolve", "help", "solution"

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageTone(str, Enum):
    POSITIVE  = "POSITIVE"
    NEUTRAL   = "NEUTRAL"
    NEGATIVE  = "NEGATIVE"
    HOSTILE   = "HOSTILE"


class UrgencyLevel(str, Enum):
    LOW     = "LOW"
    MEDIUM  = "MEDIUM"
    HIGH    = "HIGH"
    EXTREME = "EXTREME"


class ThreatLevel(str, Enum):
    NONE     = "NONE"
    IMPLIED  = "IMPLIED"
    EXPLICIT = "EXPLICIT"


class ResolutionOpenness(str, Enum):
    OPEN        = "OPEN"       # Willing to work with merchant
    CONDITIONAL = "CONDITIONAL" # Will accept resolution IF conditions met
    CLOSED      = "CLOSED"     # Demands specific outcome, no flexibility
    ADVERSARIAL = "ADVERSARIAL" # Explicitly hostile, not seeking resolution


_POSITIVE_KEYWORDS = frozenset([
    "thank", "appreciate", "happy", "pleased", "satisfied", "great",
    "wonderful", "good", "love", "excellent", "perfect", "glad",
])

_NEGATIVE_KEYWORDS = frozenset([
    "disappointed", "frustrated", "upset", "annoyed", "unacceptable",
    "terrible", "awful", "horrible", "bad", "wrong", "broken", "damaged",
    "missing", "never", "worst", "poor", "disgust",
])

_HOSTILE_KEYWORDS = frozenset([
    "scam", "fraud", "fake", "liar", "lie", "steal", "criminal", "thief",
    "con", "corrupt", "lawsuit", "bbb", "ftc", "attorney", "lawyer",
    "sue", "court", "illegal", "police", "fbi",
])

_THREAT_KEYWORDS = frozenset([
    "chargeback", "dispute", "report", "review", "feedback", "warn",
    "alert", "post", "social media", "twitter", "facebook", "yelp",
    "bbb", "attorney general", "consumer protection",
])

_URGENCY_KEYWORDS = frozenset([
    "immediately", "asap", "urgent", "right now", "today", "now",
    "hurry", "quickly", "immediately", "deadline", "last chance",
    "final", "expire",
])

_RESOLUTION_KEYWORDS = frozenset([
    "refund", "replace", "fix", "resolve", "help", "solution", "credit",
    "exchange", "return", "repair", "address", "correct", "make right",
])


def _tokenize(text: str) -> list[str]:
    import re
    return re.findall(r"[a-z]+", text.lower())


def _count_matches(tokens: list[str], keywords: frozenset[str]) -> int:
    return sum(1 for t in tokens if t in keywords)


@dataclass
class MessageAnalysis:
    """Analysis of a single buyer message."""
    message_index: int
    tone: MessageTone
    urgency: UrgencyLevel
    threat_level: ThreatLevel
    resolution_openness: ResolutionOpenness
    positive_signals: int
    negative_signals: int
    hostile_signals: int
    threat_signals: int
    urgency_signals: int
    resolution_signals: int
    escalation_risk_score: float   # 0–100


@dataclass
class CommunicationSentimentInput:
    """Input for communication sentiment analysis."""
    dispute_id: str
    buyer_id: str
    messages: list[str]            # List of buyer message texts
    claim_type: str = "INR"
    days_since_first_message: int = 1


@dataclass
class CommunicationSentimentReport:
    """Aggregated communication sentiment report."""
    dispute_id: str
    buyer_id: str

    # Per-message analyses
    message_analyses: list[MessageAnalysis]

    # Aggregate
    overall_tone: MessageTone
    peak_urgency: UrgencyLevel
    peak_threat_level: ThreatLevel
    overall_resolution_openness: ResolutionOpenness

    # Risk score
    escalation_risk_score: float   # 0–100 (higher = more likely to escalate)
    escalation_probability: float  # 0.0–1.0

    # Trend
    tone_deteriorating: bool       # Messages getting more negative over time

    # Response guidance
    recommended_response_tone: str
    response_strategy: str
    priority_flag: str             # "URGENT" / "STANDARD" / "MONITOR"

    summary: str


def _analyze_message(text: str, idx: int) -> MessageAnalysis:
    tokens = _tokenize(text)
    pos = _count_matches(tokens, _POSITIVE_KEYWORDS)
    neg = _count_matches(tokens, _NEGATIVE_KEYWORDS)
    hos = _count_matches(tokens, _HOSTILE_KEYWORDS)
    thr = _count_matches(tokens, _THREAT_KEYWORDS)
    urg = _count_matches(tokens, _URGENCY_KEYWORDS)
    res = _count_matches(tokens, _RESOLUTION_KEYWORDS)

    # Tone
    if hos >= 2:
        tone = MessageTone.HOSTILE
    elif hos >= 1 or (neg >= 3 and thr >= 1):
        tone = MessageTone.HOSTILE
    elif neg >= 2:
        tone = MessageTone.NEGATIVE
    elif pos > neg:
        tone = MessageTone.POSITIVE
    else:
        tone = MessageTone.NEUTRAL

    # Urgency
    if urg >= 3:
        urgency = UrgencyLevel.EXTREME
    elif urg >= 2:
        urgency = UrgencyLevel.HIGH
    elif urg >= 1:
        urgency = UrgencyLevel.MEDIUM
    else:
        urgency = UrgencyLevel.LOW

    # Threat
    if hos >= 2 or (thr >= 2 and "chargeback" in text.lower()):
        threat = ThreatLevel.EXPLICIT
    elif thr >= 1 or hos >= 1:
        threat = ThreatLevel.IMPLIED
    else:
        threat = ThreatLevel.NONE

    # Resolution openness
    if tone == MessageTone.HOSTILE or threat == ThreatLevel.EXPLICIT:
        openness = ResolutionOpenness.ADVERSARIAL
    elif res >= 1 and neg <= 1:
        openness = ResolutionOpenness.OPEN
    elif res >= 1:
        openness = ResolutionOpenness.CONDITIONAL
    else:
        openness = ResolutionOpenness.CLOSED

    # Escalation risk score (0–100)
    score = 0.0
    score += min(40, hos * 15)
    score += min(20, thr * 8)
    score += min(20, neg * 5)
    score += min(10, urg * 4)
    score -= min(15, pos * 5)
    score -= min(10, res * 3)
    score = round(max(0.0, min(100.0, score)), 1)

    return MessageAnalysis(
        message_index=idx,
        tone=tone,
        urgency=urgency,
        threat_level=threat,
        resolution_openness=openness,
        positive_signals=pos,
        negative_signals=neg,
        hostile_signals=hos,
        threat_signals=thr,
        urgency_signals=urg,
        resolution_signals=res,
        escalation_risk_score=score,
    )


def analyze_communication_sentiment(
        inp: CommunicationSentimentInput) -> CommunicationSentimentReport:
    """
    Analyze a sequence of buyer messages and produce escalation risk guidance.
    """
    if not inp.messages:
        # No messages — neutral defaults
        empty = CommunicationSentimentReport(
            dispute_id=inp.dispute_id,
            buyer_id=inp.buyer_id,
            message_analyses=[],
            overall_tone=MessageTone.NEUTRAL,
            peak_urgency=UrgencyLevel.LOW,
            peak_threat_level=ThreatLevel.NONE,
            overall_resolution_openness=ResolutionOpenness.OPEN,
            escalation_risk_score=0.0,
            escalation_probability=0.0,
            tone_deteriorating=False,
            recommended_response_tone="Friendly and proactive",
            response_strategy="Reach out proactively to understand buyer concern",
            priority_flag="STANDARD",
            summary=f"Sentiment {inp.dispute_id}: no messages — STANDARD",
        )
        return empty

    analyses = [_analyze_message(msg, i) for i, msg in enumerate(inp.messages)]

    # Aggregate tone — worst tone wins
    _tone_rank = {MessageTone.POSITIVE: 0, MessageTone.NEUTRAL: 1,
                  MessageTone.NEGATIVE: 2, MessageTone.HOSTILE: 3}
    overall_tone = max(analyses, key=lambda a: _tone_rank[a.tone]).tone

    # Peak urgency
    _urg_rank = {UrgencyLevel.LOW: 0, UrgencyLevel.MEDIUM: 1,
                 UrgencyLevel.HIGH: 2, UrgencyLevel.EXTREME: 3}
    peak_urgency = max(analyses, key=lambda a: _urg_rank[a.urgency]).urgency

    # Peak threat
    _thr_rank = {ThreatLevel.NONE: 0, ThreatLevel.IMPLIED: 1, ThreatLevel.EXPLICIT: 2}
    peak_threat = max(analyses, key=lambda a: _thr_rank[a.threat_level]).threat_level

    # Overall resolution openness — worst case
    _open_rank = {ResolutionOpenness.OPEN: 0, ResolutionOpenness.CONDITIONAL: 1,
                  ResolutionOpenness.CLOSED: 2, ResolutionOpenness.ADVERSARIAL: 3}
    overall_openness = max(analyses, key=lambda a: _open_rank[a.resolution_openness]).resolution_openness

    # Escalation risk — weighted average with recency bias
    n = len(analyses)
    weights = [1 + i * 0.5 for i in range(n)]
    total_w = sum(weights)
    weighted_score = sum(a.escalation_risk_score * w
                         for a, w in zip(analyses, weights)) / total_w
    escalation_risk = round(weighted_score, 1)
    escalation_prob = round(min(0.99, escalation_risk / 100 * 1.2), 4)

    # Tone deteriorating: last half worse than first half
    if n >= 2:
        mid = n // 2
        early_avg = sum(a.escalation_risk_score for a in analyses[:mid]) / mid
        late_avg  = sum(a.escalation_risk_score for a in analyses[mid:]) / (n - mid)
        deteriorating = late_avg > early_avg + 10
    else:
        deteriorating = False

    # Response guidance
    if overall_tone == MessageTone.HOSTILE or peak_threat == ThreatLevel.EXPLICIT:
        resp_tone = "Professional and de-escalating; avoid matching hostility"
        strategy  = ("Acknowledge concern, state facts clearly, offer one concrete resolution; "
                     "document all communications for dispute evidence")
        priority  = "URGENT"
    elif overall_tone == MessageTone.NEGATIVE and peak_urgency in (UrgencyLevel.HIGH, UrgencyLevel.EXTREME):
        resp_tone = "Empathetic and solution-focused"
        strategy  = "Respond within 2h; offer specific resolution timeline"
        priority  = "URGENT"
    elif overall_tone == MessageTone.NEGATIVE:
        resp_tone = "Empathetic and apologetic"
        strategy  = "Respond within 8h; acknowledge frustration and propose resolution"
        priority  = "STANDARD"
    elif overall_openness == ResolutionOpenness.OPEN:
        resp_tone = "Helpful and friendly"
        strategy  = "Standard response with resolution options; no urgency"
        priority  = "STANDARD"
    else:
        resp_tone = "Professional and clear"
        strategy  = "Respond within 24h with factual account; invite resolution"
        priority  = "MONITOR"

    summary = (
        f"Sentiment {inp.dispute_id} [{inp.buyer_id}]: "
        f"msgs={len(inp.messages)} | tone={overall_tone.value} | "
        f"esc_risk={escalation_risk:.0f} | threat={peak_threat.value} | "
        f"priority={priority}"
    )

    return CommunicationSentimentReport(
        dispute_id=inp.dispute_id,
        buyer_id=inp.buyer_id,
        message_analyses=analyses,
        overall_tone=overall_tone,
        peak_urgency=peak_urgency,
        peak_threat_level=peak_threat,
        overall_resolution_openness=overall_openness,
        escalation_risk_score=escalation_risk,
        escalation_probability=escalation_prob,
        tone_deteriorating=deteriorating,
        recommended_response_tone=resp_tone,
        response_strategy=strategy,
        priority_flag=priority,
        summary=summary,
    )
