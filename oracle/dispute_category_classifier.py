"""
Dispute Category Classifier — classify incoming disputes into actionable
strategy buckets before deep analysis begins.

Given signals available at dispute intake (reason code, amount, network,
buyer history, evidence availability, seller protection eligibility),
this oracle assigns a primary category and recommended first action.

Categories:
  AUTO_ACCEPT     — Not worth fighting; accept immediately
  STRONG_FIGHT    — High confidence win; file response right away
  STANDARD_FIGHT  — Fight with normal evidence gathering
  ESCALATION      — Route to senior agent or legal team
  CE3_CANDIDATE   — Compelling Evidence 3.0 eligible (Visa unauthorized)
  INR_TRACK       — INR with strong delivery evidence; fight
  SNAD_ACCEPT     — SNAD with no policy out; accept early
  SP_ELIGIBLE     — Seller Protection covers this; submit SP claim
  NEEDS_REVIEW    — Ambiguous; needs human review

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeCategory(str, Enum):
    AUTO_ACCEPT    = "AUTO_ACCEPT"      # Accept now, no fight value
    STRONG_FIGHT   = "STRONG_FIGHT"     # Fight immediately — high win probability
    STANDARD_FIGHT = "STANDARD_FIGHT"   # Fight with standard evidence gathering
    ESCALATION     = "ESCALATION"       # Send to senior agent or legal
    CE3_CANDIDATE  = "CE3_CANDIDATE"    # CE3.0 eligible — Visa unauthorized
    INR_TRACK      = "INR_TRACK"        # INR with delivery evidence
    SNAD_ACCEPT    = "SNAD_ACCEPT"      # SNAD with no viable defense
    SP_ELIGIBLE    = "SP_ELIGIBLE"      # Seller Protection claim available
    NEEDS_REVIEW   = "NEEDS_REVIEW"     # Ambiguous — human needed


class FirstAction(str, Enum):
    ACCEPT_IMMEDIATELY  = "ACCEPT_IMMEDIATELY"
    FILE_SP_CLAIM       = "FILE_SP_CLAIM"
    SUBMIT_CE3_PACKAGE  = "SUBMIT_CE3_PACKAGE"
    FILE_RESPONSE       = "FILE_RESPONSE"
    GATHER_EVIDENCE     = "GATHER_EVIDENCE"
    ESCALATE_TO_AGENT   = "ESCALATE_TO_AGENT"
    REQUEST_MORE_INFO   = "REQUEST_MORE_INFO"


class SellerProtectionResult(str, Enum):
    ELIGIBLE    = "ELIGIBLE"
    INELIGIBLE  = "INELIGIBLE"
    PARTIAL     = "PARTIAL"     # Eligible but missing one requirement


@dataclass
class DisputeClassifierInput:
    """Signals available at dispute intake."""
    dispute_id: str
    reason_code: str            # e.g. "UNAUTHORIZED", "INR", "SNAD", "SUBSCRIPTION"
    network: str                # PAYPAL, VISA, MC, AMEX, DISCOVER
    transaction_amount: float

    # Timeline
    days_since_transaction: int = 0
    days_since_dispute_opened: int = 0
    response_window_days: int = 20

    # Evidence available at intake
    has_tracking_with_delivery: bool = False
    has_signature_confirmation: bool = False   # Required for ≥$750 SP
    has_3ds_authentication: bool = False
    has_avs_match: bool = False
    has_buyer_communication: bool = False
    evidence_completeness_score: int = 0       # 0–100 from evidence_gap_analyzer

    # Buyer signals
    buyer_dispute_count: int = 0
    buyer_risk_class: str = "NORMAL"           # TRUSTED/NORMAL/ELEVATED/HIGH_RISK/FRAUDSTER
    buyer_dispute_rate: float = 0.0
    is_prior_ce3_transaction: bool = False     # 2+ prior non-disputed txns with this buyer

    # Merchant context
    ltv_tier: str = "MEDIUM"                   # VIP/HIGH/MEDIUM/LOW/NEGLIGIBLE
    has_seller_protection: bool = True         # Merchant enrolled in SP
    seller_protection_history: str = "NORMAL" # NORMAL / EXHAUSTED

    # Win probability (optional — from prior analysis)
    win_probability_estimate: Optional[float] = None


@dataclass
class CategorySignal:
    """A signal that contributed to the classification."""
    signal: str
    value: str
    weight: int   # Points added toward category


@dataclass
class DisputeClassificationReport:
    """Full classification result."""
    dispute_id: str
    reason_code: str
    network: str

    primary_category: DisputeCategory
    confidence: float              # 0–1
    first_action: FirstAction

    seller_protection: SellerProtectionResult
    sp_missing_requirement: str    # If PARTIAL, what's missing

    ce3_eligible: bool
    ce3_boost: float               # +0.18 if eligible

    contributing_signals: list[CategorySignal]
    flags: list[str]               # Notable conditions
    rationale: str
    summary: str


# ─── Reason code routing tables ──────────────────────────────────────────────

_SP_COVERED_CODES = {"INR", "UNAUTHORIZED", "10.4", "4807"}
_SP_NEVER_CODES   = {"SNAD", "13.1", "13.3", "4853"}
_CE3_CODES        = {"UNAUTHORIZED", "10.4", "4807"}
_INR_CODES        = {"INR", "ITEM_NOT_RECEIVED"}
_SNAD_CODES       = {"SNAD", "13.1", "13.3", "SIGNIFICANTLY_NOT_AS_DESCRIBED"}
_SUB_CODES        = {"SUBSCRIPTION", "12.6", "RECURRING"}
_LOW_VALUE_THRESHOLD = 25.0        # Accept without fight if below this


def _check_seller_protection(inp: DisputeClassifierInput) -> tuple[SellerProtectionResult, str]:
    """Return SP eligibility and missing requirement if any."""
    code = inp.reason_code.upper()

    if not inp.has_seller_protection:
        return SellerProtectionResult.INELIGIBLE, "Merchant not enrolled in Seller Protection"

    if code in _SP_NEVER_CODES:
        return SellerProtectionResult.INELIGIBLE, f"Reason code {code} never covered by SP"

    if code not in _SP_COVERED_CODES:
        return SellerProtectionResult.INELIGIBLE, f"Reason code {code} not covered by SP"

    # INR + UNAUTHORIZED — need tracking; Unauthorized ≥$750 also needs signature
    if not inp.has_tracking_with_delivery and code in _INR_CODES:
        return SellerProtectionResult.PARTIAL, "No delivery confirmation — SP requires proof of delivery"

    if inp.transaction_amount >= 750 and not inp.has_signature_confirmation:
        return SellerProtectionResult.PARTIAL, f"Amount ${inp.transaction_amount:.0f} ≥ $750 requires signature confirmation"

    if inp.seller_protection_history == "EXHAUSTED":
        return SellerProtectionResult.INELIGIBLE, "Merchant has exhausted Seller Protection for this reason code"

    return SellerProtectionResult.ELIGIBLE, ""


def _ce3_check(inp: DisputeClassifierInput) -> tuple[bool, float]:
    """Check CE3.0 eligibility. Returns (eligible, win_boost)."""
    code = inp.reason_code.upper()
    if code not in _CE3_CODES:
        return False, 0.0
    if inp.network.upper() != "VISA":
        return False, 0.0
    if not inp.is_prior_ce3_transaction:
        return False, 0.0
    return True, 0.18


def classify_dispute(inp: DisputeClassifierInput) -> DisputeClassificationReport:
    """Classify a dispute into an actionable category."""
    code = inp.reason_code.upper()
    net  = inp.network.upper()

    sp_result, sp_missing = _check_seller_protection(inp)
    ce3_eligible, ce3_boost = _ce3_check(inp)

    signals: list[CategorySignal] = []
    flags: list[str] = []

    # ── Scoring buckets (accumulate points per category) ─────────────────────
    scores: dict[DisputeCategory, int] = {c: 0 for c in DisputeCategory}

    # --- Amount signal ---
    if inp.transaction_amount < _LOW_VALUE_THRESHOLD:
        scores[DisputeCategory.AUTO_ACCEPT] += 25
        signals.append(CategorySignal("transaction_amount", f"${inp.transaction_amount:.2f}", 25))
    elif inp.transaction_amount >= 500:
        scores[DisputeCategory.ESCALATION] += 10
        signals.append(CategorySignal("high_value", f"${inp.transaction_amount:.0f}", 10))

    # --- Seller Protection eligible → strong fight path ---
    if sp_result == SellerProtectionResult.ELIGIBLE:
        scores[DisputeCategory.SP_ELIGIBLE] += 40
        signals.append(CategorySignal("seller_protection", "ELIGIBLE", 40))
    elif sp_result == SellerProtectionResult.PARTIAL:
        scores[DisputeCategory.SP_ELIGIBLE] += 15
        flags.append(f"SP partially eligible: {sp_missing}")

    # --- CE3.0 path ---
    if ce3_eligible:
        scores[DisputeCategory.CE3_CANDIDATE] += 50
        signals.append(CategorySignal("ce3_eligible", f"VISA unauthorized + prior txns +{ce3_boost:.0%}", 50))

    # --- INR with tracking ---
    if code in _INR_CODES and inp.has_tracking_with_delivery:
        scores[DisputeCategory.INR_TRACK] += 35
        signals.append(CategorySignal("inr_with_delivery_proof", "tracking confirmed", 35))

    # --- SNAD signals ---
    if code in _SNAD_CODES:
        if inp.evidence_completeness_score < 40:
            scores[DisputeCategory.SNAD_ACCEPT] += 30
            signals.append(CategorySignal("snad_low_evidence", f"score={inp.evidence_completeness_score}", 30))
        else:
            scores[DisputeCategory.STANDARD_FIGHT] += 20
            signals.append(CategorySignal("snad_with_evidence", f"score={inp.evidence_completeness_score}", 20))

    # --- Subscription codes ---
    if code in _SUB_CODES:
        if inp.has_buyer_communication:
            scores[DisputeCategory.STANDARD_FIGHT] += 20
        else:
            scores[DisputeCategory.STANDARD_FIGHT] += 10

    # --- High win probability ---
    wp = inp.win_probability_estimate
    if wp is not None:
        if wp >= 0.70:
            scores[DisputeCategory.STRONG_FIGHT] += 30
            signals.append(CategorySignal("win_prob", f"{wp:.0%}", 30))
        elif wp >= 0.50:
            scores[DisputeCategory.STANDARD_FIGHT] += 20
            signals.append(CategorySignal("win_prob", f"{wp:.0%}", 20))
        elif wp < 0.30:
            scores[DisputeCategory.AUTO_ACCEPT] += 15
            signals.append(CategorySignal("low_win_prob", f"{wp:.0%}", 15))

    # --- Evidence completeness ---
    if inp.evidence_completeness_score >= 70:
        scores[DisputeCategory.STRONG_FIGHT] += 20
        signals.append(CategorySignal("evidence_completeness", f"{inp.evidence_completeness_score}/100", 20))
    elif inp.evidence_completeness_score < 40:
        scores[DisputeCategory.AUTO_ACCEPT] += 10

    # --- Buyer risk ---
    if inp.buyer_risk_class in ("HIGH_RISK", "FRAUDSTER"):
        scores[DisputeCategory.STRONG_FIGHT] += 15
        signals.append(CategorySignal("buyer_risk", inp.buyer_risk_class, 15))
        flags.append("Serial dispute buyer detected — fight assertively")
    elif inp.buyer_risk_class == "TRUSTED" and inp.buyer_dispute_count == 0:
        scores[DisputeCategory.STANDARD_FIGHT] += 5

    # --- Escalation triggers ---
    if inp.transaction_amount >= 2000:
        scores[DisputeCategory.ESCALATION] += 20
        flags.append(f"High-value dispute: ${inp.transaction_amount:.0f} — legal/senior review recommended")
    if inp.buyer_dispute_rate >= 0.20:
        scores[DisputeCategory.ESCALATION] += 10
        flags.append(f"Buyer dispute rate {inp.buyer_dispute_rate:.0%} — fraud team flag")

    # --- Deadline pressure: if window nearly closed ---
    days_left = inp.response_window_days - inp.days_since_dispute_opened
    if days_left <= 2:
        flags.append(f"URGENT: only {days_left} day(s) left in response window")
        # Force immediate action — only STRONG_FIGHT or SP_ELIGIBLE
        if scores[DisputeCategory.SP_ELIGIBLE] > 0 or scores[DisputeCategory.STRONG_FIGHT] > 0:
            scores[DisputeCategory.STRONG_FIGHT] += 20
        else:
            scores[DisputeCategory.AUTO_ACCEPT] += 20

    # --- LTV safety net: never auto-accept VIP ---
    if inp.ltv_tier == "VIP" and scores[DisputeCategory.AUTO_ACCEPT] > scores[DisputeCategory.STANDARD_FIGHT]:
        scores[DisputeCategory.AUTO_ACCEPT] = 0
        scores[DisputeCategory.NEEDS_REVIEW] += 20
        flags.append("VIP buyer — overriding AUTO_ACCEPT; human review required")

    # ── Pick winner ───────────────────────────────────────────────────────────
    primary_cat = max(scores, key=lambda c: scores[c])
    top_score   = scores[primary_cat]

    # Confidence: ratio of winner score to sum of all scores
    total_score = sum(scores.values())
    confidence  = round(top_score / max(total_score, 1), 2)
    confidence  = min(1.0, confidence)

    # If nothing scored, default to NEEDS_REVIEW
    if top_score == 0:
        primary_cat = DisputeCategory.NEEDS_REVIEW
        confidence  = 1.0

    # ── Map category → first action ───────────────────────────────────────────
    action_map: dict[DisputeCategory, FirstAction] = {
        DisputeCategory.AUTO_ACCEPT:    FirstAction.ACCEPT_IMMEDIATELY,
        DisputeCategory.STRONG_FIGHT:   FirstAction.FILE_RESPONSE,
        DisputeCategory.STANDARD_FIGHT: FirstAction.GATHER_EVIDENCE,
        DisputeCategory.ESCALATION:     FirstAction.ESCALATE_TO_AGENT,
        DisputeCategory.CE3_CANDIDATE:  FirstAction.SUBMIT_CE3_PACKAGE,
        DisputeCategory.INR_TRACK:      FirstAction.FILE_RESPONSE,
        DisputeCategory.SNAD_ACCEPT:    FirstAction.ACCEPT_IMMEDIATELY,
        DisputeCategory.SP_ELIGIBLE:    FirstAction.FILE_SP_CLAIM,
        DisputeCategory.NEEDS_REVIEW:   FirstAction.REQUEST_MORE_INFO,
    }
    first_action = action_map[primary_cat]

    # ── Rationale ─────────────────────────────────────────────────────────────
    top_signal_str = "; ".join(f"{s.signal}={s.value}" for s in signals[:3]) or "no strong signals"
    rationale = (
        f"Classified as {primary_cat.value} (confidence {confidence:.0%}) based on: {top_signal_str}. "
        f"SP={sp_result.value}. CE3={ce3_eligible}. "
        f"First action: {first_action.value}."
    )

    summary = (
        f"Dispute {inp.dispute_id} [{net}|{inp.reason_code}|${inp.transaction_amount:.0f}]: "
        f"category={primary_cat.value} confidence={confidence:.0%} action={first_action.value}"
    )

    return DisputeClassificationReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        network=net,
        primary_category=primary_cat,
        confidence=confidence,
        first_action=first_action,
        seller_protection=sp_result,
        sp_missing_requirement=sp_missing,
        ce3_eligible=ce3_eligible,
        ce3_boost=ce3_boost,
        contributing_signals=signals,
        flags=flags,
        rationale=rationale,
        summary=summary,
    )
