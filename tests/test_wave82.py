"""Wave 82 — Buyer Pattern Analyzer tests."""
import pytest
from oracle.buyer_pattern_analyzer import (
    BuyerRiskLevel,
    BuyerPatternInput,
    DisputeRecord,
    analyze_buyer_pattern,
)


def _rec(dispute_id="PP-D-001", amount=100.0, reason="INR",
         merchant_won=True, escalated=False, days_ago=60):
    return DisputeRecord(
        dispute_id=dispute_id, amount_usd=amount, reason=reason,
        merchant_won=merchant_won, escalated=escalated, days_ago=days_ago,
    )


def _inp(total_tx=100, records=None, trust=None):
    return BuyerPatternInput(
        buyer_id="BUYER-TEST",
        total_transactions=total_tx,
        dispute_history=records or [],
        community_trust_score=trust,
    )


# ─── Rates ────────────────────────────────────────────────────────────────────

class TestRates:
    def test_dispute_rate_zero_when_no_disputes(self):
        r = analyze_buyer_pattern(_inp(total_tx=50, records=[]))
        assert r.dispute_rate == 0.0

    def test_dispute_rate_correct(self):
        records = [_rec(f"PP-D-{i}") for i in range(10)]
        r = analyze_buyer_pattern(_inp(total_tx=100, records=records))
        assert abs(r.dispute_rate - 0.10) < 0.001

    def test_merchant_win_rate_all_wins(self):
        records = [_rec(merchant_won=True) for _ in range(5)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.merchant_win_rate - 1.0) < 0.001

    def test_merchant_win_rate_all_losses(self):
        records = [_rec(merchant_won=False) for _ in range(5)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.merchant_win_rate - 0.0) < 0.001

    def test_escalation_rate_correct(self):
        records = [_rec(escalated=True), _rec(escalated=True), _rec(escalated=False)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.escalation_rate - 2/3) < 0.01


# ─── Risk scoring ─────────────────────────────────────────────────────────────

class TestRiskScoring:
    def test_zero_disputes_zero_risk(self):
        r = analyze_buyer_pattern(_inp(records=[]))
        assert r.risk_score == 0.0
        assert r.risk_level == BuyerRiskLevel.LOW

    def test_high_dispute_rate_raises_score(self):
        many = [_rec(f"PP-D-{i}", merchant_won=False, escalated=True) for i in range(40)]
        r = analyze_buyer_pattern(_inp(total_tx=50, records=many))
        assert r.risk_score > 0.5

    def test_low_dispute_rate_low_risk(self):
        few = [_rec("PP-D-1", merchant_won=True, escalated=False)]
        r = analyze_buyer_pattern(_inp(total_tx=500, records=few))
        assert r.risk_level == BuyerRiskLevel.LOW

    def test_risk_score_clamped_0_to_1(self):
        worst = [_rec(f"d{i}", merchant_won=False, escalated=True, days_ago=5)
                 for i in range(50)]
        r = analyze_buyer_pattern(_inp(total_tx=55, records=worst))
        assert 0.0 <= r.risk_score <= 1.0

    def test_serial_requires_high_score_and_rate(self):
        serial = [_rec(f"d{i}", merchant_won=False, escalated=True, days_ago=5)
                  for i in range(40)]
        r = analyze_buyer_pattern(_inp(total_tx=50, records=serial))
        assert r.risk_level in (BuyerRiskLevel.HIGH, BuyerRiskLevel.SERIAL)


# ─── Recency weighting ────────────────────────────────────────────────────────

class TestRecency:
    def test_recent_disputes_increase_recency_score(self):
        old_records = [_rec(days_ago=90), _rec(days_ago=120)]
        new_records = [_rec(days_ago=5), _rec(days_ago=10)]
        r_old = analyze_buyer_pattern(_inp(records=old_records))
        r_new = analyze_buyer_pattern(_inp(records=new_records))
        assert r_new.recency_score > r_old.recency_score

    def test_recency_score_range(self):
        records = [_rec(days_ago=5)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert 0.0 <= r.recency_score <= 1.0


# ─── Category diversity ───────────────────────────────────────────────────────

class TestCategoryDiversity:
    def test_single_reason_low_diversity(self):
        records = [_rec(reason="INR"), _rec(reason="INR"), _rec(reason="INR")]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.category_diversity - 1/3) < 0.01

    def test_all_different_reasons_high_diversity(self):
        records = [_rec(reason="INR"), _rec(reason="SNAD"), _rec(reason="UNAUTH")]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.category_diversity - 1.0) < 0.001


# ─── KERNEL trust adjustment ──────────────────────────────────────────────────

class TestKernelAdjustment:
    def test_high_trust_lowers_risk_score(self):
        records = [_rec(merchant_won=False, escalated=True) for _ in range(5)]
        r_no_trust = analyze_buyer_pattern(_inp(records=records, trust=None))
        r_trusted  = analyze_buyer_pattern(_inp(records=records, trust=0.95))
        assert r_trusted.risk_score <= r_no_trust.risk_score

    def test_low_trust_raises_risk_score(self):
        records = [_rec(merchant_won=False) for _ in range(5)]
        r_no_trust  = analyze_buyer_pattern(_inp(records=records, trust=None))
        r_untrusted = analyze_buyer_pattern(_inp(records=records, trust=0.05))
        assert r_untrusted.risk_score >= r_no_trust.risk_score

    def test_trust_adjustment_flag(self):
        records = [_rec()]
        r_yes = analyze_buyer_pattern(_inp(records=records, trust=0.5))
        r_no  = analyze_buyer_pattern(_inp(records=records, trust=None))
        assert r_yes.community_trust_adjusted is True
        assert r_no.community_trust_adjusted is False


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_buyer_id_echoed(self):
        r = analyze_buyer_pattern(_inp())
        assert r.buyer_id == "BUYER-TEST"

    def test_recommendation_non_empty(self):
        records = [_rec()]
        r = analyze_buyer_pattern(_inp(records=records))
        assert len(r.recommendation) > 10

    def test_paypal_signal_contains_buyer_id(self):
        records = [_rec()]
        r = analyze_buyer_pattern(_inp(records=records))
        assert "BUYER-TEST" in r.paypal_signal

    def test_total_disputes_matches_history_len(self):
        records = [_rec(f"d{i}") for i in range(7)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert r.total_disputes == 7

    def test_avg_amount_correct(self):
        records = [_rec(amount=100.0), _rec(amount=200.0)]
        r = analyze_buyer_pattern(_inp(records=records))
        assert abs(r.avg_claim_amount - 150.0) < 0.01
