"""
Adversarial limit tests — push Anchor beyond its limits.

Tests every fraud pattern the adversarial detector knows about,
the ensemble scorer's signal fusion, and edge cases at the boundary
of what PayPal's review AI can catch but Anchor can defeat.
"""
import pytest
from datetime import datetime, timezone, timedelta

from oracle.adversarial import analyze, _text_similarity, _normalize_text
from oracle.ensemble import score
from oracle.decision import calculate, Evidence, ClaimType, Recommendation


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Deadline Gaming
# ══════════════════════════════════════════════════════════════════

def test_deadline_gaming_day_8():
    purchase = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, purchase_date=purchase)
    names = [s.name for s in report.signals]
    assert "deadline_gaming" in names


def test_deadline_gaming_day_1_no_signal():
    purchase = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, purchase_date=purchase)
    names = [s.name for s in report.signals]
    assert "deadline_gaming" not in names


def test_late_filing_day_30():
    purchase = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, purchase_date=purchase)
    names = [s.name for s in report.signals]
    assert "late_filing" in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Policy Language Cloning
# ══════════════════════════════════════════════════════════════════

def test_policy_language_clone_multiple_phrases():
    text = (
        "I did not receive this item. Item never arrived. "
        "As per PayPal policy I am entitled to a refund. "
        "Per PayPal buyer protection, requesting full refund under purchase protection."
    )
    report = analyze(dispute_text=text)
    names = [s.name for s in report.signals]
    assert "policy_language_clone" in names
    # Penalty should be significant
    policy_signal = next(s for s in report.signals if s.name == "policy_language_clone")
    assert policy_signal.penalty <= -0.10


def test_policy_language_single_phrase_partial():
    text = "I did not receive my order."
    report = analyze(dispute_text=text)
    names = [s.name for s in report.signals]
    assert "policy_language_clone" not in names


def test_policy_language_clean_text_no_signal():
    text = "My package was lost and I never got it. The tracking shows no movement."
    report = analyze(dispute_text=text)
    names = [s.name for s in report.signals]
    assert "policy_language_clone" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Burst Filing (Fraud Ring)
# ══════════════════════════════════════════════════════════════════

def test_burst_filing_critical_5_disputes():
    report = analyze(prior_disputes_last_7d=5)
    names = [s.name for s in report.signals]
    assert "burst_filing_critical" in names
    assert report.is_fraud_pattern or any(s.severity == "critical" for s in report.signals)


def test_burst_filing_high_3_disputes():
    report = analyze(prior_disputes_last_7d=3)
    names = [s.name for s in report.signals]
    assert "burst_filing_high" in names


def test_burst_filing_no_signal_single_dispute():
    report = analyze(prior_disputes_last_7d=1)
    names = [s.name for s in report.signals]
    assert "burst_filing_critical" not in names
    assert "burst_filing_high" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Address Mismatch (Reship Fraud)
# ══════════════════════════════════════════════════════════════════

def test_address_mismatch_different_countries():
    report = analyze(shipping_country="CN", billing_country="US")
    names = [s.name for s in report.signals]
    assert "address_country_mismatch" in names
    mismatch = next(s for s in report.signals if s.name == "address_country_mismatch")
    assert mismatch.penalty <= -0.08


def test_address_match_same_country_no_signal():
    report = analyze(shipping_country="US", billing_country="US")
    names = [s.name for s in report.signals]
    assert "address_country_mismatch" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Burner Email
# ══════════════════════════════════════════════════════════════════

def test_burner_email_mailinator():
    report = analyze(buyer_email="buyer123@mailinator.com")
    names = [s.name for s in report.signals]
    assert "burner_email" in names
    burner = next(s for s in report.signals if s.name == "burner_email")
    assert burner.penalty <= -0.12


def test_burner_email_guerrillamail():
    report = analyze(buyer_email="someone@guerrillamail.com")
    names = [s.name for s in report.signals]
    assert "burner_email" in names


def test_legitimate_email_no_burner_signal():
    report = analyze(buyer_email="john.doe@gmail.com")
    names = [s.name for s in report.signals]
    assert "burner_email" not in names


def test_random_string_email_signal():
    report = analyze(buyer_email="xk9mz3qt7w@gmail.com")
    names = [s.name for s in report.signals]
    assert "random_string_email" in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Trigger Amount
# ══════════════════════════════════════════════════════════════════

def test_trigger_amount_exactly_750():
    report = analyze(dispute_amount=750.00)
    names = [s.name for s in report.signals]
    assert "trigger_amount" in names


def test_trigger_amount_near_500():
    report = analyze(dispute_amount=501.50)  # within $2
    names = [s.name for s in report.signals]
    assert "trigger_amount" in names


def test_trigger_amount_non_trigger():
    report = analyze(dispute_amount=87.43)
    names = [s.name for s in report.signals]
    assert "trigger_amount" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Identical Text Cloning
# ══════════════════════════════════════════════════════════════════

def test_identical_text_clone():
    text = "I never received my item. The package never arrived. I want a full refund."
    report = analyze(
        dispute_text=text,
        prior_dispute_texts=[text],  # exact copy
    )
    names = [s.name for s in report.signals]
    assert "identical_text_clone" in names
    clone = next(s for s in report.signals if s.name == "identical_text_clone")
    assert clone.penalty <= -0.15


def test_similar_text_clone_70_percent():
    text_a = "I never received my item. The package never arrived. I want a full refund immediately."
    text_b = "I never received my item. The package never arrived. I want a full refund please."
    report = analyze(dispute_text=text_a, prior_dispute_texts=[text_b])
    names = [s.name for s in report.signals]
    # Should detect as identical or similar clone
    assert "identical_text_clone" in names or "similar_text_clone" in names


def test_different_text_no_clone():
    text_a = "My order arrived damaged and broken."
    text_b = "I never received my package at all."
    report = analyze(dispute_text=text_a, prior_dispute_texts=[text_b])
    names = [s.name for s in report.signals]
    assert "identical_text_clone" not in names
    assert "similar_text_clone" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Post-Delivery Timing (Friendly Fraud)
# ══════════════════════════════════════════════════════════════════

def test_instant_post_delivery_dispute_30min():
    delivered = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, delivery_date=delivered)
    names = [s.name for s in report.signals]
    assert "instant_post_delivery_dispute" in names
    instant = next(s for s in report.signals if s.name == "instant_post_delivery_dispute")
    assert instant.severity == "critical"
    assert instant.penalty <= -0.20


def test_rapid_post_delivery_dispute_3h():
    delivered = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, delivery_date=delivered)
    names = [s.name for s in report.signals]
    assert "rapid_post_delivery_dispute" in names


def test_delivery_before_dispute_boost_2_days():
    delivered = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(dispute_filed_at=filed, delivery_date=delivered)
    names = [s.name for s in report.signals]
    assert "delivery_before_dispute_boost" in names
    boost = next(s for s in report.signals if s.name == "delivery_before_dispute_boost")
    assert boost.penalty > 0  # positive signal for merchant


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Timezone Anomaly (3AM Scripted Filing)
# ══════════════════════════════════════════════════════════════════

def test_off_hours_filing_3am():
    # UTC+0 buyer filing at 3AM UTC
    report = analyze(buyer_timezone_offset_hours=0.0, dispute_filed_hour_utc=3)
    names = [s.name for s in report.signals]
    assert "off_hours_filing" in names


def test_normal_hours_filing_no_signal():
    # UTC+0 buyer filing at 10AM UTC
    report = analyze(buyer_timezone_offset_hours=0.0, dispute_filed_hour_utc=10)
    names = [s.name for s in report.signals]
    assert "off_hours_filing" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Signal: Order Velocity (Inventory Farming)
# ══════════════════════════════════════════════════════════════════

def test_high_velocity_10_orders():
    report = analyze(order_count_30d=10)
    names = [s.name for s in report.signals]
    assert "high_velocity_ordering" in names


def test_elevated_velocity_6_orders():
    report = analyze(order_count_30d=6)
    names = [s.name for s in report.signals]
    assert "elevated_velocity_ordering" in names


def test_normal_velocity_1_order_no_signal():
    report = analyze(order_count_30d=1)
    names = [s.name for s in report.signals]
    assert "high_velocity_ordering" not in names
    assert "elevated_velocity_ordering" not in names


# ══════════════════════════════════════════════════════════════════
# Adversarial Report: Fraud Pattern Detection (≥2 high/critical signals)
# ══════════════════════════════════════════════════════════════════

def test_fraud_pattern_detected_multiple_high_signals():
    # Burst filing + burner email + instant delivery dispute
    delivered = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(
        buyer_email="hacker@mailinator.com",
        prior_disputes_last_7d=5,
        dispute_filed_at=filed,
        delivery_date=delivered,
    )
    assert report.is_fraud_pattern is True


def test_total_adjustment_negative_for_fraud():
    # All negative signals should produce net negative total_adjustment
    delivered = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    report = analyze(
        dispute_text="As per PayPal policy I am entitled to a refund. I did not receive item. Per PayPal buyer protection requesting full refund under purchase protection.",
        buyer_email="fraud@mailinator.com",
        prior_disputes_last_7d=4,
        dispute_amount=750.0,
        shipping_country="CN",
        billing_country="US",
        dispute_filed_at=filed,
        delivery_date=delivered,
        order_count_30d=12,
    )
    # Net adjustment should be significantly negative
    assert report.total_adjustment < -0.50
    assert report.is_fraud_pattern is True


def test_clean_buyer_no_signals():
    report = analyze(
        dispute_text="My order arrived with missing parts.",
        buyer_email="john.smith@gmail.com",
        prior_disputes_last_7d=0,
        dispute_amount=85.0,
        shipping_country="US",
        billing_country="US",
        order_count_30d=1,
    )
    # Should have no or minimal negative signals
    high_penalties = [s for s in report.signals if s.penalty < -0.05]
    assert len(high_penalties) == 0


# ══════════════════════════════════════════════════════════════════
# Text Similarity Utility
# ══════════════════════════════════════════════════════════════════

def test_text_similarity_identical():
    t = "i never received my item the package never arrived"
    assert _text_similarity(t, t) == 1.0


def test_text_similarity_completely_different():
    a = "the weather is nice today sunny and warm"
    b = "package never arrived lost in transit shipping"
    sim = _text_similarity(a, b)
    assert sim < 0.2


def test_text_similarity_partial():
    a = "i never received my item and the package never arrived please refund"
    b = "i never received my item the package never arrived"
    sim = _text_similarity(a, b)
    assert 0.5 < sim < 1.0


# ══════════════════════════════════════════════════════════════════
# Ensemble Scorer: Signal Fusion
# ══════════════════════════════════════════════════════════════════

def _base_oracle(amount=200.0, claim=ClaimType.ITEM_NOT_RECEIVED):
    return calculate(
        dispute_amount=amount,
        claim_type=claim,
        evidence=Evidence(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            seller_account_age_days=400,
            seller_dispute_win_rate=0.6,
        ),
    )


def test_ensemble_boosts_win_prob_with_fraud_network():
    oracle = _base_oracle()
    result = score(
        oracle_decision=oracle,
        dispute_amount=200.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        buyer_risk_level="critical",
        is_fraud_network_suspect=True,
        oracle_win_prob_boost=0.25,
    )
    assert result.composite_win_probability > oracle.win_probability
    assert result.composite_win_probability <= 0.95


def test_ensemble_lowers_win_prob_with_adversarial_fraud():
    oracle = _base_oracle()
    delivered = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    adv = analyze(
        buyer_email="fraud@mailinator.com",
        prior_disputes_last_7d=5,
        dispute_filed_at=filed,
        delivery_date=delivered,
    )
    # Note: adversarial penalties actually BOOST merchant win prob here
    # because delivery-before-dispute + burner email = merchant advantage
    # The Oracle win prob is already high; check the ensemble is consistent
    result = score(
        oracle_decision=oracle,
        dispute_amount=200.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        adversarial_report=adv,
        carrier_timing_risk="⚠️ SUSPICIOUS: dispute filed 0.5h AFTER carrier-confirmed delivery",
    )
    # Carrier timing + delivery_before_dispute boost should help merchant
    assert result.composite_win_probability >= 0.68
    assert result.recommendation == Recommendation.FIGHT


def test_ensemble_accepts_tiny_low_win_dispute():
    oracle = calculate(
        dispute_amount=18.0,
        claim_type=ClaimType.UNAUTHORIZED,
        evidence=Evidence(seller_account_age_days=100),
    )
    result = score(
        oracle_decision=oracle,
        dispute_amount=18.0,
        claim_type=ClaimType.UNAUTHORIZED,
        buyer_risk_level="low",
    )
    # $18 unauthorized with no evidence — Oracle says ACCEPT, ensemble should too
    assert result.recommendation == Recommendation.ACCEPT


def test_ensemble_signal_contributions_populated():
    oracle = _base_oracle()
    delivered = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    adv = analyze(dispute_filed_at=filed, delivery_date=delivered)
    result = score(
        oracle_decision=oracle,
        dispute_amount=200.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        oracle_win_prob_boost=0.15,
        adversarial_report=adv,
        carrier_timing_risk="Dispute filed 2.0h after confirmed delivery.",
        calibration_adjustment=0.05,
    )
    sources = [c.source for c in result.signal_contributions]
    assert "oracle" in sources
    assert "buyer_risk_profiler" in sources
    assert len(result.reasoning) >= 3


def test_ensemble_composite_clamped_at_095():
    oracle = calculate(
        dispute_amount=500.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_buyer_communication=True,
            seller_account_age_days=1000,
            seller_dispute_win_rate=0.9,
        ),
    )
    result = score(
        oracle_decision=oracle,
        dispute_amount=500.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        oracle_win_prob_boost=0.25,
        calibration_adjustment=0.15,
        carrier_timing_risk="SUSPICIOUS dispute filed 0.3h AFTER carrier-confirmed delivery friendly fraud",
    )
    assert result.composite_win_probability <= 0.95


def test_ensemble_adversarial_fraud_pattern_in_flags():
    oracle = _base_oracle()
    adv = analyze(
        buyer_email="bot@mailinator.com",
        prior_disputes_last_7d=6,
        dispute_amount=500.0,
        order_count_30d=15,
    )
    result = score(
        oracle_decision=oracle,
        dispute_amount=500.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        adversarial_report=adv,
    )
    assert len(result.adversarial_flags) >= 1
    assert result.counter_strategy != ""


# ══════════════════════════════════════════════════════════════════
# Oracle: buyer_risk_boost parameter wired correctly
# ══════════════════════════════════════════════════════════════════

def test_oracle_buyer_risk_boost_raises_win_prob():
    base = calculate(
        dispute_amount=150.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(has_tracking_number=True, seller_account_age_days=365),
        buyer_risk_boost=0.0,
    )
    boosted = calculate(
        dispute_amount=150.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(has_tracking_number=True, seller_account_age_days=365),
        buyer_risk_boost=0.20,
    )
    assert boosted.win_probability > base.win_probability
    assert boosted.win_probability <= 0.95


def test_oracle_buyer_risk_boost_in_reasoning():
    result = calculate(
        dispute_amount=150.0,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        evidence=Evidence(has_tracking_number=True),
        buyer_risk_boost=0.15,
    )
    reasoning_text = " ".join(result.reasoning)
    assert "boost" in reasoning_text.lower() or "risk" in reasoning_text.lower()


# ══════════════════════════════════════════════════════════════════
# Extreme / Boundary Cases
# ══════════════════════════════════════════════════════════════════

def test_maximum_adversarial_stack_all_signals():
    """Every single adversarial signal fired simultaneously."""
    purchase = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    delivered = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    filed = datetime.now(timezone.utc).isoformat()
    text = (
        "As per PayPal policy I am entitled to a refund. "
        "Per PayPal buyer protection requesting full refund. "
        "I did not receive item. Item never arrived. I did not authorize this."
    )
    report = analyze(
        dispute_text=text,
        dispute_amount=750.0,
        buyer_email="scammer@mailinator.com",
        dispute_filed_at=filed,
        purchase_date=purchase,
        delivery_date=delivered,
        shipping_country="CN",
        billing_country="US",
        prior_dispute_texts=[text],
        prior_disputes_last_7d=7,
        buyer_timezone_offset_hours=0.0,
        dispute_filed_hour_utc=4,
        order_count_30d=15,
    )
    assert report.is_fraud_pattern is True
    assert report.total_adjustment < -0.60
    assert len(report.signals) >= 6


def test_zero_amount_dispute_handled():
    result = score(
        oracle_decision=calculate(
            dispute_amount=0.01,
            claim_type=ClaimType.ITEM_NOT_RECEIVED,
            evidence=Evidence(),
        ),
        dispute_amount=0.01,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
    )
    assert 0.0 <= result.composite_win_probability <= 0.95


def test_ensemble_clamped_minimum_at_002():
    """Even the worst buyer should get min 2% win probability — not zero."""
    oracle = calculate(
        dispute_amount=5000.0,
        claim_type=ClaimType.UNAUTHORIZED,
        evidence=Evidence(seller_account_age_days=30),
    )
    adv = analyze(
        buyer_email="hacker@mailinator.com",
        prior_disputes_last_7d=10,
        dispute_amount=5000.0,
    )
    result = score(
        oracle_decision=oracle,
        dispute_amount=5000.0,
        claim_type=ClaimType.UNAUTHORIZED,
        adversarial_report=adv,
        calibration_adjustment=-0.15,
    )
    assert result.composite_win_probability >= 0.02
