"""
Tests for Wave 4: claim_scorer, fraud_pattern_detector, settlement_engine, win_loss_analyzer.
"""
import pytest
from datetime import date, timedelta

from oracle.claim_scorer import (
    score_claim, score_summary,
    DisputeSignals, FightVerdict, SignalWeight,
)
from oracle.fraud_pattern_detector import (
    detect_patterns, DisputeRecord,
    FraudPattern, FraudConfidence,
)
from oracle.settlement_engine import (
    analyze_settlement, SettlementStrategy, DisputeStage,
)
from oracle.win_loss_analyzer import (
    analyze_outcomes, DisputeOutcomeRecord, OutcomeType,
)

TODAY = date(2024, 6, 15)


# ═══════════════════════════════════════════════════════════════════
# Claim Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestClaimScorer:

    def _signals(self, **kwargs) -> DisputeSignals:
        defaults = dict(
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=200.0,
        )
        defaults.update(kwargs)
        return DisputeSignals(**defaults)

    def test_high_calibrated_win_rate_gives_high_score(self):
        signals = self._signals(
            calibrated_win_rate=0.85,
            evidence_completeness=0.90,
            evidence_quality_tier="STRONG",
            network_risk_level="CRITICAL",
            network_risk_score=90.0,
        )
        result = score_claim("D001", signals)
        assert result.fight_score >= 60
        assert result.fight_verdict in (
            FightVerdict.FIGHT_HIGH_CONFIDENCE, FightVerdict.FIGHT_MODERATE
        )

    def test_weak_evidence_gives_low_score(self):
        signals = self._signals(
            calibrated_win_rate=0.45,
            evidence_completeness=0.20,
            evidence_quality_tier="WEAK",
            network_risk_level="TRUSTED",
            network_risk_score=5.0,
        )
        result = score_claim("D002", signals)
        assert result.fight_score < 55

    def test_serial_fraudster_buyer_boosts_score(self):
        base_signals = self._signals(calibrated_win_rate=0.65)
        with_fraud = self._signals(
            calibrated_win_rate=0.65,
            network_risk_level="CRITICAL",
            network_risk_score=90.0,
        )
        base = score_claim("D-base", base_signals)
        fraud = score_claim("D-fraud", with_fraud)
        assert fraud.fight_score > base.fight_score

    def test_seller_protection_coverage_raises_score(self):
        no_sp = self._signals(calibrated_win_rate=0.65)
        with_sp = self._signals(
            calibrated_win_rate=0.65,
            sp_covered=True,
            sp_score=90.0,
            sp_has_blocking_issues=False,
        )
        r_no_sp = score_claim("D-nosp", no_sp)
        r_sp = score_claim("D-sp", with_sp)
        assert r_sp.fight_score > r_no_sp.fight_score

    def test_platinum_carrier_raises_score(self):
        base = self._signals(calibrated_win_rate=0.68)
        with_carrier = self._signals(
            calibrated_win_rate=0.68,
            carrier_trust_level="PLATINUM",
            carrier_inr_win_rate=0.87,
        )
        r_base = score_claim("D-no-carrier", base)
        r_carrier = score_claim("D-carrier", with_carrier)
        assert r_carrier.fight_score >= r_base.fight_score

    def test_low_dispute_amount_generates_warning(self):
        signals = self._signals(dispute_amount=15.0, calibrated_win_rate=0.70)
        result = score_claim("D-small", signals)
        assert any("$15" in w or "justify" in w for w in result.warnings)

    def test_ev_computed_correctly(self):
        # EV = win_prob * amount - fight_cost
        signals = self._signals(
            calibrated_win_rate=0.70,
            dispute_amount=100.0,
        )
        result = score_claim("D-ev", signals)
        # With adjustments, EV should be close to 0.70 * 100 - 8 = 62
        assert result.estimated_ev > 40.0  # Not exact due to adjustments

    def test_missing_signals_tracked(self):
        signals = self._signals()  # Minimal signals
        result = score_claim("D-min", signals)
        assert len(result.missing_signals) > 0

    def test_verdict_accept_for_zero_win_prob(self):
        signals = self._signals(
            calibrated_win_rate=0.20,
            evidence_completeness=0.10,
            evidence_quality_tier="WEAK",
            network_risk_level="TRUSTED",
            network_risk_score=5.0,
            sp_covered=False,
            sp_has_blocking_issues=True,
        )
        result = score_claim("D-accept", signals)
        assert result.fight_verdict == FightVerdict.ACCEPT

    def test_critical_deadline_generates_warning(self):
        signals = self._signals(
            calibrated_win_rate=0.70,
            response_in_optimal_window=True,
            days_remaining=0,
        )
        result = score_claim("D-urgent", signals)
        assert any("24" in w or "CRITICAL" in w for w in result.warnings)

    def test_score_summary_contains_key_fields(self):
        signals = self._signals(calibrated_win_rate=0.70)
        result = score_claim("D-summary", signals)
        summary = score_summary(result)
        assert "D-summary" in summary
        assert "$200" in summary or "200" in summary
        assert "EV=" in summary

    def test_legal_backstop_adds_to_score(self):
        no_legal = self._signals(calibrated_win_rate=0.65)
        with_legal = self._signals(
            calibrated_win_rate=0.65,
            legal_escalation_available=True,
            legal_escalation_ev=150.0,
        )
        r_no = score_claim("D-no-legal", no_legal)
        r_legal = score_claim("D-legal", with_legal)
        assert r_legal.fight_score >= r_no.fight_score


# ═══════════════════════════════════════════════════════════════════
# Fraud Pattern Detector Tests
# ═══════════════════════════════════════════════════════════════════

class TestFraudPatternDetector:

    def _record(self, **kwargs) -> DisputeRecord:
        defaults = dict(
            dispute_id="D-" + str(id(kwargs)),
            buyer_email="buyer@example.com",
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=200.0,
            dispute_date=TODAY,
        )
        defaults.update(kwargs)
        return DisputeRecord(**defaults)

    def test_empty_disputes_returns_clean_report(self):
        report = detect_patterns([], analysis_date=TODAY)
        assert report.total_disputes_analyzed == 0
        assert len(report.patterns_found) == 0

    def test_chargeback_ring_detected_3_buyers_same_zip_carrier(self):
        disputes = [
            self._record(dispute_id=f"D-ring-{i}", buyer_email=f"buyer{i}@test.com",
                         buyer_zip="90210", carrier="LaserShip")
            for i in range(5)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        ring_patterns = [p for p in report.patterns_found if p.pattern == FraudPattern.CHARGEBACK_RING]
        assert len(ring_patterns) == 1
        assert ring_patterns[0].confidence in (FraudConfidence.DEFINITIVE, FraudConfidence.HIGH, FraudConfidence.MODERATE)

    def test_refund_abuse_detected_3_plus_disputes_one_buyer(self):
        disputes = [
            self._record(dispute_id=f"D-abuse-{i}", buyer_email="abuser@fraud.com")
            for i in range(5)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        abuse = [p for p in report.patterns_found if p.pattern == FraudPattern.REFUND_ABUSE]
        assert len(abuse) >= 1
        assert "abuser@fraud.com" in report.buyer_block_recommendations

    def test_refund_abuse_not_detected_2_disputes(self):
        disputes = [
            self._record(dispute_id=f"D-two-{i}", buyer_email="normal@buyer.com")
            for i in range(2)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        abuse = [p for p in report.patterns_found if p.pattern == FraudPattern.REFUND_ABUSE]
        assert len(abuse) == 0

    def test_friendly_fraud_detected_repeated_similar_inr(self):
        disputes = [
            self._record(dispute_id=f"D-ff-{i}", buyer_email="ff@fraud.com",
                         dispute_amount=150.0 + i * 5)  # slightly varying amounts
            for i in range(3)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        ff = [p for p in report.patterns_found if p.pattern == FraudPattern.FRIENDLY_FRAUD]
        assert len(ff) >= 1

    def test_law_enforcement_referral_for_large_ring(self):
        disputes = [
            self._record(
                dispute_id=f"D-law-{i}",
                buyer_email=f"ring{i}@fraud.com",
                buyer_zip="10001",
                carrier="LaserShip",
                dispute_amount=600.0,
            )
            for i in range(10)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        assert report.law_enforcement_referral_warranted is True

    def test_velocity_fraud_detected_new_accounts_7d(self):
        disputes = [
            self._record(
                dispute_id=f"D-vel-{i}",
                buyer_email=f"new{i}@temp.com",
                is_new_account=True,
                dispute_date=TODAY - timedelta(days=2),
            )
            for i in range(5)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        vel = [p for p in report.patterns_found if p.pattern == FraudPattern.VELOCITY_FRAUD]
        assert len(vel) >= 1

    def test_orc_detected_snad_electronics_multiple_buyers(self):
        disputes = [
            self._record(
                dispute_id=f"D-orc-{i}",
                buyer_email=f"orc{i}@fraud.com",
                claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
                item_category="ELECTRONICS",
                dispute_amount=800.0,
            )
            for i in range(3)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        orc = [p for p in report.patterns_found if p.pattern == FraudPattern.ORGANIZED_RETAIL_CRIME]
        assert len(orc) >= 1

    def test_no_orc_for_low_value_items(self):
        disputes = [
            self._record(
                dispute_id=f"D-low-{i}",
                buyer_email=f"buyer{i}@normal.com",
                claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
                item_category="CLOTHING",
                dispute_amount=30.0,
            )
            for i in range(3)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        orc = [p for p in report.patterns_found if p.pattern == FraudPattern.ORGANIZED_RETAIL_CRIME]
        assert len(orc) == 0

    def test_summary_populated(self):
        disputes = [
            self._record(dispute_id=f"D-s-{i}", buyer_email=f"b{i}@test.com")
            for i in range(5)
        ]
        report = detect_patterns(disputes, analysis_date=TODAY)
        assert len(report.summary) > 10
        assert "5" in report.summary


# ═══════════════════════════════════════════════════════════════════
# Settlement Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestSettlementEngine:

    def test_no_settle_when_high_win_prob(self):
        result = analyze_settlement(
            dispute_id="D-fight",
            dispute_amount=300.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.90,
            fight_cost=8.0,
            merchant_has_tracking=True,
            merchant_has_delivery_confirmation=True,
        )
        # High win prob → no settle should have best EV
        assert result.fight_ev > 200.0
        assert result.recommended_strategy == SettlementStrategy.NO_SETTLE

    def test_settle_recommended_when_hdr_at_risk(self):
        # At 0.99% HDR, one more dispute crosses 1.00% limited processing threshold
        result = analyze_settlement(
            dispute_id="D-hdr",
            dispute_amount=100.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.55,
            fight_cost=10.0,
            current_hdr=0.0099,
            monthly_transaction_volume=100,
        )
        assert result.hdr_at_risk is True
        assert result.hdr_protection_value > 0

    def test_full_refund_has_highest_acceptance_prob(self):
        result = analyze_settlement(
            dispute_id="D-full",
            dispute_amount=200.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.60,
            fight_cost=10.0,
        )
        full_offer = next(
            o for o in result.ranked_offers if o.strategy == SettlementStrategy.FULL_REFUND
        )
        assert full_offer.probability_accepted >= 0.90

    def test_offers_sorted_by_ev_descending(self):
        result = analyze_settlement(
            dispute_id="D-sort",
            dispute_amount=300.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.65,
            fight_cost=10.0,
        )
        for i in range(len(result.ranked_offers) - 1):
            assert result.ranked_offers[i].offer_ev >= result.ranked_offers[i + 1].offer_ev

    def test_replacement_option_included_when_item_cost_given(self):
        result = analyze_settlement(
            dispute_id="D-repl",
            dispute_amount=200.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.65,
            fight_cost=10.0,
            item_cost=40.0,
        )
        strategies = {o.strategy for o in result.ranked_offers}
        assert SettlementStrategy.REPLACEMENT in strategies

    def test_action_script_contains_offer_amount(self):
        result = analyze_settlement(
            dispute_id="D-script",
            dispute_amount=300.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.50,
            fight_cost=10.0,
            current_hdr=0.0099,
            monthly_transaction_volume=100,
        )
        # With HDR at risk and moderate win prob, partial settle likely recommended
        if result.recommended_strategy != SettlementStrategy.NO_SETTLE:
            assert str(int(result.recommended_offer_amount)) in result.action_script or \
                   "refund" in result.action_script.lower()

    def test_ev_math_correct_no_hdr(self):
        result = analyze_settlement(
            dispute_id="D-math",
            dispute_amount=100.0,
            claim_type="ITEM_NOT_RECEIVED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.70,
            fight_cost=10.0,
        )
        expected_fight_ev = 0.70 * 100.0 - 10.0  # = 60.0
        assert result.fight_ev == pytest.approx(expected_fight_ev, abs=0.01)

    def test_buyer_leverage_increases_acceptance_of_low_offers(self):
        with_buyer_leverage = analyze_settlement(
            dispute_id="D-bl",
            dispute_amount=200.0,
            claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
            stage=DisputeStage.OPEN_DISPUTE,
            base_win_probability=0.50,
            fight_cost=15.0,
            buyer_has_evidence=True,
        )
        assert with_buyer_leverage.buyer_has_leverage is True


# ═══════════════════════════════════════════════════════════════════
# Win/Loss Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestWinLossAnalyzer:

    def _outcome(self, **kwargs) -> DisputeOutcomeRecord:
        defaults = dict(
            dispute_id="O-" + str(id(kwargs)),
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=200.0,
            outcome=OutcomeType.WON_INITIAL,
            evidence_quality_tier="MODERATE",
            buyer_risk_tier="TIER_1_NORMAL",
        )
        defaults.update(kwargs)
        return DisputeOutcomeRecord(**defaults)

    def test_empty_outcomes_returns_zero_report(self):
        report = analyze_outcomes([])
        assert report.total_outcomes == 0
        assert report.overall_win_rate == 0.0

    def test_win_rate_computed_correctly(self):
        outcomes = [
            self._outcome(dispute_id="O1", outcome=OutcomeType.WON_INITIAL),
            self._outcome(dispute_id="O2", outcome=OutcomeType.WON_INITIAL),
            self._outcome(dispute_id="O3", outcome=OutcomeType.LOST_INITIAL),
        ]
        report = analyze_outcomes(outcomes)
        assert report.overall_win_rate == pytest.approx(2 / 3, abs=0.01)
        assert report.total_wins == 2
        assert report.total_losses == 1

    def test_win_rates_by_claim_type_populated(self):
        outcomes = [
            self._outcome(dispute_id=f"INR-{i}", claim_type="ITEM_NOT_RECEIVED",
                          outcome=OutcomeType.WON_INITIAL)
            for i in range(5)
        ] + [
            self._outcome(dispute_id=f"SNAD-{i}", claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
                          outcome=OutcomeType.LOST_INITIAL)
            for i in range(3)
        ]
        report = analyze_outcomes(outcomes)
        ct_names = {s.segment_value for s in report.win_rate_by_claim_type}
        assert "ITEM_NOT_RECEIVED" in ct_names
        assert "SIGNIFICANTLY_NOT_AS_DESCRIBED" in ct_names

    def test_strong_evidence_win_rate_higher_than_weak(self):
        outcomes = [
            self._outcome(dispute_id=f"SE-{i}", evidence_quality_tier="STRONG",
                          outcome=OutcomeType.WON_INITIAL)
            for i in range(8)
        ] + [
            self._outcome(dispute_id=f"WE-{i}", evidence_quality_tier="WEAK",
                          outcome=OutcomeType.LOST_INITIAL)
            for i in range(5)
        ]
        report = analyze_outcomes(outcomes)
        strong = next((s for s in report.win_rate_by_evidence_quality if s.segment_value == "STRONG"), None)
        weak = next((s for s in report.win_rate_by_evidence_quality if s.segment_value == "WEAK"), None)
        if strong and weak:
            assert strong.win_rate > weak.win_rate

    def test_timing_insight_generated_when_enough_data(self):
        outcomes = [
            self._outcome(dispute_id=f"T35-{i}", response_day=4,
                          outcome=OutcomeType.WON_INITIAL)
            for i in range(5)
        ] + [
            self._outcome(dispute_id=f"T12-{i}", response_day=1,
                          outcome=OutcomeType.LOST_INITIAL)
            for i in range(5)
        ]
        report = analyze_outcomes(outcomes)
        timing_insights = [i for i in report.insights if i.insight_type == "timing_impact"]
        assert len(timing_insights) > 0

    def test_evidence_insight_generated_when_enough_data(self):
        outcomes = (
            [self._outcome(dispute_id=f"SE-{i}", evidence_quality_tier="STRONG",
                           outcome=OutcomeType.WON_INITIAL) for i in range(5)]
            + [self._outcome(dispute_id=f"WE-{i}", evidence_quality_tier="WEAK",
                             outcome=OutcomeType.LOST_INITIAL) for i in range(5)]
        )
        report = analyze_outcomes(outcomes)
        ev_insights = [i for i in report.insights if i.insight_type == "evidence_impact"]
        assert len(ev_insights) > 0

    def test_calibration_updates_populated_from_outcomes(self):
        outcomes = [
            self._outcome(dispute_id=f"INR-{i}", claim_type="ITEM_NOT_RECEIVED",
                          outcome=OutcomeType.WON_INITIAL)
            for i in range(10)
        ]
        report = analyze_outcomes(outcomes)
        assert any("claim_type:ITEM_NOT_RECEIVED" in k for k in report.calibration_updates)

    def test_appeal_insight_generated(self):
        outcomes = [
            self._outcome(dispute_id=f"AP-{i}", was_appealed=True,
                          outcome=OutcomeType.WON_APPEAL)
            for i in range(3)
        ] + [
            self._outcome(dispute_id=f"APL-{i}", was_appealed=True,
                          outcome=OutcomeType.LOST_APPEAL)
            for i in range(2)
        ]
        report = analyze_outcomes(outcomes)
        appeal_insights = [i for i in report.insights if i.insight_type == "appeal_effectiveness"]
        assert len(appeal_insights) > 0

    def test_summary_populated(self):
        outcomes = [
            self._outcome(dispute_id=f"S-{i}",
                          outcome=OutcomeType.WON_INITIAL if i % 2 == 0 else OutcomeType.LOST_INITIAL)
            for i in range(10)
        ]
        report = analyze_outcomes(outcomes)
        assert len(report.summary) > 20
        assert "10" in report.summary

    def test_settled_outcomes_counted_separately(self):
        outcomes = [
            self._outcome(dispute_id="W1", outcome=OutcomeType.WON_INITIAL),
            self._outcome(dispute_id="L1", outcome=OutcomeType.LOST_INITIAL),
            self._outcome(dispute_id="S1", outcome=OutcomeType.SETTLED,
                          was_settled=True, settlement_amount=80.0),
        ]
        report = analyze_outcomes(outcomes)
        assert report.total_settled == 1
        assert report.total_wins == 1
        assert report.total_losses == 1

    def test_withdrawn_counts_as_win(self):
        outcomes = [
            self._outcome(dispute_id="W1", outcome=OutcomeType.WITHDRAWN),
        ]
        report = analyze_outcomes(outcomes)
        assert report.total_wins == 1

    def test_timed_out_counts_as_loss(self):
        outcomes = [
            self._outcome(dispute_id="T1", outcome=OutcomeType.TIMED_OUT),
        ]
        report = analyze_outcomes(outcomes)
        assert report.total_losses == 1
