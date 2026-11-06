"""
Upgrade tests — covers all 6 strengthened systems.
"""
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock


# ══════════════════════════════════════════════════════════════════
# System 1: Dispute Rate Monitor — Trajectory Forecasting
# ══════════════════════════════════════════════════════════════════

def test_compute_trajectory_rising_rate():
    from agents.risk_monitor import _compute_trajectory
    # 30d rate = 0.008, 7d rate = 0.012, next threshold = 0.010
    velocity, days = _compute_trajectory(0.008, 0.012, 0.010)
    assert velocity > 0
    assert days is not None
    assert days > 0


def test_compute_trajectory_falling_rate():
    from agents.risk_monitor import _compute_trajectory
    velocity, days = _compute_trajectory(0.009, 0.005, 0.010)
    assert velocity < 0
    assert days is None  # falling — won't breach


def test_compute_trajectory_already_breached():
    from agents.risk_monitor import _compute_trajectory
    # current_rate already above next_threshold
    velocity, days = _compute_trajectory(0.012, 0.015, 0.010)
    assert days is None  # already past threshold


def test_compute_trajectory_flat():
    from agents.risk_monitor import _compute_trajectory
    velocity, days = _compute_trajectory(0.005, 0.005, 0.010)
    assert velocity == 0.0
    assert days is None


def test_account_health_has_trajectory_fields():
    from agents.risk_monitor import AccountHealth, DisputeRateWindow
    w = DisputeRateWindow(30, 10, 500.0, 2000, 10000.0)
    h = AccountHealth(
        merchant_id="m1", window_30d=w, window_90d=w,
        threshold_status="elevated", threshold_description="...",
        next_threshold=0.010, next_threshold_name="limited",
        safe_fight_quota=5, threshold_breach_at=15,
        projected_rate_all_accepted=0.005, projected_rate_all_fought=0.008,
        fight_posture="fight_freely", posture_reason="OK",
        open_disputes=2, checked_at="2026-11-06T00:00:00Z",
        rate_velocity_per_day=0.0002, days_until_next_threshold=25,
    )
    assert h.rate_velocity_per_day == 0.0002
    assert h.days_until_next_threshold == 25


# ══════════════════════════════════════════════════════════════════
# System 2: Carrier Verifier — DHL + Timing Analysis
# ══════════════════════════════════════════════════════════════════

def test_detect_carrier_dhl_jd_prefix():
    from agents.carrier import detect_carrier
    assert detect_carrier("JD014600006281792012") == "dhl"


def test_detect_carrier_ups_still_works():
    from agents.carrier import detect_carrier
    assert detect_carrier("1Z999AA10123456784") == "ups"


def test_detect_carrier_usps_still_works():
    from agents.carrier import detect_carrier
    assert detect_carrier("9400111899223397245374") == "usps"


def test_delivery_timing_risk_suspicious_1h():
    from agents.carrier import delivery_timing_risk, TrackingResult
    delivery = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    disputed = datetime.now(timezone.utc).isoformat()
    result = TrackingResult(
        tracking_number="123", carrier="UPS",
        delivered=True, delivery_date=delivery,
        delivery_location="New York, NY", signature_obtained=False,
        latest_status="Delivered", carrier_url="http://example.com",
    )
    signal = delivery_timing_risk(result, disputed)
    assert "SUSPICIOUS" in signal
    assert "friendly fraud" in signal.lower() or "1." in signal


def test_delivery_timing_risk_2_days_later():
    from agents.carrier import delivery_timing_risk, TrackingResult
    delivery = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    disputed = datetime.now(timezone.utc).isoformat()
    result = TrackingResult(
        tracking_number="123", carrier="FedEx",
        delivered=True, delivery_date=delivery,
        delivery_location=None, signature_obtained=False,
        latest_status="Delivered", carrier_url="http://example.com",
    )
    signal = delivery_timing_risk(result, disputed)
    assert "2." in signal or "days" in signal or "day" in signal


def test_delivery_timing_risk_before_delivery():
    from agents.carrier import delivery_timing_risk, TrackingResult
    delivery = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    disputed = datetime.now(timezone.utc).isoformat()
    result = TrackingResult(
        tracking_number="123", carrier="USPS",
        delivered=True, delivery_date=delivery,
        delivery_location=None, signature_obtained=False,
        latest_status="Delivered", carrier_url="http://example.com",
    )
    signal = delivery_timing_risk(result, disputed)
    assert signal == ""  # filed before delivery = genuine INR, no anomaly


def test_delivery_timing_risk_not_delivered():
    from agents.carrier import delivery_timing_risk, TrackingResult
    result = TrackingResult(
        tracking_number="123", carrier="DHL",
        delivered=False, delivery_date=None,
        delivery_location=None, signature_obtained=False,
        latest_status="In Transit", carrier_url="http://example.com",
    )
    assert delivery_timing_risk(result, datetime.now(timezone.utc).isoformat()) == ""


@pytest.mark.asyncio
async def test_dhl_verify_no_api_key_returns_none():
    os.environ.pop("DHL_API_KEY", None)
    from agents.carrier import _verify_dhl
    result = await _verify_dhl("JD014600006281792012")
    assert result is None


# ══════════════════════════════════════════════════════════════════
# System 3: Buyer Risk Profiler — Cross-Merchant + Win Prob Boost
# ══════════════════════════════════════════════════════════════════

def test_win_probability_boost_fraud_network():
    from agents.fraud_detector import win_probability_boost, BuyerRiskProfile
    p = BuyerRiskProfile(
        buyer_email="bad@test.com", merchant_id="m1",
        total_disputes_90d=4, total_disputes_180d=6,
        disputes_reversed_90d=2, total_transactions_90d=5,
        dispute_rate=0.8, risk_level="critical",
        risk_score=0.95, is_serial_disputer=True,
        recommendation="HIGH RISK",
        cross_merchant_dispute_count=4,
        is_fraud_network_suspect=True,
        oracle_win_prob_boost=0.0,
    )
    boost = win_probability_boost(p)
    assert boost == 0.25


def test_win_probability_boost_critical_no_reversals():
    from agents.fraud_detector import win_probability_boost, BuyerRiskProfile
    p = BuyerRiskProfile(
        buyer_email="b@test.com", merchant_id="m1",
        total_disputes_90d=5, total_disputes_180d=8,
        disputes_reversed_90d=0, total_transactions_90d=6,
        dispute_rate=0.83, risk_level="critical",
        risk_score=0.80, is_serial_disputer=True,
        recommendation="HIGH RISK",
    )
    assert win_probability_boost(p) == 0.12


def test_win_probability_boost_low_risk_zero():
    from agents.fraud_detector import win_probability_boost, BuyerRiskProfile
    p = BuyerRiskProfile(
        buyer_email="good@test.com", merchant_id="m1",
        total_disputes_90d=0, total_disputes_180d=0,
        disputes_reversed_90d=0, total_transactions_90d=20,
        dispute_rate=0.0, risk_level="low",
        risk_score=0.0, is_serial_disputer=False,
        recommendation="Normal risk",
    )
    assert win_probability_boost(p) == 0.0


def test_score_buyer_cross_merchant_raises_score():
    from agents.fraud_detector import _score_buyer
    # Same buyer, same everything, but add 3 cross-merchant disputes
    score_no_cm, _, _, _ = _score_buyer(2, 3, 0, 0.1, 0)
    score_with_cm, _, _, _ = _score_buyer(2, 3, 0, 0.1, 3)
    assert score_with_cm > score_no_cm


def test_no_es_fallback_has_cross_merchant_fields():
    # Verify new fields exist on the no-ES fallback profile
    import asyncio
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from agents.fraud_detector import score_buyer
    profile = asyncio.run(score_buyer("test@example.com", "merchant1"))
    assert hasattr(profile, "cross_merchant_dispute_count")
    assert hasattr(profile, "is_fraud_network_suspect")
    assert hasattr(profile, "oracle_win_prob_boost")
    assert profile.oracle_win_prob_boost == 0.0


# ══════════════════════════════════════════════════════════════════
# System 4: Settlement Negotiation Engine — Age Factor + Min Floor
# ══════════════════════════════════════════════════════════════════

def test_minimum_floor_skips_tiny_disputes():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    result = evaluate_negotiation(
        dispute_amount=20.0,  # below $30 floor
        win_probability=0.55,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        ev_fight=5.0,
    )
    assert result.should_negotiate is False
    assert "minimum" in result.reasoning.lower()


def test_age_adjusted_fraction_fresh():
    from agents.negotiator import _age_adjusted_fraction
    assert _age_adjusted_fraction(0.50, 1) == 0.50  # no change < 3 days


def test_age_adjusted_fraction_one_week():
    from agents.negotiator import _age_adjusted_fraction
    assert _age_adjusted_fraction(0.50, 5) == 0.53  # +3% at 3-7 days


def test_age_adjusted_fraction_two_weeks():
    from agents.negotiator import _age_adjusted_fraction
    assert _age_adjusted_fraction(0.50, 10) == 0.56  # +6% at 8-14 days


def test_age_adjusted_fraction_old():
    from agents.negotiator import _age_adjusted_fraction
    assert _age_adjusted_fraction(0.50, 20) == 0.60  # +10% at >14 days


def test_age_adjusted_fraction_capped():
    from agents.negotiator import _age_adjusted_fraction
    # Cap at 0.90 regardless of age
    result = _age_adjusted_fraction(0.85, 20)
    assert result <= 0.90


def test_evaluate_negotiation_older_dispute_higher_offer():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    fresh = evaluate_negotiation(200.0, 0.55, ClaimType.ITEM_NOT_RECEIVED, 30.0, dispute_age_days=0)
    old = evaluate_negotiation(200.0, 0.55, ClaimType.ITEM_NOT_RECEIVED, 30.0, dispute_age_days=20)
    # Older dispute should have higher offer fraction / amount
    assert old.offer_fraction >= fresh.offer_fraction


# ══════════════════════════════════════════════════════════════════
# System 5: Deadline Guardian — Response Template + Recovery
# ══════════════════════════════════════════════════════════════════

def test_generate_response_template_inr_with_delivery():
    from agents.deadline_tracker import generate_response_template, OpenDispute
    d = OpenDispute(
        dispute_id="PP-D-999",
        merchant_id="m1",
        amount=85.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at="2026-11-01T00:00:00Z",
        response_deadline="2026-11-11T00:00:00Z",
    )
    text = generate_response_template(
        d,
        tracking_number="9400111899223397245374",
        delivered=True,
        delivery_date="2026-11-03T14:00:00Z",
        delivery_location="Austin, TX",
    )
    assert "9400111899223397245374" in text
    assert "Austin, TX" in text
    assert "delivered" in text.lower()


def test_generate_response_template_snad():
    from agents.deadline_tracker import generate_response_template, OpenDispute
    d = OpenDispute(
        dispute_id="PP-D-888",
        merchant_id="m1",
        amount=120.0,
        reason_code="SIGNIFICANTLY_NOT_AS_DESCRIBED",
        opened_at="2026-11-01T00:00:00Z",
        response_deadline="2026-11-11T00:00:00Z",
    )
    text = generate_response_template(d)
    assert "listing" in text.lower() or "described" in text.lower()


def test_generate_response_template_unauthorized():
    from agents.deadline_tracker import generate_response_template, OpenDispute
    d = OpenDispute(
        dispute_id="PP-D-777",
        merchant_id="m1",
        amount=200.0,
        reason_code="UNAUTHORIZED",
        opened_at="2026-11-01T00:00:00Z",
        response_deadline="2026-11-11T00:00:00Z",
    )
    text = generate_response_template(d)
    assert "authorized" in text.lower()


def test_generate_response_template_always_returns_string():
    from agents.deadline_tracker import generate_response_template, OpenDispute
    d = OpenDispute(
        dispute_id="PP-D-000",
        merchant_id="m1",
        amount=50.0,
        reason_code="BILLING_ISSUE",
        opened_at="2026-11-01T00:00:00Z",
        response_deadline="2026-11-11T00:00:00Z",
    )
    text = generate_response_template(d)
    assert isinstance(text, str)
    assert len(text) > 50


@pytest.mark.asyncio
async def test_check_deadlines_past_deadline_triggers_recovery():
    from agents.deadline_tracker import check_deadlines, OpenDispute

    past_deadline_dt = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    dispute = OpenDispute(
        dispute_id="PP-D-PAST",
        merchant_id="m1",
        amount=100.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at=(datetime.now(timezone.utc) - timedelta(days=11)).isoformat(),
        response_deadline=past_deadline_dt,
        last_alert_level="",  # not yet flagged past_deadline
    )

    notified = []
    auto_acted = []

    async def notify(msg): notified.append(msg)
    async def auto_accept(did, amt, reason): auto_acted.append(did)

    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[dispute]), \
         patch("agents.deadline_tracker.update_alert_level", new_callable=AsyncMock, return_value=True):
        result = await check_deadlines("m1", notify, auto_accept)

    # Should attempt recovery accept and notify merchant
    assert result["past_deadline"] == 1
    assert len(notified) >= 1
    assert "PAST DEADLINE" in notified[0] or "past" in notified[0].lower()
    assert "PP-D-PAST" in auto_acted


# ══════════════════════════════════════════════════════════════════
# System 6: Oracle Calibration — Recalibration Signal + Attribution
# ══════════════════════════════════════════════════════════════════

def test_recalibration_signal_no_adjustment_needed():
    from oracle.feedback import recalibration_signal, CalibrationStats
    stats = CalibrationStats(
        merchant_id="m1", total_records=50,
        brier_score=0.12, mean_prediction=0.65,
        actual_win_rate=0.62, calibration_error=0.03,  # under threshold
        win_rate_by_claim_type={
            "INR": {"wins": 30, "total": 40, "actual_win_rate": 0.75,
                    "avg_prediction": 0.72, "calibration_error": 0.03}
        },
        oracle_accuracy=0.75, recommendation_quality="good",
        computed_at="2026-11-06T00:00:00Z",
    )
    signal = recalibration_signal(stats)
    assert signal["adjustments_needed"] is False
    assert signal["per_claim_type"] == {}


def test_recalibration_signal_overconfident():
    from oracle.feedback import recalibration_signal, CalibrationStats
    stats = CalibrationStats(
        merchant_id="m1", total_records=100,
        brier_score=0.22, mean_prediction=0.75,
        actual_win_rate=0.55, calibration_error=0.20,
        win_rate_by_claim_type={
            "UNAUTHORIZED": {
                "wins": 11, "total": 25,
                "actual_win_rate": 0.44, "avg_prediction": 0.65,
                "calibration_error": 0.21,
            }
        },
        oracle_accuracy=0.55, recommendation_quality="needs_improvement",
        computed_at="2026-11-06T00:00:00Z",
    )
    signal = recalibration_signal(stats)
    assert signal["adjustments_needed"] is True
    assert "UNAUTHORIZED" in signal["per_claim_type"]
    adj = signal["per_claim_type"]["UNAUTHORIZED"]
    assert adj["suggested_adjustment"] < 0  # overconfident → decrease
    assert "DECREASE" in adj["action"]


def test_recalibration_signal_underconfident():
    from oracle.feedback import recalibration_signal, CalibrationStats
    stats = CalibrationStats(
        merchant_id="m1", total_records=80,
        brier_score=0.18, mean_prediction=0.60,
        actual_win_rate=0.78, calibration_error=-0.18,
        win_rate_by_claim_type={
            "ITEM_NOT_RECEIVED": {
                "wins": 55, "total": 70,
                "actual_win_rate": 0.79, "avg_prediction": 0.60,
                "calibration_error": -0.19,
            }
        },
        oracle_accuracy=0.70, recommendation_quality="fair",
        computed_at="2026-11-06T00:00:00Z",
    )
    signal = recalibration_signal(stats)
    assert signal["adjustments_needed"] is True
    adj = signal["per_claim_type"]["ITEM_NOT_RECEIVED"]
    assert adj["suggested_adjustment"] > 0  # underconfident → increase
    assert "INCREASE" in adj["action"]


def test_recalibration_signal_small_sample_ignored():
    from oracle.feedback import recalibration_signal, CalibrationStats
    stats = CalibrationStats(
        merchant_id="m1", total_records=15,
        brier_score=0.22, mean_prediction=0.70,
        actual_win_rate=0.40, calibration_error=0.30,
        win_rate_by_claim_type={
            "SNAD": {
                "wins": 4, "total": 10,  # below MIN_SAMPLE_SIZE=20
                "actual_win_rate": 0.40, "avg_prediction": 0.70,
                "calibration_error": 0.30,
            }
        },
        oracle_accuracy=0.50, recommendation_quality="needs_improvement",
        computed_at="2026-11-06T00:00:00Z",
    )
    signal = recalibration_signal(stats)
    # Small sample → no adjustments (10 < 20 minimum)
    assert signal["adjustments_needed"] is False


def test_evidence_attribution_groups_by_flags():
    from oracle.feedback import evidence_attribution, OutcomeRecord
    records = [
        OutcomeRecord("d1", "m1", "INR", 0.7, 50.0, "FIGHT", "FIGHT", "WON", 100.0,
                      evidence_flags={"has_tracking": True, "has_delivery_confirm": True}),
        OutcomeRecord("d2", "m1", "INR", 0.7, 50.0, "FIGHT", "FIGHT", "WON", 100.0,
                      evidence_flags={"has_tracking": True, "has_delivery_confirm": True}),
        OutcomeRecord("d3", "m1", "INR", 0.5, 10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={"has_tracking": True, "has_delivery_confirm": True}),
        OutcomeRecord("d4", "m1", "INR", 0.4, 5.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={"has_tracking": True, "has_delivery_confirm": True}),
        OutcomeRecord("d5", "m1", "INR", 0.4, 5.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={"has_tracking": True, "has_delivery_confirm": True}),
        # No-evidence group
        OutcomeRecord("d6", "m1", "INR", 0.3, -10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={}),
        OutcomeRecord("d7", "m1", "INR", 0.3, -10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={}),
        OutcomeRecord("d8", "m1", "INR", 0.3, -10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={}),
        OutcomeRecord("d9", "m1", "INR", 0.3, -10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={}),
        OutcomeRecord("d10", "m1", "INR", 0.3, -10.0, "FIGHT", "FIGHT", "LOST", 100.0,
                      evidence_flags={}),
    ]
    result = evidence_attribution(records)
    # tracking+delivery_confirmed group should exist and have higher win rate than no_evidence
    assert "tracking+delivery_confirmed" in result
    assert "no_evidence" in result
    tracking_rate = result["tracking+delivery_confirmed"]["win_rate"]
    no_evidence_rate = result["no_evidence"]["win_rate"]
    assert tracking_rate > no_evidence_rate


def test_evidence_attribution_min_sample_filter():
    from oracle.feedback import evidence_attribution, OutcomeRecord
    # Only 3 records with tracking+sig — below MIN_SAMPLE=5 → excluded
    records = [
        OutcomeRecord(f"d{i}", "m1", "INR", 0.8, 50.0, "FIGHT", "FIGHT", "WON", 100.0,
                      evidence_flags={"has_tracking": True, "has_signature": True})
        for i in range(3)
    ]
    result = evidence_attribution(records)
    assert "tracking+signature" not in result
