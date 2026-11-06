"""
Policy Defensibility Scorer — evaluate how defensible a merchant's
refund, return, and subscription cancellation policies are in a dispute.

A well-written, clearly disclosed policy strengthens the merchant's
position in SNAD and SUBSCRIPTION disputes. This oracle scores:
  - Policy visibility at checkout (disclosed? linked? buried?)
  - Clarity and specificity of terms
  - Final-sale / no-refund clarity
  - Subscription cancellation terms
  - Policy consistency with actual merchant behavior
  - Network compliance (policies cannot override network rules)

Output: a score 0–100 and a STRONG / ADEQUATE / WEAK / UNUSABLE rating.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PolicyDefensibility(str, Enum):
    STRONG   = "STRONG"    # Score ≥ 75 — policy is a net asset
    ADEQUATE = "ADEQUATE"  # Score 50–74 — policy helps but has gaps
    WEAK     = "WEAK"      # Score 25–49 — policy may be ignored by arbiter
    UNUSABLE = "UNUSABLE"  # Score < 25 — policy actively hurts merchant


class DisclosureLevel(str, Enum):
    PROMINENT = "PROMINENT"   # Policy link/text visible at checkout
    LINKED    = "LINKED"      # Policy linked from checkout page
    BURIED    = "BURIED"      # Policy exists but hard to find
    MISSING   = "MISSING"     # No policy disclosed at checkout


class PolicyType(str, Enum):
    RETURN_REFUND  = "RETURN_REFUND"
    FINAL_SALE     = "FINAL_SALE"
    SUBSCRIPTION   = "SUBSCRIPTION"
    DIGITAL_GOODS  = "DIGITAL_GOODS"
    SERVICES       = "SERVICES"


@dataclass
class PolicyEvidenceInput:
    """Input describing merchant's policies and how they were disclosed."""
    dispute_id: str
    policy_type: PolicyType
    reason_code: str

    # Disclosure context
    disclosure_level: DisclosureLevel = DisclosureLevel.MISSING
    buyer_checked_policy_box: bool = False      # Explicit acknowledgment
    policy_text_at_checkout: bool = False       # Policy text visible on checkout page
    policy_linked_from_checkout: bool = False   # Policy link on checkout page
    policy_in_order_confirmation: bool = False  # Policy included in confirmation email

    # Policy content quality
    has_clear_timeframe: bool = False            # e.g. "returns within 30 days"
    has_specific_conditions: bool = False        # e.g. "unopened, original packaging"
    has_no_refund_statement: bool = False        # For final-sale items
    final_sale_clearly_stated: bool = False      # "ALL SALES FINAL" type language

    # Subscription specifics
    has_cancellation_instructions: bool = False
    has_renewal_reminder: bool = False
    cancellation_days_notice: int = 0            # Days notice required to cancel

    # Consistency
    policy_applied_consistently: bool = True     # Has merchant honored this policy before?
    exceptions_made_before: bool = False         # Has merchant made exceptions?

    # Network override check
    policy_overrides_network_rules: bool = False   # Illegal — network rules always win


@dataclass
class ScoringDimension:
    """A single scoring dimension."""
    dimension: str
    score: int           # Contribution to total (can be negative)
    max_score: int
    rationale: str


@dataclass
class PolicyDefensibilityReport:
    """Full policy defensibility assessment."""
    dispute_id: str
    policy_type: PolicyType

    dimensions: list[ScoringDimension]
    total_score: int            # 0–100
    defensibility: PolicyDefensibility

    strengths: list[str]
    weaknesses: list[str]
    network_compliance_ok: bool

    win_rate_adjustment: float  # Additive adjustment to win probability (-0.15 to +0.08)
    recommendation: str
    summary: str


def score_policy_defensibility(inp: PolicyEvidenceInput) -> PolicyDefensibilityReport:
    """Score how defensible a merchant's policy is in a dispute."""
    dimensions: list[ScoringDimension] = []
    strengths: list[str] = []
    weaknesses: list[str] = []

    # ── Dimension 1: Disclosure quality (0–30) ───────────────────
    disclosure_score = {
        DisclosureLevel.PROMINENT: 30,
        DisclosureLevel.LINKED:    20,
        DisclosureLevel.BURIED:    8,
        DisclosureLevel.MISSING:   0,
    }[inp.disclosure_level]

    if inp.buyer_checked_policy_box:
        disclosure_score = min(30, disclosure_score + 10)
    if inp.policy_in_order_confirmation:
        disclosure_score = min(30, disclosure_score + 5)

    disc_rationale = f"Disclosure level: {inp.disclosure_level.value}"
    if inp.buyer_checked_policy_box:
        disc_rationale += " + buyer acknowledged"
    if disclosure_score >= 25:
        strengths.append("Policy clearly disclosed at checkout")
    elif disclosure_score < 10:
        weaknesses.append("Policy not visible at checkout — arbiter may discount it")

    dimensions.append(ScoringDimension(
        "Disclosure quality", disclosure_score, 30, disc_rationale,
    ))

    # ── Dimension 2: Policy clarity (0–25) ───────────────────────
    clarity_score = 0
    if inp.has_clear_timeframe:
        clarity_score += 10
        strengths.append("Policy specifies clear return/refund timeframe")
    else:
        weaknesses.append("No specific timeframe — vague policy")

    if inp.has_specific_conditions:
        clarity_score += 10
        strengths.append("Policy specifies exact conditions for returns")
    else:
        weaknesses.append("Policy lacks specific conditions — open to interpretation")

    if inp.has_no_refund_statement and inp.final_sale_clearly_stated:
        clarity_score += 5
        strengths.append("Final-sale policy clearly and explicitly stated")

    dimensions.append(ScoringDimension(
        "Policy clarity", clarity_score, 25,
        f"timeframe={inp.has_clear_timeframe} conditions={inp.has_specific_conditions}",
    ))

    # ── Dimension 3: Subscription terms (0–20, only for SUBSCRIPTION) ──
    if inp.policy_type == PolicyType.SUBSCRIPTION:
        sub_score = 0
        if inp.has_cancellation_instructions:
            sub_score += 10
            strengths.append("Cancellation instructions clearly provided")
        else:
            weaknesses.append("No cancellation instructions disclosed")
        if inp.has_renewal_reminder:
            sub_score += 7
            strengths.append("Renewal reminder sent before charge")
        if inp.cancellation_days_notice <= 7:
            sub_score += 3
            strengths.append(f"Reasonable cancellation notice: {inp.cancellation_days_notice} days")
        elif inp.cancellation_days_notice > 30:
            sub_score -= 5
            weaknesses.append(f"Excessive cancellation notice ({inp.cancellation_days_notice} days)")
        dimensions.append(ScoringDimension(
            "Subscription terms", sub_score, 20,
            f"cancel_instructions={inp.has_cancellation_instructions} renewal_reminder={inp.has_renewal_reminder}",
        ))
    else:
        dimensions.append(ScoringDimension(
            "Subscription terms", 10, 20, "Not a subscription dispute — partial credit", ))

    # ── Dimension 4: Consistency (0–15) ──────────────────────────
    consistency_score = 0
    if inp.policy_applied_consistently:
        consistency_score += 15
        strengths.append("Policy applied consistently across transactions")
    else:
        weaknesses.append("Policy inconsistently applied — weakens defense")
    if inp.exceptions_made_before:
        consistency_score -= 5
        weaknesses.append("Prior exceptions to policy on record")

    dimensions.append(ScoringDimension(
        "Policy consistency", consistency_score, 15,
        f"consistent={inp.policy_applied_consistently} exceptions={inp.exceptions_made_before}",
    ))

    # ── Dimension 5: Network compliance (-10 or +10) ─────────────
    if inp.policy_overrides_network_rules:
        compliance_score = -10
        weaknesses.append("Policy attempts to override network rules — will be struck down")
    else:
        compliance_score = 10
        strengths.append("Policy compliant with network dispute rules")

    network_ok = not inp.policy_overrides_network_rules
    dimensions.append(ScoringDimension(
        "Network compliance", compliance_score, 10,
        "PASS" if network_ok else "FAIL — policy overrides network rules",
    ))

    # ── Total score ───────────────────────────────────────────────
    raw_score = sum(d.score for d in dimensions)
    total_score = max(0, min(100, raw_score))

    if total_score >= 75:
        defensibility = PolicyDefensibility.STRONG
    elif total_score >= 50:
        defensibility = PolicyDefensibility.ADEQUATE
    elif total_score >= 25:
        defensibility = PolicyDefensibility.WEAK
    else:
        defensibility = PolicyDefensibility.UNUSABLE

    # Win rate adjustment
    adj_map = {
        PolicyDefensibility.STRONG:   +0.08,
        PolicyDefensibility.ADEQUATE: +0.02,
        PolicyDefensibility.WEAK:     -0.08,
        PolicyDefensibility.UNUSABLE: -0.15,
    }
    win_adj = adj_map[defensibility]

    # Recommendation
    if defensibility == PolicyDefensibility.UNUSABLE:
        rec = "Policy will not help defense — focus entirely on evidence quality"
    elif defensibility == PolicyDefensibility.WEAK:
        rec = "Policy gaps weaken defense — ensure evidence is COMPELLING to compensate"
    elif defensibility == PolicyDefensibility.ADEQUATE:
        rec = "Policy provides moderate support — cite it alongside strong evidence"
    else:
        rec = "Strong policy — lead with policy disclosure evidence in response"

    summary = (
        f"Policy {inp.dispute_id}: "
        f"score={total_score}/100 ({defensibility.value}) | "
        f"win_adj={win_adj:+.2f}"
    )

    return PolicyDefensibilityReport(
        dispute_id=inp.dispute_id,
        policy_type=inp.policy_type,
        dimensions=dimensions,
        total_score=total_score,
        defensibility=defensibility,
        strengths=strengths,
        weaknesses=weaknesses,
        network_compliance_ok=network_ok,
        win_rate_adjustment=win_adj,
        recommendation=rec,
        summary=summary,
    )
