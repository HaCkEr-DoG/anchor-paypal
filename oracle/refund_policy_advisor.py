"""
Refund Policy Advisor — advise on how the merchant's refund policy
affects dispute defensibility and what policy improvements would reduce
chargeback exposure.

Key insights:
  1. A clear, prominent return policy improves dispute win rates
  2. Final-sale / no-refund policies must be disclosed at checkout
  3. Subscription cancellation terms must be prominently disclosed
  4. Policy clarity affects Seller Protection eligibility for SNAD
  5. Policy age matters — a recently changed policy may not apply to old orders

Output per dispute:
  - PolicyCompliance: does the merchant's policy meet network requirements?
  - PolicyDefensibility: how well does the policy support fighting the dispute?
  - Policy gaps that expose the merchant to losing
  - Recommended improvements

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PolicyCompliance(str, Enum):
    COMPLIANT     = "COMPLIANT"       # Policy meets all requirements
    MINOR_GAPS    = "MINOR_GAPS"      # Small issues, still defensible
    MAJOR_GAPS    = "MAJOR_GAPS"      # Significant issues — fix before filing
    NON_COMPLIANT = "NON_COMPLIANT"   # Policy is insufficient for defense


class PolicyDefensibility(str, Enum):
    STRONG    = "STRONG"    # Policy clearly supports dispute defense
    ADEQUATE  = "ADEQUATE"  # Policy adequate but could be stronger
    WEAK      = "WEAK"      # Policy has weaknesses the buyer can exploit
    UNUSABLE  = "UNUSABLE"  # Policy cannot be used for defense


class PolicyType(str, Enum):
    RETURN      = "RETURN"
    REFUND      = "REFUND"
    SUBSCRIPTION_CANCEL = "SUBSCRIPTION_CANCEL"
    FINAL_SALE  = "FINAL_SALE"
    DIGITAL     = "DIGITAL"


@dataclass
class PolicyDetails:
    """Details about a merchant's policy."""
    policy_type: PolicyType

    # Disclosure quality
    is_on_checkout_page: bool = False     # Shown at time of purchase
    is_on_product_page: bool = False
    is_in_order_confirmation: bool = False
    requires_active_acceptance: bool = False  # Buyer must check a box

    # Policy terms
    return_window_days: Optional[int] = None   # None = no returns accepted
    has_free_return_shipping: bool = False
    is_final_sale_disclosure: bool = False

    # Subscription-specific
    cancel_requires_phone: bool = False    # Phone-only cancel = higher chargeback risk
    cancel_online_available: bool = True

    # Policy age relative to order
    policy_was_in_place_at_order: bool = True
    days_policy_active: int = 365


@dataclass
class RefundPolicyInput:
    """Input for refund policy analysis."""
    dispute_id: str
    claim_type: str    # INR / SNAD / SUBSCRIPTION / UNAUTHORIZED

    policies: list[PolicyDetails] = field(default_factory=list)

    # Order context
    order_amount: float = 0.0
    is_digital_goods: bool = False
    is_subscription: bool = False
    days_since_purchase: int = 0
    buyer_attempted_return: bool = False

    # Disclosure context
    policy_url_in_footer_only: bool = False    # Footer-only = weak
    policy_accepted_at_checkout: bool = False


@dataclass
class PolicyGap:
    gap: str
    severity: str    # HIGH / MEDIUM / LOW
    recommendation: str


@dataclass
class RefundPolicyReport:
    """Full refund policy analysis."""
    dispute_id: str
    compliance: PolicyCompliance
    defensibility: PolicyDefensibility
    gaps: list[PolicyGap]
    win_rate_impact: float    # Estimated additive impact on win rate (-0.20 to +0.10)
    policy_evidence_usable: bool
    key_findings: list[str]
    recommendations: list[str]
    summary: str


def _assess_disclosure(inp: RefundPolicyInput, policy: PolicyDetails) -> list[PolicyGap]:
    gaps = []
    if not policy.policy_was_in_place_at_order:
        gaps.append(PolicyGap(
            "Policy not in place at order date",
            "HIGH",
            "Policy changes must not be applied retroactively — cannot use this policy for defense"
        ))
    if policy.days_policy_active < 30:
        gaps.append(PolicyGap(
            "Policy recently introduced (< 30 days)",
            "MEDIUM",
            "Ensure policy was in place before the disputed order"
        ))
    if not policy.is_on_checkout_page and not policy.requires_active_acceptance:
        gaps.append(PolicyGap(
            "Policy not displayed at checkout",
            "HIGH",
            "Add policy link or summary to checkout page — mandatory for network compliance"
        ))
    if inp.policy_url_in_footer_only:
        gaps.append(PolicyGap(
            "Policy visible only in footer",
            "MEDIUM",
            "Move policy disclosure to checkout and product pages for stronger defense"
        ))
    return gaps


def _assess_subscription(policy: PolicyDetails) -> list[PolicyGap]:
    gaps = []
    if policy.cancel_requires_phone and not policy.cancel_online_available:
        gaps.append(PolicyGap(
            "Subscription cancellation requires phone call only",
            "HIGH",
            "Add self-service online cancellation — phone-only cancel is a leading chargeback trigger"
        ))
    if not policy.is_on_checkout_page:
        gaps.append(PolicyGap(
            "Subscription terms not shown at checkout",
            "HIGH",
            "Display recurring billing terms clearly at point of purchase"
        ))
    return gaps


def _assess_snad_policy(policy: PolicyDetails, days_since: int) -> list[PolicyGap]:
    gaps = []
    rw = policy.return_window_days
    if rw is not None and days_since > rw:
        gaps.append(PolicyGap(
            f"Return window expired ({days_since}d vs {rw}d policy)",
            "LOW",
            "Return window expiry strengthens defense — document clearly"
        ))
    if policy.is_final_sale_disclosure and not policy.is_on_checkout_page:
        gaps.append(PolicyGap(
            "Final-sale disclosure not at checkout",
            "HIGH",
            "Final-sale must be prominently disclosed at time of purchase — add to checkout"
        ))
    return gaps


def _compliance_level(gaps: list[PolicyGap]) -> PolicyCompliance:
    high_count = sum(1 for g in gaps if g.severity == "HIGH")
    med_count  = sum(1 for g in gaps if g.severity == "MEDIUM")
    if high_count >= 2:
        return PolicyCompliance.NON_COMPLIANT
    if high_count == 1:
        return PolicyCompliance.MAJOR_GAPS
    if med_count >= 2:
        return PolicyCompliance.MINOR_GAPS
    return PolicyCompliance.COMPLIANT


def _defensibility(compliance: PolicyCompliance,
                   policy_accepted: bool) -> PolicyDefensibility:
    if compliance == PolicyCompliance.NON_COMPLIANT:
        return PolicyDefensibility.UNUSABLE
    if compliance == PolicyCompliance.MAJOR_GAPS:
        return PolicyDefensibility.WEAK
    if compliance == PolicyCompliance.MINOR_GAPS:
        if policy_accepted:
            return PolicyDefensibility.ADEQUATE
        return PolicyDefensibility.WEAK
    # COMPLIANT
    if policy_accepted:
        return PolicyDefensibility.STRONG
    return PolicyDefensibility.ADEQUATE


def _win_rate_impact(defensibility: PolicyDefensibility) -> float:
    return {
        PolicyDefensibility.STRONG:   0.08,
        PolicyDefensibility.ADEQUATE: 0.02,
        PolicyDefensibility.WEAK:    -0.08,
        PolicyDefensibility.UNUSABLE: -0.15,
    }[defensibility]


def advise_refund_policy(inp: RefundPolicyInput) -> RefundPolicyReport:
    """Analyze merchant policy and advise on defensibility."""
    all_gaps: list[PolicyGap] = []
    findings: list[str] = []
    recs: list[str] = []

    for policy in inp.policies:
        all_gaps.extend(_assess_disclosure(inp, policy))
        if inp.is_subscription or policy.policy_type == PolicyType.SUBSCRIPTION_CANCEL:
            all_gaps.extend(_assess_subscription(policy))
        if inp.claim_type.upper() == "SNAD":
            all_gaps.extend(_assess_snad_policy(policy, inp.days_since_purchase))

    compliance    = _compliance_level(all_gaps)
    defensibility = _defensibility(compliance, inp.policy_accepted_at_checkout)
    impact        = _win_rate_impact(defensibility)
    usable        = defensibility not in (PolicyDefensibility.UNUSABLE,)

    if inp.buyer_attempted_return and any(
            p.return_window_days is not None for p in inp.policies):
        findings.append("Buyer attempted return — return policy directly at issue")

    if not inp.policies:
        all_gaps.append(PolicyGap(
            "No policy on file",
            "HIGH",
            "Create and publish a clear return/refund policy immediately"
        ))
        compliance    = PolicyCompliance.NON_COMPLIANT
        defensibility = PolicyDefensibility.UNUSABLE
        usable        = False
        findings.append("No refund policy documents provided")

    if defensibility == PolicyDefensibility.STRONG:
        findings.append("Policy is well-disclosed — include policy evidence in dispute response")
    elif defensibility == PolicyDefensibility.UNUSABLE:
        findings.append("Policy cannot be used as evidence — focus on other defense pillars")

    recs = [g.recommendation for g in all_gaps if g.severity in ("HIGH", "MEDIUM")]
    if not recs:
        recs.append("Current policy is adequate — maintain clear checkout disclosures")

    summary = (
        f"Policy {inp.dispute_id}: "
        f"compliance={compliance.value} | defense={defensibility.value} | "
        f"win_impact={impact:+.0%} | gaps={len(all_gaps)}"
    )

    return RefundPolicyReport(
        dispute_id=inp.dispute_id,
        compliance=compliance,
        defensibility=defensibility,
        gaps=all_gaps,
        win_rate_impact=round(impact, 3),
        policy_evidence_usable=usable,
        key_findings=findings,
        recommendations=recs,
        summary=summary,
    )
