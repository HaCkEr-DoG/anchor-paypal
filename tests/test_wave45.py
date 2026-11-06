"""
Tests for Wave 45: refund_calculator, chargeback_fee_estimator.
"""
import pytest
from oracle.refund_calculator import (
    calculate_refund, RefundCalculatorInput,
    RefundType, RefundStrategy, RefundRecommendation,
)
from oracle.chargeback_fee_estimator import (
    estimate_chargeback_fees, FeeEstimatorInput,
    Network, HDRTier, DisputeOutcome,
)


# ═══════════════════════════════════════════════════════════════════
# Refund Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundCalculator:

    def _inp(self, amount=100.0, win_prob=0.55, **kw) -> RefundCalculatorInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code="INR",
            transaction_amount=amount,
            win_probability=win_prob,
        )
        defaults.update(kw)
        return RefundCalculatorInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calculate_refund(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_five_scenarios_returned(self):
        r = calculate_refund(self._inp())
        assert len(r.scenarios) == 5

    def test_all_scenario_strategies_present(self):
        r = calculate_refund(self._inp())
        strategies = {s.strategy for s in r.scenarios}
        assert RefundStrategy.REFUND_IMMEDIATELY in strategies
        assert RefundStrategy.FIGHT_AND_WIN in strategies
        assert RefundStrategy.FIGHT_AND_LOSE in strategies
        assert RefundStrategy.NEGOTIATE_PARTIAL in strategies
        assert RefundStrategy.ACCEPT_CHARGEBACK in strategies

    # ── Cost formulas ─────────────────────────────────────────────

    def test_full_refund_cost_equals_amount_plus_gateway(self):
        r = calculate_refund(self._inp(amount=100.0,
                                        gateway_fee_pct=0.029,
                                        gateway_fee_flat=0.30))
        expected_gw = round(100.0 * 0.029 + 0.30, 2)
        s = next(s for s in r.scenarios if s.strategy == RefundStrategy.REFUND_IMMEDIATELY)
        assert s.gross_loss == pytest.approx(100.0)
        assert s.fees_lost == pytest.approx(expected_gw, abs=0.01)

    def test_accept_chargeback_includes_cb_fee(self):
        r = calculate_refund(self._inp(chargeback_fee=20.0))
        s = next(s for s in r.scenarios if s.strategy == RefundStrategy.ACCEPT_CHARGEBACK)
        assert s.dispute_fees == pytest.approx(20.0)

    def test_fight_and_lose_includes_fight_cost_and_cb_fee(self):
        r = calculate_refund(self._inp(fight_cost=15.0, chargeback_fee=20.0))
        s = next(s for s in r.scenarios if s.strategy == RefundStrategy.FIGHT_AND_LOSE)
        assert s.dispute_fees == pytest.approx(35.0)

    def test_ev_fight_formula(self):
        p = 0.60
        amount = 100.0
        gw = round(amount * 0.029 + 0.30, 2)
        fight_cost = 15.0
        cb_fee = 20.0

        # EV(fight) = p×(gw + fight_cost) + (1-p)×(amount + gw + fight_cost + cb_fee)
        win_cost = gw + fight_cost
        lose_cost = amount + gw + fight_cost + cb_fee
        expected = round(p * win_cost + (1 - p) * lose_cost, 2)

        r = calculate_refund(self._inp(
            amount=amount, win_prob=p,
            fight_cost=fight_cost, chargeback_fee=cb_fee,
        ))
        assert r.ev_fight == pytest.approx(expected, abs=0.5)

    # ── Partial refund ────────────────────────────────────────────

    def test_partial_refund_amount_from_pct(self):
        r = calculate_refund(self._inp(
            amount=200.0,
            can_offer_partial=True,
            partial_refund_pct=0.50,
        ))
        assert r.partial_refund_amount == pytest.approx(100.0, abs=0.01)

    def test_partial_saves_vs_chargeback_positive(self):
        r = calculate_refund(self._inp(
            amount=200.0,
            can_offer_partial=True,
            partial_refund_pct=0.50,
        ))
        # partial refund should cost less than accepting chargeback (which has cb fee + amount)
        assert r.ev_refund_partial < r.ev_accept_chargeback

    def test_partial_refund_fixed_overrides_pct(self):
        r = calculate_refund(self._inp(
            amount=200.0,
            can_offer_partial=True,
            partial_refund_pct=0.50,
            partial_refund_fixed=30.0,
        ))
        assert r.partial_refund_amount == pytest.approx(30.0)

    # ── Subscription pro-ration ───────────────────────────────────

    def test_subscription_prorated_amount_computed(self):
        r = calculate_refund(self._inp(
            amount=120.0,
            is_subscription=True,
            subscription_months_used=1,
            subscription_total_months=12,
        ))
        # 11 remaining months / 12 = 91.7% → ~$110
        assert r.partial_refund_amount == pytest.approx(110.0, abs=0.5)

    # ── VIP churn risk ────────────────────────────────────────────

    def test_vip_churn_included_in_lose_scenario(self):
        r = calculate_refund(self._inp(
            buyer_ltv=500.0,
            is_vip=True,
        ))
        s = next(s for s in r.scenarios if s.strategy == RefundStrategy.FIGHT_AND_LOSE)
        # VIP churn = 500 × 0.15 = 75
        assert s.ltv_churn_risk == pytest.approx(75.0)

    # ── HDR fee propagation ───────────────────────────────────────

    def test_hdr_fee_added_when_at_risk(self):
        r = calculate_refund(self._inp(
            hdr_at_risk=True,
            hdr_fee_per_dispute=30.0,
        ))
        for s in r.scenarios:
            assert s.hdr_fees == pytest.approx(30.0)

    def test_no_hdr_fee_when_not_at_risk(self):
        r = calculate_refund(self._inp(hdr_at_risk=False))
        for s in r.scenarios:
            assert s.hdr_fees == 0.0

    # ── Recommendation ────────────────────────────────────────────

    def test_recommendation_non_empty(self):
        r = calculate_refund(self._inp())
        assert len(r.recommendation.value) > 0

    def test_fight_recommended_on_high_win_prob(self):
        r = calculate_refund(self._inp(
            amount=500.0, win_prob=0.90,
            fight_cost=15.0, chargeback_fee=20.0,
        ))
        assert r.recommendation == RefundRecommendation.FIGHT

    def test_full_refund_recommended_when_cheaper_than_chargeback(self):
        # With low win prob, refund should be cheaper
        r = calculate_refund(self._inp(
            amount=50.0, win_prob=0.10,
            chargeback_fee=20.0, fight_cost=15.0,
        ))
        assert r.recommendation in (
            RefundRecommendation.REFUND_PROACTIVELY,
            RefundRecommendation.ACCEPT_CHARGEBACK,
            RefundRecommendation.NEGOTIATE_PARTIAL,
        )

    # ── EV totals ─────────────────────────────────────────────────

    def test_ev_values_positive(self):
        r = calculate_refund(self._inp())
        # All costs are positive
        assert r.ev_fight >= 0
        assert r.ev_refund_full >= 0
        assert r.ev_accept_chargeback >= 0


# ═══════════════════════════════════════════════════════════════════
# Chargeback Fee Estimator Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackFeeEstimator:

    def _inp(self, network=Network.PAYPAL, amount=100.0, **kw) -> FeeEstimatorInput:
        defaults = dict(
            dispute_id="D-001",
            network=network,
            transaction_amount=amount,
        )
        defaults.update(kw)
        return FeeEstimatorInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = estimate_chargeback_fees(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_summary_contains_network(self):
        r = estimate_chargeback_fees(self._inp(network=Network.VISA))
        assert "VISA" in r.summary

    def test_fee_items_non_empty(self):
        r = estimate_chargeback_fees(self._inp())
        assert len(r.fee_items) >= 1

    # ── Per-network base fees ─────────────────────────────────────

    def test_paypal_cb_fee_is_20(self):
        r = estimate_chargeback_fees(self._inp(network=Network.PAYPAL))
        cb = next(f for f in r.fee_items if "chargeback" in f.name.lower())
        assert cb.amount == pytest.approx(20.0)

    def test_visa_cb_fee_is_15(self):
        r = estimate_chargeback_fees(self._inp(network=Network.VISA))
        cb = next(f for f in r.fee_items if "chargeback" in f.name.lower())
        assert cb.amount == pytest.approx(15.0)

    def test_amex_cb_fee_is_25(self):
        r = estimate_chargeback_fees(self._inp(network=Network.AMEX))
        cb = next(f for f in r.fee_items if "chargeback" in f.name.lower())
        assert cb.amount == pytest.approx(25.0)

    # ── HDR surcharge ─────────────────────────────────────────────

    def test_hdr_fee_tier_adds_30(self):
        r = estimate_chargeback_fees(self._inp(hdr_tier=HDRTier.FEE_TIER))
        hdr = next((f for f in r.fee_items if "HDR" in f.name), None)
        assert hdr is not None
        assert hdr.amount == pytest.approx(30.0)

    def test_no_hdr_fee_in_normal_tier(self):
        r = estimate_chargeback_fees(self._inp(hdr_tier=HDRTier.NORMAL))
        hdr_items = [f for f in r.fee_items if "HDR" in f.name]
        assert len(hdr_items) == 0

    def test_severe_hdr_fee_is_30(self):
        r = estimate_chargeback_fees(self._inp(hdr_tier=HDRTier.SEVERE))
        hdr = next((f for f in r.fee_items if "HDR" in f.name), None)
        assert hdr is not None
        assert hdr.amount == pytest.approx(30.0)

    # ── Arbitration ───────────────────────────────────────────────

    def test_visa_arbitration_fee_is_500(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.VISA,
            escalated_to_arbitration=True,
        ))
        arb = next((f for f in r.fee_items if "arbitration" in f.name.lower()), None)
        assert arb is not None
        assert arb.amount == pytest.approx(500.0)

    def test_no_arbitration_fee_without_escalation(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.VISA,
            escalated_to_arbitration=False,
        ))
        arb_items = [f for f in r.fee_items if "arbitration" in f.name.lower()]
        assert len(arb_items) == 0

    def test_paypal_no_arbitration(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.PAYPAL,
            escalated_to_arbitration=True,
        ))
        arb_items = [f for f in r.fee_items if "arbitration" in f.name.lower()]
        assert len(arb_items) == 0

    # ── Pre-arbitration reversal ──────────────────────────────────

    def test_pre_arb_reversal_on_loss(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.VISA,
            assumed_outcome=DisputeOutcome.PRE_ARBITRATION_LOSS,
        ))
        pre_arb = next((f for f in r.fee_items if "reversal" in f.name.lower()), None)
        assert pre_arb is not None
        assert pre_arb.amount == pytest.approx(150.0)

    # ── Monitoring program ────────────────────────────────────────

    def test_monitoring_fee_added_when_enrolled(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.VISA,
            in_monitoring_program=True,
        ))
        mon = next((f for f in r.fee_items if "monitoring" in f.name.lower()), None)
        assert mon is not None
        assert mon.amount == pytest.approx(10.0)

    def test_no_monitoring_fee_when_not_enrolled(self):
        r = estimate_chargeback_fees(self._inp(in_monitoring_program=False))
        mon_items = [f for f in r.fee_items if "monitoring" in f.name.lower()]
        assert len(mon_items) == 0

    # ── Total calculations ────────────────────────────────────────

    def test_total_fees_equals_sum_of_items(self):
        r = estimate_chargeback_fees(self._inp())
        assert r.total_fees == pytest.approx(sum(f.amount for f in r.fee_items), abs=0.01)

    def test_total_cost_with_loss_includes_transaction(self):
        r = estimate_chargeback_fees(self._inp(amount=100.0))
        assert r.total_cost_with_loss == pytest.approx(r.total_fees + 100.0, abs=0.01)

    def test_win_cost_less_than_loss_cost(self):
        r = estimate_chargeback_fees(self._inp())
        assert r.total_cost_win < r.total_cost_with_loss

    # ── Annual projection ─────────────────────────────────────────

    def test_annual_volume_is_4x_90_day(self):
        r = estimate_chargeback_fees(self._inp(disputes_last_90_days=10))
        assert r.annual_dispute_volume == 40

    def test_annual_exposure_positive(self):
        r = estimate_chargeback_fees(self._inp(disputes_last_90_days=20))
        assert r.annual_fee_exposure > 0

    # ── Recommendation ────────────────────────────────────────────

    def test_recommendation_non_empty(self):
        r = estimate_chargeback_fees(self._inp())
        assert len(r.recommendation) > 0

    def test_hdr_recommendation_warns_about_tier(self):
        r = estimate_chargeback_fees(self._inp(hdr_tier=HDRTier.SEVERE))
        assert "SEVERE" in r.recommendation or "HDR" in r.recommendation

    def test_arbitration_recommendation_warns_about_cost(self):
        r = estimate_chargeback_fees(self._inp(
            network=Network.VISA,
            escalated_to_arbitration=True,
        ))
        assert "arb" in r.recommendation.lower() or "500" in r.recommendation

    # ── Gateway fee behavior ──────────────────────────────────────

    def test_gateway_fee_not_refunded_by_default(self):
        r = estimate_chargeback_fees(self._inp(
            amount=100.0,
            gateway_fee_pct=0.029,
            gateway_fee_flat=0.30,
            gateway_refunds_fee=False,
        ))
        gw_items = [f for f in r.fee_items if "gateway" in f.name.lower()]
        assert len(gw_items) >= 1

    def test_gateway_fee_absent_when_refunded(self):
        r = estimate_chargeback_fees(self._inp(gateway_refunds_fee=True))
        gw_items = [f for f in r.fee_items if "gateway" in f.name.lower()]
        assert len(gw_items) == 0
