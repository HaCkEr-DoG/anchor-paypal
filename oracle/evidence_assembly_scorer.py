"""
Evidence Assembly Scorer — scores and ranks a merchant's available evidence
for a dispute, identifies coverage gaps, and recommends what to gather next.

Evidence falls into five categories that PayPal weighs differently per dispute
reason. This oracle outputs:
  - A score per evidence item (0–1) based on category weight × quality
  - An overall coverage score (0–1) for the full evidence set
  - Coverage gaps: required categories that are missing or weak
  - Prioritised submission list (most impactful first)
  - Recommendation on whether evidence is sufficient to fight

Claim-type evidence weights (sum across categories is not normalised to 1
intentionally — different claims have different evidence complexity):
  INR   : tracking=0.40, delivery=0.30, comms=0.10, photo=0.05, policy=0.15
  SNAD  : photo=0.40, policy=0.25, comms=0.15, tracking=0.10, delivery=0.10
  UNAUTH: tracking=0.10, delivery=0.10, comms=0.40, policy=0.30, photo=0.10
  OTHER : tracking=0.20, delivery=0.20, comms=0.20, policy=0.20, photo=0.20

Coverage score = sum(weight_i × quality_i) for items present, then
normalised so a full set of perfect evidence = 1.0.

PayPal benefit:
  - Merchants submit the right evidence first; avoids submitting irrelevant files
  - Coverage gaps surfaced before the deadline give actionable collection tasks
  - Prevents disputes lost purely due to incomplete or disorganised submissions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ── Evidence categories ───────────────────────────────────────────────────────

class EvidenceCategory(str, Enum):
    TRACKING   = "TRACKING"    # shipping carrier tracking number / screenshot
    DELIVERY   = "DELIVERY"    # delivery confirmation (signature / photo)
    COMMS      = "COMMS"       # buyer-seller communication logs
    PHOTO      = "PHOTO"       # product photos / videos
    POLICY     = "POLICY"      # merchant refund/return policy citation


# Weights per claim type, keyed by EvidenceCategory
_WEIGHTS: dict[str, dict[EvidenceCategory, float]] = {
    "INR": {
        EvidenceCategory.TRACKING: 0.40,
        EvidenceCategory.DELIVERY: 0.30,
        EvidenceCategory.COMMS:    0.10,
        EvidenceCategory.PHOTO:    0.05,
        EvidenceCategory.POLICY:   0.15,
    },
    "SNAD": {
        EvidenceCategory.PHOTO:    0.40,
        EvidenceCategory.POLICY:   0.25,
        EvidenceCategory.COMMS:    0.15,
        EvidenceCategory.TRACKING: 0.10,
        EvidenceCategory.DELIVERY: 0.10,
    },
    "UNAUTH": {
        EvidenceCategory.COMMS:    0.40,
        EvidenceCategory.POLICY:   0.30,
        EvidenceCategory.TRACKING: 0.10,
        EvidenceCategory.DELIVERY: 0.10,
        EvidenceCategory.PHOTO:    0.10,
    },
    "OTHER": {
        EvidenceCategory.TRACKING: 0.20,
        EvidenceCategory.DELIVERY: 0.20,
        EvidenceCategory.COMMS:    0.20,
        EvidenceCategory.POLICY:   0.20,
        EvidenceCategory.PHOTO:    0.20,
    },
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class EvidenceItem:
    item_id: str
    category: EvidenceCategory
    description: str
    quality: float = 1.0    # 0 = unusable, 1 = perfect (e.g. 0.5 = screenshot only)
    file_name: Optional[str] = None


@dataclass
class ScoredEvidenceItem:
    item_id: str
    category: EvidenceCategory
    description: str
    quality: float
    file_name: Optional[str]
    weight: float               # claim-type weight for this category
    contribution: float         # weight × quality (unnormalised)
    submission_rank: int = 0    # 1-indexed, highest contribution first


@dataclass
class EvidenceGap:
    category: EvidenceCategory
    weight: float
    suggestion: str


@dataclass
class EvidenceAssemblyReport:
    dispute_id: str
    claim_type: str
    coverage_score: float                       # 0–1, 1.0 = perfect evidence set
    items: List[ScoredEvidenceItem] = field(default_factory=list)
    gaps: List[EvidenceGap] = field(default_factory=list)
    sufficient_to_fight: bool = False           # coverage_score ≥ 0.60
    overall_recommendation: str = ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _weights_for(claim_type: str) -> dict[EvidenceCategory, float]:
    return _WEIGHTS.get(claim_type.upper(), _WEIGHTS["OTHER"])


def _gap_suggestion(cat: EvidenceCategory, claim_type: str) -> str:
    _suggestions: dict[EvidenceCategory, dict[str, str]] = {
        EvidenceCategory.TRACKING: {
            "INR":    "Provide carrier tracking number and a screenshot from carrier website.",
            "SNAD":   "Include tracking to confirm item was delivered as listed.",
            "UNAUTH": "Show tracking to demonstrate order was fulfilled.",
            "OTHER":  "Attach shipping confirmation with tracking details.",
        },
        EvidenceCategory.DELIVERY: {
            "INR":    "Upload delivery confirmation: signature log or carrier photo-on-door.",
            "SNAD":   "Provide delivery confirmation showing item arrived before complaint.",
            "UNAUTH": "Delivery confirmation proves order was placed and received.",
            "OTHER":  "Attach delivery confirmation from your carrier.",
        },
        EvidenceCategory.COMMS: {
            "INR":    "Export buyer-seller messages showing delivery information was shared.",
            "SNAD":   "Show communications where buyer acknowledged the item or used it.",
            "UNAUTH": "Provide order confirmation emails and all communications with the buyer.",
            "OTHER":  "Export relevant buyer-seller messaging history.",
        },
        EvidenceCategory.PHOTO: {
            "INR":    "Photos of packaged item and shipping label help corroborate shipment.",
            "SNAD":   "High-quality product photos demonstrating item matches listing description.",
            "UNAUTH": "Photos of the packaged item can support legitimate sale evidence.",
            "OTHER":  "Attach product photos or screenshots of the listing.",
        },
        EvidenceCategory.POLICY: {
            "INR":    "Cite your shipping policy showing expected delivery window.",
            "SNAD":   "Reference your return/refund policy and how the buyer failed to follow it.",
            "UNAUTH": "Include your fraud prevention / verification policy if applicable.",
            "OTHER":  "Attach your merchant refund and return policy.",
        },
    }
    ct = claim_type.upper()
    return _suggestions.get(cat, {}).get(ct, _suggestions[cat]["OTHER"])


# ── Public API ────────────────────────────────────────────────────────────────

def score_evidence(
    dispute_id: str,
    claim_type: str,
    items: List[EvidenceItem],
) -> EvidenceAssemblyReport:
    """Score evidence items for a dispute and return an assembly report."""
    ct = claim_type.upper() if claim_type.upper() in _WEIGHTS else "OTHER"
    weights = _weights_for(ct)

    # Keep the best quality item per category (duplicates happen)
    best: dict[EvidenceCategory, EvidenceItem] = {}
    for it in items:
        if it.category not in best or it.quality > best[it.category].quality:
            best[it.category] = it

    scored: list[ScoredEvidenceItem] = []
    covered_categories = set(best.keys())

    for cat, it in best.items():
        w = weights.get(cat, 0.0)
        contribution = round(w * it.quality, 4)
        scored.append(ScoredEvidenceItem(
            item_id=it.item_id,
            category=cat,
            description=it.description,
            quality=it.quality,
            file_name=it.file_name,
            weight=w,
            contribution=contribution,
        ))

    # Sort by contribution descending for submission order
    scored.sort(key=lambda x: -x.contribution)
    for i, s in enumerate(scored):
        s.submission_rank = i + 1

    # Coverage: sum of contributions / max possible (all weights × 1.0)
    total_possible = sum(weights.values())
    total_achieved = sum(s.contribution for s in scored)
    coverage = round(min(1.0, total_achieved / total_possible) if total_possible > 0 else 0.0, 4)

    # Gaps: categories with no evidence or low weight coverage
    gaps: list[EvidenceGap] = []
    for cat, w in sorted(weights.items(), key=lambda x: -x[1]):
        if cat not in covered_categories:
            gaps.append(EvidenceGap(
                category=cat,
                weight=w,
                suggestion=_gap_suggestion(cat, ct),
            ))
        elif best[cat].quality < 0.5:
            gaps.append(EvidenceGap(
                category=cat,
                weight=w,
                suggestion=f"Improve {cat.value.lower()} quality (currently low). "
                           + _gap_suggestion(cat, ct),
            ))

    sufficient = coverage >= 0.60

    if coverage >= 0.90:
        rec = (
            f"Strong evidence set (coverage {coverage:.0%}). "
            "Submit confidently — you have high-quality evidence across key categories."
        )
    elif sufficient:
        rec = (
            f"Adequate evidence (coverage {coverage:.0%}). "
            f"Consider filling {len(gaps)} gap(s) before submitting to strengthen your case."
        )
    else:
        gap_names = ", ".join(g.category.value for g in gaps[:3])
        rec = (
            f"Insufficient evidence (coverage {coverage:.0%}). "
            f"Critical gaps: {gap_names}. Gather missing evidence before responding."
        )

    return EvidenceAssemblyReport(
        dispute_id=dispute_id,
        claim_type=ct,
        coverage_score=coverage,
        items=scored,
        gaps=gaps,
        sufficient_to_fight=sufficient,
        overall_recommendation=rec,
    )
