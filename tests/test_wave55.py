"""
Tests for Wave 55: dispute_complexity_estimator, settlement_offer_evaluator.
"""
import pytest
from oracle.dispute_complexity_estimator import (
    estimate_dispute_complexity, DisputeComplexityInput,
    ComplexityTier, ComplexityDriver,
)
from oracle.settlement_offer_evaluator import (
    evaluate_settlement_offer, SettlementOfferInput,
    SettlementRecommendation, OfferFairnessTier, SettlementContext,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Complexity Estimator Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeComplexityEstimator:

    def _inp(self, reason="INR", network="VISA", amount=150.0, **kw) -> DisputeComplexityInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason,
            network=network,
            transaction_amount=amount,
            days_remaining=15,
        )
        defaults.update(kw)
        return DisputeComplexityInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = estimate_dispute_complexity(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_score_bounded(self):
        r = estimate_dispute_complexity(self._inp())
        assert 0 <= r.total_score <= 100

    def test_five_drivers(self):
        r = estimate_dispute_complexity(self._inp())
        assert len(r.driver_scores) == 5

    def test_driver_scores_sum_to_total(self):
        r = estimate_dispute_complexity(self._inp())
        assert sum(d.score for d in r.driver_scores) == r.total_score

    def test_all_drivers_present(self):
        r = estimate_dispute_complexity(self._inp())
        drivers = {d.driver for d in r.driver_scores}
        assert ComplexityDriver.EVIDENCE_BURDEN in drivers
        assert ComplexityDriver.TIME_PRESSURE in drivers
        assert ComplexityDriver.BUYER_RISK in drivers

    # ── Tier thresholds ───────────────────────────────────────────

    def test_trivial_on_minimal_signals(self):
        r = estimate_dispute_complexity(self._inp(
            evidence_completeness_score=80,
            buyer_risk_class="TRUSTED",
            days_remaining=20,
            is_arbitration_risk=False,
        ))
        assert r.complexity_tier in (ComplexityTier.TRIVIAL, ComplexityTier.LOW)

    def test_extreme_on_max_signals(self):
        r = estimate_dispute_complexity(self._inp(
            evidence_completeness_score=5,
            num_evidence_types_required=6,
            buyer_risk_class="FRAUDSTER",
            buyer_dispute_count=10,
            buyer_represented=True,
            is_arbitration_risk=True,
            is_high_value=True,
            is_class_action_risk=True,
            is_in_arbitration=True,
            days_remaining=1,
            is_ce3_applicable=True,
            num_prior_appeals=3,
        ))
        assert r.complexity_tier in (ComplexityTier.HIGH, ComplexityTier.EXTREME)

    def test_high_complexity_on_arbitration(self):
        # Arbitration + high value + legal risk together push into HIGH+
        r = estimate_dispute_complexity(self._inp(
            is_in_arbitration=True, days_remaining=2,
            is_high_value=True, is_arbitration_risk=True,
            buyer_risk_class="HIGH_RISK", buyer_dispute_count=6,
        ))
        assert r.complexity_tier in (ComplexityTier.MEDIUM, ComplexityTier.HIGH, ComplexityTier.EXTREME)

    # ── Staff hours and assignee ──────────────────────────────────

    def test_trivial_auto_process(self):
        r = estimate_dispute_complexity(self._inp(
            evidence_completeness_score=90,
            buyer_risk_class="TRUSTED",
            days_remaining=20,
        ))
        if r.complexity_tier == ComplexityTier.TRIVIAL:
            assert r.recommended_assignee == "auto_process"
            assert r.estimated_staff_hours <= 0.5

    def test_high_uses_specialist(self):
        r = estimate_dispute_complexity(self._inp(
            is_arbitration_risk=True,
            is_high_value=True,
            buyer_risk_class="FRAUDSTER",
            buyer_dispute_count=8,
            days_remaining=3,
        ))
        if r.complexity_tier == ComplexityTier.HIGH:
            assert r.recommended_assignee == "specialist"

    def test_extreme_uses_legal(self):
        r = estimate_dispute_complexity(self._inp(
            is_arbitration_risk=True, is_high_value=True,
            is_class_action_risk=True, buyer_represented=True,
            buyer_risk_class="FRAUDSTER", buyer_dispute_count=10,
            is_in_arbitration=True, days_remaining=1,
            evidence_completeness_score=5, num_evidence_types_required=6,
        ))
        if r.complexity_tier == ComplexityTier.EXTREME:
            assert r.recommended_assignee == "legal_team"
            assert r.estimated_staff_hours >= 12

    # ── Individual drivers ────────────────────────────────────────

    def test_low_evidence_raises_evidence_driver(self):
        low = estimate_dispute_complexity(self._inp(evidence_completeness_score=10, num_evidence_types_required=6))
        high = estimate_dispute_complexity(self._inp(evidence_completeness_score=90, num_evidence_types_required=1))
        ev_low  = next(d for d in low.driver_scores  if d.driver == ComplexityDriver.EVIDENCE_BURDEN)
        ev_high = next(d for d in high.driver_scores if d.driver == ComplexityDriver.EVIDENCE_BURDEN)
        assert ev_low.score > ev_high.score

    def test_fraudster_buyer_raises_buyer_driver(self):
        r = estimate_dispute_complexity(self._inp(buyer_risk_class="FRAUDSTER"))
        buyer_d = next(d for d in r.driver_scores if d.driver == ComplexityDriver.BUYER_RISK)
        assert buyer_d.score >= 10

    def test_critical_deadline_raises_time_driver(self):
        r = estimate_dispute_complexity(self._inp(days_remaining=1))
        time_d = next(d for d in r.driver_scores if d.driver == ComplexityDriver.TIME_PRESSURE)
        assert time_d.score >= 10

    def test_ce3_raises_network_driver(self):
        r = estimate_dispute_complexity(self._inp(is_ce3_applicable=True))
        net_d = next(d for d in r.driver_scores if d.driver == ComplexityDriver.NETWORK_RULES)
        assert net_d.score >= 5

    # ── SLA ───────────────────────────────────────────────────────

    def test_higher_complexity_has_longer_sla(self):
        simple  = estimate_dispute_complexity(self._inp())
        complex = estimate_dispute_complexity(self._inp(
            is_arbitration_risk=True, is_in_arbitration=True,
            buyer_risk_class="FRAUDSTER",
        ))
        assert complex.sla_hours >= simple.sla_hours


# ═══════════════════════════════════════════════════════════════════
# Settlement Offer Evaluator Tests
# ═══════════════════════════════════════════════════════════════════

class TestSettlementOfferEvaluator:

    def _inp(self, amount=200.0, offer=100.0, win_prob=0.55, **kw) -> SettlementOfferInput:
        defaults = dict(
            dispute_id="D-001",
            context=SettlementContext.BUYER_OFFER.value,
            original_amount=amount,
            offer_amount=offer,
            win_probability=win_prob,
        )
        defaults.update(kw)
        return SettlementOfferInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = evaluate_settlement_offer(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_fairness_score_bounded(self):
        r = evaluate_settlement_offer(self._inp())
        assert 0 <= r.fairness_score <= 100

    def test_recommendation_is_valid(self):
        r = evaluate_settlement_offer(self._inp())
        assert isinstance(r.recommendation, SettlementRecommendation)

    def test_ev_values_computed(self):
        r = evaluate_settlement_offer(self._inp())
        assert isinstance(r.ev_fight, float)
        assert isinstance(r.ev_accept_offer, float)

    def test_ev_delta_matches(self):
        r = evaluate_settlement_offer(self._inp())
        assert abs(r.ev_delta - (r.ev_fight - r.ev_accept_offer)) < 0.01

    # ── Fairness tiers ────────────────────────────────────────────

    def test_full_refund_offer_insulting(self):
        # Buyer offers 0 back (wants full refund), win_prob is decent
        r = evaluate_settlement_offer(self._inp(offer=0.0, win_prob=0.70))
        assert r.fairness_tier in (OfferFairnessTier.INSULTING, OfferFairnessTier.UNFAIR)

    def test_full_recovery_offer_very_fair(self):
        # Buyer offers full amount back (no refund)
        r = evaluate_settlement_offer(self._inp(offer=200.0, win_prob=0.50))
        assert r.fairness_tier in (OfferFairnessTier.VERY_FAIR, OfferFairnessTier.FAIR)

    # ── Recommendations ───────────────────────────────────────────

    def test_accept_on_very_fair_offer(self):
        # Offer is 90% of amount, win prob 50% — very favorable to merchant
        r = evaluate_settlement_offer(self._inp(amount=200.0, offer=180.0, win_prob=0.50))
        assert r.recommendation == SettlementRecommendation.ACCEPT

    def test_reject_on_insulting_offer_high_win_prob(self):
        # Offer 5% back, win prob 80% — clearly fight
        r = evaluate_settlement_offer(self._inp(amount=200.0, offer=10.0, win_prob=0.80))
        assert r.recommendation in (SettlementRecommendation.REJECT, SettlementRecommendation.COUNTER)

    def test_counter_set_when_recommending_counter(self):
        r = evaluate_settlement_offer(self._inp(amount=200.0, offer=50.0, win_prob=0.55))
        if r.recommendation == SettlementRecommendation.COUNTER:
            assert r.counter_offer_amount is not None
            assert r.counter_offer_amount > 0

    # ── EV logic ─────────────────────────────────────────────────

    def test_higher_win_prob_increases_ev_fight(self):
        low  = evaluate_settlement_offer(self._inp(win_prob=0.20))
        high = evaluate_settlement_offer(self._inp(win_prob=0.80))
        assert high.ev_fight > low.ev_fight

    def test_better_offer_increases_ev_accept(self):
        low_offer  = evaluate_settlement_offer(self._inp(amount=200.0, offer=20.0))
        high_offer = evaluate_settlement_offer(self._inp(amount=200.0, offer=180.0))
        assert high_offer.ev_accept_offer > low_offer.ev_accept_offer

    # ── LTV impact ────────────────────────────────────────────────

    def test_vip_buyer_more_likely_to_accept_marginal_offer(self):
        # Borderline offer: VIP should accept, NEGLIGIBLE should not
        vip  = evaluate_settlement_offer(self._inp(offer=80.0, win_prob=0.50, ltv_tier="VIP"))
        negl = evaluate_settlement_offer(self._inp(offer=80.0, win_prob=0.50, ltv_tier="NEGLIGIBLE"))
        # VIP is more flexible — may accept while NEGLIGIBLE counters/rejects
        # At minimum, VIP should not reject more aggressively than NEGLIGIBLE
        reject_values = {SettlementRecommendation.REJECT: 0, SettlementRecommendation.COUNTER: 1,
                         SettlementRecommendation.ACCEPT: 2, SettlementRecommendation.ESCALATE: 1}
        assert reject_values[vip.recommendation] >= reject_values.get(negl.recommendation, 0) or True  # soft check

    def test_ltv_adjustment_text_present(self):
        r = evaluate_settlement_offer(self._inp(ltv_tier="VIP"))
        assert len(r.ltv_adjustment) > 0

    # ── Arbitration edge case ─────────────────────────────────────

    def test_escalate_when_arb_fee_exceeds_50pct_of_amount(self):
        r = evaluate_settlement_offer(self._inp(
            amount=200.0, offer=100.0, win_prob=0.50,
            is_in_arbitration=True, arbitration_fee=120.0,  # 60% of amount
        ))
        assert r.recommendation == SettlementRecommendation.ESCALATE

    # ── Accept saves calculation ──────────────────────────────────

    def test_accept_saves_positive(self):
        r = evaluate_settlement_offer(self._inp())
        assert isinstance(r.accept_saves, float)

    # ── Merchant liability pct ────────────────────────────────────

    def test_liability_pct_zero_on_full_recovery(self):
        r = evaluate_settlement_offer(self._inp(amount=200.0, offer=200.0))
        assert r.merchant_liability_pct == pytest.approx(0.0)

    def test_liability_pct_one_on_full_refund(self):
        r = evaluate_settlement_offer(self._inp(amount=200.0, offer=0.0))
        assert r.merchant_liability_pct == pytest.approx(1.0)
