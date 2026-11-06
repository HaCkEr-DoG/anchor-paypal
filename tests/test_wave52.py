"""
Tests for Wave 52: dispute_resolution_timer, merchant_risk_scorer.
"""
import pytest
from oracle.dispute_resolution_timer import (
    compute_dispute_timeline, DisputeTimerInput,
    DisputePhase, ActionPriority,
)
from oracle.merchant_risk_scorer import (
    score_merchant_risk, MerchantRiskInput,
    HdrTier, RiskTrend, RiskDimension,
)

DAY = 86400
BASE_NOW = 1700000000


# ═══════════════════════════════════════════════════════════════════
# Dispute Resolution Timer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeResolutionTimer:

    def _inp(self, network="VISA", phase="CHARGEBACK", days_since_phase_start=5, **kw) -> DisputeTimerInput:
        phase_started = BASE_NOW - days_since_phase_start * DAY
        defaults = dict(
            dispute_id="D-001",
            network=network,
            phase=phase,
            dispute_opened_epoch=phase_started,
            phase_started_epoch=phase_started,
            now_epoch=BASE_NOW,
        )
        defaults.update(kw)
        return DisputeTimerInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = compute_dispute_timeline(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_response_deadline_set(self):
        r = compute_dispute_timeline(self._inp())
        assert r.response_deadline.deadline_epoch > BASE_NOW

    def test_pct_elapsed_bounded(self):
        r = compute_dispute_timeline(self._inp())
        assert 0.0 <= r.pct_phase_elapsed <= 1.0

    # ── Network window sizes ──────────────────────────────────────

    def test_paypal_window_10_days(self):
        r = compute_dispute_timeline(self._inp(network="PAYPAL", days_since_phase_start=1))
        assert r.days_total_in_phase == 10

    def test_visa_window_20_days(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=1))
        assert r.days_total_in_phase == 20

    def test_amex_window_20_days(self):
        r = compute_dispute_timeline(self._inp(network="AMEX", days_since_phase_start=1))
        assert r.days_total_in_phase == 20

    # ── Days remaining ────────────────────────────────────────────

    def test_days_remaining_decreases_with_elapsed(self):
        early = compute_dispute_timeline(self._inp(days_since_phase_start=1))
        late  = compute_dispute_timeline(self._inp(days_since_phase_start=15))
        assert early.response_deadline.days_remaining > late.response_deadline.days_remaining

    def test_expired_when_past_deadline(self):
        r = compute_dispute_timeline(self._inp(network="PAYPAL", days_since_phase_start=12))
        assert r.response_deadline.is_expired
        assert r.urgency_band == "EXPIRED"

    # ── Urgency bands ─────────────────────────────────────────────

    def test_critical_urgency_at_1_day_left(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=19))
        assert r.urgency_band == "CRITICAL"

    def test_high_urgency_at_3_days_left(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=16))
        assert r.urgency_band in ("CRITICAL", "HIGH")

    def test_low_urgency_at_15_days_left(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=1))
        assert r.urgency_band in ("MODERATE", "LOW")

    # ── Pre-arb deadline ─────────────────────────────────────────

    def test_visa_has_pre_arb_deadline(self):
        r = compute_dispute_timeline(self._inp(network="VISA", phase="CHARGEBACK"))
        assert r.pre_arb_deadline is not None

    def test_paypal_no_pre_arb_deadline(self):
        r = compute_dispute_timeline(self._inp(network="PAYPAL", phase="CHARGEBACK"))
        assert r.pre_arb_deadline is None

    def test_amex_no_pre_arb_deadline(self):
        r = compute_dispute_timeline(self._inp(network="AMEX", phase="CHARGEBACK"))
        assert r.pre_arb_deadline is None

    # ── Arbitration deadline ──────────────────────────────────────

    def test_visa_has_arb_deadline(self):
        r = compute_dispute_timeline(self._inp(network="VISA"))
        assert r.arb_deadline is not None

    def test_arb_deadline_later_than_response_deadline(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=1))
        assert r.arb_deadline.deadline_epoch > r.response_deadline.deadline_epoch

    # ── Action queue ──────────────────────────────────────────────

    def test_action_queue_non_empty_when_not_expired(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=5))
        assert len(r.action_queue) >= 1

    def test_critical_today_non_empty_at_deadline(self):
        r = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=19))
        assert len(r.critical_today) >= 1

    # ── Phase tracking ────────────────────────────────────────────

    def test_phase_enum_set_correctly(self):
        r = compute_dispute_timeline(self._inp(phase="CHARGEBACK"))
        assert r.phase == DisputePhase.CHARGEBACK

    def test_pct_elapsed_increases_over_time(self):
        early = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=2))
        late  = compute_dispute_timeline(self._inp(network="VISA", days_since_phase_start=10))
        assert late.pct_phase_elapsed > early.pct_phase_elapsed


# ═══════════════════════════════════════════════════════════════════
# Merchant Risk Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestMerchantRiskScorer:

    def _inp(self, txns=1000, disputes=5, **kw) -> MerchantRiskInput:
        defaults = dict(
            merchant_id="M-001",
            total_transactions_90d=txns,
            total_transaction_value_90d=txns * 50.0,
            total_disputes_90d=disputes,
            open_disputes=max(1, disputes // 3),
            disputes_won_90d=max(0, disputes // 2),
            disputes_lost_90d=max(0, disputes // 3),
            disputes_accepted_90d=max(0, disputes // 6),
        )
        defaults.update(kw)
        return MerchantRiskInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = score_merchant_risk(self._inp(merchant_id="M-XYZ"))
        assert "M-XYZ" in r.summary

    def test_dispute_rate_computed(self):
        r = score_merchant_risk(self._inp(txns=1000, disputes=5))
        assert abs(r.dispute_rate_90d - 0.005) < 0.0001

    def test_five_dimensions(self):
        r = score_merchant_risk(self._inp())
        assert len(r.dimensions) == 5

    def test_overall_score_bounded(self):
        r = score_merchant_risk(self._inp())
        assert 0 <= r.overall_risk_score <= 100

    # ── HDR tier classification ───────────────────────────────────

    def test_normal_tier_below_threshold(self):
        r = score_merchant_risk(self._inp(txns=10000, disputes=4))
        assert r.hdr_tier == HdrTier.NORMAL

    def test_elevated_tier(self):
        # 0.6% → ELEVATED
        r = score_merchant_risk(self._inp(txns=1000, disputes=6))
        assert r.hdr_tier == HdrTier.ELEVATED

    def test_fee_tier_at_1_5_pct(self):
        # 1.5% → FEE_TIER
        r = score_merchant_risk(self._inp(txns=1000, disputes=15))
        assert r.hdr_tier == HdrTier.FEE_TIER

    def test_severe_at_3_pct(self):
        r = score_merchant_risk(self._inp(txns=1000, disputes=30))
        assert r.hdr_tier == HdrTier.SEVERE

    def test_fee_tier_exposure_per_dispute(self):
        r = score_merchant_risk(self._inp(txns=1000, disputes=15))
        assert r.fee_exposure_per_dispute == 30.0

    def test_normal_tier_no_extra_fee(self):
        r = score_merchant_risk(self._inp(txns=10000, disputes=4))
        assert r.fee_exposure_per_dispute == 0.0

    # ── Trend ────────────────────────────────────────────────────

    def test_improving_trend_on_lower_rate(self):
        # Prior: 20/1000 = 2%, current: 5/1000 = 0.5%
        r = score_merchant_risk(self._inp(
            txns=1000, disputes=5,
            total_transactions_prior=1000, total_disputes_prior=20,
        ))
        assert r.trend == RiskTrend.IMPROVING

    def test_critical_trend_on_sharp_increase(self):
        # Prior: 2/1000 = 0.2%, current: 15/1000 = 1.5%
        r = score_merchant_risk(self._inp(
            txns=1000, disputes=15,
            total_transactions_prior=1000, total_disputes_prior=2,
        ))
        assert r.trend in (RiskTrend.WORSENING, RiskTrend.CRITICAL)

    def test_stable_trend_no_prior(self):
        r = score_merchant_risk(self._inp())
        assert r.trend == RiskTrend.STABLE

    # ── Revenue at risk ───────────────────────────────────────────

    def test_revenue_at_risk_matches_input(self):
        r = score_merchant_risk(self._inp(open_dispute_value=5000.0))
        assert r.revenue_at_risk == 5000.0

    # ── Recommendations ───────────────────────────────────────────

    def test_recommendations_non_empty(self):
        r = score_merchant_risk(self._inp())
        assert len(r.recommendations) >= 1

    def test_urgent_action_on_fee_tier(self):
        r = score_merchant_risk(self._inp(txns=1000, disputes=15))
        assert len(r.urgent_actions) >= 1

    def test_next_tier_projected_correctly(self):
        # At 1.0% (LIMITED), 20% more = 1.2% still LIMITED
        r = score_merchant_risk(self._inp(txns=1000, disputes=10))
        assert r.hdr_tier == HdrTier.LIMITED
        # hdr_tier_next should be LIMITED or FEE_TIER
        assert r.hdr_tier_next in (HdrTier.LIMITED, HdrTier.FEE_TIER)
