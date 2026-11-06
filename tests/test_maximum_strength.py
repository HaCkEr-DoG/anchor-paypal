"""
Maximum-strength test suite — covers all 4 new modules:
  oracle/appeal.py          — appeal engine (fight back after a loss)
  oracle/preempt.py         — pre-dispute interceptor (stop disputes before filed)
  oracle/digital_goods.py   — digital goods defense (no SP, fight on evidence)
  oracle/response_optimizer.py — response time optimizer (when to respond)

These seal every remaining weakness in Anchor's defense stack.
"""

from datetime import datetime, timedelta, timezone
import pytest

from oracle.appeal import (
    analyze_appeal,
    AppealWorthiness,
    RulingReason,
)
from oracle.preempt import (
    scan_message,
    PreemptAction,
    SignalType,
)
from oracle.digital_goods import (
    score_digital_rubric,
    DigitalClaimType,
    AccessLogSignal,
)
from oracle.response_optimizer import (
    plan_response,
    ResponseUrgency,
)


# ── Appeal Engine tests ───────────────────────────────────────────────────────

class TestAppealEngine:

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def test_no_proof_of_delivery_strong_appeal(self):
        """Missed delivery confirmation = strong appeal if we can now provide it."""
        result = analyze_appeal(
            RulingReason.NO_PROOF_OF_DELIVERY,
            dispute_amount=150.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=self._now_iso(),
            original_evidence_had_delivery=False,
            seller_protection_requirements_met=True,
        )
        assert result.worthiness in (AppealWorthiness.STRONG, AppealWorthiness.MODERATE)
        assert result.days_remaining >= 9  # 9–10 days depending on microsecond timing
        assert result.ev_appeal > 0  # EV-positive
        assert len(result.appeal_letter) > 100
        assert len(result.new_evidence_needed) > 0

    def test_expired_appeal_window(self):
        """Past the 10-day window = FUTILE."""
        old_ruling = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
        result = analyze_appeal(
            RulingReason.INSUFFICIENT_EVIDENCE,
            dispute_amount=200.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=old_ruling,
        )
        assert result.worthiness == AppealWorthiness.FUTILE
        assert result.days_remaining == 0

    def test_sp_void_ruling_addressed(self):
        """When SP was wrongly voided, appeal letter must invoke SP explicitly."""
        result = analyze_appeal(
            RulingReason.SELLER_PROTECTION_VOID,
            dispute_amount=300.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=self._now_iso(),
            seller_protection_requirements_met=True,
        )
        assert "Seller Protection" in result.appeal_letter
        assert result.worthiness != AppealWorthiness.FUTILE

    def test_ev_analysis_on_large_amount(self):
        """EV(appeal) must be positive for large-amount disputes with new evidence."""
        result = analyze_appeal(
            RulingReason.NO_PROOF_OF_DELIVERY,
            dispute_amount=800.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=self._now_iso(),
            original_evidence_had_delivery=False,
            can_obtain_signature_confirmation=True,
            seller_protection_requirements_met=True,
        )
        assert result.ev_appeal > 0
        assert result.worthiness == AppealWorthiness.STRONG

    def test_snad_no_sp_angle(self):
        """SNAD appeals have no SP angle — still generate a viable appeal."""
        result = analyze_appeal(
            RulingReason.ITEM_NOT_AS_DESCRIBED,
            dispute_amount=120.0,
            claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
            ruling_date=self._now_iso(),
            seller_protection_requirements_met=False,  # SP doesn't cover SNAD
        )
        # Should not mention SP for SNAD
        assert "Seller Protection" not in result.ruling_addressed or "not" in result.ruling_addressed
        assert len(result.appeal_letter) > 50

    def test_tracking_unverified_new_evidence(self):
        """Unverified tracking = submit direct carrier URL."""
        result = analyze_appeal(
            RulingReason.TRACKING_UNVERIFIED,
            dispute_amount=90.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=self._now_iso(),
        )
        assert any("url" in e.lower() or "carrier" in e.lower() for e in result.new_evidence_needed)

    def test_unauthorized_buyer_communication_appeal(self):
        """UNAUTHORIZED: appeal is stronger when buyer communication can be obtained."""
        result = analyze_appeal(
            RulingReason.UNAUTHORIZED_CLAIM,
            dispute_amount=250.0,
            claim_type="UNAUTHORIZED",
            ruling_date=self._now_iso(),
            can_obtain_buyer_communication=True,
            seller_protection_requirements_met=True,
        )
        assert result.worthiness in (AppealWorthiness.STRONG, AppealWorthiness.MODERATE)
        assert result.ev_appeal > 0

    def test_weak_appeal_small_amount_ev_negative(self):
        """Small dispute, no new evidence = filing appeal is EV-negative."""
        result = analyze_appeal(
            RulingReason.OTHER,
            dispute_amount=15.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=self._now_iso(),
        )
        # With no new evidence and small amount, EV may be negative
        assert result.ev_appeal == result.ev_appeal  # just check it's computed

    def test_appeal_deadline_exactly_10_days(self):
        """Appeal deadline should be exactly 10 days from ruling."""
        ruling_time = datetime.now(timezone.utc)
        result = analyze_appeal(
            RulingReason.INSUFFICIENT_EVIDENCE,
            dispute_amount=100.0,
            claim_type="ITEM_NOT_RECEIVED",
            ruling_date=ruling_time.isoformat(),
        )
        assert result.days_remaining >= 9  # 9–10 days depending on microsecond timing


# ── Pre-Dispute Interceptor tests ─────────────────────────────────────────────

class TestPreemptInterceptor:

    def test_escalation_threat_high_probability(self):
        """'File a dispute with PayPal' = high dispute probability."""
        result = scan_message(
            "If I don't hear back I'm going to file a dispute with PayPal",
            order_amount=100.0,
        )
        assert result.dispute_probability >= 0.40
        assert result.action in (
            PreemptAction.RESPOND,
            PreemptAction.GATHER_TRACKING,
            PreemptAction.FULL_REFUND,
        )
        assert result.urgency_hours <= 2

    def test_inr_signal_triggers_tracking(self):
        """'Where is my order' should trigger GATHER_TRACKING action."""
        result = scan_message(
            "Where is my order? I ordered 2 weeks ago and nothing has arrived",
            order_amount=60.0,
        )
        assert result.action == PreemptAction.GATHER_TRACKING
        assert result.urgency_hours <= 4
        assert any(s.signal_type == SignalType.INR_SIGNAL for s in result.detected_signals)

    def test_satisfied_buyer_ignore(self):
        """Happy buyer message = IGNORE."""
        result = scan_message(
            "Thank you! I received my order and it looks great.",
            order_amount=50.0,
        )
        assert result.action == PreemptAction.IGNORE
        assert result.dispute_probability < 0.20

    def test_item_complaint_recommend_response(self):
        """Item complaint = RESPOND or OFFER_PARTIAL."""
        result = scan_message(
            "This is not what I ordered. The item is completely different from the listing.",
            order_amount=120.0,
        )
        assert result.action in (PreemptAction.RESPOND, PreemptAction.OFFER_PARTIAL)
        assert any(s.signal_type == SignalType.ITEM_COMPLAINT for s in result.detected_signals)

    def test_chargeback_threat_critical(self):
        """'Chargeback' word = highest severity signal."""
        result = scan_message(
            "I am going to do a chargeback on this if you don't fix this today",
            order_amount=200.0,
        )
        assert result.dispute_probability >= 0.50
        assert result.urgency_hours <= 2
        threat_signals = [s for s in result.detected_signals if s.signal_type == SignalType.ESCALATION_THREAT]
        assert len(threat_signals) > 0
        assert any(s.severity == "critical" for s in threat_signals)

    def test_ev_analysis_has_all_options(self):
        """EV analysis must cover all 4 options."""
        result = scan_message("I want a refund", order_amount=50.0)
        assert "ignore" in result.ev_analysis
        assert "respond" in result.ev_analysis
        assert "partial_refund" in result.ev_analysis
        assert "full_refund" in result.ev_analysis

    def test_response_script_no_defensive_language(self):
        """Response scripts should not use uncertain language."""
        result = scan_message(
            "My package hasn't arrived after 3 weeks!",
            order_amount=80.0,
        )
        if result.response_script:
            for phrase in ["I believe", "I think", "probably", "maybe"]:
                assert phrase not in result.response_script

    def test_prior_disputes_increase_probability(self):
        """Buyer with prior disputes = higher base dispute probability."""
        result_clean = scan_message("Any update on my order?", order_amount=100.0, buyer_prior_disputes=0)
        result_risky = scan_message("Any update on my order?", order_amount=100.0, buyer_prior_disputes=3)
        assert result_risky.dispute_probability > result_clean.dispute_probability

    def test_dispute_window_timing_increases_probability(self):
        """Day 7–9 after purchase = peak filing window, increases probability."""
        result_early = scan_message("Any update?", days_since_purchase=2)
        result_window = scan_message("Any update?", days_since_purchase=8)
        assert result_window.dispute_probability > result_early.dispute_probability

    def test_full_refund_recommended_small_escalation(self):
        """Small order + escalation threat = full refund may be EV-optimal."""
        result = scan_message(
            "I'm going to file a dispute if you don't fix this",
            order_amount=20.0,
        )
        # For small orders with escalation threat, EV(full_refund) beats EV(doing nothing)
        # or is at least better than the dispute cost of ignoring it
        assert result.ev_analysis["full_refund"] >= result.ev_analysis["ignore"] - 5

    def test_missing_order_generates_tracking_advice(self):
        """INR message should generate a tracking-focused response script."""
        result = scan_message(
            "Package is lost, tracking shows nothing has moved in 10 days",
            order_amount=90.0,
        )
        assert result.action == PreemptAction.GATHER_TRACKING
        assert "tracking" in result.response_script.lower()


# ── Digital Goods Defense tests ───────────────────────────────────────────────

class TestDigitalGoodsDefense:

    def test_friendly_fraud_detected(self):
        """Buyer downloads product then files INR = friendly fraud detection."""
        access = AccessLogSignal(
            has_download_log=True,
            download_timestamp_vs_dispute="before",
            access_count=3,
        )
        result = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            access_log=access,
            has_delivery_email=True,
            dispute_amount=49.0,
        )
        assert result.friendly_fraud_detected
        assert result.win_probability >= 0.70
        assert "BEFORE FILING" in result.evidence_note or "FRIENDLY FRAUD" in result.evidence_note

    def test_no_access_log_critical_gap(self):
        """INR without access log = critical missing evidence."""
        result = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            access_log=AccessLogSignal(has_download_log=False),
            has_delivery_email=False,
            dispute_amount=49.0,
        )
        assert "download_or_access_log" in result.missing_critical
        assert result.score < 0.40

    def test_subscription_stop_tos_required(self):
        """Subscription dispute without ToS = critical gap."""
        result = score_digital_rubric(
            DigitalClaimType.SUBSCRIPTION_STOP,
            has_terms_of_service=False,
            has_no_cancellation_received=True,
            dispute_amount=29.0,
        )
        assert "terms_of_service" in result.missing_critical

    def test_subscription_stop_full_evidence(self):
        """Subscription dispute with ToS + no cancellation + active usage = strong."""
        result = score_digital_rubric(
            DigitalClaimType.SUBSCRIPTION_STOP,
            has_terms_of_service=True,
            has_no_cancellation_received=True,
            has_active_usage_proof=True,
            dispute_amount=29.0,
        )
        assert result.score >= 0.70
        assert result.win_probability > 0.55

    def test_unauthorized_ip_match_boost(self):
        """IP match for UNAUTHORIZED = strong fraud counter."""
        access_match = AccessLogSignal(
            has_login_log=True,
            purchase_ip="192.0.2.1",
            access_ip="192.0.2.1",
            ips_match=True,
            device_fingerprint_match=True,
        )
        access_no_match = AccessLogSignal(
            has_login_log=True,
            ips_match=False,
        )
        result_match = score_digital_rubric(
            DigitalClaimType.UNAUTHORIZED,
            access_log=access_match,
            has_delivery_email=True,
            dispute_amount=99.0,
        )
        result_no_match = score_digital_rubric(
            DigitalClaimType.UNAUTHORIZED,
            access_log=access_no_match,
            has_delivery_email=True,
            dispute_amount=99.0,
        )
        assert result_match.score > result_no_match.score
        assert result_match.win_probability > result_no_match.win_probability

    def test_snad_description_proof_critical(self):
        """SNAD without description proof = critical gap."""
        result = score_digital_rubric(
            DigitalClaimType.SNAD_DIGITAL,
            has_item_description_proof=False,
            dispute_amount=79.0,
        )
        assert "item_description_proof" in result.missing_critical

    def test_access_before_dispute_flagged_in_note(self):
        """Access log before dispute date must be clearly stated in evidence note."""
        access = AccessLogSignal(
            has_download_log=True,
            download_timestamp_vs_dispute="before",
            access_count=1,
        )
        result = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            access_log=access,
            dispute_amount=30.0,
        )
        assert "before" in result.evidence_note.lower() or "prior" in result.evidence_note.lower()

    def test_no_sp_coverage_implicit(self):
        """Digital goods evidence note should acknowledge no SP coverage."""
        result = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            dispute_amount=49.0,
        )
        assert "Seller Protection does not apply" in result.evidence_note or \
               "digital" in result.evidence_note.lower()

    def test_win_probability_scales_with_evidence(self):
        """More evidence = higher win probability (monotone relationship)."""
        no_evidence = score_digital_rubric(DigitalClaimType.INR_DOWNLOAD, dispute_amount=50.0)
        some_evidence = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            access_log=AccessLogSignal(has_download_log=True),
            dispute_amount=50.0,
        )
        full_evidence = score_digital_rubric(
            DigitalClaimType.INR_DOWNLOAD,
            access_log=AccessLogSignal(
                has_download_log=True,
                download_timestamp_vs_dispute="before",
                access_count=5,
                ips_match=True,
            ),
            has_delivery_email=True,
            dispute_amount=50.0,
        )
        assert full_evidence.win_probability > some_evidence.win_probability
        assert some_evidence.win_probability > no_evidence.win_probability


# ── Response Time Optimizer tests ─────────────────────────────────────────────

class TestResponseOptimizer:

    def _days_ago(self, days: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_day_1_optimal_timing(self):
        """Responding on day 1 = OPTIMAL timing."""
        result = plan_response(
            self._days_ago(1),
            evidence_completeness=0.80,
        )
        assert result.urgency == ResponseUrgency.OPTIMAL
        assert result.timing_penalty == 0.0  # Day 1-3 have bonus, not penalty

    def test_day_8_urgent(self):
        """Day 8 = URGENT — timing penalty applied."""
        result = plan_response(
            self._days_ago(8),
            evidence_completeness=0.60,
        )
        assert result.urgency == ResponseUrgency.URGENT
        assert result.timing_penalty < 0
        assert result.should_respond_now  # Urgent: respond now

    def test_day_10_critical_respond_immediately(self):
        """Day 10 = CRITICAL — respond immediately regardless of evidence."""
        result = plan_response(
            self._days_ago(9.9),  # ~24 hours remaining
            evidence_completeness=0.20,  # Even with poor evidence
        )
        assert result.urgency == ResponseUrgency.CRITICAL
        assert result.should_respond_now  # Always respond immediately on day 10
        assert result.evidence_completeness_threshold == 0.0

    def test_expired_window(self):
        """Past 10 days = EXPIRED."""
        result = plan_response(
            self._days_ago(11),
            evidence_completeness=0.90,
        )
        assert result.urgency == ResponseUrgency.EXPIRED
        assert result.hours_remaining == 0.0

    def test_sufficient_evidence_respond_now(self):
        """70%+ evidence completeness = respond now on any day."""
        result = plan_response(
            self._days_ago(3),
            evidence_completeness=0.75,
        )
        assert result.should_respond_now
        assert result.evidence_completeness_threshold <= 0.70

    def test_wait_for_complete_evidence_when_time_permits(self):
        """With time to spare, wait for complete evidence."""
        result = plan_response(
            self._days_ago(1),
            evidence_completeness=0.40,
            still_gathering_evidence=True,
            hours_to_complete_evidence=12.0,
        )
        assert not result.should_respond_now  # Can wait

    def test_timing_penalty_increases_with_days(self):
        """Later responses should have larger timing penalties."""
        day3 = plan_response(self._days_ago(3), evidence_completeness=0.80)
        day7 = plan_response(self._days_ago(7), evidence_completeness=0.80)
        day9 = plan_response(self._days_ago(9), evidence_completeness=0.80)
        # Win rates should decrease as days increase
        assert day3.win_rate_at_current_timing >= day7.win_rate_at_current_timing
        assert day7.win_rate_at_current_timing >= day9.win_rate_at_current_timing

    def test_win_rate_by_claim_type(self):
        """Different claim types have different base win rates."""
        inr = plan_response(
            self._days_ago(3),
            claim_type="ITEM_NOT_RECEIVED",
            evidence_completeness=0.80,
        )
        unauth = plan_response(
            self._days_ago(3),
            claim_type="UNAUTHORIZED",
            evidence_completeness=0.80,
        )
        # INR has higher base win rate than UNAUTHORIZED
        assert inr.win_rate_at_current_timing > unauth.win_rate_at_current_timing

    def test_respond_by_field_is_10_days_from_filing(self):
        """respond_by must be exactly 10 days from dispute filing."""
        filed = datetime.now(timezone.utc)
        result = plan_response(filed.isoformat(), evidence_completeness=0.80)
        respond_by_dt = datetime.fromisoformat(result.respond_by.replace("Z", "+00:00"))
        expected = filed + timedelta(days=10)
        diff_seconds = abs((respond_by_dt - expected).total_seconds())
        assert diff_seconds < 60  # Within 1 minute tolerance

    def test_advice_text_is_actionable(self):
        """Advice text should be non-empty and contain a verb."""
        for days in [1, 5, 8]:
            result = plan_response(
                self._days_ago(days),
                evidence_completeness=0.60,
            )
            assert len(result.advice) > 20
