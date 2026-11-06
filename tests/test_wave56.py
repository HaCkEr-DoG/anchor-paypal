"""
Tests for Wave 56: win_probability_calibrator, dispute_portfolio_analyzer.
"""
import pytest
from oracle.win_probability_calibrator import (
    calibrate_win_probability, WinProbabilityInput,
    CalibrationConfidence,
)
from oracle.dispute_portfolio_analyzer import (
    analyze_dispute_portfolio, PortfolioAnalysisInput, PortfolioDispute,
    PortfolioRiskLevel, UrgencyBucket,
)


# ═══════════════════════════════════════════════════════════════════
# Win Probability Calibrator Tests
# ═══════════════════════════════════════════════════════════════════

class TestWinProbabilityCalibrator:

    def _inp(self, reason="INR", network="VISA", **kw) -> WinProbabilityInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason,
            network=network,
            days_remaining_in_window=10,
            response_window_days=20,
        )
        defaults.update(kw)
        return WinProbabilityInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calibrate_win_probability(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_probability_bounded(self):
        r = calibrate_win_probability(self._inp())
        assert 0.02 <= r.final_probability <= 0.97

    def test_adjustments_sum_to_total(self):
        r = calibrate_win_probability(self._inp())
        assert abs(sum(a.adjustment for a in r.adjustments) - r.total_adjustment) < 0.001

    def test_confidence_interval_ordered(self):
        r = calibrate_win_probability(self._inp())
        assert r.confidence_interval_low < r.confidence_interval_high

    def test_base_rate_set(self):
        r = calibrate_win_probability(self._inp(reason="INR"))
        assert r.base_rate == pytest.approx(0.52)

    def test_snad_base_rate(self):
        r = calibrate_win_probability(self._inp(reason="SNAD"))
        assert r.base_rate == pytest.approx(0.38)

    def test_subscription_base_rate(self):
        r = calibrate_win_probability(self._inp(reason="SUBSCRIPTION"))
        assert r.base_rate == pytest.approx(0.55)

    # ── Network multipliers ───────────────────────────────────────

    def test_amex_lowers_probability(self):
        visa = calibrate_win_probability(self._inp(reason="INR", network="VISA"))
        amex = calibrate_win_probability(self._inp(reason="INR", network="AMEX"))
        assert visa.network_multiplied > amex.network_multiplied

    def test_visa_multiplier_applied(self):
        r = calibrate_win_probability(self._inp(reason="INR", network="VISA"))
        assert r.network_multiplied > r.base_rate  # VISA multiplier > 1.0

    # ── Positive adjustments ──────────────────────────────────────

    def test_3ds_boosts_unauthorized(self):
        base = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", has_3ds_authentication=False))
        auth = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", has_3ds_authentication=True))
        assert auth.final_probability > base.final_probability

    def test_tracking_boosts_inr(self):
        base  = calibrate_win_probability(self._inp(reason="INR", has_tracking_with_delivery=False))
        track = calibrate_win_probability(self._inp(reason="INR", has_tracking_with_delivery=True))
        assert track.final_probability > base.final_probability

    def test_ce3_adds_18pp(self):
        base = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", network="VISA", ce3_eligible=False))
        ce3  = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", network="VISA", ce3_eligible=True))
        assert abs((ce3.final_probability - base.final_probability) - 0.18) < 0.05

    def test_sp_boosts_probability(self):
        no_sp = calibrate_win_probability(self._inp(seller_protection_eligible=False))
        sp    = calibrate_win_probability(self._inp(seller_protection_eligible=True))
        assert sp.final_probability > no_sp.final_probability

    def test_strong_evidence_boosts(self):
        weak   = calibrate_win_probability(self._inp(evidence_completeness_score=20))
        strong = calibrate_win_probability(self._inp(evidence_completeness_score=90))
        assert strong.final_probability > weak.final_probability

    # ── Negative adjustments ──────────────────────────────────────

    def test_no_3ds_on_unauthorized_reduces(self):
        with_3ds    = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", has_3ds_authentication=True))
        without_3ds = calibrate_win_probability(self._inp(reason="UNAUTHORIZED", has_3ds_authentication=False))
        assert with_3ds.final_probability > without_3ds.final_probability

    def test_last_minute_filing_reduces(self):
        early = calibrate_win_probability(self._inp(days_remaining_in_window=18, response_window_days=20))
        late  = calibrate_win_probability(self._inp(days_remaining_in_window=1,  response_window_days=20))
        assert early.final_probability > late.final_probability

    # ── Confidence ────────────────────────────────────────────────

    def test_many_signals_give_high_confidence(self):
        r = calibrate_win_probability(self._inp(
            has_tracking_with_delivery=True,
            has_3ds_authentication=True,
            has_avs_match=True,
            has_buyer_communication=True,
            ce3_eligible=False,
            seller_protection_eligible=True,
            evidence_completeness_score=85,
        ))
        assert r.confidence in (CalibrationConfidence.HIGH, CalibrationConfidence.MEDIUM)

    def test_no_signals_give_low_confidence(self):
        r = calibrate_win_probability(self._inp())
        assert r.confidence in (CalibrationConfidence.LOW, CalibrationConfidence.MEDIUM)

    # ── Key drivers ───────────────────────────────────────────────

    def test_key_drivers_non_empty_when_signals_present(self):
        r = calibrate_win_probability(self._inp(has_tracking_with_delivery=True, evidence_completeness_score=85))
        assert len(r.key_drivers) >= 1


# ═══════════════════════════════════════════════════════════════════
# Dispute Portfolio Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

def _make_dispute(did="D-001", reason="INR", net="VISA", amt=200.0, days=10, wp=0.55, **kw) -> PortfolioDispute:
    defaults = dict(
        dispute_id=did, reason_code=reason, network=net,
        amount=amt, days_remaining=days, win_probability=wp,
    )
    defaults.update(kw)
    return PortfolioDispute(**defaults)


class TestDisputePortfolioAnalyzer:

    def _inp(self, disputes=None, txns=1000) -> PortfolioAnalysisInput:
        return PortfolioAnalysisInput(
            merchant_id="M-001",
            disputes=disputes or [],
            total_transactions_90d=txns,
        )

    # ── Empty portfolio ───────────────────────────────────────────

    def test_empty_portfolio_zero_amounts(self):
        r = analyze_dispute_portfolio(self._inp())
        assert r.total_disputes == 0
        assert r.total_amount_at_risk == 0.0

    def test_empty_portfolio_low_risk(self):
        r = analyze_dispute_portfolio(self._inp())
        assert r.portfolio_risk_level == PortfolioRiskLevel.LOW

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute()]))
        assert "M-001" in r.summary

    def test_dispute_count_correct(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute("D-1"), _make_dispute("D-2")]))
        assert r.total_disputes == 2

    def test_total_amount_sums_correctly(self):
        r = analyze_dispute_portfolio(self._inp([
            _make_dispute("D-1", amt=100.0),
            _make_dispute("D-2", amt=200.0),
        ]))
        assert r.total_amount_at_risk == pytest.approx(300.0)

    # ── SP coverage ───────────────────────────────────────────────

    def test_sp_covered_amount_correct(self):
        r = analyze_dispute_portfolio(self._inp([
            _make_dispute("D-1", amt=100.0, is_in_sp=True),
            _make_dispute("D-2", amt=200.0, is_in_sp=False),
        ]))
        assert r.sp_covered_amount == pytest.approx(100.0)
        assert r.unprotected_amount == pytest.approx(200.0)

    # ── EV computations ──────────────────────────────────────────

    def test_ev_fought_less_than_zero_on_low_win_prob(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute(wp=0.10, amt=200.0)]))
        assert r.total_ev_if_all_fought < 0

    def test_ev_fought_vs_accepted_ordering_at_high_win_prob(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute(wp=0.80, amt=200.0)]))
        assert r.total_ev_if_all_fought > r.total_ev_if_all_accepted

    # ── Reason code breakdown ─────────────────────────────────────

    def test_reason_code_breakdown_counts(self):
        r = analyze_dispute_portfolio(self._inp([
            _make_dispute("D-1", reason="INR"),
            _make_dispute("D-2", reason="INR"),
            _make_dispute("D-3", reason="SNAD"),
        ]))
        rc_map = {b.reason_code: b.count for b in r.by_reason_code}
        assert rc_map["INR"] == 2
        assert rc_map["SNAD"] == 1

    def test_reason_code_sorted_by_amount(self):
        r = analyze_dispute_portfolio(self._inp([
            _make_dispute("D-1", reason="INR",  amt=100.0),
            _make_dispute("D-2", reason="SNAD", amt=500.0),
        ]))
        assert r.by_reason_code[0].reason_code == "SNAD"

    # ── Urgency breakdown ─────────────────────────────────────────

    def test_urgency_critical_on_1_day(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute(days=1)]))
        assert any(u.bucket == UrgencyBucket.CRITICAL for u in r.by_urgency)

    def test_urgency_low_on_30_days(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute(days=30)]))
        assert any(u.bucket == UrgencyBucket.LOW for u in r.by_urgency)

    # ── Top disputes ─────────────────────────────────────────────

    def test_top_disputes_at_most_5(self):
        disputes = [_make_dispute(f"D-{i}", amt=float(i * 50)) for i in range(1, 10)]
        r = analyze_dispute_portfolio(self._inp(disputes))
        assert len(r.top_disputes) <= 5

    def test_top_dispute_rank_starts_at_1(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute()]))
        assert r.top_disputes[0].priority_rank == 1

    # ── HDR impact ────────────────────────────────────────────────

    def test_hdr_rate_computed(self):
        disputes = [_make_dispute(f"D-{i}", is_hdr_counting=True) for i in range(5)]
        r = analyze_dispute_portfolio(self._inp(disputes, txns=1000))
        assert r.hdr_dispute_count == 5
        assert abs(r.hdr_impact_rate - 0.005) < 0.0001

    def test_sp_disputes_reduce_hdr_count(self):
        # Non-HDR disputes don't count toward the rate
        disputes = [_make_dispute("D-1", is_hdr_counting=False)]
        r = analyze_dispute_portfolio(self._inp(disputes))
        assert r.hdr_dispute_count == 0

    # ── Risk level ────────────────────────────────────────────────

    def test_critical_risk_on_many_disputes(self):
        disputes = [_make_dispute(f"D-{i}") for i in range(60)]
        r = analyze_dispute_portfolio(self._inp(disputes))
        assert r.portfolio_risk_level == PortfolioRiskLevel.CRITICAL

    def test_low_risk_on_few_disputes(self):
        r = analyze_dispute_portfolio(self._inp([_make_dispute()]))
        assert r.portfolio_risk_level in (PortfolioRiskLevel.LOW, PortfolioRiskLevel.MODERATE)

    # ── Avg win prob ─────────────────────────────────────────────

    def test_avg_win_prob_computed(self):
        r = analyze_dispute_portfolio(self._inp([
            _make_dispute("D-1", wp=0.60),
            _make_dispute("D-2", wp=0.40),
        ]))
        assert abs(r.portfolio_avg_win_prob - 0.50) < 0.001
