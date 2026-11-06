"""
Tests for Wave 50: unauthorized_transaction_scorer, dispute_outcome_tracker.
"""
import pytest
from oracle.unauthorized_transaction_scorer import (
    score_unauthorized_transaction, UnauthorizedTransactionInput,
    AuthVerificationResult, UnauthorizedDefensibility,
)
from oracle.dispute_outcome_tracker import (
    compute_dispute_performance, DisputeOutcomeRecord, OutcomeResult,
    PerformanceTrend,
)

BASE_NOW = 1700000000
DAY = 86400


# ═══════════════════════════════════════════════════════════════════
# Unauthorized Transaction Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestUnauthorizedTransactionScorer:

    def _inp(self, amount=100.0, network="VISA", **kw) -> UnauthorizedTransactionInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=amount,
            network=network,
            reason_code="10.4",
        )
        defaults.update(kw)
        return UnauthorizedTransactionInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = score_unauthorized_transaction(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_score_bounded(self):
        r = score_unauthorized_transaction(self._inp())
        assert 0 <= r.total_score <= 100

    def test_dimensions_count(self):
        r = score_unauthorized_transaction(self._inp())
        assert len(r.dimensions) == 5

    # ── CE 3.0 ───────────────────────────────────────────────────

    def test_ce3_eligible_on_visa_with_2_prior_txns(self):
        r = score_unauthorized_transaction(self._inp(
            network="VISA", prior_non_disputed_txn_count=2,
        ))
        assert r.ce3_applicable is True

    def test_ce3_not_eligible_on_mc(self):
        r = score_unauthorized_transaction(self._inp(
            network="MC", prior_non_disputed_txn_count=5,
        ))
        assert r.ce3_applicable is False

    def test_ce3_not_eligible_with_1_txn(self):
        r = score_unauthorized_transaction(self._inp(
            network="VISA", prior_non_disputed_txn_count=1,
        ))
        assert r.ce3_applicable is False

    def test_ce3_boosts_win_adj(self):
        without = score_unauthorized_transaction(self._inp(
            network="VISA", prior_non_disputed_txn_count=0,
        ))
        with_ce3 = score_unauthorized_transaction(self._inp(
            network="VISA", prior_non_disputed_txn_count=2,
        ))
        assert with_ce3.win_rate_adjustment > without.win_rate_adjustment

    # ── 3DS ──────────────────────────────────────────────────────

    def test_3ds_pass_boosts_auth_dimension(self):
        r = score_unauthorized_transaction(self._inp(
            three_d_secure_result=AuthVerificationResult.PASS,
        ))
        auth_dim = next(d for d in r.dimensions if "Identity" in d.dimension)
        assert auth_dim.score >= 15

    def test_3ds_not_run_in_weaknesses(self):
        r = score_unauthorized_transaction(self._inp(
            three_d_secure_result=AuthVerificationResult.NOT_RUN,
        ))
        assert any("3DS" in w or "3D" in w for w in r.weaknesses)

    # ── Strong defense ────────────────────────────────────────────

    def test_strong_defensibility_full_auth(self):
        r = score_unauthorized_transaction(self._inp(
            network="VISA",
            three_d_secure_result=AuthVerificationResult.PASS,
            cvv_result=AuthVerificationResult.PASS,
            avs_result=AuthVerificationResult.PASS,
            ip_matches_prior_sessions=True,
            device_fingerprint_matches=True,
            shipped_to_account_address=True,
            prior_purchases_on_account=5,
            account_age_days=365,
            login_before_purchase=True,
            login_from_known_device=True,
            prior_non_disputed_txn_count=3,
            prior_unauthorized_disputes=0,
        ))
        assert r.defensibility in (UnauthorizedDefensibility.STRONG, UnauthorizedDefensibility.ADEQUATE)
        assert r.win_rate_adjustment > 0

    # ── Weak defense ──────────────────────────────────────────────

    def test_weak_defensibility_no_evidence(self):
        r = score_unauthorized_transaction(self._inp())
        assert r.defensibility in (
            UnauthorizedDefensibility.WEAK, UnauthorizedDefensibility.INDEFENSIBLE,
        )

    def test_serial_disputer_in_weaknesses(self):
        r = score_unauthorized_transaction(self._inp(
            prior_unauthorized_disputes=5,
        ))
        assert any("serial" in w.lower() or "prior" in w.lower() for w in r.weaknesses)

    # ── CVV / AVS ────────────────────────────────────────────────

    def test_cvv_pass_in_strengths(self):
        r = score_unauthorized_transaction(self._inp(
            cvv_result=AuthVerificationResult.PASS,
        ))
        assert any("CVV" in s for s in r.strengths)

    def test_avs_pass_in_strengths(self):
        r = score_unauthorized_transaction(self._inp(
            avs_result=AuthVerificationResult.PASS,
        ))
        assert any("AVS" in s or "address" in s.lower() for s in r.strengths)

    # ── Recommendation ────────────────────────────────────────────

    def test_recommendation_non_empty(self):
        r = score_unauthorized_transaction(self._inp())
        assert len(r.recommendation) > 0


# ═══════════════════════════════════════════════════════════════════
# Dispute Outcome Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeOutcomeTracker:

    def _record(self, dispute_id="D-001", network="VISA", code="INR",
                amount=100.0, outcome=OutcomeResult.WON,
                days_ago=5, pred=0.50) -> DisputeOutcomeRecord:
        return DisputeOutcomeRecord(
            dispute_id=dispute_id,
            network=network,
            reason_code=code,
            transaction_amount=amount,
            outcome=outcome,
            resolved_epoch=BASE_NOW - days_ago * DAY,
            predicted_win_probability=pred,
        )

    # ── Empty input ───────────────────────────────────────────────

    def test_empty_records_returns_report(self):
        r = compute_dispute_performance([], BASE_NOW)
        assert r.total_disputes == 0
        assert r.overall_win_rate == 0.0

    # ── Basic stats ───────────────────────────────────────────────

    def test_total_disputes_count(self):
        records = [
            self._record("D-001"), self._record("D-002"), self._record("D-003"),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.total_disputes == 3

    def test_win_rate_calculation(self):
        records = [
            self._record("D-001", outcome=OutcomeResult.WON),
            self._record("D-002", outcome=OutcomeResult.WON),
            self._record("D-003", outcome=OutcomeResult.LOST),
            self._record("D-004", outcome=OutcomeResult.LOST),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.overall_win_rate == pytest.approx(0.50, abs=0.01)

    def test_revenue_recovered_sums_won(self):
        records = [
            self._record("D-001", amount=100.0, outcome=OutcomeResult.WON),
            self._record("D-002", amount=200.0, outcome=OutcomeResult.WON),
            self._record("D-003", amount=50.0,  outcome=OutcomeResult.LOST),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.revenue_recovered == pytest.approx(300.0, abs=0.01)

    def test_revenue_lost_sums_lost_and_accepted(self):
        records = [
            self._record("D-001", amount=100.0, outcome=OutcomeResult.LOST),
            self._record("D-002", amount=50.0,  outcome=OutcomeResult.ACCEPTED),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        # revenue_lost tracks LOST+ACCEPTED combined; revenue_accepted tracks ACCEPTED separately
        assert r.revenue_lost >= 100.0
        assert r.revenue_accepted >= 50.0

    # ── Segmentation ─────────────────────────────────────────────

    def test_by_network_contains_all_networks(self):
        records = [
            self._record("D-001", network="VISA"),
            self._record("D-002", network="MC"),
            self._record("D-003", network="VISA"),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        nets = {s.segment_key for s in r.by_network}
        assert "VISA" in nets
        assert "MC" in nets

    def test_by_reason_code_segmentation(self):
        records = [
            self._record("D-001", code="INR"),
            self._record("D-002", code="SNAD"),
            self._record("D-003", code="INR"),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        codes = {s.segment_key for s in r.by_reason_code}
        assert "INR" in codes
        assert "SNAD" in codes

    # ── Trend ────────────────────────────────────────────────────

    def test_improving_trend_on_better_recent_win_rate(self):
        # Recent (last 30d): 4 won, 1 lost = 80%
        # Prior (30-60d ago): 1 won, 4 lost = 20%
        records = []
        for i in range(4):
            records.append(self._record(f"R-{i}", outcome=OutcomeResult.WON, days_ago=5))
        records.append(self._record("R-4", outcome=OutcomeResult.LOST, days_ago=5))
        for i in range(1):
            records.append(self._record(f"P-{i}", outcome=OutcomeResult.WON, days_ago=40))
        for i in range(4):
            records.append(self._record(f"P-{i+1}", outcome=OutcomeResult.LOST, days_ago=40))
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.performance_trend == PerformanceTrend.IMPROVING

    def test_insufficient_data_trend_on_few_records(self):
        records = [self._record("D-001", days_ago=5)]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.performance_trend == PerformanceTrend.INSUFFICIENT_DATA

    # ── Top loss reason ───────────────────────────────────────────

    def test_top_loss_reason_identified(self):
        records = [
            self._record("D-001", code="SNAD", outcome=OutcomeResult.LOST),
            self._record("D-002", code="SNAD", outcome=OutcomeResult.LOST),
            self._record("D-003", code="INR",  outcome=OutcomeResult.LOST),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.top_loss_reason == "SNAD"

    # ── Prediction calibration ────────────────────────────────────

    def test_calibration_delta_positive_when_better_than_predicted(self):
        # Actual win rate = 100%, predicted = 50% → delta = +0.50
        records = [
            self._record("D-001", outcome=OutcomeResult.WON, pred=0.50),
            self._record("D-002", outcome=OutcomeResult.WON, pred=0.50),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert r.prediction_calibration_delta > 0

    # ── Recommendations ───────────────────────────────────────────

    def test_recommendations_non_empty(self):
        records = [self._record()]
        r = compute_dispute_performance(records, BASE_NOW)
        assert len(r.recommendations) >= 1

    def test_low_win_rate_recommendation(self):
        # Win rate < 40%: 1 won, 3 lost
        records = [
            self._record("D-001", outcome=OutcomeResult.WON),
            self._record("D-002", outcome=OutcomeResult.LOST),
            self._record("D-003", outcome=OutcomeResult.LOST),
            self._record("D-004", outcome=OutcomeResult.LOST),
        ]
        r = compute_dispute_performance(records, BASE_NOW)
        assert any("win rate" in rec.lower() or "40%" in rec for rec in r.recommendations)
