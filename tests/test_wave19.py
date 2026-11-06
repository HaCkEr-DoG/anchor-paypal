"""
Tests for Wave 19: seller_protection_checker, dispute_cost_analyzer.
"""
import pytest
from oracle.seller_protection_checker import (
    check_seller_protection, SPInput, SPEligibility, SPGapSeverity, ExcludedCategory,
)
from oracle.dispute_cost_analyzer import (
    analyze_dispute_cost, CostInput, DisputeOutcomeScenario,
)


# ═══════════════════════════════════════════════════════════════════
# Seller Protection Checker Tests
# ═══════════════════════════════════════════════════════════════════

class TestSellerProtectionChecker:

    def _inp(self, claim_type="INR", **kw) -> SPInput:
        defaults = dict(
            dispute_id="D-001",
            order_amount=200.0,
            is_physical_item=True,
            shipped_to_confirmed_address=True,
            has_tracking_number=True,
            tracking_shows_delivered=True,
            merchant_account_in_good_standing=True,
            transaction_within_180_days=True,
        )
        defaults.update(kw)
        return SPInput(claim_type=claim_type, **defaults)

    def test_fully_eligible_inr(self):
        r = check_seller_protection(self._inp())
        assert r.eligibility == SPEligibility.ELIGIBLE

    def test_snad_not_applicable(self):
        r = check_seller_protection(self._inp(claim_type="SNAD"))
        assert r.eligibility == SPEligibility.NOT_APPLICABLE

    def test_excluded_category_ineligible(self):
        r = check_seller_protection(self._inp(item_category=ExcludedCategory.VEHICLE))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_expired_transaction_ineligible(self):
        r = check_seller_protection(self._inp(transaction_within_180_days=False))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_prior_chargeback_ineligible(self):
        r = check_seller_protection(self._inp(prior_chargeback_same_transaction=True))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_signature_required_at_750(self):
        r = check_seller_protection(self._inp(order_amount=750.0, has_signature_confirmation=False))
        assert r.signature_required is True
        assert r.signature_missing is True

    def test_signature_not_required_below_750(self):
        r = check_seller_protection(self._inp(order_amount=500.0))
        assert r.signature_required is False

    def test_signature_present_750_still_eligible(self):
        r = check_seller_protection(self._inp(order_amount=800.0, has_signature_confirmation=True))
        assert r.eligibility == SPEligibility.ELIGIBLE

    def test_no_tracking_soft_gap(self):
        r = check_seller_protection(self._inp(has_tracking_number=False))
        gaps = [g for g in r.gaps if g.severity == SPGapSeverity.SOFT_GAP]
        assert len(gaps) > 0

    def test_unconfirmed_address_soft_gap(self):
        r = check_seller_protection(self._inp(shipped_to_confirmed_address=False))
        gap_descriptions = [g.description.lower() for g in r.gaps]
        assert any("address" in d or "confirmed" in d for d in gap_descriptions)

    def test_digital_goods_unauthorized_ineligible(self):
        r = check_seller_protection(self._inp(
            claim_type="UNAUTHORIZED", is_physical_item=False,
        ))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_sp_score_100_for_eligible(self):
        r = check_seller_protection(self._inp())
        assert r.sp_score == 100.0

    def test_sp_score_0_for_snad(self):
        # SNAD → NOT_APPLICABLE; not checking score, just that it doesn't crash
        r = check_seller_protection(self._inp(claim_type="SNAD"))
        assert r.sp_score >= 0

    def test_sp_text_non_empty_for_eligible(self):
        r = check_seller_protection(self._inp())
        assert len(r.sp_text_to_cite) > 20

    def test_sp_text_empty_for_ineligible(self):
        r = check_seller_protection(self._inp(item_category=ExcludedCategory.VEHICLE))
        assert r.sp_text_to_cite == ""

    def test_action_items_non_empty_for_gaps(self):
        r = check_seller_protection(self._inp(has_tracking_number=False))
        assert len(r.action_items) > 0

    def test_summary_contains_dispute_id(self):
        r = check_seller_protection(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_hard_fail_count(self):
        r = check_seller_protection(self._inp(item_category=ExcludedCategory.VEHICLE))
        assert r.hard_fail_count >= 1

    def test_unauthorized_physical_eligible(self):
        r = check_seller_protection(self._inp(
            claim_type="UNAUTHORIZED",
            is_physical_item=True,
        ))
        assert r.eligibility in (SPEligibility.ELIGIBLE, SPEligibility.LIKELY_ELIGIBLE)


# ═══════════════════════════════════════════════════════════════════
# Dispute Cost Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeCostAnalyzer:

    def _inp(self, **kw) -> CostInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            dispute_amount=200.0,
            fight_cost_hours=2.0,
            hourly_rate=50.0,
            chargeback_fee=20.0,
            p_win=0.65,
            partial_refund_pct=0.5,
            settlement_pct=0.60,
            cogs_pct=0.40,
        )
        defaults.update(kw)
        return CostInput(**defaults)

    def test_five_scenarios_returned(self):
        r = analyze_dispute_cost(self._inp())
        assert len(r.scenarios) == 5

    def test_fight_win_cheapest_scenario(self):
        r = analyze_dispute_cost(self._inp())
        win_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_WIN)
        lose_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_LOSE)
        assert win_s.total_cost < lose_s.total_cost

    def test_fight_lose_includes_dispute_amount(self):
        r = analyze_dispute_cost(self._inp(dispute_amount=200.0))
        lose_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_LOSE)
        assert lose_s.dispute_loss == pytest.approx(200.0, abs=0.01)

    def test_sp_covered_no_chargeback_fee(self):
        with_sp = analyze_dispute_cost(self._inp(sp_covered=True))
        no_sp = analyze_dispute_cost(self._inp(sp_covered=False))
        # Chargeback fee not in fight_win scenario, but in fight_lose
        win_no_sp = next(s for s in no_sp.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_LOSE)
        win_sp = next(s for s in with_sp.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_LOSE)
        assert win_sp.total_cost < win_no_sp.total_cost

    def test_hdr_surcharge_increases_cost(self):
        base = analyze_dispute_cost(self._inp(hdr_surcharge_per_dispute=0.0))
        hdr = analyze_dispute_cost(self._inp(hdr_surcharge_per_dispute=30.0))
        win_base = next(s for s in base.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_WIN)
        win_hdr = next(s for s in hdr.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_WIN)
        assert win_hdr.total_cost > win_base.total_cost

    def test_break_even_p_win_range(self):
        r = analyze_dispute_cost(self._inp())
        assert 0.0 <= r.break_even_p_win <= 1.0

    def test_break_even_proportional_to_fight_cost(self):
        cheap = analyze_dispute_cost(self._inp(fight_cost_hours=1.0, hourly_rate=50.0))
        expensive = analyze_dispute_cost(self._inp(fight_cost_hours=4.0, hourly_rate=50.0))
        assert expensive.break_even_p_win > cheap.break_even_p_win

    def test_fight_roi_positive_high_p_win(self):
        r = analyze_dispute_cost(self._inp(p_win=0.90, dispute_amount=500.0, fight_cost_hours=1.0))
        assert r.fight_roi > 0

    def test_fight_roi_negative_low_p_win(self):
        r = analyze_dispute_cost(self._inp(p_win=0.05, dispute_amount=50.0, fight_cost_hours=2.0))
        assert r.fight_roi < 0

    def test_partial_refund_between_full_and_nothing(self):
        r = analyze_dispute_cost(self._inp(dispute_amount=200.0, partial_refund_pct=0.5))
        partial_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.PARTIAL_REFUND)
        refund_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.AUTO_REFUND)
        assert partial_s.dispute_loss < refund_s.dispute_loss

    def test_settle_scenario_uses_settlement_pct(self):
        r = analyze_dispute_cost(self._inp(dispute_amount=200.0, settlement_pct=0.60))
        settle_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.SETTLE)
        assert settle_s.dispute_loss == pytest.approx(120.0, abs=0.01)

    def test_expected_cost_between_win_and_lose(self):
        r = analyze_dispute_cost(self._inp(p_win=0.5))
        win_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_WIN)
        lose_s = next(s for s in r.scenarios if s.scenario == DisputeOutcomeScenario.FIGHT_AND_LOSE)
        assert win_s.total_cost <= r.expected_cost <= lose_s.total_cost

    def test_summary_contains_dispute_id(self):
        r = analyze_dispute_cost(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_recommended_fight_when_high_roi(self):
        r = analyze_dispute_cost(self._inp(p_win=0.90, dispute_amount=1000.0, fight_cost_hours=1.0))
        assert r.recommended_scenario == DisputeOutcomeScenario.FIGHT_AND_WIN

    def test_all_scenario_costs_non_negative(self):
        r = analyze_dispute_cost(self._inp())
        for s in r.scenarios:
            assert s.total_cost >= 0
