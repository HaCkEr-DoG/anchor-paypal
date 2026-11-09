"""
Settlement Oracle — fight-vs-accept expected value engine.

This is the component no existing tool has. Every dispute tool either always
fights or always accepts. This calculates the actual economic decision.

Key insight: PayPal charges a $15 dispute fee win OR lose. For small amounts,
fighting costs more than accepting. The Oracle calculates the break-even.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import math


class ClaimType(str, Enum):
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED"
    SIGNIFICANTLY_NOT_AS_DESCRIBED = "SIGNIFICANTLY_NOT_AS_DESCRIBED"
    UNAUTHORIZED = "UNAUTHORIZED"
    BILLING_ISSUE = "BILLING_ISSUE"


class Recommendation(str, Enum):
    FIGHT = "FIGHT"
    ACCEPT = "ACCEPT"
    INSUFFICIENT_EVIDENCE = "GATHER_EVIDENCE_FIRST"


@dataclass
class Evidence:
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_buyer_communication: bool = False
    has_item_description_proof: bool = False
    has_refund_already_issued: bool = False
    has_digital_delivery_proof: bool = False  # download logs, access logs
    seller_account_age_days: int = 0
    seller_dispute_win_rate: float = 0.5  # historical win rate for this seller

    # Channel3 product graph (live when CHANNEL3_API_KEY is set)
    has_channel3_product_match: bool = False       # product found in 100M+ catalog
    channel3_price_variance_pct: float = 1.0       # 0.0=exact match, 1.0=no data; <0.15 boosts SNAD
    channel3_category_risk: float = 0.5            # 0=low-fraud category, 1=high-fraud

    # Kernel stealth-browser evidence (live when KERNEL_API_KEY is set)
    has_kernel_tracking_screenshot: bool = False   # visual delivery screenshot from carrier site
    has_signature_confirmation: bool = False        # adult/direct signature on delivery

    # Buyer risk signals (from fraud_detector)
    buyer_prior_dispute_count: int = 0             # buyer's dispute history
    is_first_purchase: bool = False                 # higher risk for unauthorized claims
    shipping_matches_buyer_address: bool = True     # address mismatch → fraud signal

    # Operational mode
    is_automated_fight: bool = True                 # Anchor handles it; reduces time cost


@dataclass
class OracleDecision:
    recommendation: Recommendation
    ev_fight: float           # expected value of fighting (can be negative)
    ev_accept: float          # incremental recovery value of accepting: $0 baseline
    win_probability: float
    confidence: float         # 0-1, how confident the Oracle is
    break_even_probability: float  # minimum p(win) needed to make fighting worth it
    reasoning: list[str]      # human-readable explanation steps
    time_to_respond_hours: int  # urgency signal


# Base win rates from PayPal dispute outcome data (industry research + Chargeflow data)
# Source: Chargeflow reports 75% win rate on INAD; 8.1% is industry manual average.
# UNAUTHORIZED: industry ceiling 5–15% without 3DS authentication (revised down from 21%).
BASE_WIN_RATES = {
    ClaimType.ITEM_NOT_RECEIVED: 0.68,       # Strong — tracking usually settles it
    ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED: 0.38,  # Subjective — harder to prove
    ClaimType.UNAUTHORIZED: 0.10,            # Weakest — card-issuer side; 5–15% without 3DS
    ClaimType.BILLING_ISSUE: 0.55,           # Moderate — paper trail usually clear
}

# PayPal charges two fee tiers:
#   Standard:    $15, REFUNDABLE on win
#   US high-volume: $30, NON-REFUNDABLE (eligible ratio ≥1.5% AND >100 sales in previous 3 full months)
PAYPAL_DISPUTE_FEE_STANDARD = 15.00
PAYPAL_DISPUTE_FEE_HIGH_VOLUME = 30.00
# time_cost = HOURS_TO_FIGHT × hourly_rate represents the merchant's operational cost.
# Default $75 ($50/h × 1.5h) benchmarks against a manual workflow and keeps demo
# outputs conservative. For zero-touch digital merchants where Anchor handles the
# full fight path autonomously, this scales toward $0–$15 (light audit overhead).
# When time_cost ≥ dispute_amount, break_even ≥ 1.0 — fighting is never EV-positive
# regardless of evidence; the Oracle will always return ACCEPT for such disputes.
DEFAULT_HOURLY_RATE = 50.00  # Seller's implicit time cost to fight
HOURS_TO_FIGHT = 1.5         # Realistic time to gather + submit evidence


def score_evidence(evidence: Evidence, claim_type: ClaimType) -> tuple[float, list[str]]:
    """
    Returns (multiplier, reasons) where multiplier is in [0.40, 1.55].

    Uses additive deltas on a base of 1.0 so each signal contributes a
    bounded amount — no single piece of evidence can dominate the score.
    Channel3 product data and Kernel screenshots are incorporated when live.
    """
    delta = 0.0
    reasons: list[str] = []

    # ── Claim-type primary evidence ──────────────────────────────────────────
    if claim_type == ClaimType.ITEM_NOT_RECEIVED:
        if evidence.has_tracking_number and evidence.has_delivery_confirmation:
            delta += 0.38   # Both = near-certain proof of delivery
            reasons.append("Tracking + confirmed delivery (+38%)")
        elif evidence.has_tracking_number and evidence.has_kernel_tracking_screenshot:
            delta += 0.35   # Kernel screenshot of carrier page is strong visual proof
            reasons.append("Tracking + Kernel carrier screenshot (+35%)")
        elif evidence.has_tracking_number:
            delta += 0.18
            reasons.append("Tracking number present (+18%)")
        else:
            delta -= 0.42   # Without any tracking, INR is very hard to win
            reasons.append("No tracking — hard to prove delivery (−42%)")

        if evidence.has_signature_confirmation:
            delta += 0.10   # Adult signature = additional layer of proof
            reasons.append("Signature confirmation (+10%)")

    elif claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        if evidence.has_item_description_proof and evidence.has_buyer_communication:
            delta += 0.28
            reasons.append("Item description proof + buyer communication (+28%)")
        elif evidence.has_item_description_proof:
            delta += 0.14
            reasons.append("Item description proof (+14%)")

        # Channel3: price match vs catalog is strong SNAD counter-evidence
        if evidence.has_channel3_product_match:
            if evidence.channel3_price_variance_pct < 0.05:
                delta += 0.18   # Price exactly matches catalog → item is as described
                reasons.append("Channel3: price within 5% of catalog (+18%)")
            elif evidence.channel3_price_variance_pct < 0.15:
                delta += 0.10
                reasons.append("Channel3: price within 15% of catalog (+10%)")
            else:
                delta += 0.04   # At least product exists in catalog
                reasons.append("Channel3: product found in catalog (+4%)")

        if evidence.has_digital_delivery_proof:
            delta += 0.20   # Access/download logs are hard to fake
            reasons.append("Digital delivery proof (access logs) (+20%)")

    elif claim_type == ClaimType.UNAUTHORIZED:
        # Unauthorized: card-issuer sided. Base rate 10% — only fight with strong signals.
        if evidence.has_buyer_communication:
            delta += 0.28   # Explicit authorization message is gold
            reasons.append("Buyer authorization communication (+28%)")
        if evidence.shipping_matches_buyer_address:
            delta += 0.14   # Shipped to their address → unlikely truly unauthorized
            reasons.append("Shipping matches buyer address on file (+14%)")
        else:
            delta -= 0.05
            reasons.append("Address mismatch — elevated fraud signal (−5%)")
        if evidence.is_first_purchase:
            delta -= 0.08   # First-time buyers with unauthorized claims are riskier
            reasons.append("First purchase — higher unauthorized risk (−8%)")
        if not evidence.has_buyer_communication and not evidence.shipping_matches_buyer_address:
            delta -= 0.10   # Both missing → almost certain loss
            reasons.append("No communication + address mismatch (−10%)")

    elif claim_type == ClaimType.BILLING_ISSUE:
        if evidence.has_buyer_communication:
            delta += 0.12
            reasons.append("Buyer communication on record (+12%)")
        if evidence.has_item_description_proof:
            delta += 0.10
            reasons.append("Item description documentation (+10%)")

    # ── Buyer risk signals ───────────────────────────────────────────────────
    if evidence.buyer_prior_dispute_count >= 3:
        delta += 0.12   # Serial disputer — PayPal pattern-matches this
        reasons.append(f"Buyer has {evidence.buyer_prior_dispute_count} prior disputes (+12%)")
    elif evidence.buyer_prior_dispute_count == 0:
        delta -= 0.03   # No history = neutral-to-slight positive for buyer
        reasons.append("Buyer has no prior disputes (−3%)")

    # Channel3 category risk (high-fraud categories: electronics, luxury, crypto)
    if evidence.has_channel3_product_match and evidence.channel3_category_risk > 0.7:
        delta -= 0.08
        reasons.append("Channel3: high-fraud product category (−8%)")
    elif evidence.has_channel3_product_match and evidence.channel3_category_risk < 0.3:
        delta += 0.05
        reasons.append("Channel3: low-fraud product category (+5%)")

    # ── Seller account trust signals ─────────────────────────────────────────
    if evidence.seller_account_age_days > 365 * 3:
        delta += 0.08
        reasons.append("Seller account 3+ years old (+8%)")
    elif evidence.seller_account_age_days > 365:
        delta += 0.04
        reasons.append("Seller account 1–3 years old (+4%)")
    elif evidence.seller_account_age_days < 90:
        delta -= 0.10
        reasons.append("Seller account < 90 days — scrutinized more (−10%)")

    if evidence.seller_dispute_win_rate > 0.70:
        delta += 0.07
        reasons.append(f"Seller win rate {evidence.seller_dispute_win_rate:.0%} (+7%)")
    elif evidence.seller_dispute_win_rate < 0.30:
        delta -= 0.08
        reasons.append(f"Seller win rate {evidence.seller_dispute_win_rate:.0%} (−8%)")

    multiplier = max(0.40, min(1.0 + delta, 1.55))
    return multiplier, reasons


def calculate(
    dispute_amount: float,
    claim_type: ClaimType,
    evidence: Evidence,
    deadline_hours: int = 72,
    seller_hourly_rate: float = DEFAULT_HOURLY_RATE,
    similar_dispute_win_rate: Optional[float] = None,  # from Elasticsearch history
    high_volume_tier: bool = False,  # Confirmed/estimated PayPal fee tier; see README caveat
    dispute_fee_override: Optional[float] = None,  # user-supplied actual fee
    currency_code: str = "USD",  # ISO 4217 — Oracle fees are USD; non-USD produces a warning
    buyer_risk_boost: float = 0.0,  # additive boost from fraud_detector.win_probability_boost
) -> OracleDecision:
    """
    Core Oracle calculation.

    EV(fight)  = P(win) × amount − effective_cost
    EV(accept) = 0 (incremental recovery baseline; the disputed funds are already at risk)

    Standard tier:    effective_cost = time_cost + (1−p_win) × $15
    High-volume tier: effective_cost = time_cost + $30

    Break-even (minimum p_win to justify fighting):
      Standard:    p_break = (time_cost + 15) / (amount + 15)
      High-volume: p_break = (time_cost + 30) / amount

    NOTE: dispute fees ($15/$30) are USD constants. For non-USD disputes the EV
    comparison is cross-currency; treat the result as directional, not precise.
    """
    if not math.isfinite(dispute_amount) or dispute_amount < 0:
        raise ValueError("dispute_amount must be a finite, non-negative number")
    if dispute_amount > 100_000:
        raise ValueError("dispute_amount exceeds $100,000 — verify the currency and amount")
    if not math.isfinite(seller_hourly_rate) or seller_hourly_rate < 0:
        raise ValueError("seller_hourly_rate must be a finite, non-negative number")
    if dispute_fee_override is not None and (
        not math.isfinite(dispute_fee_override) or dispute_fee_override < 0
    ):
        raise ValueError("dispute_fee_override must be a finite, non-negative number")

    reasoning = []

    if currency_code.upper() != "USD":
        reasoning.append(
            f"⚠ Currency: {currency_code.upper()} — dispute fees ($15/$30) are USD constants; "
            "EV comparison is cross-currency and directional only"
        )

    # Fee tier — user override takes priority
    if dispute_fee_override is not None:
        dispute_fee = dispute_fee_override
        fee_note = f"${dispute_fee:.0f} (custom)"
    elif high_volume_tier:
        dispute_fee = PAYPAL_DISPUTE_FEE_HIGH_VOLUME
        fee_note = f"${dispute_fee:.0f} non-refundable (high-volume tier)"
    else:
        dispute_fee = PAYPAL_DISPUTE_FEE_STANDARD
        fee_note = f"${dispute_fee:.0f} refundable on win (standard tier)"
    reasoning.append(f"Dispute fee: {fee_note}")

    # Base probability
    p_base = BASE_WIN_RATES[claim_type]
    reasoning.append(f"Base win rate for {claim_type.value}: {p_base:.0%}")

    # Evidence-adjusted probability
    evidence_multiplier, evidence_reasons = score_evidence(evidence, claim_type)
    reasoning.extend(evidence_reasons)
    p_win = min(p_base * evidence_multiplier, 0.95)  # Never claim 100%

    # Buyer risk boost from fraud detector (applied before historical blend)
    if buyer_risk_boost > 0:
        p_win = min(p_win + buyer_risk_boost, 0.95)
        reasoning.append(f"Fraud detector boost: +{buyer_risk_boost:.0%} → {p_win:.0%}")

    # If we have historical data from Elasticsearch, blend it in (40% weight)
    if similar_dispute_win_rate is not None:
        historical_rate = min(max(float(similar_dispute_win_rate), 0.0), 1.0)
        p_win = min(max((p_win * 0.6) + (historical_rate * 0.4), 0.0), 0.95)
        reasoning.append(
            f"Blended with {historical_rate:.0%} Elastic historical win rate → {p_win:.0%}"
        )
    else:
        reasoning.append(f"Evidence-adjusted win probability: {p_win:.0%}")

    # Automation discount: when Anchor fights autonomously, human time cost is
    # ~10 min of oversight rather than 1.5h of manual evidence gathering.
    if evidence.is_automated_fight:
        time_cost = seller_hourly_rate * (10 / 60)   # 10 minutes
        reasoning.append(f"Automated fight — time cost ${time_cost:.2f} (10 min oversight)")
    else:
        time_cost = HOURS_TO_FIGHT * seller_hourly_rate
        reasoning.append(f"Manual fight — time cost ${time_cost:.2f} ({HOURS_TO_FIGHT}h)")

    # Break-even: solve EV(fight) = 0 for p analytically.
    # Standard tier: p*(A+15) = time+15  →  p = (time+15)/(A+15)
    # High-volume:   p*A      = time+30  →  p = (time+30)/A
    if dispute_amount == 0:
        break_even = 1.0
    elif high_volume_tier:
        break_even = (time_cost + dispute_fee) / dispute_amount
    else:
        break_even = (time_cost + dispute_fee) / (dispute_amount + dispute_fee)
    break_even = min(break_even, 1.0)  # cap: >1.0 means fighting is never EV-positive

    reasoning.append(
        f"Break-even win probability: {break_even:.0%} "
        f"({fee_note} + ~{HOURS_TO_FIGHT}h time = ${time_cost + dispute_fee:.0f} effective)"
    )

    # Expected values
    # Standard tier: fee refunded on win, so effective cost = time_cost + (1-p_win)*dispute_fee
    # High-volume: fee non-refundable, so effective cost = time_cost + dispute_fee
    if high_volume_tier:
        effective_cost = time_cost + dispute_fee
    else:
        effective_cost = time_cost + (1 - p_win) * dispute_fee
    ev_fight = (p_win * dispute_amount) - effective_cost
    # Both actions use the same incremental-recovery baseline. Accepting the
    # dispute recovers none of the disputed amount; fighting earns expected
    # recovery minus its handling and fee costs.
    ev_accept = 0.0

    reasoning.append(
        f"EV(fight) = {p_win:.0%} × ${dispute_amount:.2f} - ${effective_cost:.0f} effective cost = ${ev_fight:.2f}"
    )
    reasoning.append("EV(accept) = $0.00 incremental recovery (accepting yields no recovery)")

    # Decision
    ev_delta = ev_fight - ev_accept
    if ev_fight > ev_accept:
        recommendation = Recommendation.FIGHT
        reasoning.append(f"→ FIGHT: expected gain ${ev_fight:.2f}")
    elif p_win > break_even * 0.85 and _has_critical_evidence_gap(evidence, claim_type):
        # Close to break-even but missing key evidence — gather before deciding
        recommendation = Recommendation.INSUFFICIENT_EVIDENCE
        reasoning.append("→ GATHER EVIDENCE: win probability close to break-even, missing key proof")
    else:
        recommendation = Recommendation.ACCEPT
        reasoning.append(f"→ ACCEPT: fighting costs more than it recovers (EV=${ev_fight:.2f})")

    # Confidence: distance from break-even × evidence quality
    # A strong decision far from break-even with rich evidence = high confidence.
    # A borderline decision with minimal evidence = low confidence regardless of EV.
    ev_clarity = min(abs(p_win - break_even) / 0.25, 1.0)  # 25% margin = full EV clarity
    evidence_richness = sum([
        evidence.has_tracking_number,
        evidence.has_delivery_confirmation,
        evidence.has_kernel_tracking_screenshot,
        evidence.has_buyer_communication,
        evidence.has_item_description_proof,
        evidence.has_channel3_product_match,
        evidence.has_digital_delivery_proof,
        evidence.has_signature_confirmation,
    ]) / 4.0   # 4 pieces = fully rich (diminishing returns beyond that)
    confidence = round(min((ev_clarity * 0.65) + (min(evidence_richness, 1.0) * 0.35), 1.0), 3)

    # Urgency
    urgency_note = ""
    if deadline_hours < 24:
        urgency_note = f" ⚠️ URGENT: {deadline_hours}h remaining to respond"
        reasoning.append(urgency_note)

    return OracleDecision(
        recommendation=recommendation,
        ev_fight=round(ev_fight, 2),
        ev_accept=round(ev_accept, 2),
        win_probability=round(p_win, 3),
        confidence=confidence,
        break_even_probability=round(break_even, 3),
        reasoning=reasoning,
        time_to_respond_hours=deadline_hours,
    )


def _has_critical_evidence_gap(evidence: Evidence, claim_type: ClaimType) -> bool:
    """Returns True if the key evidence for this claim type is missing."""
    if claim_type == ClaimType.ITEM_NOT_RECEIVED:
        return not evidence.has_tracking_number
    if claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        return not evidence.has_item_description_proof
    if claim_type == ClaimType.UNAUTHORIZED:
        return not evidence.has_buyer_communication
    return False


# ── Quick sanity check ────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Scenario 1: $45 item not received — seller has tracking + delivery proof
    result = calculate(
        dispute_amount=45.00,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            # Neutral account history keeps this example focused on delivery proof.
            seller_account_age_days=365,
            seller_dispute_win_rate=0.5,
        ),
    )
    print(f"\nScenario 1: $45 INR with tracking")
    print(f"  Recommendation: {result.recommendation.value}")
    print(f"  Win prob: {result.win_probability:.0%} | EV(fight): ${result.ev_fight}")
    for r in result.reasoning:
        print(f"  → {r}")

    # Scenario 2: $12 item — not worth fighting regardless
    result2 = calculate(
        dispute_amount=12.00,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(has_tracking_number=True),
    )
    print(f"\nScenario 2: $12 INR — fee exceeds recovery")
    print(f"  Recommendation: {result2.recommendation.value}")
    print(f"  Break-even: {result2.break_even_probability:.0%} | EV(fight): ${result2.ev_fight}")

    # Scenario 3: $200 SNAD without evidence
    result3 = calculate(
        dispute_amount=200.00,
        claim_type=ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
        evidence=Evidence(seller_account_age_days=180),
    )
    print(f"\nScenario 3: $200 SNAD, no evidence")
    print(f"  Recommendation: {result3.recommendation.value}")
    print(f"  Win prob: {result3.win_probability:.0%} | EV(fight): ${result3.ev_fight}")

    # Scenario 4: $150 unauthorized — no buyer communication (should accept)
    result4 = calculate(
        dispute_amount=150.00,
        claim_type=ClaimType.UNAUTHORIZED,
        evidence=Evidence(seller_account_age_days=400),
    )
    print(f"\nScenario 4: $150 unauthorized, no buyer-comm evidence")
    print(f"  Recommendation: {result4.recommendation.value}")
    print(f"  Win prob: {result4.win_probability:.0%} | EV(fight): ${result4.ev_fight}")
    for r in result4.reasoning:
        print(f"  → {r}")

    # Scenario 5: $300 INR at high-volume tier — non-refundable $30 fee changes math
    result5 = calculate(
        dispute_amount=300.00,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(has_tracking_number=True, has_delivery_confirmation=True),
        high_volume_tier=True,
    )
    print(f"\nScenario 5: $300 INR, high-volume tier ($30 non-refundable)")
    print(f"  Recommendation: {result5.recommendation.value}")
    print(f"  Win prob: {result5.win_probability:.0%} | EV(fight): ${result5.ev_fight}")
    for r in result5.reasoning:
        print(f"  → {r}")
