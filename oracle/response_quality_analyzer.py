"""
Dispute Response Quality Analyzer — grade written responses before submission.

A merchant can have full Seller Protection eligibility AND strong evidence, yet
still lose because their written response is poorly structured, omits key
statements, or uses language that hurts rather than helps their case.

This module grades a dispute response on:
  1. Completeness — does it cover all required statement categories?
  2. Evidence references — does it explicitly cite the evidence provided?
  3. Tone — is it professional, factual, and free of emotional/aggressive language?
  4. Structure — does it follow the recommended dispute response format?
  5. Specificity — does it include dates, amounts, tracking numbers where needed?
  6. Buyer-empathy — does it acknowledge the buyer's concern (reduces escalation)?

Returns:
  - quality_score: 0.0 – 1.0
  - grade: A / B / C / D / F
  - dimension_scores: per-dimension breakdown
  - issues: list of specific problems found
  - improvements: actionable rewrites for weak sections
  - estimated_impact: how much this response improves the win probability
  - paypal_signal: used to track merchant response quality cohorts

PayPal benefit:
  - Merchants with well-written responses resolve disputes faster
    (fewer back-and-forth requests from the dispute resolution team)
  - Routing poor-quality responses to coaching reduces case queue depth
  - Response quality correlates with dispute escalation rate — a leading
    indicator that PayPal can track at the merchant segment level
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseGrade(str, Enum):
    A = "A"   # score ≥ 0.85 — excellent, submit as-is
    B = "B"   # score 0.70–0.84 — good, minor improvements optional
    C = "C"   # score 0.50–0.69 — needs improvement before submitting
    D = "D"   # score 0.30–0.49 — significant gaps, do not submit yet
    F = "F"   # score < 0.30 — unusable, rewrite required


@dataclass
class DimensionScore:
    name: str
    score: float        # 0.0 – 1.0
    weight: float       # contribution to overall score
    issues: list[str]
    suggestions: list[str]


@dataclass
class ResponseAnalysisInput:
    dispute_id: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    response_text: str
    transaction_amount: float = 0.0

    # Context about what evidence was submitted
    has_tracking: bool = False
    has_delivery_scan: bool = False
    has_order_confirmation: bool = False
    has_product_photos: bool = False
    has_buyer_communication: bool = False


@dataclass
class ResponseQualityReport:
    dispute_id: str
    reason_code: str
    quality_score: float
    grade: ResponseGrade
    dimensions: list[DimensionScore]
    issues: list[str]
    improvements: list[str]
    estimated_win_probability_boost: float  # delta P(win) from quality improvement
    submit_ready: bool
    paypal_signal: str
    recommendation: str


# ─── Tone analysis ────────────────────────────────────────────────────────────

_AGGRESSIVE_PHRASES = [
    r"\bfraud\b", r"\bliar\b", r"\bcheat\b", r"\bscam\b", r"\bthief\b",
    r"\bsteal\b", r"\bstole\b", r"\babuse\b", r"\bdishonest\b", r"\bdisgusting\b",
    r"\boutrageous\b", r"\bhorrible\b", r"\bterrible customer\b",
    r"\bunacceptable\b", r"\bcompletely wrong\b",
]

_PROFESSIONAL_INDICATORS = [
    r"\brespectfully\b", r"\bplease find\b", r"\bwe shipped\b", r"\btracking\b",
    r"\bdelivered\b", r"\bconfirm\b", r"\bplease review\b", r"\battached\b",
    r"\bprovide\b", r"\bdocumentation\b", r"\brecords\b",
]

_EMPATHY_PHRASES = [
    r"\bunderstand\b", r"\bsorry\b", r"\bapologize\b", r"\bregret\b",
    r"\bappreciate\b", r"\bvalue your\b", r"\bconcern\b", r"\bexperience\b",
]


def _check_tone(text: str) -> DimensionScore:
    lower = text.lower()
    aggressive = sum(1 for p in _AGGRESSIVE_PHRASES if re.search(p, lower))
    professional = sum(1 for p in _PROFESSIONAL_INDICATORS if re.search(p, lower))
    empathy = sum(1 for p in _EMPATHY_PHRASES if re.search(p, lower))

    issues = []
    suggestions = []

    tone_score = 1.0
    if aggressive > 0:
        tone_score -= min(0.5, aggressive * 0.15)
        issues.append(f"Response contains {aggressive} aggressive/accusatory phrase(s). PayPal reviewers favor neutral, factual language.")
        suggestions.append("Remove emotional language. Replace accusations with factual statements: 'Our records show delivery was confirmed' not 'The buyer is lying'.")

    if professional < 2:
        tone_score -= 0.20
        issues.append("Response lacks professional language markers. Add phrases like 'Please find attached', 'Our records confirm', 'As shown by the tracking'.")

    if empathy == 0:
        tone_score -= 0.10
        suggestions.append("Add a brief empathy statement: 'We understand the buyer's concern and have provided documentation to resolve this promptly.'")

    tone_score = max(0.0, round(tone_score, 4))

    return DimensionScore(
        name="tone",
        score=tone_score,
        weight=0.20,
        issues=issues,
        suggestions=suggestions,
    )


# ─── Completeness check ───────────────────────────────────────────────────────

_REQUIRED_ELEMENTS: dict[str, list[str]] = {
    "INR": [
        "tracking", "shipped", "deliver", "carrier", "package",
    ],
    "SNAD": [
        "descri", "as listed", "as shown", "photo", "condition", "product",
    ],
    "UNAUTHORIZED": [
        "address", "ship", "deliver", "confirm",
    ],
    "SUBSCRIPTION": [
        "terms", "subscri", "renew", "notification", "agreed",
    ],
}


def _check_completeness(inp: ResponseAnalysisInput) -> DimensionScore:
    lower = inp.response_text.lower()
    required = _REQUIRED_ELEMENTS.get(inp.reason_code.upper(), _REQUIRED_ELEMENTS["INR"])
    present = [kw for kw in required if kw in lower]
    missing = [kw for kw in required if kw not in lower]

    score = len(present) / max(len(required), 1)
    issues = []
    suggestions = []

    if missing:
        issues.append(f"Response is missing coverage of: {', '.join(missing[:3])}.")
        suggestions.append(
            f"For a {inp.reason_code} dispute, your response MUST address: "
            + ", ".join(required[:4]) + "."
        )

    # Check for transaction amount reference
    if inp.transaction_amount > 0:
        amt_str = f"{inp.transaction_amount:.2f}"
        if amt_str[:3] not in lower and str(int(inp.transaction_amount)) not in lower:
            score -= 0.10
            issues.append("Transaction amount not referenced. Including the specific amount strengthens traceability.")

    return DimensionScore(
        name="completeness",
        score=max(0.0, round(score, 4)),
        weight=0.30,
        issues=issues,
        suggestions=suggestions,
    )


# ─── Evidence citation check ──────────────────────────────────────────────────

def _check_evidence_citation(inp: ResponseAnalysisInput) -> DimensionScore:
    lower = inp.response_text.lower()
    score = 0.5  # base
    issues = []
    suggestions = []

    if inp.has_tracking and "tracking" not in lower:
        issues.append("You have a tracking number but didn't mention it in the response. Always cite 'Tracking: [number]' explicitly.")
        score -= 0.20
    elif inp.has_tracking:
        score += 0.15

    if inp.has_delivery_scan and "deliver" not in lower:
        issues.append("Delivery scan exists but response doesn't reference delivery confirmation.")
        score -= 0.15
    elif inp.has_delivery_scan:
        score += 0.15

    if inp.has_order_confirmation and "order" not in lower and "confirm" not in lower:
        issues.append("Order confirmation is available but not mentioned in the response.")
        score -= 0.10
    elif inp.has_order_confirmation:
        score += 0.10

    if inp.has_product_photos and "photo" not in lower and "image" not in lower and "picture" not in lower:
        issues.append("Product photos are available but not referenced. Add 'As shown in the attached photos...'")
        score -= 0.10
    elif inp.has_product_photos:
        score += 0.10

    if score < 0.5 and not issues:
        suggestions.append("Explicitly reference all submitted evidence: 'Please see attached: [tracking confirmation, order receipt, delivery scan]'.")

    return DimensionScore(
        name="evidence_citation",
        score=max(0.0, min(1.0, round(score, 4))),
        weight=0.25,
        issues=issues,
        suggestions=suggestions,
    )


# ─── Specificity check ────────────────────────────────────────────────────────

_DATE_PATTERN = re.compile(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}\b', re.IGNORECASE)
_TRACKING_PATTERN = re.compile(r'\b[A-Z0-9]{8,30}\b')


def _check_specificity(inp: ResponseAnalysisInput) -> DimensionScore:
    text = inp.response_text
    issues = []
    suggestions = []
    score = 0.5

    # Dates
    dates_found = _DATE_PATTERN.findall(text)
    if dates_found:
        score += 0.20
    else:
        issues.append("No specific dates found. Add exact dates: order date, ship date, delivery date.")
        score -= 0.15

    # Tracking numbers (all-caps alphanumeric sequences)
    if inp.has_tracking:
        tracking_refs = _TRACKING_PATTERN.findall(text)
        # Filter out short common words
        tracking_refs = [t for t in tracking_refs if len(t) >= 10]
        if not tracking_refs:
            issues.append("Tracking number not explicitly stated in response body.")
            score -= 0.15

    word_count = len(text.split())
    if word_count < 50:
        issues.append(f"Response is very short ({word_count} words). PayPal recommends at least 100 words for compelling responses.")
        score -= 0.20
        suggestions.append("Expand the response with specific details: exact dates, amounts, carrier name, and what steps were taken.")
    elif word_count >= 100:
        score += 0.10

    return DimensionScore(
        name="specificity",
        score=max(0.0, min(1.0, round(score, 4))),
        weight=0.15,
        issues=issues,
        suggestions=suggestions,
    )


# ─── Structure check ──────────────────────────────────────────────────────────

def _check_structure(inp: ResponseAnalysisInput) -> DimensionScore:
    text = inp.response_text
    lower = text.lower()
    issues = []
    suggestions = []
    score = 0.5

    # Has an opening statement?
    has_opening = any(lower.startswith(p) for p in [
        "we ", "i ", "dear ", "this response", "in response", "regarding",
        "our company", "as the seller",
    ])
    if has_opening:
        score += 0.15
    else:
        issues.append("Response lacks a clear opening statement identifying the merchant and dispute.")

    # Has a conclusion / request?
    has_closing = any(p in lower for p in [
        "therefore", "accordingly", "we request", "we kindly request",
        "please find", "we respectfully", "close", "resolve", "in conclusion",
    ])
    if has_closing:
        score += 0.15
    else:
        issues.append("Response lacks a clear closing statement requesting a decision.")
        suggestions.append("End with: 'Therefore, we respectfully request that this dispute be resolved in our favor based on the attached documentation.'")

    # Paragraph breaks?
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) >= 2:
        score += 0.10
    else:
        suggestions.append("Break response into paragraphs: (1) Background, (2) Evidence, (3) Conclusion.")

    return DimensionScore(
        name="structure",
        score=max(0.0, min(1.0, round(score, 4))),
        weight=0.10,
        issues=issues,
        suggestions=suggestions,
    )


# ─── Main function ────────────────────────────────────────────────────────────

def analyze_response_quality(inp: ResponseAnalysisInput) -> ResponseQualityReport:
    """
    Analyze a dispute response for quality and return grade + improvements.
    """
    dimensions = [
        _check_completeness(inp),
        _check_evidence_citation(inp),
        _check_tone(inp.response_text),
        _check_specificity(inp),
        _check_structure(inp),
    ]

    total_weight = sum(d.weight for d in dimensions)
    score = sum(d.score * d.weight for d in dimensions) / total_weight
    score = round(score, 4)

    if score >= 0.85:
        grade = ResponseGrade.A
    elif score >= 0.70:
        grade = ResponseGrade.B
    elif score >= 0.50:
        grade = ResponseGrade.C
    elif score >= 0.30:
        grade = ResponseGrade.D
    else:
        grade = ResponseGrade.F

    all_issues = [issue for d in dimensions for issue in d.issues]
    all_improvements = [sug for d in dimensions for sug in d.suggestions]

    # Win probability boost estimate
    base_boost = {
        ResponseGrade.A: 0.05,
        ResponseGrade.B: 0.03,
        ResponseGrade.C: 0.0,
        ResponseGrade.D: -0.05,
        ResponseGrade.F: -0.15,
    }[grade]
    win_prob_boost = base_boost

    submit_ready = grade in (ResponseGrade.A, ResponseGrade.B) and len([i for i in all_issues if "tracking" in i.lower() or "not mentioned" in i.lower()]) == 0

    recommendation = {
        ResponseGrade.A: "Response is excellent. Submit immediately.",
        ResponseGrade.B: "Response is good. Address minor improvements then submit.",
        ResponseGrade.C: f"Response needs work. Fix {len(all_issues)} issue(s) before submitting.",
        ResponseGrade.D: "Response has significant gaps. Do not submit yet — rewrite key sections.",
        ResponseGrade.F: "Response is insufficient. Full rewrite required.",
    }[grade]

    paypal_signal = (
        f"Dispute {inp.dispute_id} ({inp.reason_code}): response grade {grade.value} "
        f"(score={score:.2f}). "
        + (f"Issues: {len(all_issues)}. " if all_issues else "No major issues. ")
        + (f"Win probability impact: {win_prob_boost:+.0%}." if win_prob_boost != 0 else "Neutral impact on win probability.")
    )

    return ResponseQualityReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        quality_score=score,
        grade=grade,
        dimensions=dimensions,
        issues=all_issues,
        improvements=all_improvements,
        estimated_win_probability_boost=win_prob_boost,
        submit_ready=submit_ready,
        paypal_signal=paypal_signal,
        recommendation=recommendation,
    )
