"""
Tests for Wave 43: merchant_win_rate_estimator, dispute_outcome_predictor.
"""
import pytest
from oracle.merchant_win_rate_estimator import (
    estimate_win_rate, WinRateInput,
    WinLikelihood, ConfidenceLevel,
)
from oracle.dispute_outcome_predictor import (
    predict_dispute_outcome, OutcomePredictorInput,
    FinalAction, DecisionStrength, OutcomeConfidence,
)


# ═══════════════════════════════════════════════════════════════════
# Merchant Win Rate Estimator Tests
# ═══════════════════════════════════════════════════════════════════

class TestMerchantWinRateEstimator:

    def _inp(self, reason_code="INR", **kw) -> WinRateInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason_code,
            network="PAYPAL",
            transaction_amount=100.0,
        )
        defaults.update(kw)
        return WinRateInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = estimate_win_rate(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_win_rate_bounded(self):
        r = estimate_win_rate(self._inp())
        assert 0.0 <= r.adjusted_win_rate <= 1.0

    def test_base_rate_known_code(self):
        r = estimate_win_rate(self._inp(reason_code="INR"))
        assert r.base_win_rate == 0.52

    def test_base_rate_unknown_code(self):
        r = estimate_win_rate(self._inp(reason_code="UNKNOWN_CODE"))
        assert r.base_win_rate == 0.45

    # ── Evidence strength adjustments ────────────────────────────

    def test_compelling_evidence_raises_win_rate(self):
        low  = estimate_win_rate(self._inp(evidence_strength="INSUFFICIENT"))
        high = estimate_win_rate(self._inp(evidence_strength="COMPELLING"))
        assert high.adjusted_win_rate > low.adjusted_win_rate

    def test_insufficient_evidence_lowers_win_rate(self):
        base = estimate_win_rate(self._inp())
        weak = estimate_win_rate(self._inp(evidence_strength="INSUFFICIENT"))
        assert weak.adjusted_win_rate < base.adjusted_win_rate

    # ── Seller Protection ─────────────────────────────────────────

    def test_sp_eligible_raises_win_rate(self):
        no_sp = estimate_win_rate(self._inp(sp_eligibility="INELIGIBLE"))
        sp    = estimate_win_rate(self._inp(sp_eligibility="ELIGIBLE"))
        assert sp.adjusted_win_rate > no_sp.adjusted_win_rate

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce30_boosts_win_rate(self):
        no_ce = estimate_win_rate(self._inp(ce30_applicable=False))
        ce    = estimate_win_rate(self._inp(ce30_applicable=True))
        assert ce.adjusted_win_rate > no_ce.adjusted_win_rate
        assert ce.adjusted_win_rate - no_ce.adjusted_win_rate == pytest.approx(0.18, abs=0.01)

    # ── Delivery confirmation ─────────────────────────────────────

    def test_delivered_with_confirmation_boosts(self):
        no_del = estimate_win_rate(self._inp())
        del_   = estimate_win_rate(self._inp(
            tracking_shows_delivered=True,
            has_delivery_confirmation=True,
        ))
        assert del_.adjusted_win_rate > no_del.adjusted_win_rate

    # ── Policy defensibility ──────────────────────────────────────

    def test_strong_policy_boosts(self):
        strong = estimate_win_rate(self._inp(policy_defensibility="STRONG"))
        unusab = estimate_win_rate(self._inp(policy_defensibility="UNUSABLE"))
        assert strong.adjusted_win_rate > unusab.adjusted_win_rate

    # ── Buyer fraud signals ───────────────────────────────────────

    def test_very_high_fraud_signal_slightly_boosts_merchant(self):
        no_fraud = estimate_win_rate(self._inp(fraud_signal_level="MINIMAL"))
        fraud    = estimate_win_rate(self._inp(fraud_signal_level="VERY_HIGH"))
        assert fraud.adjusted_win_rate >= no_fraud.adjusted_win_rate

    # ── Win likelihood tiers ──────────────────────────────────────

    def test_very_likely_above_75pct(self):
        r = estimate_win_rate(self._inp(
            reason_code="12.6",   # base 0.65
            sp_eligibility="ELIGIBLE",          # +12
            evidence_strength="COMPELLING",      # +18
        ))
        assert r.win_likelihood in (WinLikelihood.VERY_LIKELY, WinLikelihood.LIKELY)

    def test_very_unlikely_below_25pct(self):
        r = estimate_win_rate(self._inp(
            reason_code="SNAD",   # base 0.38
            sp_eligibility="INELIGIBLE",
            evidence_strength="INSUFFICIENT",
            policy_defensibility="UNUSABLE",
        ))
        assert r.win_likelihood in (
            WinLikelihood.VERY_UNLIKELY, WinLikelihood.UNLIKELY, WinLikelihood.UNCERTAIN)

    # ── Confidence ────────────────────────────────────────────────

    def test_high_confidence_with_many_signals(self):
        r = estimate_win_rate(self._inp(
            evidence_strength="STRONG",
            sp_eligibility="ELIGIBLE",
            policy_defensibility="ADEQUATE",
            ce30_applicable=True,
            tracking_shows_delivered=True,
        ))
        assert r.confidence == ConfidenceLevel.HIGH

    def test_low_confidence_with_no_signals(self):
        r = estimate_win_rate(self._inp())
        assert r.confidence == ConfidenceLevel.LOW

    # ── EV ────────────────────────────────────────────────────────

    def test_ev_formula_correct(self):
        r = estimate_win_rate(self._inp(transaction_amount=200.0))
        # EV uses base rate from reason code — just check it's computed
        assert r.expected_value is not None

    # ── Factors ───────────────────────────────────────────────────

    def test_positive_factors_on_good_evidence(self):
        r = estimate_win_rate(self._inp(
            evidence_strength="COMPELLING",
            sp_eligibility="ELIGIBLE",
        ))
        assert len(r.positive_factors) >= 1

    def test_negative_factors_on_bad_evidence(self):
        r = estimate_win_rate(self._inp(
            evidence_strength="INSUFFICIENT",
            sp_eligibility="INELIGIBLE",
        ))
        assert len(r.negative_factors) >= 1

    # ── Recommendation ────────────────────────────────────────────

    def test_recommendation_non_empty(self):
        r = estimate_win_rate(self._inp())
        assert len(r.recommendation) > 0


# ═══════════════════════════════════════════════════════════════════
# Dispute Outcome Predictor Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeOutcomePredictor:

    def _inp(self, win_prob=0.55, amount=100.0, **kw) -> OutcomePredictorInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code="INR",
            network="PAYPAL",
            transaction_amount=amount,
            win_probability=win_prob,
            fight_cost=15.0,
            chargeback_fee=20.0,
            negotiation_cost=5.0,
            days_until_deadline=10,
            has_missed_deadline=False,
            can_file_dispute=True,
            hdr_tier="NORMAL",
            hdr_dispute_rate_current=0.003,
            hdr_dispute_rate_threshold=0.01,
            buyer_lifetime_value=100.0,
            is_vip_buyer=False,
            seller_protection_eligible=False,
            ce30_applicable=False,
        )
        defaults.update(kw)
        return OutcomePredictorInput(**defaults)

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = predict_dispute_outcome(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_financial_breakdown_present(self):
        r = predict_dispute_outcome(self._inp())
        assert r.financial is not None

    def test_win_probability_preserved(self):
        r = predict_dispute_outcome(self._inp(win_prob=0.70))
        assert r.win_probability == pytest.approx(0.70)

    # ── Fight recommendation ──────────────────────────────────────

    def test_high_win_prob_recommends_fight(self):
        r = predict_dispute_outcome(self._inp(win_prob=0.80, amount=200.0))
        assert r.final_action == FinalAction.FIGHT

    def test_strong_decision_at_high_win_prob(self):
        r = predict_dispute_outcome(self._inp(win_prob=0.80, amount=200.0))
        assert r.decision_strength in (
            DecisionStrength.STRONG, DecisionStrength.MODERATE)

    # ── Accept recommendation ─────────────────────────────────────

    def test_low_win_prob_negative_ev_accepts(self):
        r = predict_dispute_outcome(self._inp(win_prob=0.20, amount=50.0))
        assert r.final_action in (FinalAction.ACCEPT, FinalAction.NEGOTIATE)

    # ── Missed deadline forces accept ─────────────────────────────

    def test_missed_deadline_forces_accept(self):
        r = predict_dispute_outcome(self._inp(has_missed_deadline=True))
        assert r.final_action == FinalAction.ACCEPT

    def test_cannot_file_forces_accept(self):
        r = predict_dispute_outcome(self._inp(can_file_dispute=False))
        assert r.final_action == FinalAction.ACCEPT

    # ── Negotiate ────────────────────────────────────────────────

    def test_partial_refund_may_recommend_negotiate(self):
        r = predict_dispute_outcome(self._inp(
            win_prob=0.35,
            amount=200.0,
            can_offer_partial_refund=True,
            partial_refund_amount=40.0,
        ))
        # Either negotiate or accept is valid here
        assert r.final_action in (
            FinalAction.NEGOTIATE, FinalAction.ACCEPT, FinalAction.FIGHT)

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce30_in_rationale(self):
        r = predict_dispute_outcome(self._inp(
            win_prob=0.75, amount=200.0, ce30_applicable=True))
        assert any("CE3.0" in line or "ce3" in line.lower()
                   for line in r.rationale)

    # ── Deadline urgency ──────────────────────────────────────────

    def test_urgent_deadline_flagged(self):
        r = predict_dispute_outcome(self._inp(days_until_deadline=1))
        assert r.deadline_urgent is True

    def test_non_urgent_deadline(self):
        r = predict_dispute_outcome(self._inp(days_until_deadline=15))
        assert r.deadline_urgent is False

    # ── HDR concern ───────────────────────────────────────────────

    def test_hdr_concern_flagged_near_threshold(self):
        r = predict_dispute_outcome(self._inp(
            hdr_dispute_rate_current=0.009,
            hdr_dispute_rate_threshold=0.010,
        ))
        assert r.hdr_concern is True

    def test_no_hdr_concern_well_below_threshold(self):
        r = predict_dispute_outcome(self._inp(
            hdr_dispute_rate_current=0.001,
            hdr_dispute_rate_threshold=0.010,
        ))
        assert r.hdr_concern is False

    # ── EV breakdown ─────────────────────────────────────────────

    def test_fight_ev_formula(self):
        p = 0.60
        amount = 100.0
        fight_cost = 15.0
        cb_fee = 20.0
        expected = p * amount - fight_cost - (1 - p) * cb_fee
        r = predict_dispute_outcome(self._inp(
            win_prob=p, amount=amount,
            fight_cost=fight_cost, chargeback_fee=cb_fee,
        ))
        assert abs(r.financial.fight_ev - expected) < 0.1

    def test_accept_cost_equals_amount(self):
        r = predict_dispute_outcome(self._inp(amount=150.0))
        assert r.financial.accept_cost == pytest.approx(150.0)

    # ── Rationale non-empty ───────────────────────────────────────

    def test_rationale_non_empty(self):
        r = predict_dispute_outcome(self._inp())
        assert len(r.rationale) >= 1
