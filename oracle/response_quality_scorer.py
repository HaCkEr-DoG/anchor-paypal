"""
Response Quality Scorer — evaluate the completeness and persuasiveness
of a draft dispute response before submission.

Scores five quality dimensions:
  1. Evidence completeness (0–30): all required evidence attached
  2. Factual accuracy (0–25): dates, amounts, tracking consistent
  3. Argumentation strength (0–20): reason code addressed directly
  4. Format compliance (0–15): network-required sections present
  5. Tone and professionalism (0–10): no emotional language, clear

Output: quality score 0–100, tier, specific improvements, approval signal.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseQualityTier(str, Enum):
    EXCELLENT  = "EXCELLENT"   # Score ≥ 85 — submit as-is
    GOOD       = "GOOD"        # Score 70–84 — minor improvements
    ADEQUATE   = "ADEQUATE"    # Score 55–69 — improvements recommended
    WEAK       = "WEAK"        # Score 40–54 — revise before submitting
    INADEQUATE = "INADEQUATE"  # Score < 40  — substantial rewrite needed


@dataclass
class ResponseDraftInput:
    """Input representing a prepared dispute response."""
    dispute_id: str
    reason_code: str
    network: str

    # Evidence
    evidence_items_attached: int = 0      # Count of attached docs
    required_evidence_count: int = 3      # How many are needed
    has_tracking_evidence: bool = False
    has_auth_evidence: bool = False
    has_policy_evidence: bool = False
    has_communication_log: bool = False

    # Factual consistency checks
    dates_consistent: bool = True          # Dates in response match records
    amounts_consistent: bool = True        # Dollar amounts match
    tracking_numbers_valid: bool = True    # Tracking numbers match carrier records
    buyer_identity_confirmed: bool = True  # Buyer name/address in response match order

    # Argumentation
    reason_code_addressed: bool = True     # Response explicitly refutes the stated reason
    has_counter_narrative: bool = False    # Provides alternate explanation of events
    cites_network_rules: bool = False      # References specific network rules/regs
    has_supporting_precedent: bool = False # Prior dispute outcomes cited

    # Format
    has_cover_letter: bool = False
    has_evidence_index: bool = False
    meets_page_limit: bool = True
    follows_network_template: bool = False

    # Tone
    tone_professional: bool = True
    no_threats_or_demands: bool = True
    concise_under_1000_words: bool = True


@dataclass
class QualityDimension:
    dimension: str
    score: int
    max_score: int
    findings: list[str]


@dataclass
class ResponseQualityReport:
    """Full response quality assessment."""
    dispute_id: str
    reason_code: str

    dimensions: list[QualityDimension]
    total_score: int
    quality_tier: ResponseQualityTier

    improvements: list[str]        # Specific changes to make
    strengths: list[str]           # What's already good
    blocking_issues: list[str]     # Must-fix before submission

    approved_to_submit: bool       # True if score ≥ 70
    summary: str


def score_response_quality(inp: ResponseDraftInput) -> ResponseQualityReport:
    """Score the quality of a prepared dispute response."""
    dimensions: list[QualityDimension] = []
    improvements: list[str] = []
    strengths: list[str] = []
    blocking: list[str] = []

    # ── Dimension 1: Evidence completeness (0–30) ─────────────────
    ev_score = 0
    ev_findings: list[str] = []

    coverage = min(1.0, inp.evidence_items_attached / max(inp.required_evidence_count, 1))
    ev_score += round(15 * coverage)

    if inp.has_tracking_evidence:
        ev_score += 5
        strengths.append("Tracking/delivery evidence included")
    else:
        improvements.append("Add tracking and delivery confirmation documents")

    if inp.has_auth_evidence:
        ev_score += 5
        strengths.append("Authentication evidence (3DS/CVV/IP) included")
    elif inp.reason_code.upper() in ("UNAUTHORIZED", "10.4", "4807"):
        improvements.append("Add authentication records (3DS, CVV result, IP log) — critical for this reason code")
        blocking.append("No authentication evidence for unauthorized dispute")

    if inp.has_policy_evidence:
        ev_score += 5
        strengths.append("Policy documentation attached")
    elif inp.reason_code.upper() in ("SUBSCRIPTION", "12.6", "SNAD", "13.3"):
        improvements.append("Attach policy display screenshot and terms-of-service")

    if inp.has_communication_log:
        ev_score += 2 if ev_score < 28 else 0
        strengths.append("Buyer communication history attached")

    if inp.evidence_items_attached < inp.required_evidence_count:
        gap = inp.required_evidence_count - inp.evidence_items_attached
        improvements.append(f"Attach {gap} more required evidence items")
        ev_findings.append(f"Only {inp.evidence_items_attached}/{inp.required_evidence_count} evidence items present")

    dimensions.append(QualityDimension("Evidence completeness", min(30, ev_score), 30, ev_findings))

    # ── Dimension 2: Factual accuracy (0–25) ─────────────────────
    fact_score = 25
    fact_findings: list[str] = []

    if not inp.dates_consistent:
        fact_score -= 10
        blocking.append("Date inconsistencies detected — reconcile before submission")
        fact_findings.append("Transaction dates in response don't match records")
    if not inp.amounts_consistent:
        fact_score -= 8
        blocking.append("Dollar amount mismatch — will undermine credibility")
        fact_findings.append("Disputed amount doesn't match transaction amount")
    if not inp.tracking_numbers_valid:
        fact_score -= 5
        improvements.append("Verify tracking number matches carrier records exactly")
        fact_findings.append("Tracking number may not match carrier records")
    if not inp.buyer_identity_confirmed:
        fact_score -= 2
        improvements.append("Confirm buyer name and address match order records")

    if fact_score == 25:
        strengths.append("All factual details appear consistent")

    dimensions.append(QualityDimension("Factual accuracy", max(0, fact_score), 25, fact_findings))

    # ── Dimension 3: Argumentation strength (0–20) ───────────────
    arg_score = 0
    arg_findings: list[str] = []

    if inp.reason_code_addressed:
        arg_score += 10
        strengths.append("Response directly addresses the stated reason code")
    else:
        blocking.append("Response does not address the specific reason code — must fix")
        arg_findings.append(f"Reason code {inp.reason_code} not explicitly refuted")

    if inp.has_counter_narrative:
        arg_score += 5
        strengths.append("Counter-narrative provided explaining merchant's perspective")
    else:
        improvements.append("Add a clear counter-narrative explaining what actually happened")

    if inp.cites_network_rules:
        arg_score += 3
        strengths.append("Network rules cited in support")
    else:
        improvements.append(f"Reference specific {inp.network} dispute rules that support your position")

    if inp.has_supporting_precedent:
        arg_score += 2

    dimensions.append(QualityDimension("Argumentation strength", min(20, arg_score), 20, arg_findings))

    # ── Dimension 4: Format compliance (0–15) ────────────────────
    fmt_score = 0
    fmt_findings: list[str] = []

    if inp.has_cover_letter:
        fmt_score += 5
        strengths.append("Cover letter present")
    else:
        improvements.append("Add a cover letter summarizing your position in 1–2 paragraphs")

    if inp.has_evidence_index:
        fmt_score += 4
        strengths.append("Evidence index included")
    else:
        improvements.append("Include an evidence index (Exhibit A, B, C…) for easy reference")

    if inp.meets_page_limit:
        fmt_score += 3
    else:
        improvements.append("Response exceeds recommended length — trim to under 10 pages")
        fmt_findings.append("Response may exceed network page limit")

    if inp.follows_network_template:
        fmt_score += 3
        strengths.append("Follows network-specific template")

    dimensions.append(QualityDimension("Format compliance", min(15, fmt_score), 15, fmt_findings))

    # ── Dimension 5: Tone and professionalism (0–10) ─────────────
    tone_score = 10
    tone_findings: list[str] = []

    if not inp.tone_professional:
        tone_score -= 5
        improvements.append("Rewrite in formal, neutral business language")
        tone_findings.append("Response contains informal or emotional language")

    if not inp.no_threats_or_demands:
        tone_score -= 4
        blocking.append("Remove any threats or demands — networks reject adversarial tone")
        tone_findings.append("Threatening or demanding language detected")

    if not inp.concise_under_1000_words:
        tone_score -= 1
        improvements.append("Trim narrative to under 1000 words — conciseness improves readability")

    dimensions.append(QualityDimension("Tone and professionalism", max(0, tone_score), 10, tone_findings))

    # ── Total ─────────────────────────────────────────────────────
    total = sum(d.score for d in dimensions)
    total = max(0, min(100, total))

    if total >= 85:
        tier = ResponseQualityTier.EXCELLENT
    elif total >= 70:
        tier = ResponseQualityTier.GOOD
    elif total >= 55:
        tier = ResponseQualityTier.ADEQUATE
    elif total >= 40:
        tier = ResponseQualityTier.WEAK
    else:
        tier = ResponseQualityTier.INADEQUATE

    approved = total >= 70 and len(blocking) == 0

    summary = (
        f"Response quality {inp.dispute_id}: "
        f"score={total}/100 ({tier.value}) | "
        f"blocking_issues={len(blocking)} | "
        f"approved={approved}"
    )

    return ResponseQualityReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        dimensions=dimensions,
        total_score=total,
        quality_tier=tier,
        improvements=improvements,
        strengths=strengths,
        blocking_issues=blocking,
        approved_to_submit=approved,
        summary=summary,
    )
