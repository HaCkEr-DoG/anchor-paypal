"""
Tests for Wave 22: merchant_risk_scorer, customer_ltv_estimator.
"""
import pytest
from oracle.merchant_risk_scorer import (
    score_merchant_risk, MerchantRiskInput, RiskTier, RiskDimension,
)
from oracle.customer_ltv_estimator import (
    estimate_customer_ltv, CustomerLTVInput, RetentionOutcome,
)


# ═══════════════════════════════════════════════════════════════════
# Merchant Risk Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestMerchantRiskScorer:

    def _inp(self, **kw) -> MerchantRiskInput:
        defaults = dict(
            merchant_id="M-001",
            disputes_last_90d=5,
            transactions_last_90d=2000,     # 0.25% — healthy
            total_disputes_ever=50,
            disputes_won=35,                 # 70% win rate
            disputes_responded_on_time=47,
            disputes_responded_late_or_missed=3,
            pct_inr=0.60,
            pct_snad=0.25,
            pct_unauthorized=0.15,
            pct_sp_covered=0.75,
            avg_evidence_score=72.0,
            months_on_paypal=36,
        )
        defaults.update(kw)
        return MerchantRiskInput(**defaults)

    # ── Overall score ────────────────────────────────────────────

    def test_healthy_merchant_score_above_75(self):
        r = score_merchant_risk(self._inp())
        assert r.composite_score >= 75
        assert r.risk_tier == RiskTier.HEALTHY

    def test_high_dispute_rate_lowers_score(self):
        # 3% dispute rate — severe
        r = score_merchant_risk(self._inp(disputes_last_90d=60, transactions_last_90d=2000))
        assert r.composite_score < 75

    def test_low_win_rate_lowers_score(self):
        r = score_merchant_risk(self._inp(disputes_won=5, total_disputes_ever=50))
        assert r.composite_score < 70

    def test_critical_tier_very_high_dispute_rate(self):
        r = score_merchant_risk(self._inp(
            disputes_last_90d=120, transactions_last_90d=2000,  # 6%
            disputes_won=5, total_disputes_ever=50,
            disputes_responded_on_time=20, disputes_responded_late_or_missed=30,
        ))
        assert r.risk_tier in (RiskTier.CRITICAL, RiskTier.ELEVATED)

    def test_composite_score_range(self):
        r = score_merchant_risk(self._inp())
        assert 0 <= r.composite_score <= 100

    # ── Dimension scores ─────────────────────────────────────────

    def test_six_dimensions_returned(self):
        r = score_merchant_risk(self._inp())
        assert len(r.dimensions) == 6

    def test_dimension_scores_in_range(self):
        r = score_merchant_risk(self._inp())
        for d in r.dimensions:
            assert 0 <= d.score <= 100

    def test_weights_sum_to_one(self):
        r = score_merchant_risk(self._inp())
        total_weight = sum(d.weight for d in r.dimensions)
        assert abs(total_weight - 1.0) < 0.001

    def test_weighted_contributions_sum_to_composite(self):
        r = score_merchant_risk(self._inp())
        total_contrib = sum(d.weighted_contribution for d in r.dimensions)
        assert abs(total_contrib - r.composite_score) < 0.5

    def test_hdr_zone_adds_recommendation(self):
        r = score_merchant_risk(self._inp(is_in_hdr_zone=True))
        assert any("hdr" in rec.lower() for rec in r.recommendations)

    # ── Specific dimension tests ─────────────────────────────────

    def test_zero_transactions_does_not_crash(self):
        r = score_merchant_risk(self._inp(transactions_last_90d=0))
        assert r.composite_score >= 0

    def test_zero_disputes_does_not_crash(self):
        r = score_merchant_risk(self._inp(total_disputes_ever=0))
        assert r.composite_score >= 0

    def test_poor_response_hygiene_lowers_score(self):
        good = score_merchant_risk(self._inp(
            disputes_responded_on_time=49, disputes_responded_late_or_missed=1,
        ))
        bad = score_merchant_risk(self._inp(
            disputes_responded_on_time=25, disputes_responded_late_or_missed=25,
        ))
        assert bad.composite_score < good.composite_score

    def test_low_sp_coverage_lowers_score(self):
        high_sp = score_merchant_risk(self._inp(pct_sp_covered=0.90))
        low_sp  = score_merchant_risk(self._inp(pct_sp_covered=0.20))
        assert low_sp.composite_score < high_sp.composite_score

    def test_snad_heavy_mix_lowers_score(self):
        balanced = score_merchant_risk(self._inp(pct_inr=0.70, pct_snad=0.20, pct_unauthorized=0.10))
        snad_heavy = score_merchant_risk(self._inp(pct_inr=0.20, pct_snad=0.60, pct_unauthorized=0.20))
        assert snad_heavy.composite_score < balanced.composite_score

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = score_merchant_risk(self._inp(merchant_id="M-XYZ"))
        assert "M-XYZ" in r.summary

    def test_dispute_rate_pct_matches_inputs(self):
        r = score_merchant_risk(self._inp(disputes_last_90d=10, transactions_last_90d=1000))
        assert abs(r.dispute_rate_pct - 1.0) < 0.001   # 1.00%

    def test_recommendations_non_empty_for_elevated(self):
        r = score_merchant_risk(self._inp(
            disputes_last_90d=30, transactions_last_90d=2000,
            disputes_won=10, total_disputes_ever=50,
        ))
        assert len(r.recommendations) > 0


# ═══════════════════════════════════════════════════════════════════
# Customer LTV Estimator Tests
# ═══════════════════════════════════════════════════════════════════

class TestCustomerLTVEstimator:

    def _inp(self, **kw) -> CustomerLTVInput:
        defaults = dict(
            dispute_id="D-001",
            buyer_id="B-001",
            total_orders=10,
            total_spend=1000.0,
            months_as_customer=24,
            dispute_count_lifetime=1,
            dispute_amount=50.0,
            p_win_dispute=0.60,
            fight_cost=25.0,
            estimated_years_remaining=3.0,
        )
        defaults.update(kw)
        return CustomerLTVInput(**defaults)

    # ── LTV calculation ───────────────────────────────────────────

    def test_ltv_positive_for_loyal_buyer(self):
        r = estimate_customer_ltv(self._inp())
        assert r.discounted_ltv > 0

    def test_raw_ltv_exceeds_discounted(self):
        r = estimate_customer_ltv(self._inp())
        assert r.raw_ltv >= r.discounted_ltv

    def test_high_dispute_rate_reduces_ltv(self):
        low_dr  = estimate_customer_ltv(self._inp(dispute_count_lifetime=0))
        high_dr = estimate_customer_ltv(self._inp(dispute_count_lifetime=5, total_orders=10))
        assert high_dr.discounted_ltv < low_dr.discounted_ltv

    def test_first_time_buyer_ltv_less_than_loyal(self):
        loyal = estimate_customer_ltv(self._inp(total_orders=10, months_as_customer=24,
                                                total_spend=1000.0, dispute_count_lifetime=0))
        # first-time buyer: low total spend (AOV=$50) so risk-adjusted LTV stays below loyal buyer
        first = estimate_customer_ltv(self._inp(total_orders=1, months_as_customer=1,
                                                total_spend=50.0, dispute_count_lifetime=0))
        assert first.discounted_ltv < loyal.discounted_ltv

    # ── Recommendation logic ─────────────────────────────────────

    def test_loyal_buyer_proactive_refund_justified(self):
        r = estimate_customer_ltv(self._inp(
            total_orders=15, months_as_customer=36,
            dispute_count_lifetime=0,
            dispute_amount=30.0,
        ))
        assert r.recommendation in (
            RetentionOutcome.PROACTIVE_REFUND_JUSTIFIED,
            RetentionOutcome.PARTIAL_CONCESSION_OPTIMAL,
        )

    def test_high_dispute_rate_buyer_fight(self):
        r = estimate_customer_ltv(self._inp(
            dispute_count_lifetime=4, total_orders=10,  # 40% dispute rate
        ))
        assert r.recommendation == RetentionOutcome.FIGHT_DISPUTE

    def test_low_ltv_low_value_recommendation(self):
        r = estimate_customer_ltv(self._inp(
            total_orders=1, months_as_customer=1,
            total_spend=10.0, dispute_amount=200.0,
            dispute_count_lifetime=0,
        ))
        # LTV << dispute_amount → low value or fight
        assert r.recommendation in (
            RetentionOutcome.LOW_VALUE_IGNORE,
            RetentionOutcome.FIGHT_DISPUTE,
        )

    # ── Retention EV ─────────────────────────────────────────────

    def test_ev_retain_proactive_less_than_ltv(self):
        r = estimate_customer_ltv(self._inp())
        assert r.ev_retain_proactive < r.discounted_ltv

    def test_concession_cap_non_negative(self):
        r = estimate_customer_ltv(self._inp())
        assert r.concession_cap >= 0

    def test_loyal_buyer_flag(self):
        loyal = estimate_customer_ltv(self._inp(total_orders=10, dispute_count_lifetime=0))
        assert loyal.is_loyal_buyer is True

    def test_first_buyer_not_loyal(self):
        r = estimate_customer_ltv(self._inp(total_orders=1, dispute_count_lifetime=0))
        assert r.is_loyal_buyer is False

    def test_dispute_rate_calculation(self):
        r = estimate_customer_ltv(self._inp(total_orders=10, dispute_count_lifetime=2))
        assert abs(r.dispute_rate - 0.20) < 0.001

    def test_purchase_frequency_positive(self):
        r = estimate_customer_ltv(self._inp(total_orders=12, months_as_customer=12))
        assert r.purchase_frequency_per_year > 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = estimate_customer_ltv(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_rationale_non_empty(self):
        r = estimate_customer_ltv(self._inp())
        assert len(r.rationale) > 10

    def test_zero_orders_does_not_crash(self):
        r = estimate_customer_ltv(self._inp(total_orders=0, months_as_customer=0, total_spend=0))
        assert r.discounted_ltv >= 0
