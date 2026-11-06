"""
Tests for Wave 24: refund_policy_engine, fee_impact_calculator.
"""
import pytest
from oracle.refund_policy_engine import (
    evaluate_refund_policy, RefundPolicyInput, RefundMode,
    RefundPolicyType, ClaimBasis, PolicyEligibility,
)
from oracle.fee_impact_calculator import (
    calculate_fee_impact, FeeImpactInput, DisputeOutcome, FeeCategory,
)


# ═══════════════════════════════════════════════════════════════════
# Refund Policy Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundPolicyEngine:

    def _inp(self, **kw) -> RefundPolicyInput:
        defaults = dict(
            dispute_id="D-001",
            claim_basis=ClaimBasis.ITEM_NOT_RECEIVED,
            transaction_amount=100.0,
            days_since_purchase=10,
            policy_type=RefundPolicyType.STANDARD,
            policy_window_days=30,
            fight_cost=25.0,
            p_win_if_fight=0.60,
            hdr_rate_pct=0.30,
        )
        defaults.update(kw)
        return RefundPolicyInput(**defaults)

    # ── Policy eligibility ────────────────────────────────────────

    def test_inr_standard_policy_eligible(self):
        r = evaluate_refund_policy(self._inp())
        assert r.policy_eligibility == PolicyEligibility.ELIGIBLE

    def test_final_sale_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            policy_type=RefundPolicyType.FINAL_SALE,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    def test_changed_mind_outside_window_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            policy_type=RefundPolicyType.STANDARD,
            days_since_purchase=45,
            policy_window_days=30,
            policy_covers_changed_mind=False,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    def test_no_questions_asked_within_window_eligible(self):
        r = evaluate_refund_policy(self._inp(
            policy_type=RefundPolicyType.NO_QUESTIONS_ASKED,
            days_since_purchase=15,
            policy_window_days=30,
        ))
        assert r.policy_eligibility == PolicyEligibility.ELIGIBLE

    def test_no_questions_asked_outside_window_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            policy_type=RefundPolicyType.NO_QUESTIONS_ASKED,
            days_since_purchase=45,
            policy_window_days=30,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    def test_digital_goods_changed_mind_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            item_is_digital=True,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    def test_custom_item_changed_mind_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            item_is_custom=True,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    def test_promo_non_refundable_ineligible(self):
        r = evaluate_refund_policy(self._inp(
            was_promo_purchase=True,
            promo_non_refundable=True,
        ))
        assert r.policy_eligibility == PolicyEligibility.INELIGIBLE

    # ── Refund mode decisions ─────────────────────────────────────

    def test_duplicate_charge_full_refund(self):
        r = evaluate_refund_policy(self._inp(claim_basis=ClaimBasis.DUPLICATE_CHARGE))
        assert r.recommended_mode == RefundMode.FULL_REFUND
        assert r.recommended_amount == pytest.approx(100.0, abs=0.01)

    def test_eligible_large_item_not_returned_conditional(self):
        r = evaluate_refund_policy(self._inp(
            transaction_amount=200.0,
            claim_basis=ClaimBasis.NOT_AS_DESCRIBED,
            item_returned=False,
        ))
        assert r.recommended_mode == RefundMode.CONDITIONAL_REFUND

    def test_eligible_small_item_not_returned_full_refund(self):
        r = evaluate_refund_policy(self._inp(
            transaction_amount=30.0,
            claim_basis=ClaimBasis.NOT_AS_DESCRIBED,
            item_returned=False,
        ))
        assert r.recommended_mode == RefundMode.FULL_REFUND

    def test_exchange_only_policy_gives_exchange(self):
        r = evaluate_refund_policy(self._inp(
            policy_type=RefundPolicyType.EXCHANGE_ONLY,
            claim_basis=ClaimBasis.NOT_AS_DESCRIBED,
        ))
        assert r.recommended_mode == RefundMode.EXCHANGE_ONLY

    def test_ineligible_with_hdr_breach_gives_partial(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            policy_type=RefundPolicyType.FINAL_SALE,
            hdr_would_breach=True,
        ))
        assert r.recommended_mode == RefundMode.PARTIAL_REFUND

    def test_decline_refund_final_sale_no_hdr_pressure(self):
        r = evaluate_refund_policy(self._inp(
            claim_basis=ClaimBasis.CHANGED_MIND,
            policy_type=RefundPolicyType.FINAL_SALE,
            hdr_would_breach=False,
            p_win_if_fight=0.90,  # strong win probability
            buyer_ltv=0.0,
        ))
        assert r.recommended_mode == RefundMode.DECLINE_REFUND

    def test_custom_policy_partial_amount(self):
        r = evaluate_refund_policy(self._inp(
            policy_type=RefundPolicyType.CUSTOM,
            policy_partial_pct=0.60,
            transaction_amount=100.0,
        ))
        assert r.recommended_mode == RefundMode.PARTIAL_REFUND
        assert r.recommended_amount == pytest.approx(60.0, abs=0.01)

    # ── Economics ─────────────────────────────────────────────────

    def test_cost_refund_positive(self):
        r = evaluate_refund_policy(self._inp())
        assert r.cost_refund_now > 0

    def test_returned_item_reduces_refund_cost(self):
        no_return  = evaluate_refund_policy(self._inp(item_returned=False, item_resale_value=40.0))
        with_return = evaluate_refund_policy(self._inp(item_returned=True,  item_resale_value=40.0))
        assert with_return.cost_refund_now < no_return.cost_refund_now

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = evaluate_refund_policy(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_refund_pct_matches_amount(self):
        r = evaluate_refund_policy(self._inp(
            policy_type=RefundPolicyType.CUSTOM,
            policy_partial_pct=0.75,
            transaction_amount=200.0,
        ))
        assert abs(r.refund_pct - 0.75) < 0.001
        assert r.recommended_amount == pytest.approx(150.0, abs=0.01)

    def test_hdr_risk_note_non_empty(self):
        r = evaluate_refund_policy(self._inp())
        assert len(r.hdr_risk_note) > 5


# ═══════════════════════════════════════════════════════════════════
# Fee Impact Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestFeeImpactCalculator:

    def _inp(self, **kw) -> FeeImpactInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=100.0,
            paypal_rate_pct=3.49,
            paypal_fixed_fee=0.49,
            hdr_rate_pct=0.30,
            sp_applicable=False,
            fight_hours=1.0,
            hourly_rate=25.0,
        )
        defaults.update(kw)
        return FeeImpactInput(**defaults)

    # ── Outcome rankings ──────────────────────────────────────────

    def test_lose_costs_more_than_win(self):
        r = calculate_fee_impact(self._inp())
        assert r.outcome_lose.net_loss > r.outcome_win.net_loss

    def test_refund_now_less_costly_than_lose(self):
        r = calculate_fee_impact(self._inp())
        assert r.outcome_refund_now.net_loss < r.outcome_lose.net_loss

    def test_partial_refund_between_win_and_full_refund(self):
        r = calculate_fee_impact(self._inp(partial_refund_pct=0.50))
        assert r.outcome_win.net_loss < r.outcome_partial.net_loss
        assert r.outcome_partial.net_loss < r.outcome_refund_now.net_loss

    # ── HDR surcharge ─────────────────────────────────────────────

    def test_hdr_surcharge_inactive_below_1_5_pct(self):
        r = calculate_fee_impact(self._inp(hdr_rate_pct=1.49))
        assert r.hdr_surcharge_active is False
        assert r.hdr_surcharge_per_dispute == 0.0

    def test_hdr_surcharge_active_at_1_5_pct(self):
        r = calculate_fee_impact(self._inp(hdr_rate_pct=1.50))
        assert r.hdr_surcharge_active is True
        assert r.hdr_surcharge_per_dispute == 30.0

    def test_hdr_surcharge_increases_lose_cost(self):
        no_hdr  = calculate_fee_impact(self._inp(hdr_rate_pct=1.0))
        with_hdr = calculate_fee_impact(self._inp(hdr_rate_pct=2.0))
        assert with_hdr.outcome_lose.net_loss > no_hdr.outcome_lose.net_loss

    # ── Seller Protection waiver ──────────────────────────────────

    def test_sp_waives_chargeback_fee(self):
        no_sp  = calculate_fee_impact(self._inp(sp_applicable=False))
        with_sp = calculate_fee_impact(self._inp(sp_applicable=True))
        # With SP, chargeback fee is waived → lower loss on LOSE outcome
        assert with_sp.outcome_lose.net_loss < no_sp.outcome_lose.net_loss

    def test_sp_chargeback_fee_marked_waived(self):
        r = calculate_fee_impact(self._inp(sp_applicable=True))
        lose_items = r.outcome_lose.fee_items
        cb_item = next((f for f in lose_items if f.category == FeeCategory.CHARGEBACK_FEE), None)
        assert cb_item is not None
        assert cb_item.is_waived is True

    # ── Breakeven ─────────────────────────────────────────────────

    def test_breakeven_p_win_in_range(self):
        r = calculate_fee_impact(self._inp())
        assert 0.0 <= r.breakeven_p_win <= 1.0

    def test_higher_transaction_raises_breakeven(self):
        small = calculate_fee_impact(self._inp(transaction_amount=50.0))
        large = calculate_fee_impact(self._inp(transaction_amount=500.0))
        # Larger amount → more to lose → fighting is more attractive → lower breakeven
        assert large.breakeven_p_win <= small.breakeven_p_win

    def test_fee_delta_positive(self):
        r = calculate_fee_impact(self._inp())
        assert r.fee_delta_win_vs_lose > 0

    # ── Report fields ─────────────────────────────────────────────

    def test_all_outcomes_present(self):
        r = calculate_fee_impact(self._inp())
        assert r.outcome_win.outcome == DisputeOutcome.WIN
        assert r.outcome_lose.outcome == DisputeOutcome.LOSE
        assert r.outcome_refund_now.outcome == DisputeOutcome.REFUND_NOW
        assert r.outcome_partial.outcome == DisputeOutcome.PARTIAL

    def test_summary_contains_dispute_id(self):
        r = calculate_fee_impact(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_win_net_loss_includes_labor(self):
        r = calculate_fee_impact(self._inp(fight_hours=2.0, hourly_rate=30.0))
        # Win still costs labor (60) + transaction fee
        labor_items = [f for f in r.outcome_win.fee_items
                       if f.category == FeeCategory.DISPUTE_LABOR]
        assert len(labor_items) == 1
        assert labor_items[0].amount == pytest.approx(60.0, abs=0.01)

    def test_refund_now_no_labor_cost(self):
        r = calculate_fee_impact(self._inp())
        labor_items = [f for f in r.outcome_refund_now.fee_items
                       if f.category == FeeCategory.DISPUTE_LABOR]
        assert len(labor_items) == 0

    def test_effective_loss_pct_between_0_and_2(self):
        r = calculate_fee_impact(self._inp())
        for outcome in (r.outcome_win, r.outcome_lose, r.outcome_refund_now, r.outcome_partial):
            assert 0 <= outcome.effective_loss_pct <= 2.0
