"""
Tests for Wave 7: dispute_predictor, revenue_impact.
"""
import pytest
from datetime import date

from oracle.dispute_predictor import (
    predict_dispute_risk, batch_predict, OrderRiskInput,
    DisputeRiskTier, OrderAction, DisputePrediction,
)
from oracle.revenue_impact import (
    calculate_revenue_impact, DisputePortfolioInput,
    ImpactCategory, RevenueImpactReport,
)

TODAY = date(2024, 6, 15)


# ═══════════════════════════════════════════════════════════════════
# Dispute Predictor Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputePredictor:

    def _order(self, **kwargs) -> OrderRiskInput:
        defaults = dict(
            order_id="O-" + str(id(kwargs)),
            buyer_account_age_days=180,
            buyer_total_orders=5,
            buyer_total_disputes=0,
            buyer_disputes_last_90d=0,
            buyer_dispute_rate=0.0,
            order_amount=100.0,
            product_category="general",
            carrier="FEDEX_GROUND",
        )
        defaults.update(kwargs)
        return OrderRiskInput(**defaults)

    def test_clean_buyer_very_low_risk(self):
        o = self._order(
            buyer_account_age_days=400,
            buyer_total_orders=10,
            buyer_total_disputes=0,
            buyer_dispute_rate=0.0,
        )
        p = predict_dispute_risk(o, today=TODAY)
        assert p.risk_tier in (DisputeRiskTier.VERY_LOW, DisputeRiskTier.LOW)

    def test_new_account_elevated_risk(self):
        o = self._order(buyer_account_age_days=15)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.risk_tier not in (DisputeRiskTier.VERY_LOW,)

    def test_high_buyer_dispute_rate_high_risk(self):
        # 30% dispute rate + no established buyer discount (rate != 0) → elevated risk
        o = self._order(buyer_dispute_rate=0.30, buyer_total_disputes=3, buyer_total_orders=10)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.risk_tier in (DisputeRiskTier.MODERATE, DisputeRiskTier.HIGH, DisputeRiskTier.CRITICAL)

    def test_cross_merchant_flag_reject(self):
        o = self._order(buyer_is_network_flagged=True)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.recommended_action == OrderAction.REJECT

    def test_freight_forwarder_reject(self):
        o = self._order(shipping_to_freight_forwarder=True)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.recommended_action == OrderAction.REJECT

    def test_very_low_risk_process(self):
        o = self._order(
            buyer_account_age_days=500, buyer_total_orders=20,
            buyer_dispute_rate=0.0, carrier="FEDEX_EXPRESS",
        )
        p = predict_dispute_risk(o, today=TODAY)
        assert p.recommended_action in (OrderAction.PROCESS, OrderAction.VERIFY_ADDRESS)

    def test_high_dispute_rate_buyer_hold_for_review(self):
        o = self._order(
            buyer_dispute_rate=0.30, buyer_disputes_last_90d=3,
            order_amount=400.0, buyer_total_orders=5,
        )
        p = predict_dispute_risk(o, today=TODAY)
        assert p.recommended_action in (OrderAction.HOLD_FOR_REVIEW, OrderAction.REJECT)

    def test_lasership_increases_risk(self):
        o_good = self._order(carrier="FEDEX_EXPRESS")
        o_bad = self._order(carrier="LASERSHIP")
        p_good = predict_dispute_risk(o_good, today=TODAY)
        p_bad = predict_dispute_risk(o_bad, today=TODAY)
        assert p_bad.dispute_probability > p_good.dispute_probability

    def test_digital_goods_floor_applied(self):
        o = self._order(is_digital_goods=True, product_category="digital_goods")
        p = predict_dispute_risk(o, today=TODAY)
        assert p.dispute_probability >= 0.06

    def test_holiday_period_increases_risk(self):
        o_normal = self._order()
        o_holiday = self._order(is_holiday_period=True)
        p_n = predict_dispute_risk(o_normal, today=TODAY)
        p_h = predict_dispute_risk(o_holiday, today=TODAY)
        assert p_h.dispute_probability > p_n.dispute_probability

    def test_billing_shipping_mismatch_risk_flag(self):
        o = self._order(billing_shipping_mismatch=True)
        p = predict_dispute_risk(o, today=TODAY)
        assert any("MISMATCH" in f for f in p.risk_flags)

    def test_expected_loss_computed(self):
        o = self._order(order_amount=200.0, buyer_dispute_rate=0.10, buyer_total_orders=5)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.expected_loss > 0

    def test_expected_loss_less_than_order_amount(self):
        o = self._order(order_amount=200.0)
        p = predict_dispute_risk(o, today=TODAY)
        assert p.expected_loss < o.order_amount

    def test_contributing_factors_populated(self):
        o = self._order(
            buyer_account_age_days=15, buyer_dispute_rate=0.20,
            billing_shipping_mismatch=True,
        )
        p = predict_dispute_risk(o, today=TODAY)
        assert len(p.contributing_factors) >= 2

    def test_established_buyer_discount(self):
        o_new = self._order(buyer_total_orders=1, buyer_dispute_rate=0.0)
        o_est = self._order(buyer_total_orders=15, buyer_dispute_rate=0.0)
        p_new = predict_dispute_risk(o_new, today=TODAY)
        p_est = predict_dispute_risk(o_est, today=TODAY)
        assert p_est.dispute_probability < p_new.dispute_probability

    def test_order_velocity_spike_flagged(self):
        o = self._order(orders_in_last_24h=5)
        p = predict_dispute_risk(o, today=TODAY)
        assert any("VELOCITY" in f for f in p.risk_flags)

    def test_confidence_at_least_60pct(self):
        o = self._order()
        p = predict_dispute_risk(o, today=TODAY)
        assert p.confidence >= 0.60

    def test_batch_predict_sorted_desc(self):
        orders = [
            self._order(order_id="O-low", buyer_dispute_rate=0.0),
            self._order(order_id="O-high", buyer_dispute_rate=0.30, buyer_total_orders=10),
            self._order(order_id="O-med", buyer_dispute_rate=0.08, buyer_total_orders=5),
        ]
        results = batch_predict(orders, today=TODAY)
        probs = [r.dispute_probability for r in results]
        assert probs == sorted(probs, reverse=True)

    def test_gift_cards_high_base_rate(self):
        # gift_cards base = 8%, use first-time buyer (no established discount)
        o = self._order(product_category="gift_cards", buyer_total_orders=1, buyer_total_disputes=0)
        p = predict_dispute_risk(o, today=TODAY)
        # 8% × carrier_multiplier > any other product base
        o_general = self._order(product_category="general", buyer_total_orders=1, buyer_total_disputes=0)
        p_general = predict_dispute_risk(o_general, today=TODAY)
        assert p.dispute_probability > p_general.dispute_probability

    def test_dispute_probability_capped_at_95(self):
        o = self._order(
            buyer_dispute_rate=0.50, buyer_disputes_last_90d=3,
            buyer_account_age_days=5, buyer_is_network_flagged=True,
            carrier="LASERSHIP", is_holiday_period=True,
        )
        p = predict_dispute_risk(o, today=TODAY)
        assert p.dispute_probability <= 0.95

    def test_dispute_probability_at_least_minimum(self):
        o = self._order()
        p = predict_dispute_risk(o, today=TODAY)
        assert p.dispute_probability >= 0.001


# ═══════════════════════════════════════════════════════════════════
# Revenue Impact Tests
# ═══════════════════════════════════════════════════════════════════

class TestRevenueImpact:

    def _inp(self, **kwargs) -> DisputePortfolioInput:
        defaults = dict(
            period_days=90,
            total_transactions=1000,
            total_gmv=100000.0,
            total_disputes=20,
            disputes_won_by_merchant=13,
            disputes_lost_by_merchant=7,
            avg_dispute_amount=150.0,
            avg_fight_cost=25.0,
            hourly_staff_rate=35.0,
            minutes_per_dispute=30.0,
            current_hdr_rate=0.020,
            is_in_fee_tier=False,
            annual_cost_of_capital=0.08,
            avg_days_to_resolve=45,
            monthly_revenue=50000.0,
            hdr_revenue_restriction_pct=0.0,
        )
        defaults.update(kwargs)
        return DisputePortfolioInput(**defaults)

    def test_dispute_rate_computed(self):
        r = calculate_revenue_impact(self._inp())
        assert r.dispute_rate == pytest.approx(0.020, abs=0.001)

    def test_merchant_win_rate_computed(self):
        r = calculate_revenue_impact(self._inp())
        # 13 wins / 20 total resolved = 65%
        assert r.merchant_win_rate == pytest.approx(0.65, abs=0.01)

    def test_direct_loss_correct(self):
        r = calculate_revenue_impact(self._inp())
        # 7 losses × $150 = $1050
        assert r.direct_loss == pytest.approx(1050.0, abs=1.0)

    def test_fight_cost_total_correct(self):
        r = calculate_revenue_impact(self._inp())
        # 20 disputes × $25 = $500
        assert r.fight_cost_total == pytest.approx(500.0, abs=1.0)

    def test_no_hdr_fee_when_not_in_tier(self):
        r = calculate_revenue_impact(self._inp(is_in_fee_tier=False))
        assert r.hdr_fee_total == 0.0

    def test_hdr_fee_applied_when_in_tier(self):
        r = calculate_revenue_impact(self._inp(
            is_in_fee_tier=True,
            current_hdr_rate=0.025,  # 2.5% > 1.5% threshold
            total_transactions=1000,
        ))
        # Disputes above 1.5% = (2.5% - 1.5%) × 1000 = 10 disputes
        # 10 × $30 = $300
        assert r.hdr_fee_total == pytest.approx(300.0, abs=1.0)

    def test_capital_cost_nonzero(self):
        r = calculate_revenue_impact(self._inp())
        assert r.capital_cost_total > 0

    def test_operational_cost_nonzero(self):
        r = calculate_revenue_impact(self._inp())
        assert r.operational_cost_total > 0

    def test_total_cost_sum_of_components(self):
        r = calculate_revenue_impact(self._inp())
        expected_total = (
            r.direct_loss + r.fight_cost_total + r.hdr_fee_total
            + r.capital_cost_total + r.operational_cost_total
            + r.revenue_restriction_loss
        )
        assert r.total_cost == pytest.approx(expected_total, rel=0.001)

    def test_gross_savings_from_fighting(self):
        r = calculate_revenue_impact(self._inp())
        # 13 wins × $150 = $1950
        assert r.gross_savings_from_fighting == pytest.approx(1950.0, abs=1.0)

    def test_net_savings_from_fighting(self):
        r = calculate_revenue_impact(self._inp())
        # $1950 gross - $500 fight cost = $1450
        assert r.net_savings_from_fighting == pytest.approx(1450.0, abs=1.0)

    def test_roi_of_fighting_positive(self):
        r = calculate_revenue_impact(self._inp())
        assert r.roi_of_fighting > 0

    def test_line_items_include_all_categories(self):
        r = calculate_revenue_impact(self._inp())
        cats = {li.category for li in r.line_items}
        assert ImpactCategory.DIRECT_LOSS in cats
        assert ImpactCategory.FIGHT_COST in cats
        assert ImpactCategory.TOTAL in cats

    def test_summary_populated(self):
        r = calculate_revenue_impact(self._inp())
        assert len(r.summary) > 50
        assert "dispute" in r.summary.lower()

    def test_recommendations_populated(self):
        r = calculate_revenue_impact(self._inp())
        assert len(r.recommendations) >= 1

    def test_revenue_restriction_loss_zero_by_default(self):
        r = calculate_revenue_impact(self._inp(hdr_revenue_restriction_pct=0.0))
        assert r.revenue_restriction_loss == 0.0

    def test_revenue_restriction_nonzero_when_set(self):
        r = calculate_revenue_impact(self._inp(
            hdr_revenue_restriction_pct=0.10,
            monthly_revenue=50000.0,
        ))
        assert r.revenue_restriction_loss > 0

    def test_cost_per_dispute_reasonable(self):
        r = calculate_revenue_impact(self._inp())
        # Should be more than just fight cost but less than avg dispute amount
        assert r.cost_per_dispute > 25.0
        assert r.cost_per_dispute < 300.0

    def test_cost_as_pct_of_gmv(self):
        r = calculate_revenue_impact(self._inp(total_gmv=100000.0))
        # Cost should be < 10% of GMV for a typical merchant
        assert r.cost_as_pct_of_gmv < 0.10

    def test_zero_disputes_no_crash(self):
        r = calculate_revenue_impact(self._inp(
            total_disputes=0,
            disputes_won_by_merchant=0,
            disputes_lost_by_merchant=0,
        ))
        assert r.total_cost == 0.0
        assert r.dispute_rate == 0.0

    def test_low_win_rate_generates_recommendation(self):
        r = calculate_revenue_impact(self._inp(
            disputes_won_by_merchant=4,
            disputes_lost_by_merchant=16,
        ))
        recs_lower = [rec.lower() for rec in r.recommendations]
        assert any("win rate" in rec for rec in recs_lower)
