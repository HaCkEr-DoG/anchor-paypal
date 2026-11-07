"""Wave 86 — Dispute Deadline Prioritizer tests."""
import math
import pytest
from oracle.dispute_deadline_prioritizer import (
    UrgencyLabel,
    OpenDispute, PrioritizedDispute, DeadlinePriorityReport,
    prioritize_disputes,
    _urgency_score, _ev_score, _type_weight, _urgency_label, _action_rec,
)


def _d(dispute_id="D-001", claim_type="INR", amount_usd=100.0,
       days=5, win_prob=0.50, evidence_ready=False):
    return OpenDispute(
        dispute_id=dispute_id,
        claim_type=claim_type,
        amount_usd=amount_usd,
        days_until_deadline=days,
        win_probability=win_prob,
        evidence_ready=evidence_ready,
    )


# ─── Urgency score (exponential decay) ────────────────────────────────────────

class TestUrgencyScore:
    def test_zero_days_returns_one(self):
        assert _urgency_score(0) == 1.0

    def test_negative_days_returns_one(self):
        assert _urgency_score(-1) == 1.0

    def test_three_days_approx_third(self):
        # exp(-3/3) = exp(-1) ≈ 0.368
        assert abs(_urgency_score(3) - math.exp(-1)) < 1e-9

    def test_seven_days_low(self):
        # exp(-7/3) ≈ 0.097
        assert _urgency_score(7) < 0.15

    def test_monotone_decreasing(self):
        scores = [_urgency_score(d) for d in range(0, 15)]
        assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))


# ─── EV score ─────────────────────────────────────────────────────────────────

class TestEvScore:
    def test_zero_amount_zero_ev(self):
        assert _ev_score(0.9, 0.0) == 0.0

    def test_zero_prob_zero_ev(self):
        assert _ev_score(0.0, 500.0) == 0.0

    def test_reference_exact_one(self):
        # ev = 1.0 * 500 = 500 → capped at 1.0
        assert _ev_score(1.0, 500.0) == 1.0

    def test_above_reference_capped(self):
        assert _ev_score(1.0, 1000.0) == 1.0

    def test_half_reference(self):
        assert abs(_ev_score(0.5, 500.0) - 0.5) < 1e-9


# ─── Claim type weights ───────────────────────────────────────────────────────

class TestTypeWeight:
    def test_unauth_highest(self):
        assert _type_weight("UNAUTH") == 1.00

    def test_inr_second(self):
        assert _type_weight("INR") == 0.80

    def test_snad_third(self):
        assert _type_weight("SNAD") == 0.60

    def test_other_lowest(self):
        assert _type_weight("OTHER") == 0.50

    def test_unknown_falls_to_other(self):
        assert _type_weight("UNKNOWN_CLAIM") == _type_weight("OTHER")

    def test_case_insensitive(self):
        assert _type_weight("unauth") == _type_weight("UNAUTH")


# ─── Urgency labels ───────────────────────────────────────────────────────────

class TestUrgencyLabel:
    def test_zero_days_critical(self):
        assert _urgency_label(0) == UrgencyLabel.CRITICAL

    def test_one_day_critical(self):
        assert _urgency_label(1) == UrgencyLabel.CRITICAL

    def test_two_days_high(self):
        assert _urgency_label(2) == UrgencyLabel.HIGH

    def test_three_days_high(self):
        assert _urgency_label(3) == UrgencyLabel.HIGH

    def test_four_days_medium(self):
        assert _urgency_label(4) == UrgencyLabel.MEDIUM

    def test_seven_days_medium(self):
        assert _urgency_label(7) == UrgencyLabel.MEDIUM

    def test_eight_days_low(self):
        assert _urgency_label(8) == UrgencyLabel.LOW

    def test_thirty_days_low(self):
        assert _urgency_label(30) == UrgencyLabel.LOW


# ─── Action recommendations ───────────────────────────────────────────────────

class TestActionRecommendation:
    def test_critical_today_no_evidence(self):
        rec = _action_rec(UrgencyLabel.CRITICAL, 0, False)
        assert "today" in rec.lower()
        assert "gather" in rec.lower() or "evidence" in rec.lower()

    def test_critical_tomorrow_evidence_ready(self):
        rec = _action_rec(UrgencyLabel.CRITICAL, 1, True)
        assert "submit" in rec.lower()

    def test_high_evidence_ready_says_submit_today(self):
        rec = _action_rec(UrgencyLabel.HIGH, 2, True)
        assert "submit" in rec.lower() and "today" in rec.lower()

    def test_high_no_evidence_says_gather(self):
        rec = _action_rec(UrgencyLabel.HIGH, 3, False)
        assert "gather" in rec.lower()

    def test_medium_no_evidence_mentions_review(self):
        rec = _action_rec(UrgencyLabel.MEDIUM, 5, False)
        assert "review" in rec.lower() or "prepare" in rec.lower()

    def test_low_mentions_days(self):
        rec = _action_rec(UrgencyLabel.LOW, 14, False)
        assert "14" in rec


# ─── prioritize_disputes integration ─────────────────────────────────────────

class TestPrioritizeDisputes:
    def test_empty_returns_report(self):
        r = prioritize_disputes([])
        assert isinstance(r, DeadlinePriorityReport)
        assert len(r.disputes) == 0

    def test_single_dispute_rank_one(self):
        r = prioritize_disputes([_d()])
        assert len(r.disputes) == 1
        assert r.disputes[0].rank == 1

    def test_ranks_sequential(self):
        disputes = [_d(f"D-{i:03d}", days=i + 1) for i in range(5)]
        r = prioritize_disputes(disputes)
        ranks = [d.rank for d in r.disputes]
        assert ranks == list(range(1, 6))

    def test_critical_dispute_ranks_first(self):
        low = _d("low", days=20, amount_usd=10.0)
        critical = _d("crit", days=0, amount_usd=50.0)
        r = prioritize_disputes([low, critical])
        assert r.disputes[0].dispute_id == "crit"

    def test_unauth_outranks_other_at_same_deadline(self):
        other = _d("other", claim_type="OTHER", days=5)
        unauth = _d("unauth", claim_type="UNAUTH", days=5)
        r = prioritize_disputes([other, unauth])
        assert r.disputes[0].dispute_id == "unauth"

    def test_critical_count_correct(self):
        disputes = [
            _d("c1", days=0),
            _d("c2", days=1),
            _d("h1", days=2),
        ]
        r = prioritize_disputes(disputes)
        assert r.critical_count == 2

    def test_high_count_correct(self):
        disputes = [
            _d("h1", days=2),
            _d("h2", days=3),
            _d("m1", days=5),
        ]
        r = prioritize_disputes(disputes)
        assert r.high_count == 2

    def test_total_ev_at_risk(self):
        disputes = [
            _d("d1", amount_usd=100.0, win_prob=0.5),   # EV = 50
            _d("d2", amount_usd=200.0, win_prob=0.4),   # EV = 80
        ]
        r = prioritize_disputes(disputes)
        assert abs(r.total_ev_at_risk_usd - 130.0) < 0.01

    def test_recommendation_summary_non_empty(self):
        r = prioritize_disputes([_d()])
        assert len(r.recommendation_summary) > 10

    def test_critical_summary_mentions_critical(self):
        r = prioritize_disputes([_d("c1", days=0)])
        assert "CRITICAL" in r.recommendation_summary

    def test_no_critical_no_high_summary_mentions_monitored(self):
        r = prioritize_disputes([_d("m1", days=10)])
        assert "monitored" in r.recommendation_summary.lower()

    def test_priority_score_in_zero_one(self):
        disputes = [
            _d("d1", days=0, amount_usd=1000.0, claim_type="UNAUTH", win_prob=1.0),
            _d("d2", days=100, amount_usd=0.0, claim_type="OTHER", win_prob=0.0),
        ]
        r = prioritize_disputes(disputes)
        for pd in r.disputes:
            assert 0.0 <= pd.priority_score <= 1.0

    def test_evidence_ready_in_recommendation(self):
        r = prioritize_disputes([_d("d1", days=0, evidence_ready=True)])
        rec = r.disputes[0].action_recommendation
        assert "submit" in rec.lower()

    def test_dispute_fields_preserved(self):
        d = _d("TEST-999", claim_type="SNAD", amount_usd=99.0, days=4,
               win_prob=0.65, evidence_ready=True)
        r = prioritize_disputes([d])
        pd = r.disputes[0]
        assert pd.dispute_id == "TEST-999"
        assert pd.claim_type == "SNAD"
        assert pd.amount_usd == 99.0
        assert pd.win_probability == 0.65
        assert pd.evidence_ready is True
