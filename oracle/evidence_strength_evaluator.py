"""
Evidence Strength Evaluator — assess the cumulative strength of the
merchant's evidence package for a dispute, and determine whether it
meets the threshold to win.

Evidence quality directly impacts win probability:
  - Strong evidence (tracking + delivery + customer comms) → ~75% win
  - Partial evidence (tracking only) → ~55% win
  - Minimal evidence → ~25% win
  - No evidence → ~5% win

This module also identifies gaps in the evidence package so the
merchant can prioritize collection before the response deadline.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceCategory(str, Enum):
    PROOF_OF_DELIVERY   = "PROOF_OF_DELIVERY"
    PROOF_OF_IDENTITY   = "PROOF_OF_IDENTITY"
    TRANSACTION_RECORD  = "TRANSACTION_RECORD"
    CUSTOMER_COMMS      = "CUSTOMER_COMMS"
    PRODUCT_DESCRIPTION = "PRODUCT_DESCRIPTION"
    REFUND_POLICY       = "REFUND_POLICY"
    USAGE_LOGS          = "USAGE_LOGS"
    LEGAL_AGREEMENT     = "LEGAL_AGREEMENT"


class EvidenceQuality(str, Enum):
    DEFINITIVE  = "DEFINITIVE"   # Conclusive — directly proves merchant's case
    STRONG      = "STRONG"       # Highly persuasive
    MODERATE    = "MODERATE"     # Useful but not conclusive
    WEAK        = "WEAK"         # Circumstantial
    MISSING     = "MISSING"      # Required but absent


class PackageStrength(str, Enum):
    COMPELLING  = "COMPELLING"   # Win probability ≥0.75
    ADEQUATE    = "ADEQUATE"     # Win probability 0.50–0.74
    MARGINAL    = "MARGINAL"     # Win probability 0.25–0.49
    INSUFFICIENT = "INSUFFICIENT" # Win probability <0.25


@dataclass
class EvidenceItem:
    """One piece of evidence."""
    name: str
    category: EvidenceCategory
    quality: EvidenceQuality
    is_present: bool
    description: str = ""


@dataclass
class EvidencePackageInput:
    """Input: the dispute context and available evidence."""
    dispute_id: str
    claim_type: str
    disputed_amount: float

    items: list[EvidenceItem] = field(default_factory=list)

    # Shorthand flags (used when items list is empty / sparse)
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_order_confirmation: bool = False
    has_customer_communication: bool = False
    has_refund_policy: bool = False
    has_terms_of_service: bool = False
    has_ip_address_log: bool = False
    has_usage_logs: bool = False

    # Deadline pressure
    days_until_deadline: int = 10


def _build_items_from_flags(inp: EvidencePackageInput) -> list[EvidenceItem]:
    """Convert shorthand boolean flags into EvidenceItem list."""
    items: list[EvidenceItem] = []

    def add(name: str, cat: EvidenceCategory, present: bool,
            q_yes: EvidenceQuality = EvidenceQuality.STRONG,
            q_no: EvidenceQuality = EvidenceQuality.MISSING) -> None:
        items.append(EvidenceItem(
            name=name, category=cat,
            quality=q_yes if present else q_no,
            is_present=present,
        ))

    add("tracking_number",         EvidenceCategory.PROOF_OF_DELIVERY,  inp.has_tracking_number,  EvidenceQuality.MODERATE)
    add("delivery_confirmation",   EvidenceCategory.PROOF_OF_DELIVERY,  inp.has_delivery_confirmation, EvidenceQuality.STRONG)
    add("signature_confirmation",  EvidenceCategory.PROOF_OF_DELIVERY,  inp.has_signature_confirmation, EvidenceQuality.DEFINITIVE)
    add("order_confirmation",      EvidenceCategory.TRANSACTION_RECORD, inp.has_order_confirmation, EvidenceQuality.STRONG)
    add("customer_communication",  EvidenceCategory.CUSTOMER_COMMS,     inp.has_customer_communication, EvidenceQuality.MODERATE)
    add("refund_policy",           EvidenceCategory.REFUND_POLICY,      inp.has_refund_policy, EvidenceQuality.MODERATE)
    add("terms_of_service",        EvidenceCategory.LEGAL_AGREEMENT,    inp.has_terms_of_service, EvidenceQuality.MODERATE)
    add("ip_address_log",          EvidenceCategory.PROOF_OF_IDENTITY,  inp.has_ip_address_log, EvidenceQuality.STRONG)
    add("usage_logs",              EvidenceCategory.USAGE_LOGS,         inp.has_usage_logs, EvidenceQuality.STRONG)

    return items


_QUALITY_WEIGHT = {
    EvidenceQuality.DEFINITIVE: 1.0,
    EvidenceQuality.STRONG:     0.70,
    EvidenceQuality.MODERATE:   0.40,
    EvidenceQuality.WEAK:       0.15,
    EvidenceQuality.MISSING:    0.0,
}

# How many "points" each category can contribute at most
_CATEGORY_MAX = {
    EvidenceCategory.PROOF_OF_DELIVERY:   30.0,
    EvidenceCategory.TRANSACTION_RECORD:  20.0,
    EvidenceCategory.CUSTOMER_COMMS:      15.0,
    EvidenceCategory.PROOF_OF_IDENTITY:   15.0,
    EvidenceCategory.USAGE_LOGS:          10.0,
    EvidenceCategory.PRODUCT_DESCRIPTION: 5.0,
    EvidenceCategory.REFUND_POLICY:       5.0,
    EvidenceCategory.LEGAL_AGREEMENT:     5.0,
}

_TOTAL_MAX = sum(_CATEGORY_MAX.values())   # 105.0


def _score_package(items: list[EvidenceItem]) -> float:
    """Return a score 0–100 for the evidence package."""
    # Accumulate per-category, capped at category max
    cat_scores: dict[EvidenceCategory, float] = {c: 0.0 for c in EvidenceCategory}
    for item in items:
        if item.is_present:
            w = _QUALITY_WEIGHT[item.quality]
            cat_max = _CATEGORY_MAX.get(item.category, 5.0)
            cat_scores[item.category] = min(
                cat_scores[item.category] + w * cat_max,
                cat_max,
            )
    total = sum(cat_scores.values())
    return round(min(100.0, total / _TOTAL_MAX * 100), 1)


def _win_probability(score: float) -> float:
    """Map evidence score to win probability."""
    # Piecewise linear: 0→0.05, 50→0.60, 75→0.80, 100→0.92
    if score >= 75:
        return round(0.80 + (score - 75) / 100, 2)
    if score >= 50:
        return round(0.60 + (score - 50) / 125, 2)
    if score >= 25:
        return round(0.25 + (score - 25) / 71.4, 2)
    return round(0.05 + score / 500, 2)


def _package_strength(p_win: float) -> PackageStrength:
    if p_win >= 0.75:
        return PackageStrength.COMPELLING
    if p_win >= 0.50:
        return PackageStrength.ADEQUATE
    if p_win >= 0.25:
        return PackageStrength.MARGINAL
    return PackageStrength.INSUFFICIENT


def _gaps(items: list[EvidenceItem], claim_type: str) -> list[str]:
    """List the most impactful missing evidence items."""
    gaps: list[str] = []
    ct = claim_type.upper()

    for item in items:
        if not item.is_present:
            # Prioritize by claim type relevance
            if ct in ("INR",) and item.category == EvidenceCategory.PROOF_OF_DELIVERY:
                gaps.insert(0, item.name)
            elif ct in ("UNAUTHORIZED",) and item.category == EvidenceCategory.PROOF_OF_IDENTITY:
                gaps.insert(0, item.name)
            else:
                gaps.append(item.name)
    return gaps


@dataclass
class EvidencePackageReport:
    """Full evaluation of an evidence package."""
    dispute_id: str
    evidence_score: float          # 0–100
    package_strength: PackageStrength
    win_probability: float

    present_items: list[str]
    missing_items: list[str]
    priority_gaps: list[str]       # Most critical missing items

    deadline_risk: str             # LOW / MEDIUM / HIGH / CRITICAL
    recommendation: str
    summary: str


def evaluate_evidence_package(inp: EvidencePackageInput) -> EvidencePackageReport:
    """Evaluate the strength of a merchant's evidence package."""
    # Build item list from flags if not explicitly provided
    items = inp.items if inp.items else _build_items_from_flags(inp)

    score = _score_package(items)
    p_win = _win_probability(score)
    strength = _package_strength(p_win)
    gaps = _gaps(items, inp.claim_type)

    present = [i.name for i in items if i.is_present]
    missing = [i.name for i in items if not i.is_present]

    # Deadline risk
    if inp.days_until_deadline <= 1:
        deadline_risk = "CRITICAL"
    elif inp.days_until_deadline <= 3:
        deadline_risk = "HIGH"
    elif inp.days_until_deadline <= 7:
        deadline_risk = "MEDIUM"
    else:
        deadline_risk = "LOW"

    # Recommendation
    if strength == PackageStrength.COMPELLING:
        rec = "Submit evidence now — package is compelling, file response immediately"
    elif strength == PackageStrength.ADEQUATE:
        rec = "Submit evidence; consider collecting priority gaps to strengthen package"
    elif strength == PackageStrength.MARGINAL:
        if gaps:
            rec = f"Collect priority evidence first: {', '.join(gaps[:2])}"
        else:
            rec = "Package is marginal — consider partial settlement to reduce risk"
    else:
        rec = "Insufficient evidence — refund or settle to avoid chargeback fee"

    summary = (
        f"Evidence {inp.dispute_id}: "
        f"score={score:.0f}/100 | strength={strength.value} | "
        f"p_win={p_win:.0%} | gaps={len(missing)}"
    )

    return EvidencePackageReport(
        dispute_id=inp.dispute_id,
        evidence_score=score,
        package_strength=strength,
        win_probability=p_win,
        present_items=present,
        missing_items=missing,
        priority_gaps=gaps[:3],
        deadline_risk=deadline_risk,
        recommendation=rec,
        summary=summary,
    )
