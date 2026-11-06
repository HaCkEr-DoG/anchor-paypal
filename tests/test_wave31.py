"""
Tests for Wave 31: customer_ltv_calculator, dispute_batch_prioritizer.
"""
import pytest
from oracle.customer_ltv_calculator import (
    calculate_customer_ltv, CustomerLTVInput, LTVTier,
)
from oracle.dispute_batch_prioritizer import (
    prioritize_disputes, BatchPrioritizerInput, DisputeForPriority,
    PriorityBand,
)


# ═══════════════════════════════════════════════════════════════════
# Customer LTV Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestCustomerLTVCalculator:

    def _inp(self, **kw) -> CustomerLTVInput:
        defaults = dict(
            customer_id="C-001",
            total_order_count=10,
            total_order_value=500.0,
            months_as_customer=12,
            dispute_amount=100.0,
        )
        defaults.update(kw)
        return CustomerLTVInput(**defaults)

    # ── Tier classification ───────────────────────────────────────

    def test_vip_when_flagged(self):
        r = calculate_customer_ltv(self._inp(is_vip_flagged=True))
        assert r.ltv_tier == LTVTier.VIP

    def test_negligible_no_history(self):
        r = calculate_customer_ltv(self._inp(
            total_order_count=0, total_order_value=0.0, months_as_customer=0))
        assert r.ltv_tier == LTVTier.NEGLIGIBLE

    def test_high_fraud_zeroes_ltv(self):
        r = calculate_customer_ltv(self._inp(fraud_risk_score=80.0))
        assert r.fraud_discounted_ltv == 0.0
        assert r.fraud_discount_applied == 0.0

    def test_low_fraud_keeps_ltv(self):
        r = calculate_customer_ltv(self._inp(fraud_risk_score=10.0))
        assert r.fraud_discount_applied == 1.0
        assert r.fraud_discounted_ltv > 0

    def test_medium_fraud_discounts_ltv(self):
        no_fraud = calculate_customer_ltv(self._inp(fraud_risk_score=0.0))
        med_fraud = calculate_customer_ltv(self._inp(fraud_risk_score=50.0))
        assert med_fraud.fraud_discounted_ltv < no_fraud.fraud_discounted_ltv

    # ── LTV calculation ───────────────────────────────────────────

    def test_projected_ltv_positive_for_active_buyer(self):
        r = calculate_customer_ltv(self._inp(
            total_order_count=12, total_order_value=600.0, months_as_customer=6))
        assert r.projected_ltv > 0

    def test_historical_ltv_matches_input(self):
        r = calculate_customer_ltv(self._inp(total_order_value=500.0))
        assert r.historical_ltv == pytest.approx(500.0)

    def test_recent_activity_boosts_projected(self):
        no_recent = calculate_customer_ltv(self._inp(
            orders_last_6_months=0, value_last_6_months=0))
        with_recent = calculate_customer_ltv(self._inp(
            orders_last_6_months=6, value_last_6_months=300.0))
        assert with_recent.projected_ltv >= no_recent.projected_ltv

    # ── Concession logic ──────────────────────────────────────────

    def test_vip_max_concession_100pct(self):
        r = calculate_customer_ltv(self._inp(is_vip_flagged=True))
        assert r.max_concession_pct == pytest.approx(1.0)

    def test_negligible_max_concession_zero(self):
        r = calculate_customer_ltv(self._inp(
            total_order_count=0, total_order_value=0.0, months_as_customer=0))
        assert r.max_concession_pct == pytest.approx(0.0)

    def test_concession_amount_derived_from_dispute_amount(self):
        r = calculate_customer_ltv(self._inp(
            is_vip_flagged=True, dispute_amount=200.0))
        assert r.max_concession_amount == pytest.approx(200.0)  # 100% × 200

    def test_high_dispute_rate_reduces_ltv(self):
        low_rate  = calculate_customer_ltv(self._inp(dispute_rate=0.01))
        high_rate = calculate_customer_ltv(self._inp(dispute_rate=0.25))
        assert high_rate.fraud_discounted_ltv < low_rate.fraud_discounted_ltv

    # ── Fight recommendation ──────────────────────────────────────

    def test_recommend_fight_for_negligible(self):
        r = calculate_customer_ltv(self._inp(
            total_order_count=0, total_order_value=0.0, months_as_customer=0))
        assert r.recommend_full_fight is True

    def test_recommend_vip_treatment_for_vip(self):
        r = calculate_customer_ltv(self._inp(is_vip_flagged=True))
        assert r.recommend_vip_treatment is True

    def test_no_vip_treatment_for_low(self):
        r = calculate_customer_ltv(self._inp(
            total_order_count=0, total_order_value=0.0))
        assert r.recommend_vip_treatment is False

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_customer_id(self):
        r = calculate_customer_ltv(self._inp(customer_id="C-XYZ"))
        assert "C-XYZ" in r.summary

    def test_actions_non_empty(self):
        r = calculate_customer_ltv(self._inp())
        assert len(r.immediate_actions) >= 1


# ═══════════════════════════════════════════════════════════════════
# Dispute Batch Prioritizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeBatchPrioritizer:

    def _dispute(self, dispute_id="D-001", **kw) -> DisputeForPriority:
        defaults = dict(
            claim_type="INR",
            amount=100.0,
            days_until_deadline=5,
            p_win=0.60,
        )
        defaults.update(kw)
        return DisputeForPriority(dispute_id=dispute_id, **defaults)

    def _inp(self, disputes=None, **kw) -> BatchPrioritizerInput:
        if disputes is None:
            disputes = [self._dispute()]
        return BatchPrioritizerInput(
            merchant_id="M-001",
            disputes=disputes,
            fight_cost_per_dispute=10.0,
            chargeback_fee=20.0,
            **kw,
        )

    # ── Basic functionality ───────────────────────────────────────

    def test_single_dispute_returned(self):
        r = prioritize_disputes(self._inp())
        assert len(r.prioritized) == 1

    def test_empty_batch_ok(self):
        r = prioritize_disputes(self._inp(disputes=[]))
        assert r.total_disputes == 0

    def test_sorted_highest_first(self):
        disputes = [
            self._dispute("D-low",  days_until_deadline=20, amount=10.0),
            self._dispute("D-high", days_until_deadline=1,  amount=500.0),
        ]
        r = prioritize_disputes(self._inp(disputes=disputes))
        assert r.prioritized[0].dispute_id == "D-high"

    # ── Priority scoring ──────────────────────────────────────────

    def test_overdue_has_higher_score_than_future(self):
        overdue = prioritize_disputes(self._inp(disputes=[
            self._dispute(days_until_deadline=0)]))
        future = prioritize_disputes(self._inp(disputes=[
            self._dispute(days_until_deadline=30)]))
        assert overdue.prioritized[0].priority_score > future.prioritized[0].priority_score

    def test_large_amount_higher_score(self):
        small = prioritize_disputes(self._inp(disputes=[
            self._dispute(amount=10.0)]))
        large = prioritize_disputes(self._inp(disputes=[
            self._dispute(amount=1000.0)]))
        assert large.prioritized[0].priority_score > small.prioritized[0].priority_score

    def test_hdr_active_boosts_score(self):
        no_hdr  = prioritize_disputes(self._inp(disputes=[
            self._dispute(is_hdr_active=False)]))
        with_hdr = prioritize_disputes(self._inp(disputes=[
            self._dispute(is_hdr_active=True)]))
        assert with_hdr.prioritized[0].priority_score > no_hdr.prioritized[0].priority_score

    def test_sp_boosts_score(self):
        no_sp  = prioritize_disputes(self._inp(disputes=[
            self._dispute(sp_eligible=False)]))
        with_sp = prioritize_disputes(self._inp(disputes=[
            self._dispute(sp_eligible=True)]))
        assert with_sp.prioritized[0].priority_score > no_sp.prioritized[0].priority_score

    # ── Priority bands ────────────────────────────────────────────

    def test_overdue_critical_dispute_in_p1(self):
        r = prioritize_disputes(self._inp(disputes=[
            self._dispute(
                days_until_deadline=0,
                amount=500.0,
                p_win=0.80,
                is_hdr_active=True,
                sp_eligible=True,
            )
        ]))
        assert r.prioritized[0].priority_band == PriorityBand.P1_CRITICAL

    def test_low_urgency_in_p4(self):
        r = prioritize_disputes(self._inp(disputes=[
            self._dispute(
                days_until_deadline=60,
                amount=5.0,
                p_win=0.30,
            )
        ]))
        assert r.prioritized[0].priority_band == PriorityBand.P4_LOW

    # ── Band counts ───────────────────────────────────────────────

    def test_band_counts_sum_to_total(self):
        disputes = [self._dispute(f"D-{i}", days_until_deadline=i*5)
                    for i in range(5)]
        r = prioritize_disputes(self._inp(disputes=disputes))
        assert r.p1_count + r.p2_count + r.p3_count + r.p4_count == r.total_disputes

    # ── EV calculations ───────────────────────────────────────────

    def test_ev_positive_for_high_pwin(self):
        r = prioritize_disputes(self._inp(disputes=[
            self._dispute(amount=200.0, p_win=0.90)]))
        assert r.prioritized[0].ev_estimate > 0

    def test_ev_negative_for_low_pwin_small_amount(self):
        r = prioritize_disputes(self._inp(disputes=[
            self._dispute(amount=10.0, p_win=0.10)]))
        assert r.prioritized[0].ev_estimate < 0

    def test_total_ev_sums_correctly(self):
        disputes = [self._dispute(f"D-{i}") for i in range(3)]
        r = prioritize_disputes(self._inp(disputes=disputes))
        expected = sum(p.ev_estimate for p in r.prioritized)
        assert abs(r.total_ev_if_all_fought - expected) < 0.01

    # ── Summary fields ────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = prioritize_disputes(self._inp())
        assert "M-001" in r.summary

    def test_top_actions_non_empty(self):
        r = prioritize_disputes(self._inp())
        assert len(r.top_actions) >= 1

    def test_total_amount_sums_all_disputes(self):
        disputes = [
            self._dispute("D-1", amount=100.0),
            self._dispute("D-2", amount=200.0),
        ]
        r = prioritize_disputes(self._inp(disputes=disputes))
        assert r.total_amount_at_risk == pytest.approx(300.0)
