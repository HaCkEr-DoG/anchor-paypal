"""
Tests for Wave 26: dispute_escalation_predictor, settlement_offer_optimizer.
"""
import pytest
from oracle.dispute_escalation_predictor import (
    predict_escalation, DisputeEscalationInput, EscalationRisk,
    EscalationDriver, EscalationMitigation,
)
from oracle.settlement_offer_optimizer import (
    optimize_settlement, SettlementOfferInput, SettlementStrategy,
    SettlementRound,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Escalation Predictor Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeEscalationPredictor:

    def _inp(self, **kw) -> DisputeEscalationInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            dispute_amount=100.0,
            days_open=3,
            buyer_threatened_chargeback=False,
            buyer_tone_hostile=False,
            buyer_expressed_frustration=False,
            buyer_satisfied_with_progress=False,
            merchant_responded=True,
            days_until_merchant_responded=1,
            resolution_offered=False,
            resolution_type="none",
            buyer_prior_escalations=0,
            buyer_escalated_within_24h=False,
            has_delivery_confirmation=False,
            base_escalation_probability=0.15,
        )
        defaults.update(kw)
        return DisputeEscalationInput(**defaults)

    # ── Probability range ─────────────────────────────────────────

    def test_probability_in_range(self):
        r = predict_escalation(self._inp())
        assert 0.0 <= r.escalation_probability <= 1.0

    def test_base_probability_no_drivers(self):
        r = predict_escalation(self._inp())
        assert r.escalation_probability < 0.40

    # ── Drivers raise probability ──────────────────────────────────

    def test_chargeback_threat_raises_probability(self):
        no_threat = predict_escalation(self._inp())
        threat    = predict_escalation(self._inp(buyer_threatened_chargeback=True))
        assert threat.escalation_probability > no_threat.escalation_probability

    def test_hostile_tone_raises_probability(self):
        calm    = predict_escalation(self._inp())
        hostile = predict_escalation(self._inp(buyer_tone_hostile=True))
        assert hostile.escalation_probability > calm.escalation_probability

    def test_long_open_duration_raises_probability(self):
        short = predict_escalation(self._inp(days_open=2))
        long  = predict_escalation(self._inp(days_open=10))
        assert long.escalation_probability > short.escalation_probability

    def test_no_merchant_response_raises_probability(self):
        responded  = predict_escalation(self._inp(merchant_responded=True))
        no_respond = predict_escalation(self._inp(merchant_responded=False))
        assert no_respond.escalation_probability > responded.escalation_probability

    def test_prior_escalations_raise_probability(self):
        clean  = predict_escalation(self._inp(buyer_prior_escalations=0))
        repeat = predict_escalation(self._inp(buyer_prior_escalations=2))
        assert repeat.escalation_probability > clean.escalation_probability

    def test_unauthorized_claim_raises_probability(self):
        inr         = predict_escalation(self._inp(claim_type="INR"))
        unauthorized = predict_escalation(self._inp(claim_type="UNAUTHORIZED"))
        assert unauthorized.escalation_probability > inr.escalation_probability

    # ── Mitigations reduce probability ───────────────────────────

    def test_resolution_offered_reduces_probability(self):
        no_res = predict_escalation(self._inp())
        with_res = predict_escalation(self._inp(
            resolution_offered=True,
            resolution_type="refund",
        ))
        assert with_res.escalation_probability < no_res.escalation_probability

    def test_buyer_satisfied_reduces_probability(self):
        unsatisfied = predict_escalation(self._inp())
        satisfied   = predict_escalation(self._inp(buyer_satisfied_with_progress=True))
        assert satisfied.escalation_probability < unsatisfied.escalation_probability

    def test_delivery_confirmed_reduces_probability(self):
        no_confirm = predict_escalation(self._inp())
        confirmed  = predict_escalation(self._inp(has_delivery_confirmation=True))
        assert confirmed.escalation_probability < no_confirm.escalation_probability

    # ── Risk levels ───────────────────────────────────────────────

    def test_low_risk_for_clean_dispute(self):
        r = predict_escalation(self._inp(
            buyer_satisfied_with_progress=True,
            resolution_offered=True,
            resolution_type="refund",
            days_until_merchant_responded=1,
        ))
        assert r.risk_level in (EscalationRisk.LOW, EscalationRisk.MODERATE)

    def test_critical_risk_for_very_hostile_dispute(self):
        r = predict_escalation(self._inp(
            buyer_threatened_chargeback=True,
            buyer_tone_hostile=True,
            buyer_expressed_frustration=True,
            merchant_responded=False,
            days_open=10,
            buyer_prior_escalations=2,
        ))
        assert r.risk_level in (EscalationRisk.CRITICAL, EscalationRisk.HIGH)

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = predict_escalation(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_intervention_points_non_empty(self):
        r = predict_escalation(self._inp())
        assert len(r.intervention_points) >= 1

    def test_no_response_intervention_first(self):
        r = predict_escalation(self._inp(merchant_responded=False))
        assert any("respond" in p.lower() or "response" in p.lower()
                   for p in r.intervention_points)

    def test_active_drivers_non_negative(self):
        r = predict_escalation(self._inp())
        assert r.active_drivers >= 0

    def test_fee_exposure_positive(self):
        r = predict_escalation(self._inp())
        assert r.additional_fee_if_escalated > 0

    def test_expected_loss_proportional_to_probability(self):
        low  = predict_escalation(self._inp())
        high = predict_escalation(self._inp(buyer_threatened_chargeback=True))
        assert high.expected_additional_loss > low.expected_additional_loss

    def test_deadline_hours_critical_less_than_low(self):
        low  = predict_escalation(self._inp())
        crit = predict_escalation(self._inp(
            buyer_threatened_chargeback=True,
            buyer_tone_hostile=True,
            merchant_responded=False,
        ))
        assert crit.action_deadline_hours < low.action_deadline_hours


# ═══════════════════════════════════════════════════════════════════
# Settlement Offer Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestSettlementOfferOptimizer:

    def _inp(self, **kw) -> SettlementOfferInput:
        defaults = dict(
            dispute_id="D-001",
            claim_amount=100.0,
            claim_type="INR",
            p_win_if_fight=0.55,
            fight_cost=25.0,
            chargeback_fee=20.0,
            sp_covers_chargeback=False,
            escalation_probability=0.15,
            buyer_fraud_score=20.0,
            buyer_tone_hostile=False,
            buyer_threatened_chargeback=False,
            buyer_ltv=0.0,
            is_vip_customer=False,
            max_acceptable_pct=1.0,
            hdr_rate_pct=0.30,
            round_number=1,
            previous_offer_pct=0.0,
            buyer_rejected_previous=False,
        )
        defaults.update(kw)
        return SettlementOfferInput(**defaults)

    # ── Core economics ────────────────────────────────────────────

    def test_ev_fight_positive(self):
        r = optimize_settlement(self._inp())
        assert r.ev_fight > 0

    def test_full_refund_ev_equals_claim(self):
        r = optimize_settlement(self._inp(claim_amount=100.0))
        assert r.ev_full_refund == pytest.approx(101.0, abs=0.01)  # 100 + 1 processing

    def test_breakeven_in_range(self):
        r = optimize_settlement(self._inp())
        assert 0.0 <= r.breakeven_offer_pct <= 1.0

    def test_sp_coverage_reduces_ev_fight(self):
        no_sp = optimize_settlement(self._inp(sp_covers_chargeback=False))
        with_sp = optimize_settlement(self._inp(sp_covers_chargeback=True))
        assert with_sp.ev_fight < no_sp.ev_fight

    def test_higher_win_prob_lowers_ev_fight(self):
        low_win  = optimize_settlement(self._inp(p_win_if_fight=0.30))
        high_win = optimize_settlement(self._inp(p_win_if_fight=0.80))
        assert high_win.ev_fight < low_win.ev_fight

    # ── Offer structure ───────────────────────────────────────────

    def test_offer_amount_within_claim(self):
        r = optimize_settlement(self._inp(claim_amount=200.0))
        assert r.primary_offer.offer_amount <= 200.0

    def test_offer_pct_within_max_acceptable(self):
        r = optimize_settlement(self._inp(max_acceptable_pct=0.70))
        assert r.primary_offer.offer_pct <= 0.70 + 0.001

    def test_acceptance_probability_in_range(self):
        r = optimize_settlement(self._inp())
        assert 0.0 <= r.primary_offer.acceptance_probability <= 1.0

    # ── LTV adjustment ────────────────────────────────────────────

    def test_vip_customer_increases_offer(self):
        normal = optimize_settlement(self._inp())
        vip    = optimize_settlement(self._inp(is_vip_customer=True))
        assert vip.primary_offer.offer_pct >= normal.primary_offer.offer_pct

    def test_high_ltv_increases_offer(self):
        low_ltv  = optimize_settlement(self._inp(buyer_ltv=0.0))
        high_ltv = optimize_settlement(self._inp(buyer_ltv=600.0))
        assert high_ltv.ltv_adjusted_offer_pct >= low_ltv.ltv_adjusted_offer_pct

    def test_ltv_premium_non_negative(self):
        r = optimize_settlement(self._inp())
        assert r.ltv_premium >= 0.0

    # ── Fraud adjustment ──────────────────────────────────────────

    def test_high_fraud_reduces_offer(self):
        low_fraud  = optimize_settlement(self._inp(buyer_fraud_score=10.0))
        high_fraud = optimize_settlement(self._inp(buyer_fraud_score=80.0))
        assert high_fraud.primary_offer.offer_pct <= low_fraud.primary_offer.offer_pct

    def test_fraud_discount_non_negative(self):
        r = optimize_settlement(self._inp(buyer_fraud_score=75.0))
        assert r.fraud_discount >= 0.0

    # ── Hostile buyer adjustment ──────────────────────────────────

    def test_hostile_buyer_increases_offer(self):
        calm    = optimize_settlement(self._inp())
        hostile = optimize_settlement(self._inp(buyer_tone_hostile=True))
        assert hostile.primary_offer.offer_pct >= calm.primary_offer.offer_pct

    # ── Strategy classification ───────────────────────────────────

    def test_full_refund_on_very_poor_fight_ev(self):
        r = optimize_settlement(self._inp(
            p_win_if_fight=0.05,
            fight_cost=50.0,
            escalation_probability=0.80,
            claim_amount=50.0,
        ))
        assert r.recommended_strategy in (
            SettlementStrategy.FULL_REFUND, SettlementStrategy.GENEROUS)

    def test_decline_on_strong_fight_ev(self):
        r = optimize_settlement(self._inp(
            p_win_if_fight=0.95,
            buyer_fraud_score=80.0,
            escalation_probability=0.05,
        ))
        assert r.recommended_strategy in (
            SettlementStrategy.DECLINE, SettlementStrategy.CONSERVATIVE)

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = optimize_settlement(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_negotiation_notes_non_empty(self):
        r = optimize_settlement(self._inp())
        assert len(r.negotiation_notes) >= 1

    def test_fallback_offer_not_none(self):
        r = optimize_settlement(self._inp())
        assert r.fallback_offer is not None

    def test_fallback_pct_at_least_primary(self):
        r = optimize_settlement(self._inp())
        assert r.fallback_offer.offer_pct >= r.primary_offer.offer_pct

    def test_round2_offer_higher_than_round1(self):
        r1 = optimize_settlement(self._inp(round_number=1))
        r2 = optimize_settlement(self._inp(
            round_number=2,
            buyer_rejected_previous=True,
            previous_offer_pct=r1.primary_offer.offer_pct,
        ))
        assert r2.primary_offer.offer_pct >= r1.primary_offer.offer_pct
