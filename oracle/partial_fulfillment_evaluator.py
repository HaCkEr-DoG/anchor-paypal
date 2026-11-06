"""
Partial Fulfillment Evaluator — assess SNAD disputes where the buyer
received part of the order or an order that partially matches the
item description, and compute the fair refund amount.

For SNAD (Significantly Not As Described) and partial shipment disputes:
  - Evaluates how much of the order was fulfilled correctly
  - Scores item condition vs. advertised description
  - Computes a defensible partial refund amount
  - Determines if partial acceptance is better than full fight or full accept

Scoring dimensions:
  1. Quantity match (0–30): Were all ordered units delivered?
  2. Condition match (0–30): Does item condition match listing description?
  3. Specification match (0–25): Do specs/features match (size, color, model)?
  4. Functionality (0–15): Does the item work as intended?
  Deductions: for clearly mis-described items

Output: fulfillment score, recommended refund amount, fight/accept/partial advice

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FulfillmentVerdict(str, Enum):
    AS_DESCRIBED         = "AS_DESCRIBED"       # Score ≥85 — fight the SNAD
    SUBSTANTIALLY_MET    = "SUBSTANTIALLY_MET"  # Score 65–84 — partial refund reasonable
    PARTIALLY_MET        = "PARTIALLY_MET"      # Score 40–64 — substantial partial refund
    SIGNIFICANTLY_SHORT  = "SIGNIFICANTLY_SHORT"  # Score 20–39 — large refund warranted
    NOT_AS_DESCRIBED     = "NOT_AS_DESCRIBED"   # Score <20 — full refund or accept


class ConditionMismatch(str, Enum):
    NONE         = "NONE"       # Matches listing
    MINOR        = "MINOR"      # Slight wear, cosmetic only
    MODERATE     = "MODERATE"   # Noticeable difference from listing
    SIGNIFICANT  = "SIGNIFICANT"  # Major condition difference
    COUNTERFEIT  = "COUNTERFEIT"  # Item is fake — immediately indefensible


@dataclass
class PartialFulfillmentInput:
    """Input describing order fulfillment vs. what was advertised."""
    dispute_id: str
    transaction_amount: float
    reason_code: str  # SNAD or similar

    # Quantity
    ordered_quantity: int = 1
    delivered_quantity: int = 1

    # Condition
    advertised_condition: str = ""       # e.g. "New", "Like New", "Used - Good"
    received_condition: str = ""         # What buyer reports
    condition_mismatch: ConditionMismatch = ConditionMismatch.NONE

    # Specification match
    advertised_specs_match: bool = True  # Size, color, model number etc.
    missing_accessories: bool = False    # Items listed but not included
    wrong_variant_sent: bool = False     # e.g. wrong size / wrong color

    # Functionality
    item_functional: bool = True         # Does it work?
    partial_functionality: bool = False  # Works but not fully
    safety_issue: bool = False           # Dangerous item — always full refund

    # Buyer's claim strength
    buyer_has_photos: bool = False       # Buyer provided photo evidence
    buyer_returned_item: bool = False    # Item returned


@dataclass
class FulfillmentDimension:
    """Score for one fulfillment dimension."""
    dimension: str
    score: int
    max_score: int
    rationale: str


@dataclass
class PartialFulfillmentReport:
    """Full evaluation of partial fulfillment dispute."""
    dispute_id: str
    transaction_amount: float

    dimensions: list[FulfillmentDimension]
    total_score: int            # 0–100
    verdict: FulfillmentVerdict

    recommended_refund_amount: float
    recommended_refund_pct: float
    defensible_keep_amount: float

    fight_recommendation: str   # "FIGHT" / "PARTIAL_REFUND" / "FULL_REFUND"
    rationale: list[str]
    buyer_evidence_noted: list[str]

    win_rate_adjustment: float  # Adjustment to base win probability
    summary: str


def evaluate_partial_fulfillment(inp: PartialFulfillmentInput) -> PartialFulfillmentReport:
    """Evaluate partial fulfillment and compute recommended refund."""
    dimensions: list[FulfillmentDimension] = []
    rationale: list[str] = []
    buyer_evidence: list[str] = []

    # ── Safety check — immediate full refund ─────────────────────
    if inp.safety_issue:
        return PartialFulfillmentReport(
            dispute_id=inp.dispute_id,
            transaction_amount=inp.transaction_amount,
            dimensions=[],
            total_score=0,
            verdict=FulfillmentVerdict.NOT_AS_DESCRIBED,
            recommended_refund_amount=inp.transaction_amount,
            recommended_refund_pct=1.0,
            defensible_keep_amount=0.0,
            fight_recommendation="FULL_REFUND",
            rationale=["Safety issue reported — full refund, do not fight"],
            buyer_evidence_noted=[],
            win_rate_adjustment=-0.30,
            summary=f"Dispute {inp.dispute_id}: SAFETY ISSUE — full refund required",
        )

    # ── Dimension 1: Quantity match (0–30) ────────────────────────
    if inp.delivered_quantity >= inp.ordered_quantity:
        qty_score = 30
        rationale.append("All ordered units delivered")
    elif inp.delivered_quantity == 0:
        qty_score = 0
        rationale.append("CRITICAL: Nothing delivered — full refund warranted")
    else:
        pct_delivered = inp.delivered_quantity / max(inp.ordered_quantity, 1)
        qty_score = round(30 * pct_delivered)
        rationale.append(
            f"Partial delivery: {inp.delivered_quantity}/{inp.ordered_quantity} units delivered"
        )
    dimensions.append(FulfillmentDimension(
        "Quantity match", qty_score, 30,
        f"delivered={inp.delivered_quantity}/{inp.ordered_quantity}",
    ))

    # ── Dimension 2: Condition match (0–30) ───────────────────────
    cond_scores = {
        ConditionMismatch.NONE:        30,
        ConditionMismatch.MINOR:       22,
        ConditionMismatch.MODERATE:    12,
        ConditionMismatch.SIGNIFICANT:  5,
        ConditionMismatch.COUNTERFEIT:  0,
    }
    cond_score = cond_scores[inp.condition_mismatch]
    if inp.condition_mismatch == ConditionMismatch.COUNTERFEIT:
        rationale.append("COUNTERFEIT item — indefensible, full refund required")
    elif inp.condition_mismatch != ConditionMismatch.NONE:
        rationale.append(
            f"Condition mismatch: {inp.condition_mismatch.value} — "
            f"advertised '{inp.advertised_condition}' vs received '{inp.received_condition}'"
        )
    dimensions.append(FulfillmentDimension(
        "Condition match", cond_score, 30,
        f"mismatch={inp.condition_mismatch.value}",
    ))

    # ── Dimension 3: Specification match (0–25) ───────────────────
    spec_score = 25
    if not inp.advertised_specs_match:
        spec_score -= 15
        rationale.append("Specifications do not match listing")
    if inp.wrong_variant_sent:
        spec_score -= 10
        rationale.append("Wrong variant shipped (wrong size/color/model)")
    if inp.missing_accessories:
        spec_score -= 5
        rationale.append("Listed accessories not included")
    spec_score = max(0, spec_score)
    dimensions.append(FulfillmentDimension(
        "Specification match", spec_score, 25,
        f"specs_match={inp.advertised_specs_match} wrong_variant={inp.wrong_variant_sent}",
    ))

    # ── Dimension 4: Functionality (0–15) ─────────────────────────
    if inp.item_functional and not inp.partial_functionality:
        func_score = 15
    elif inp.partial_functionality:
        func_score = 7
        rationale.append("Item partially functional")
    else:
        func_score = 0
        rationale.append("Item non-functional")
    dimensions.append(FulfillmentDimension(
        "Functionality", func_score, 15,
        f"functional={inp.item_functional} partial={inp.partial_functionality}",
    ))

    # ── Total score ───────────────────────────────────────────────
    total = sum(d.score for d in dimensions)
    total = max(0, min(100, total))

    # ── Verdict ───────────────────────────────────────────────────
    if inp.condition_mismatch == ConditionMismatch.COUNTERFEIT or inp.delivered_quantity == 0:
        verdict = FulfillmentVerdict.NOT_AS_DESCRIBED
    elif total >= 85:
        verdict = FulfillmentVerdict.AS_DESCRIBED
    elif total >= 65:
        verdict = FulfillmentVerdict.SUBSTANTIALLY_MET
    elif total >= 40:
        verdict = FulfillmentVerdict.PARTIALLY_MET
    elif total >= 20:
        verdict = FulfillmentVerdict.SIGNIFICANTLY_SHORT
    else:
        verdict = FulfillmentVerdict.NOT_AS_DESCRIBED

    # ── Recommended refund ────────────────────────────────────────
    # Refund pct = 1 - (score / 100), clamped sensibly
    refund_pct_map = {
        FulfillmentVerdict.AS_DESCRIBED:        0.0,
        FulfillmentVerdict.SUBSTANTIALLY_MET:   0.20,
        FulfillmentVerdict.PARTIALLY_MET:       0.45,
        FulfillmentVerdict.SIGNIFICANTLY_SHORT: 0.70,
        FulfillmentVerdict.NOT_AS_DESCRIBED:    1.0,
    }
    refund_pct = refund_pct_map[verdict]
    # Adjust for partial delivery
    if inp.delivered_quantity < inp.ordered_quantity:
        undelivered_pct = 1 - (inp.delivered_quantity / max(inp.ordered_quantity, 1))
        refund_pct = max(refund_pct, undelivered_pct)

    refund_amount = round(inp.transaction_amount * refund_pct, 2)
    keep_amount   = round(inp.transaction_amount - refund_amount, 2)

    # ── Fight recommendation ──────────────────────────────────────
    if verdict == FulfillmentVerdict.AS_DESCRIBED:
        fight_rec = "FIGHT"
        win_adj = +0.05
    elif verdict in (FulfillmentVerdict.SUBSTANTIALLY_MET,):
        fight_rec = "PARTIAL_REFUND"
        win_adj = -0.05
    elif verdict in (FulfillmentVerdict.PARTIALLY_MET, FulfillmentVerdict.SIGNIFICANTLY_SHORT):
        fight_rec = "PARTIAL_REFUND"
        win_adj = -0.10
    else:
        fight_rec = "FULL_REFUND"
        win_adj = -0.20

    if inp.buyer_has_photos:
        buyer_evidence.append("Buyer has photographic evidence of condition mismatch")
        win_adj -= 0.05

    if inp.buyer_returned_item:
        buyer_evidence.append("Item has been returned — provide return receipt")

    summary = (
        f"Fulfillment {inp.dispute_id}: "
        f"score={total}/100 ({verdict.value}) | "
        f"refund={refund_pct:.0%} (${refund_amount:.0f}) | {fight_rec}"
    )

    return PartialFulfillmentReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        dimensions=dimensions,
        total_score=total,
        verdict=verdict,
        recommended_refund_amount=refund_amount,
        recommended_refund_pct=refund_pct,
        defensible_keep_amount=keep_amount,
        fight_recommendation=fight_rec,
        rationale=rationale,
        buyer_evidence_noted=buyer_evidence,
        win_rate_adjustment=win_adj,
        summary=summary,
    )
