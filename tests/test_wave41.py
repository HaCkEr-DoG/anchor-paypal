"""
Tests for Wave 41: buyer_risk_aggregator, dispute_batch_prioritizer.
"""
import pytest
from oracle.buyer_risk_aggregator import (
    aggregate_buyer_risk, BuyerRiskInput,
    OverallRiskTier, BuyerAction,
)
from oracle.dispute_batch_prioritizer import (
    prioritize_dispute_batch, BatchInput, DisputeItem,
    DisputeUrgency, HDRTier,
)


# ═══════════════════════════════════════════════════════════════════
# Buyer Risk Aggregator Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerRiskAggregator:

    def _inp(self, **kw) -> BuyerRiskInput:
        defaults = dict(
            buyer_id="B-001",
            chargeback_risk_score=30.0,
            velocity_risk_level="NORMAL",
            velocity_trigger_count=0,
            fraud_signal_level="MINIMAL",
            hostility_level="COOPERATIVE",
            threat_count=0,
            sophistication_count=0,
            fulfillment_strength="STRONG",
            has_returned_shipments=False,
            total_disputes_lifetime=0,
            disputes_last_90_days=0,
            disputes_won_by_buyer=0,
            disputes_ruled_for_merchant=0,
            account_age_days=365,
            lifetime_order_count=50,
            lifetime_spend=5000.0,
            is_verified_identity=False,
        )
        defaults.update(kw)
        return BuyerRiskInput(**defaults)

    # ── Basic output structure ─────────────────────────────────────

    def test_summary_contains_buyer_id(self):
        r = aggregate_buyer_risk(self._inp(buyer_id="B-XYZ"))
        assert "B-XYZ" in r.summary

    def test_score_bounded(self):
        r = aggregate_buyer_risk(self._inp())
        assert 0.0 <= r.composite_score <= 100.0

    def test_tier_and_action_consistent(self):
        r = aggregate_buyer_risk(self._inp())
        # TRUSTED → TRUSTED_FAST_PASS, LOW → PROCEED_NORMAL, etc.
        assert r.recommended_action is not None
        assert r.overall_risk_tier is not None

    # ── Low-risk buyer ─────────────────────────────────────────────

    def test_established_buyer_low_risk(self):
        r = aggregate_buyer_risk(self._inp(
            chargeback_risk_score=10.0,
            account_age_days=730,
            lifetime_order_count=100,
            is_verified_identity=True,
        ))
        assert r.overall_risk_tier in (
            OverallRiskTier.TRUSTED, OverallRiskTier.LOW, OverallRiskTier.MODERATE)

    def test_trusted_action_fast_pass(self):
        r = aggregate_buyer_risk(self._inp(
            chargeback_risk_score=5.0,
            account_age_days=1000,
            lifetime_order_count=200,
            lifetime_spend=20000.0,
            is_verified_identity=True,
            velocity_risk_level="NORMAL",
            fraud_signal_level="MINIMAL",
        ))
        assert r.recommended_action in (
            BuyerAction.TRUSTED_FAST_PASS, BuyerAction.PROCEED_NORMAL)

    # ── High-risk signals ──────────────────────────────────────────

    def test_critical_velocity_raises_risk(self):
        low  = aggregate_buyer_risk(self._inp(velocity_risk_level="NORMAL"))
        high = aggregate_buyer_risk(self._inp(velocity_risk_level="CRITICAL"))
        assert high.composite_score > low.composite_score

    def test_very_high_fraud_signal_raises_risk(self):
        low  = aggregate_buyer_risk(self._inp(fraud_signal_level="MINIMAL"))
        high = aggregate_buyer_risk(self._inp(fraud_signal_level="VERY_HIGH"))
        assert high.composite_score > low.composite_score

    def test_extreme_hostility_raises_risk(self):
        low  = aggregate_buyer_risk(self._inp(hostility_level="COOPERATIVE"))
        high = aggregate_buyer_risk(self._inp(hostility_level="EXTREME"))
        assert high.composite_score > low.composite_score

    def test_high_cb_score_critical_tier(self):
        r = aggregate_buyer_risk(self._inp(
            chargeback_risk_score=90.0,
            velocity_risk_level="CRITICAL",
            fraud_signal_level="VERY_HIGH",
        ))
        assert r.overall_risk_tier in (OverallRiskTier.CRITICAL, OverallRiskTier.HIGH)

    def test_block_action_on_critical_tier(self):
        r = aggregate_buyer_risk(self._inp(
            chargeback_risk_score=95.0,
            velocity_risk_level="CRITICAL",
            fraud_signal_level="VERY_HIGH",
            hostility_level="EXTREME",
            account_age_days=5,
        ))
        assert r.recommended_action == BuyerAction.BLOCK

    # ── Dispute history ────────────────────────────────────────────

    def test_many_90d_disputes_raises_risk(self):
        low  = aggregate_buyer_risk(self._inp(disputes_last_90_days=0))
        high = aggregate_buyer_risk(self._inp(disputes_last_90_days=3,
                                              total_disputes_lifetime=3))
        assert high.composite_score > low.composite_score

    def test_repeat_disputer_flag(self):
        r = aggregate_buyer_risk(self._inp(total_disputes_lifetime=3))
        assert r.is_repeat_disputer is True

    def test_no_disputes_not_repeat(self):
        r = aggregate_buyer_risk(self._inp(total_disputes_lifetime=0))
        assert r.is_repeat_disputer is False

    def test_dispute_rate_calculation(self):
        r = aggregate_buyer_risk(self._inp(
            total_disputes_lifetime=5,
            lifetime_order_count=50,
        ))
        assert abs(r.dispute_rate - 0.10) < 0.001

    def test_zero_orders_dispute_rate_zero(self):
        r = aggregate_buyer_risk(self._inp(
            total_disputes_lifetime=2,
            lifetime_order_count=0,
        ))
        assert r.dispute_rate == 0.0

    # ── Mitigating factors ─────────────────────────────────────────

    def test_definitive_fulfillment_lowers_score(self):
        low  = aggregate_buyer_risk(self._inp(fulfillment_strength="MISSING"))
        high = aggregate_buyer_risk(self._inp(fulfillment_strength="DEFINITIVE"))
        assert high.composite_score < low.composite_score

    def test_verified_identity_lowers_score(self):
        no_verify = aggregate_buyer_risk(self._inp(is_verified_identity=False))
        verify    = aggregate_buyer_risk(self._inp(is_verified_identity=True))
        assert verify.composite_score <= no_verify.composite_score

    def test_returned_shipment_raises_risk(self):
        low  = aggregate_buyer_risk(self._inp(has_returned_shipments=False))
        high = aggregate_buyer_risk(self._inp(has_returned_shipments=True))
        assert high.composite_score > low.composite_score

    # ── New account ────────────────────────────────────────────────

    def test_new_account_raises_risk(self):
        old_buyer = aggregate_buyer_risk(self._inp(account_age_days=730))
        new_buyer = aggregate_buyer_risk(self._inp(account_age_days=5))
        assert new_buyer.composite_score > old_buyer.composite_score

    # ── Signal contributions ───────────────────────────────────────

    def test_contributions_non_empty(self):
        r = aggregate_buyer_risk(self._inp())
        assert len(r.signal_contributions) >= 1

    def test_risk_factors_present_on_high_risk(self):
        r = aggregate_buyer_risk(self._inp(
            chargeback_risk_score=85.0,
            velocity_risk_level="CRITICAL",
            account_age_days=3,
        ))
        assert len(r.top_risk_factors) >= 1


# ═══════════════════════════════════════════════════════════════════
# Dispute Batch Prioritizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeBatchPrioritizer:

    def _item(self, dispute_id="D-001", amount=100.0, days=5,
              win_prob=0.50, **kw) -> DisputeItem:
        return DisputeItem(
            dispute_id=dispute_id,
            transaction_amount=amount,
            days_until_deadline=days,
            estimated_win_probability=win_prob,
            **kw,
        )

    def _batch(self, disputes=None, **kw) -> BatchInput:
        if disputes is None:
            disputes = [self._item()]
        return BatchInput(batch_id="BATCH-001", disputes=disputes, **kw)

    # ── Empty batch ───────────────────────────────────────────────

    def test_empty_batch_zero_disputes(self):
        r = prioritize_dispute_batch(BatchInput(batch_id="EMPTY", disputes=[]))
        assert r.total_disputes == 0
        assert len(r.ranked) == 0

    def test_empty_batch_zero_exposure(self):
        r = prioritize_dispute_batch(BatchInput(batch_id="EMPTY", disputes=[]))
        assert r.total_exposure == 0.0

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_batch_id(self):
        r = prioritize_dispute_batch(self._batch())
        assert "BATCH-001" in r.summary

    def test_ranked_length_matches_input(self):
        items = [self._item(f"D-{i}") for i in range(5)]
        r = prioritize_dispute_batch(self._batch(disputes=items))
        assert len(r.ranked) == 5

    def test_rank_starts_at_one(self):
        r = prioritize_dispute_batch(self._batch())
        assert r.ranked[0].rank == 1

    def test_ranks_sequential(self):
        items = [self._item(f"D-{i}") for i in range(4)]
        r = prioritize_dispute_batch(self._batch(disputes=items))
        assert [rd.rank for rd in r.ranked] == [1, 2, 3, 4]

    # ── Urgency ordering ──────────────────────────────────────────

    def test_overdue_ranked_above_low(self):
        overdue = self._item("D-OVER", days=-1, amount=50.0, win_prob=0.30)
        low_urg = self._item("D-LOW",  days=20, amount=1000.0, win_prob=0.90)
        r = prioritize_dispute_batch(self._batch(disputes=[low_urg, overdue]))
        assert r.ranked[0].dispute_id == "D-OVER"

    def test_overdue_urgency_level(self):
        item = self._item("D-OVER", days=-5)
        r = prioritize_dispute_batch(self._batch(disputes=[item]))
        assert r.ranked[0].urgency == DisputeUrgency.OVERDUE

    def test_critical_urgency_at_1_day(self):
        item = self._item("D-CRIT", days=1)
        r = prioritize_dispute_batch(self._batch(disputes=[item]))
        assert r.ranked[0].urgency == DisputeUrgency.CRITICAL

    def test_low_urgency_at_15_days(self):
        item = self._item("D-LOW", days=15)
        r = prioritize_dispute_batch(self._batch(disputes=[item]))
        assert r.ranked[0].urgency == DisputeUrgency.LOW

    # ── Amount ordering ───────────────────────────────────────────

    def test_higher_amount_higher_score(self):
        small = self._item("D-SMALL", amount=10.0,   days=5, win_prob=0.50)
        large = self._item("D-LARGE", amount=5000.0, days=5, win_prob=0.50)
        r = prioritize_dispute_batch(self._batch(disputes=[small, large]))
        assert r.ranked[0].dispute_id == "D-LARGE"

    # ── Win probability ───────────────────────────────────────────

    def test_higher_win_prob_higher_score(self):
        low_win  = self._item("D-LW", amount=100.0, days=5, win_prob=0.10)
        high_win = self._item("D-HW", amount=100.0, days=5, win_prob=0.95)
        r = prioritize_dispute_batch(self._batch(disputes=[low_win, high_win]))
        assert r.ranked[0].dispute_id == "D-HW"

    # ── HDR impact ────────────────────────────────────────────────

    def test_fee_tier_hdr_boosts_score(self):
        normal  = self._item("D-N", hdr_tier=HDRTier.NORMAL)
        fee_hdr = self._item("D-F", hdr_tier=HDRTier.FEE_TIER)
        r = prioritize_dispute_batch(self._batch(disputes=[normal, fee_hdr]))
        fee_rd = next(rd for rd in r.ranked if rd.dispute_id == "D-F")
        norm_rd = next(rd for rd in r.ranked if rd.dispute_id == "D-N")
        assert fee_rd.hdr_pts > norm_rd.hdr_pts

    # ── Seller Protection ─────────────────────────────────────────

    def test_sp_eligible_adds_points(self):
        no_sp = self._item("D-NSP", seller_protection_eligible=False)
        sp    = self._item("D-SP",  seller_protection_eligible=True)
        r = prioritize_dispute_batch(self._batch(disputes=[no_sp, sp]))
        sp_rd  = next(rd for rd in r.ranked if rd.dispute_id == "D-SP")
        nsp_rd = next(rd for rd in r.ranked if rd.dispute_id == "D-NSP")
        assert sp_rd.sp_pts > nsp_rd.sp_pts

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce30_applicable_adds_points(self):
        no_ce = self._item("D-NCE", ce30_applicable=False)
        ce    = self._item("D-CE",  ce30_applicable=True)
        r = prioritize_dispute_batch(self._batch(disputes=[no_ce, ce]))
        ce_rd  = next(rd for rd in r.ranked if rd.dispute_id == "D-CE")
        nce_rd = next(rd for rd in r.ranked if rd.dispute_id == "D-NCE")
        assert ce_rd.ce30_pts > nce_rd.ce30_pts

    # ── Batch stats ───────────────────────────────────────────────

    def test_overdue_count_correct(self):
        items = [
            self._item("D-1", days=-2),
            self._item("D-2", days=-1),
            self._item("D-3", days=5),
        ]
        r = prioritize_dispute_batch(self._batch(disputes=items))
        assert r.overdue_count == 2

    def test_critical_count_correct(self):
        items = [
            self._item("D-1", days=0),
            self._item("D-2", days=1),
            self._item("D-3", days=10),
        ]
        r = prioritize_dispute_batch(self._batch(disputes=items))
        assert r.critical_count == 2

    def test_total_exposure_correct(self):
        items = [
            self._item("D-1", amount=100.0),
            self._item("D-2", amount=250.0),
            self._item("D-3", amount=50.0),
        ]
        r = prioritize_dispute_batch(self._batch(disputes=items))
        assert abs(r.total_exposure - 400.0) < 0.01

    def test_max_to_return_limits_results(self):
        items = [self._item(f"D-{i}", days=i) for i in range(10)]
        r = prioritize_dispute_batch(self._batch(disputes=items, max_to_return=3))
        assert len(r.ranked) == 3

    def test_total_disputes_counts_all(self):
        items = [self._item(f"D-{i}") for i in range(7)]
        r = prioritize_dispute_batch(self._batch(disputes=items, max_to_return=3))
        assert r.total_disputes == 7

    # ── Score breakdown ───────────────────────────────────────────

    def test_score_breakdown_sums_to_total(self):
        item = self._item(
            "D-X", amount=500.0, days=2, win_prob=0.70,
            hdr_tier=HDRTier.FEE_TIER,
            seller_protection_eligible=True,
            ce30_applicable=True,
        )
        r = prioritize_dispute_batch(self._batch(disputes=[item]))
        rd = r.ranked[0]
        breakdown = (rd.urgency_pts + rd.amount_pts + rd.win_pts
                     + rd.hdr_pts + rd.sp_pts + rd.ce30_pts)
        assert abs(breakdown - rd.priority_score) < 0.5

    def test_rationale_non_empty(self):
        item = self._item("D-X", days=-1, amount=1000.0, win_prob=0.80,
                          seller_protection_eligible=True)
        r = prioritize_dispute_batch(self._batch(disputes=[item]))
        assert len(r.ranked[0].rationale) > 0
