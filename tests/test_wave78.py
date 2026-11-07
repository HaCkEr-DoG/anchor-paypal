"""Wave 78 — Dispute Outcome Predictor tests."""
import pytest
from oracle.dispute_outcome_predictor import (
    MerchantTier,
    PredictedOutcome,
    OutcomePredictorInput,
    predict_outcome,
)


def _inp(
    reason_code="INR",
    evidence_coverage=0.80,
    response_hours=12.0,
    tx_age_days=5,
    sp=False,
    tier=MerchantTier.SILVER,
    repeat_disputer=False,
    amount=299.0,
):
    return OutcomePredictorInput(
        dispute_id="PP-D-PRED",
        reason_code=reason_code,
        evidence_coverage=evidence_coverage,
        response_hours=response_hours,
        transaction_age_days=tx_age_days,
        seller_protection_eligible=sp,
        merchant_tier=tier,
        buyer_is_repeat_disputer=repeat_disputer,
        transaction_amount=amount,
    )


# ─── Probability constraints ──────────────────────────────────────────────────

class TestProbabilityConstraints:
    def test_probabilities_sum_to_one(self):
        r = predict_outcome(_inp())
        assert abs(r.p_win + r.p_partial_win + r.p_lose - 1.0) < 0.001

    def test_p_win_in_range(self):
        r = predict_outcome(_inp())
        assert 0.05 <= r.p_win <= 0.95

    def test_all_probs_non_negative(self):
        r = predict_outcome(_inp())
        assert r.p_win >= 0.0
        assert r.p_partial_win >= 0.0
        assert r.p_lose >= 0.0

    def test_extreme_good_case_clamp_at_0_95(self):
        r = predict_outcome(_inp(
            reason_code="INR", evidence_coverage=1.0, response_hours=1.0,
            tx_age_days=1, sp=True, tier=MerchantTier.GOLD, repeat_disputer=False
        ))
        assert r.p_win <= 0.95

    def test_extreme_bad_case_clamp_at_0_05(self):
        r = predict_outcome(_inp(
            reason_code="UNAUTHORIZED", evidence_coverage=0.0, response_hours=200.0,
            tx_age_days=90, sp=False, tier=MerchantTier.SUSPEND, repeat_disputer=True
        ))
        assert r.p_win >= 0.05


# ─── Predicted outcome labeling ──────────────────────────────────────────────

class TestPredictedOutcome:
    def test_high_win_prob_predicts_win(self):
        r = predict_outcome(_inp(
            reason_code="INR", evidence_coverage=1.0, response_hours=5.0,
            tx_age_days=3, sp=True, tier=MerchantTier.GOLD
        ))
        assert r.predicted_outcome == PredictedOutcome.WIN

    def test_low_win_prob_predicts_lose(self):
        r = predict_outcome(_inp(
            reason_code="UNAUTHORIZED", evidence_coverage=0.0, response_hours=200.0,
            tx_age_days=90, sp=False, tier=MerchantTier.SUSPEND, repeat_disputer=True
        ))
        assert r.predicted_outcome == PredictedOutcome.LOSE

    def test_dispute_id_echoed(self):
        r = predict_outcome(_inp())
        assert r.dispute_id == "PP-D-PRED"


# ─── Factor contributions ─────────────────────────────────────────────────────

class TestFactorContributions:
    def test_seven_factors_returned(self):
        r = predict_outcome(_inp())
        assert len(r.factor_contributions) == 7

    def test_sp_adds_positive_delta(self):
        r_no_sp = predict_outcome(_inp(sp=False))
        r_sp = predict_outcome(_inp(sp=True))
        assert r_sp.p_win > r_no_sp.p_win

    def test_full_evidence_boosts_win(self):
        r_low = predict_outcome(_inp(evidence_coverage=0.0))
        r_high = predict_outcome(_inp(evidence_coverage=1.0))
        assert r_high.p_win > r_low.p_win

    def test_repeat_disputer_lowers_win(self):
        r_normal = predict_outcome(_inp(repeat_disputer=False))
        r_repeat = predict_outcome(_inp(repeat_disputer=True))
        assert r_repeat.p_win < r_normal.p_win

    def test_gold_tier_better_than_suspend(self):
        r_gold = predict_outcome(_inp(tier=MerchantTier.GOLD))
        r_sus = predict_outcome(_inp(tier=MerchantTier.SUSPEND))
        assert r_gold.p_win > r_sus.p_win

    def test_fast_response_better_than_slow(self):
        r_fast = predict_outcome(_inp(response_hours=10.0))
        r_slow = predict_outcome(_inp(response_hours=100.0))
        assert r_fast.p_win > r_slow.p_win

    def test_inr_better_than_unauthorized(self):
        r_inr = predict_outcome(_inp(reason_code="INR"))
        r_unauth = predict_outcome(_inp(reason_code="UNAUTHORIZED"))
        assert r_inr.p_win > r_unauth.p_win


# ─── EV-adjusted probability ──────────────────────────────────────────────────

class TestEVAdjustedProbability:
    def test_ev_win_prob_at_least_p_win(self):
        # ev_p_win = p_win + 0.5*p_partial ≥ p_win
        r = predict_outcome(_inp())
        assert r.win_probability_for_ev >= r.p_win

    def test_ev_win_prob_at_most_1(self):
        r = predict_outcome(_inp())
        assert r.win_probability_for_ev <= 1.0

    def test_ev_win_formula(self):
        r = predict_outcome(_inp())
        expected = r.p_win + 0.5 * r.p_partial_win
        assert abs(r.win_probability_for_ev - expected) < 0.001


# ─── Confidence classification ────────────────────────────────────────────────

class TestConfidence:
    def test_confidence_is_valid_value(self):
        r = predict_outcome(_inp())
        assert r.confidence in ("HIGH", "MEDIUM", "LOW")

    def test_strong_outcome_high_confidence(self):
        r = predict_outcome(_inp(
            reason_code="INR", evidence_coverage=1.0, response_hours=5.0,
            tx_age_days=3, sp=True, tier=MerchantTier.GOLD
        ))
        assert r.confidence == "HIGH"


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_paypal_signal_contains_dispute_id(self):
        r = predict_outcome(_inp())
        assert "PP-D-PRED" in r.paypal_signal

    def test_paypal_signal_contains_reason_code(self):
        r = predict_outcome(_inp(reason_code="SNAD"))
        assert "SNAD" in r.paypal_signal

    def test_factor_descriptions_non_empty(self):
        r = predict_outcome(_inp())
        for f in r.factor_contributions:
            assert len(f.description) > 5
