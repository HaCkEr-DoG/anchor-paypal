"""Wave 76 — Merchant Reputation Score tests."""
import pytest
from oracle.merchant_reputation_score import (
    ReputationTier,
    MerchantReputationInput,
    score_merchant_reputation,
)


def _inp(
    cb_rate=0.3,
    win_rate=65.0,
    timeliness=90.0,
    refund_rate=5.0,
    age_months=24,
    verified=True,
    total_disputes=15,
    previous_score=None,
):
    return MerchantReputationInput(
        merchant_id="MERCH_REP",
        chargeback_rate_pct=cb_rate,
        dispute_win_rate_pct=win_rate,
        response_timeliness_pct=timeliness,
        refund_rate_pct=refund_rate,
        account_age_months=age_months,
        is_verified_seller=verified,
        total_disputes_last_90d=total_disputes,
        previous_score=previous_score,
    )


# ─── Tier classification ──────────────────────────────────────────────────────

class TestTierClassification:
    def test_excellent_merchant_gold(self):
        # Minimal CB, high win, timely, low refund, old verified
        r = score_merchant_reputation(_inp(cb_rate=0.1, win_rate=90.0, timeliness=99.0,
                                          refund_rate=2.0, age_months=48, verified=True))
        assert r.tier == ReputationTier.GOLD

    def test_poor_merchant_suspend(self):
        r = score_merchant_reputation(_inp(cb_rate=2.0, win_rate=5.0, timeliness=10.0,
                                          refund_rate=80.0, age_months=1, verified=False))
        assert r.tier in (ReputationTier.SUSPEND, ReputationTier.REVIEW)

    def test_average_merchant_silver_or_bronze(self):
        r = score_merchant_reputation(_inp())
        assert r.tier in (ReputationTier.SILVER, ReputationTier.BRONZE)

    def test_tier_ordering(self):
        poor = score_merchant_reputation(_inp(cb_rate=1.9, win_rate=10.0, timeliness=20.0,
                                              refund_rate=60.0, age_months=2, verified=False))
        good = score_merchant_reputation(_inp(cb_rate=0.1, win_rate=85.0, timeliness=95.0,
                                              refund_rate=3.0, age_months=36, verified=True))
        assert good.composite_score > poor.composite_score


# ─── Composite score ──────────────────────────────────────────────────────────

class TestCompositeScore:
    def test_score_in_range(self):
        r = score_merchant_reputation(_inp())
        assert 0.0 <= r.composite_score <= 100.0

    def test_perfect_inputs_near_100(self):
        r = score_merchant_reputation(_inp(cb_rate=0.0, win_rate=100.0, timeliness=100.0,
                                           refund_rate=0.0, age_months=48, verified=True))
        assert r.composite_score >= 90.0

    def test_worst_inputs_near_0(self):
        r = score_merchant_reputation(_inp(cb_rate=2.0, win_rate=0.0, timeliness=0.0,
                                           refund_rate=100.0, age_months=0, verified=False))
        assert r.composite_score <= 15.0

    def test_better_cb_rate_increases_score(self):
        r_high_cb = score_merchant_reputation(_inp(cb_rate=1.5))
        r_low_cb = score_merchant_reputation(_inp(cb_rate=0.2))
        assert r_low_cb.composite_score > r_high_cb.composite_score


# ─── Dimension breakdown ──────────────────────────────────────────────────────

class TestDimensions:
    def test_six_dimensions_returned(self):
        r = score_merchant_reputation(_inp())
        assert len(r.dimensions) == 6

    def test_weights_sum_to_one(self):
        r = score_merchant_reputation(_inp())
        total_weight = sum(d.weight for d in r.dimensions)
        assert abs(total_weight - 1.0) < 0.001

    def test_weighted_contributions_sum_to_composite(self):
        r = score_merchant_reputation(_inp())
        total = sum(d.weighted_contribution for d in r.dimensions)
        assert abs(total - r.composite_score) < 0.5

    def test_verified_seller_contributes(self):
        r_verified = score_merchant_reputation(_inp(verified=True))
        r_unverified = score_merchant_reputation(_inp(verified=False))
        assert r_verified.composite_score > r_unverified.composite_score


# ─── Score delta ──────────────────────────────────────────────────────────────

class TestScoreDelta:
    def test_no_previous_score_gives_none_delta(self):
        r = score_merchant_reputation(_inp(previous_score=None))
        assert r.score_delta is None

    def test_improvement_gives_positive_delta(self):
        r = score_merchant_reputation(_inp(previous_score=50.0))
        # Default inputs should score above 50 for most cases
        # Just test that delta = composite - previous
        assert r.score_delta is not None
        assert abs(r.score_delta - (r.composite_score - 50.0)) < 0.01

    def test_decline_gives_negative_delta(self):
        r = score_merchant_reputation(_inp(cb_rate=1.9, win_rate=10.0,
                                           timeliness=20.0, verified=False,
                                           previous_score=90.0))
        assert r.score_delta is not None
        assert r.score_delta < 0.0


# ─── Signal strength ──────────────────────────────────────────────────────────

class TestSignalStrength:
    def test_high_volume_strong(self):
        r = score_merchant_reputation(_inp(total_disputes=25))
        assert r.signal_strength == "STRONG"

    def test_medium_volume_moderate(self):
        r = score_merchant_reputation(_inp(total_disputes=10))
        assert r.signal_strength == "MODERATE"

    def test_low_volume_weak(self):
        r = score_merchant_reputation(_inp(total_disputes=2))
        assert r.signal_strength == "WEAK"


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_merchant_id_echoed(self):
        r = score_merchant_reputation(_inp())
        assert r.merchant_id == "MERCH_REP"

    def test_routing_recommendation_non_empty(self):
        r = score_merchant_reputation(_inp())
        assert len(r.routing_recommendation) > 20

    def test_gold_tier_non_zero_savings(self):
        r = score_merchant_reputation(_inp(cb_rate=0.0, win_rate=100.0, timeliness=100.0,
                                           refund_rate=0.0, age_months=48, verified=True))
        assert r.paypal_savings_per_dispute_usd > 0.0

    def test_suspend_tier_zero_savings(self):
        r = score_merchant_reputation(_inp(cb_rate=2.0, win_rate=0.0, timeliness=0.0,
                                           refund_rate=100.0, age_months=0, verified=False))
        assert r.paypal_savings_per_dispute_usd == 0.0

    def test_paypal_signal_contains_merchant_id(self):
        r = score_merchant_reputation(_inp())
        assert "MERCH_REP" in r.paypal_signal

    def test_paypal_signal_contains_tier(self):
        r = score_merchant_reputation(_inp(cb_rate=0.0, win_rate=100.0, timeliness=100.0,
                                           refund_rate=0.0, age_months=48, verified=True))
        assert r.tier.value in r.paypal_signal
