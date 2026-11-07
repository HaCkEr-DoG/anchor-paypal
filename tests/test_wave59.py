"""
Tests for Wave 59: chargeback_prevention_advisor, buyer_ltv_decision_engine.
"""
import pytest
from oracle.chargeback_prevention_advisor import (
    advise_chargeback_prevention, PreventionInput,
    PreventionUrgency, PreventionAction, BuyerTier,
)
from oracle.buyer_ltv_decision_engine import (
    calculate_ltv_decision, LTVDecisionInput,
    LTVTier, LTVAction,
)


# ═══════════════════════════════════════════════════════════════════
# Chargeback Prevention Advisor Tests
# ═══════════════════════════════════════════════════════════════════

def _prev(transaction_id="T-001", merchant_id="M-001", reason_code="INR",
          amount=150.0, hours_since_purchase=18.0, **kw) -> PreventionInput:
    defaults = dict(
        transaction_id=transaction_id, merchant_id=merchant_id,
        reason_code=reason_code, amount=amount,
        hours_since_purchase=hours_since_purchase,
    )
    defaults.update(kw)
    return PreventionInput(**defaults)


class TestChargebackPreventionAdvisor:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_transaction_id(self):
        r = advise_chargeback_prevention(_prev(transaction_id="T-XYZ"))
        assert "T-XYZ" in r.summary

    def test_prevention_probability_bounded(self):
        r = advise_chargeback_prevention(_prev())
        assert 0.0 <= r.prevention_probability <= 1.0

    def test_has_at_least_one_step(self):
        r = advise_chargeback_prevention(_prev())
        assert len(r.recommended_steps) >= 1

    def test_primary_action_matches_first_step(self):
        r = advise_chargeback_prevention(_prev())
        assert r.primary_action == r.recommended_steps[0].action

    # ── Urgency levels ────────────────────────────────────────────

    def test_immediate_urgency_on_very_early_inquiry(self):
        r = advise_chargeback_prevention(_prev(
            hours_since_purchase=4.0, is_inquiry_stage=True,
        ))
        assert r.urgency == PreventionUrgency.IMMEDIATE

    def test_urgent_on_24h(self):
        r = advise_chargeback_prevention(_prev(hours_since_purchase=20.0))
        assert r.urgency == PreventionUrgency.URGENT

    def test_standard_on_48h(self):
        r = advise_chargeback_prevention(_prev(hours_since_purchase=48.0))
        assert r.urgency == PreventionUrgency.STANDARD

    def test_monitoring_on_old_dispute(self):
        r = advise_chargeback_prevention(_prev(hours_since_purchase=200.0))
        assert r.urgency == PreventionUrgency.MONITORING

    # ── Inquiry stage bonus ───────────────────────────────────────

    def test_inquiry_stage_boosts_probability(self):
        no_inquiry = advise_chargeback_prevention(_prev(is_inquiry_stage=False))
        with_inquiry = advise_chargeback_prevention(_prev(is_inquiry_stage=True))
        assert with_inquiry.prevention_probability >= no_inquiry.prevention_probability

    def test_unresolved_complaint_lowers_probability(self):
        resolved = advise_chargeback_prevention(_prev(buyer_complaint_unresolved=False))
        unresolved = advise_chargeback_prevention(_prev(buyer_complaint_unresolved=True))
        assert unresolved.prevention_probability < resolved.prevention_probability

    # ── INR specific ──────────────────────────────────────────────

    def test_inr_tracking_step_when_in_transit_with_tracking(self):
        r = advise_chargeback_prevention(_prev(
            reason_code="INR",
            delivery_status="IN_TRANSIT",
            carrier_has_tracking=True,
        ))
        actions = [s.action for s in r.recommended_steps]
        assert PreventionAction.SEND_TRACKING_UPDATE in actions

    def test_inr_reshipment_on_lost(self):
        r = advise_chargeback_prevention(_prev(
            reason_code="INR",
            delivery_status="LOST",
        ))
        actions = [s.action for s in r.recommended_steps]
        assert PreventionAction.EXPEDITE_RESHIPMENT in actions

    # ── SNAD specific ─────────────────────────────────────────────

    def test_snad_recommends_partial_refund(self):
        r = advise_chargeback_prevention(_prev(reason_code="SNAD", amount=100.0))
        actions = [s.action for s in r.recommended_steps]
        assert PreventionAction.PARTIAL_REFUND_OFFER in actions

    def test_snad_partial_refund_cheaper_than_chargeback(self):
        r = advise_chargeback_prevention(_prev(reason_code="SNAD", amount=100.0))
        partial_step = next(s for s in r.recommended_steps
                            if s.action == PreventionAction.PARTIAL_REFUND_OFFER)
        assert partial_step.cost_estimate < r.chargeback_cost_if_lost

    # ── UNAUTHORIZED specific ─────────────────────────────────────

    def test_unauthorized_suggests_proactive_refund(self):
        r = advise_chargeback_prevention(_prev(reason_code="UNAUTHORIZED", amount=50.0))
        assert r.primary_action == PreventionAction.PROACTIVE_REFUND

    # ── SUBSCRIPTION specific ─────────────────────────────────────

    def test_subscription_suggests_proactive_refund(self):
        r = advise_chargeback_prevention(_prev(reason_code="SUBSCRIPTION"))
        assert r.primary_action == PreventionAction.PROACTIVE_REFUND

    # ── VIP buyer handling ────────────────────────────────────────

    def test_vip_buyer_escalated_to_manager(self):
        r = advise_chargeback_prevention(_prev(
            buyer_tier="VIP",
            hours_since_purchase=48.0,  # not IMMEDIATE urgency
        ))
        actions = [s.action for s in r.recommended_steps]
        assert PreventionAction.ESCALATE_TO_MANAGER in actions

    def test_vip_relationship_risk_high(self):
        r = advise_chargeback_prevention(_prev(buyer_tier="VIP"))
        assert r.relationship_risk == "HIGH"

    def test_unknown_tier_relationship_risk_normal(self):
        r = advise_chargeback_prevention(_prev(buyer_tier="UNKNOWN"))
        assert r.relationship_risk == "NORMAL"

    # ── ROI ───────────────────────────────────────────────────────

    def test_chargeback_cost_if_lost_includes_fees(self):
        r = advise_chargeback_prevention(_prev(
            amount=100.0, response_cost=15.0, chargeback_fee=20.0
        ))
        assert r.chargeback_cost_if_lost == pytest.approx(135.0)

    def test_roi_non_negative_on_high_probability(self):
        r = advise_chargeback_prevention(_prev(
            is_inquiry_stage=True, hours_since_purchase=5.0, amount=200.0,
        ))
        assert r.prevention_roi >= 0


# ═══════════════════════════════════════════════════════════════════
# Buyer LTV Decision Engine Tests
# ═══════════════════════════════════════════════════════════════════

def _ltv(dispute_id="D-001", buyer_id="B-001", dispute_amount=200.0,
         oracle_ev_fight=50.0, oracle_win_probability=0.55,
         oracle_recommendation="FIGHT", **kw) -> LTVDecisionInput:
    defaults = dict(
        dispute_id=dispute_id, buyer_id=buyer_id,
        dispute_amount=dispute_amount,
        oracle_ev_fight=oracle_ev_fight,
        oracle_win_probability=oracle_win_probability,
        oracle_recommendation=oracle_recommendation,
    )
    defaults.update(kw)
    return LTVDecisionInput(**defaults)


class TestBuyerLTVDecisionEngine:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calculate_ltv_decision(_ltv(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_all_scenarios_present(self):
        r = calculate_ltv_decision(_ltv(can_offer_partial=True))
        assert r.scenario_fight is not None
        assert r.scenario_accept is not None
        assert r.scenario_partial is not None

    def test_partial_absent_when_disabled(self):
        r = calculate_ltv_decision(_ltv(can_offer_partial=False))
        assert r.scenario_partial is None

    # ── LTV tier parsing ──────────────────────────────────────────

    def test_vip_tier_parsed(self):
        r = calculate_ltv_decision(_ltv(buyer_ltv_tier="VIP", buyer_ltv_estimate=800.0))
        assert r.buyer_ltv_tier == LTVTier.VIP

    def test_unknown_tier_falls_back_to_negligible(self):
        r = calculate_ltv_decision(_ltv(buyer_ltv_tier="UNKNOWN"))
        assert r.buyer_ltv_tier == LTVTier.NEGLIGIBLE

    # ── Oracle override ───────────────────────────────────────────

    def test_vip_high_ltv_may_override_oracle_fight(self):
        r = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="VIP",
            buyer_ltv_estimate=2000.0,
            oracle_ev_fight=30.0,       # small fight EV
            oracle_recommendation="FIGHT",
            dispute_amount=200.0,
        ))
        # With $2000 LTV at VIP churn rates, fight RAEV should be worse than accept
        if r.overrides_oracle:
            assert r.ltv_recommendation in (LTVAction.ACCEPT, LTVAction.PARTIAL_REFUND)

    def test_low_ltv_no_override(self):
        r = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="LOW",
            buyer_ltv_estimate=15.0,
            oracle_ev_fight=80.0,       # strong fight EV
            oracle_recommendation="FIGHT",
        ))
        # Low LTV + strong EV → should still fight
        assert r.ltv_recommendation == LTVAction.FIGHT
        assert not r.overrides_oracle

    # ── RAEV math ─────────────────────────────────────────────────

    def test_fight_raev_decreases_with_higher_ltv(self):
        low_ltv  = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="LOW", buyer_ltv_estimate=20.0, can_offer_partial=False,
        ))
        high_ltv = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="VIP", buyer_ltv_estimate=2000.0, can_offer_partial=False,
        ))
        assert high_ltv.scenario_fight.relationship_adjusted_ev < low_ltv.scenario_fight.relationship_adjusted_ev

    def test_accept_raev_has_negative_dispute_ev(self):
        r = calculate_ltv_decision(_ltv(dispute_amount=200.0, chargeback_fee=20.0))
        # Accept always loses money on the dispute itself
        assert r.scenario_accept.dispute_ev < 0

    # ── Habitual disputer penalty ─────────────────────────────────

    def test_habitual_disputer_lowers_ltv_estimate(self):
        normal = calculate_ltv_decision(_ltv(
            buyer_ltv_estimate=400.0, buyer_dispute_history_count=0,
        ))
        habitual = calculate_ltv_decision(_ltv(
            buyer_ltv_estimate=400.0, buyer_dispute_history_count=5,
        ))
        assert habitual.buyer_ltv_estimate < normal.buyer_ltv_estimate

    def test_lapsed_buyer_lowers_ltv_estimate(self):
        active = calculate_ltv_decision(_ltv(
            buyer_ltv_estimate=400.0, buyer_last_purchase_days_ago=30,
        ))
        lapsed = calculate_ltv_decision(_ltv(
            buyer_ltv_estimate=400.0, buyer_last_purchase_days_ago=200,
        ))
        assert lapsed.buyer_ltv_estimate < active.buyer_ltv_estimate

    # ── Churn probabilities ───────────────────────────────────────

    def test_vip_fight_higher_churn_than_accept(self):
        r = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="VIP", buyer_ltv_estimate=1000.0,
        ))
        assert r.scenario_fight.churn_probability > r.scenario_accept.churn_probability

    def test_partial_churn_between_fight_and_accept(self):
        r = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="HIGH", buyer_ltv_estimate=300.0, can_offer_partial=True,
        ))
        assert (r.scenario_accept.churn_probability
                <= r.scenario_partial.churn_probability
                <= r.scenario_fight.churn_probability)

    # ── Best RAEV is returned ─────────────────────────────────────

    def test_recommendation_has_highest_raev(self):
        r = calculate_ltv_decision(_ltv(
            buyer_ltv_tier="HIGH", buyer_ltv_estimate=400.0, can_offer_partial=True,
        ))
        scenarios = [r.scenario_fight, r.scenario_accept, r.scenario_partial]
        best_raev = max(s.relationship_adjusted_ev for s in scenarios)
        assert r.raev == pytest.approx(best_raev)

    def test_oracle_ev_fight_preserved_in_output(self):
        r = calculate_ltv_decision(_ltv(oracle_ev_fight=42.5))
        assert r.oracle_ev_fight == pytest.approx(42.5)

    # ── Summary / insight ─────────────────────────────────────────

    def test_key_insight_non_empty(self):
        r = calculate_ltv_decision(_ltv())
        assert len(r.key_insight) > 20

    def test_overrides_oracle_flag_correct(self):
        # Oracle says FIGHT; for a NEGLIGIBLE buyer we expect no override
        r = calculate_ltv_decision(_ltv(
            oracle_recommendation="FIGHT",
            buyer_ltv_tier="NEGLIGIBLE",
            buyer_ltv_estimate=5.0,
            oracle_ev_fight=100.0,
        ))
        assert r.overrides_oracle is False
