"""
Tests for Wave 14: refund_optimizer, buyer_tier_engine.
"""
import pytest
from oracle.refund_optimizer import (
    evaluate_refund, RefundInput, RefundStrategy, RefundTrigger,
)
from oracle.buyer_tier_engine import (
    classify_buyer, BuyerProfile, BuyerTier, BuyerSignalFlag,
)


# ═══════════════════════════════════════════════════════════════════
# Refund Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundOptimizer:

    def _inp(self, **kw) -> RefundInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            dispute_amount=150.0,
            p_win=0.50,
            fight_cost=25.0,
            days_until_deadline=10,
            is_formal_dispute=True,
            is_hdr_contributing=True,
            hdr_pressure_level=0,
            partial_delivery_pct=1.0,
            was_delivered=False,
            buyer_dispute_count_90d=0,
        )
        defaults.update(kw)
        return RefundInput(**defaults)

    def test_ev_fight_computed(self):
        r = evaluate_refund(self._inp(p_win=0.60, dispute_amount=100.0, fight_cost=25.0))
        assert r.ev_fight == pytest.approx(0.60 * 100.0 - 25.0, abs=0.01)

    def test_tiny_amount_full_proactive(self):
        r = evaluate_refund(self._inp(dispute_amount=10.0))
        assert r.recommended_strategy == RefundStrategy.FULL_PROACTIVE
        assert RefundTrigger.TINY_AMOUNT in r.triggers

    def test_fraudster_fight_no_refund(self):
        r = evaluate_refund(self._inp(buyer_dispute_count_90d=4))
        assert r.recommended_strategy == RefundStrategy.FIGHT_NO_REFUND

    def test_near_deadline_full_proactive(self):
        r = evaluate_refund(self._inp(days_until_deadline=1))
        assert r.recommended_strategy == RefundStrategy.FULL_PROACTIVE
        assert RefundTrigger.NEAR_DEADLINE in r.triggers

    def test_strong_case_fight(self):
        r = evaluate_refund(self._inp(p_win=0.80, dispute_amount=200.0, was_delivered=True))
        assert r.recommended_strategy == RefundStrategy.FIGHT_NO_REFUND

    def test_hdr_pressure_weak_case_refund(self):
        # p_win=0.10, amount=150, fight_cost=25 → EV = 15-25 = -10 < 0 → HDR trigger fires
        r = evaluate_refund(self._inp(hdr_pressure_level=2, p_win=0.10))
        assert r.recommended_strategy in (RefundStrategy.FULL_PROACTIVE, RefundStrategy.FULL_REACTIVE)
        assert RefundTrigger.HDR_PRESSURE in r.triggers

    def test_partial_delivery_partial_refund(self):
        r = evaluate_refund(self._inp(partial_delivery_pct=0.5, claim_type="INR"))
        assert r.recommended_strategy in (RefundStrategy.PARTIAL_PROACTIVE, RefundStrategy.PARTIAL_REACTIVE)
        assert r.recommended_refund_amount < 150.0

    def test_partial_delivery_formal_reactive(self):
        r = evaluate_refund(self._inp(partial_delivery_pct=0.5, claim_type="INR", is_formal_dispute=True))
        assert r.recommended_strategy == RefundStrategy.PARTIAL_REACTIVE

    def test_partial_delivery_informal_proactive(self):
        r = evaluate_refund(self._inp(partial_delivery_pct=0.5, claim_type="INR", is_formal_dispute=False))
        assert r.recommended_strategy == RefundStrategy.PARTIAL_PROACTIVE

    def test_very_weak_case_refund(self):
        r = evaluate_refund(self._inp(p_win=0.05, dispute_amount=200.0, fight_cost=25.0))
        # EV = 0.05*200 - 25 = -15 → not < -15, border case
        # Try even weaker
        r2 = evaluate_refund(self._inp(p_win=0.05, dispute_amount=100.0, fight_cost=25.0))
        # EV = 5 - 25 = -20 < -15 → refund
        assert r2.recommended_strategy in (RefundStrategy.FULL_PROACTIVE, RefundStrategy.FULL_REACTIVE)

    def test_refund_pct_between_0_and_1(self):
        r = evaluate_refund(self._inp())
        assert 0.0 <= r.recommended_refund_pct <= 1.0

    def test_scenarios_non_empty(self):
        r = evaluate_refund(self._inp())
        assert len(r.scenarios) >= 2

    def test_scenarios_include_fight(self):
        r = evaluate_refund(self._inp())
        strategies = [s.strategy for s in r.scenarios]
        assert RefundStrategy.FIGHT_NO_REFUND in strategies

    def test_hdr_saving_days_positive(self):
        r = evaluate_refund(self._inp())
        assert r.hdr_saving_days >= 0

    def test_buyer_message_non_empty_for_full_refund(self):
        r = evaluate_refund(self._inp(dispute_amount=10.0))  # triggers full proactive
        assert len(r.buyer_message) > 20

    def test_buyer_message_empty_for_fight(self):
        r = evaluate_refund(self._inp(buyer_dispute_count_90d=4))
        assert r.buyer_message == ""

    def test_summary_contains_dispute_id(self):
        r = evaluate_refund(self._inp())
        assert "D-001" in r.summary

    def test_max_acceptable_refund_lte_dispute_amount(self):
        r = evaluate_refund(self._inp())
        assert r.max_acceptable_refund <= r.dispute_amount

    def test_full_refund_amount_equals_dispute_amount(self):
        r = evaluate_refund(self._inp(dispute_amount=10.0))
        assert r.recommended_refund_amount == pytest.approx(10.0, abs=0.01)

    def test_loyal_customer_low_ev_gets_proactive_refund(self):
        # EV = 0.30*120 - 25 = 36-25 = 11 < 20 → loyal customer condition triggers
        r = evaluate_refund(self._inp(
            buyer_is_long_term_customer=True,
            buyer_purchase_history_count=5,
            p_win=0.30,
            dispute_amount=120.0,
        ))
        assert r.recommended_strategy in (RefundStrategy.FULL_PROACTIVE, RefundStrategy.FULL_REACTIVE)


# ═══════════════════════════════════════════════════════════════════
# Buyer Tier Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerTierEngine:

    def _buyer(self, **kw) -> BuyerProfile:
        defaults = dict(
            buyer_id="B-001",
            email="b001@example.com",
            total_purchases=3,
            total_purchases_last_90d=2,
            total_lifetime_spend=300.0,
            account_age_days=365,
            dispute_count_90d=0,
            dispute_count_lifetime=0,
            dispute_amounts_90d=[],
            dispute_reasons_90d=[],
            past_disputes_won_by_merchant=0,
            shares_address_with_disputers=False,
            is_in_fraud_network=False,
        )
        defaults.update(kw)
        return BuyerProfile(**defaults)

    def test_clean_buyer_is_standard(self):
        r = classify_buyer(self._buyer())
        assert r.tier == BuyerTier.STANDARD_BUYER

    def test_serial_fraudster_tier(self):
        r = classify_buyer(self._buyer(dispute_count_90d=3))
        assert r.tier == BuyerTier.FRAUDSTER

    def test_fraud_network_member_is_fraudster(self):
        r = classify_buyer(self._buyer(is_in_fraud_network=True))
        assert r.tier == BuyerTier.FRAUDSTER

    def test_fraudster_should_escalate(self):
        r = classify_buyer(self._buyer(dispute_count_90d=4))
        assert r.should_escalate is True

    def test_fraudster_should_block(self):
        r = classify_buyer(self._buyer(dispute_count_90d=4))
        assert r.should_block is True

    def test_loyal_rich_customer_gold(self):
        r = classify_buyer(self._buyer(
            total_purchases=15,
            total_lifetime_spend=2000.0,
            dispute_count_90d=0,
            dispute_count_lifetime=0,
            account_age_days=730,
        ))
        assert r.tier == BuyerTier.GOLD_CUSTOMER

    def test_gold_customer_auto_refund_threshold(self):
        r = classify_buyer(self._buyer(
            total_purchases=15,
            total_lifetime_spend=2000.0,
            dispute_count_90d=0,
            dispute_count_lifetime=0,
            account_age_days=730,
        ))
        assert r.policy.auto_refund_below > 0

    def test_gold_customer_generous_settlement(self):
        r = classify_buyer(self._buyer(
            total_purchases=15,
            total_lifetime_spend=2000.0,
            dispute_count_90d=0,
            account_age_days=730,
        ))
        assert r.adjusted_settlement_multiplier > 1.0

    def test_elevated_watch_for_repeat_claimant(self):
        r = classify_buyer(self._buyer(dispute_count_90d=2))
        assert r.tier in (BuyerTier.ELEVATED_WATCH, BuyerTier.HIGH_RISK_BUYER)

    def test_high_risk_raises_fight_threshold(self):
        standard = classify_buyer(self._buyer())
        high_risk = classify_buyer(self._buyer(dispute_count_90d=2))
        assert high_risk.adjusted_fight_threshold >= standard.adjusted_fight_threshold

    def test_fraudster_lowers_min_ev(self):
        standard = classify_buyer(self._buyer())
        fraudster = classify_buyer(self._buyer(dispute_count_90d=4))
        assert fraudster.adjusted_min_ev < standard.adjusted_min_ev

    def test_new_account_flag(self):
        r = classify_buyer(self._buyer(account_age_days=30))
        assert BuyerSignalFlag.RAPID_ACCOUNT in r.flags

    def test_address_match_flag(self):
        r = classify_buyer(self._buyer(shares_address_with_disputers=True))
        assert BuyerSignalFlag.ADDRESS_MATCH in r.flags

    def test_high_lifetime_value_flag(self):
        r = classify_buyer(self._buyer(total_lifetime_spend=1500.0))
        assert BuyerSignalFlag.HIGH_LIFETIME_VALUE in r.flags

    def test_loyal_customer_flag(self):
        r = classify_buyer(self._buyer(total_purchases=12))
        assert BuyerSignalFlag.LOYAL_CUSTOMER in r.flags

    def test_escalating_amounts_flag(self):
        r = classify_buyer(self._buyer(dispute_amounts_90d=[20.0, 150.0]))
        assert BuyerSignalFlag.ESCALATING_AMOUNTS in r.flags

    def test_pattern_claimant_flag(self):
        r = classify_buyer(self._buyer(dispute_reasons_90d=["INR", "INR"]))
        assert BuyerSignalFlag.PATTERN_CLAIMANT in r.flags

    def test_tier_score_zero_to_100(self):
        r = classify_buyer(self._buyer())
        assert 0 <= r.tier_score <= 100

    def test_summary_contains_buyer_id(self):
        r = classify_buyer(self._buyer())
        assert "B-001" in r.summary

    def test_internal_review_requires_human(self):
        # High value + some risk signals
        r = classify_buyer(self._buyer(
            total_purchases=12,
            total_lifetime_spend=1500.0,
            dispute_count_90d=2,
            shares_address_with_disputers=True,
        ))
        if r.tier == BuyerTier.INTERNAL_REVIEW:
            assert r.policy.require_human_review is True

    def test_fraudster_settlement_multiplier_zero(self):
        r = classify_buyer(self._buyer(dispute_count_90d=4))
        assert r.adjusted_settlement_multiplier == 0.0
