"""
Wave 71 — Merchant Dispute Benchmarker
Tests: 24 total
"""

import pytest
from oracle.merchant_benchmarker import (
    benchmark_merchant,
    MerchantMetrics,
    MerchantSegment,
    MerchantCategory,
    BenchmarkTier,
    RiskFlag,
)


def _metrics(**kwargs):
    defaults = dict(
        merchant_id="MERCH_X",
        segment=MerchantSegment.SMALL_BUSINESS,
        category=MerchantCategory.ELECTRONICS,
        total_orders=1000,
        total_disputes=10,          # 1.0% dispute rate
        disputes_won=6,
        disputes_fought=10,
        disputes_escalated=1,
        preemptive_refunds=2,
        avg_response_days=4.0,
        inr_disputes=5,
        unauthorized_disputes=2,
    )
    defaults.update(kwargs)
    return MerchantMetrics(**defaults)


class TestMerchantBenchmarker:

    # ─── Basic structure ──────────────────────────────────────────────────────

    def test_returns_report(self):
        r = benchmark_merchant(_metrics())
        assert r is not None

    def test_merchant_id_preserved(self):
        r = benchmark_merchant(_metrics())
        assert r.merchant_id == "MERCH_X"

    def test_benchmarks_list_not_empty(self):
        r = benchmark_merchant(_metrics())
        assert len(r.benchmarks) >= 4

    # ─── Computed rates ───────────────────────────────────────────────────────

    def test_dispute_rate_computed(self):
        r = benchmark_merchant(_metrics(total_orders=1000, total_disputes=10))
        assert abs(r.dispute_rate - 0.010) < 0.0001

    def test_win_rate_computed(self):
        r = benchmark_merchant(_metrics(disputes_won=8, disputes_fought=10))
        assert abs(r.win_rate - 0.80) < 0.0001

    def test_inr_share_computed(self):
        r = benchmark_merchant(_metrics(inr_disputes=5, total_disputes=10))
        assert abs(r.inr_share - 0.50) < 0.0001

    # ─── Overall health tier ──────────────────────────────────────────────────

    def test_excellent_merchant_gets_good_tier(self):
        # Very low dispute rate, high win rate
        r = benchmark_merchant(_metrics(
            total_orders=10000,
            total_disputes=20,       # 0.2% — excellent
            disputes_won=18,
            disputes_fought=20,
            disputes_escalated=0,
            avg_response_days=1.5,
            inr_disputes=8,
            unauthorized_disputes=3,
        ))
        assert r.overall_health_tier in (BenchmarkTier.EXCELLENT, BenchmarkTier.GOOD)

    def test_bad_merchant_gets_poor_tier(self):
        # High dispute rate, low win rate
        r = benchmark_merchant(_metrics(
            total_orders=1000,
            total_disputes=50,       # 5% — terrible
            disputes_won=10,
            disputes_fought=50,
            disputes_escalated=10,
            avg_response_days=12.0,
        ))
        assert r.overall_health_tier in (BenchmarkTier.POOR, BenchmarkTier.CRITICAL)

    # ─── Risk flags ───────────────────────────────────────────────────────────

    def test_approaching_chargeback_flagged(self):
        r = benchmark_merchant(_metrics(
            total_orders=1000,
            total_disputes=15,  # 1.5%
        ))
        assert RiskFlag.APPROACHING_CHARGEBACK_THRESHOLD in r.risk_flags

    def test_no_flags_for_excellent_merchant(self):
        r = benchmark_merchant(_metrics(
            total_orders=10000,
            total_disputes=15,   # 0.15%
            disputes_won=13,
            disputes_fought=15,
            disputes_escalated=0,
            avg_response_days=1.5,
            inr_disputes=5,
            unauthorized_disputes=2,
        ))
        assert len(r.risk_flags) == 0

    def test_slow_response_flagged(self):
        r = benchmark_merchant(_metrics(avg_response_days=15.0))
        assert RiskFlag.SLOW_RESPONSE_TIME in r.risk_flags

    # ─── Benchmark item structure ─────────────────────────────────────────────

    def test_dispute_rate_benchmark_present(self):
        r = benchmark_merchant(_metrics())
        names = [b.metric for b in r.benchmarks]
        assert "dispute_rate" in names

    def test_win_rate_benchmark_present(self):
        r = benchmark_merchant(_metrics())
        names = [b.metric for b in r.benchmarks]
        assert "win_rate" in names

    def test_benchmark_has_peer_p50(self):
        r = benchmark_merchant(_metrics())
        for b in r.benchmarks:
            assert b.peer_p50 >= 0.0

    def test_benchmark_tier_valid(self):
        r = benchmark_merchant(_metrics())
        valid = set(BenchmarkTier)
        for b in r.benchmarks:
            assert b.tier in valid

    # ─── Segment / category variation ────────────────────────────────────────

    def test_different_segments_produce_different_p50(self):
        enterprise = benchmark_merchant(_metrics(segment=MerchantSegment.ENTERPRISE))
        micro = benchmark_merchant(_metrics(segment=MerchantSegment.MICRO))
        ent_dr = next(b.peer_p50 for b in enterprise.benchmarks if b.metric == "dispute_rate")
        micro_dr = next(b.peer_p50 for b in micro.benchmarks if b.metric == "dispute_rate")
        assert micro_dr > ent_dr  # MICRO has higher median dispute rate

    # ─── PayPal signal & coaching ─────────────────────────────────────────────

    def test_paypal_signal_populated(self):
        r = benchmark_merchant(_metrics())
        assert len(r.paypal_signal) > 20

    def test_critical_signal_mentions_merchant(self):
        r = benchmark_merchant(_metrics(
            total_orders=1000,
            total_disputes=50,
            disputes_won=5,
            disputes_fought=50,
            disputes_escalated=10,
        ))
        assert "MERCH_X" in r.paypal_signal

    def test_merchant_coaching_populated(self):
        r = benchmark_merchant(_metrics())
        assert len(r.merchant_coaching) > 10

    # ─── Summary ─────────────────────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = benchmark_merchant(_metrics())
        assert "MERCH_X" in r.summary

    def test_summary_contains_dispute_rate(self):
        r = benchmark_merchant(_metrics())
        assert "dispute_rate" in r.summary
