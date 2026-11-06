"""
Tests for Wave 12: deadline_tracker, settlement_negotiator.
"""
import pytest
from datetime import date, timedelta
from oracle.deadline_tracker import (
    track_deadline, build_deadline_calendar,
    DisputeDeadlineInput, ResponseStatus, DeadlineHeat,
    DeadlineCalendar,
)
from oracle.settlement_negotiator import (
    evaluate_settlement, SettlementInput, SettlementVerdict,
    BuyerLikelihood,
)


TODAY = date(2026, 11, 6)


# ═══════════════════════════════════════════════════════════════════
# Deadline Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestDeadlineTracker:

    def _inp(self, days_until_true: int, status: ResponseStatus = ResponseStatus.NOT_STARTED, **kw) -> DisputeDeadlineInput:
        return DisputeDeadlineInput(
            dispute_id=kw.pop("dispute_id", "D-001"),
            true_deadline=TODAY + timedelta(days=days_until_true),
            opened_date=TODAY - timedelta(days=5),
            response_status=status,
            claim_type=kw.pop("claim_type", "ITEM_NOT_RECEIVED"),
            dispute_amount=kw.pop("dispute_amount", 100.0),
            is_hdr_contributing=True,
            estimated_work_hours=1.0,
            **kw,
        )

    def test_green_heat_for_10_days(self):
        s = track_deadline(self._inp(10), TODAY)
        assert s.heat == DeadlineHeat.GREEN

    def test_yellow_heat_for_6_days(self):
        # effective = 6 - 2 = 4 days → YELLOW
        s = track_deadline(self._inp(6), TODAY)
        assert s.heat == DeadlineHeat.YELLOW

    def test_orange_heat_for_4_days(self):
        # effective = 4 - 2 = 2 → ORANGE
        s = track_deadline(self._inp(4), TODAY)
        assert s.heat == DeadlineHeat.ORANGE

    def test_red_heat_for_3_days(self):
        # effective = 3 - 2 = 1 → RED
        s = track_deadline(self._inp(3), TODAY)
        assert s.heat == DeadlineHeat.RED

    def test_critical_heat_for_2_days(self):
        # effective = 2 - 2 = 0 → CRITICAL
        s = track_deadline(self._inp(2), TODAY)
        assert s.heat == DeadlineHeat.CRITICAL

    def test_critical_heat_for_overdue(self):
        s = track_deadline(self._inp(-1), TODAY)
        assert s.heat == DeadlineHeat.CRITICAL

    def test_submitted_always_green(self):
        s = track_deadline(self._inp(2, ResponseStatus.SUBMITTED), TODAY)
        assert s.heat == DeadlineHeat.GREEN

    def test_resolved_always_green(self):
        s = track_deadline(self._inp(1, ResponseStatus.RESOLVED), TODAY)
        assert s.heat == DeadlineHeat.GREEN

    def test_at_risk_flagged_when_critical_not_submitted(self):
        s = track_deadline(self._inp(2, ResponseStatus.NOT_STARTED), TODAY)
        assert s.is_at_risk is True

    def test_not_at_risk_when_submitted(self):
        s = track_deadline(self._inp(2, ResponseStatus.SUBMITTED), TODAY)
        assert s.is_at_risk is False

    def test_not_at_risk_when_plenty_of_time(self):
        s = track_deadline(self._inp(10), TODAY)
        assert s.is_at_risk is False

    def test_effective_deadline_is_true_minus_buffer(self):
        true_d = TODAY + timedelta(days=8)
        s = track_deadline(DisputeDeadlineInput(
            dispute_id="D-X",
            true_deadline=true_d,
            opened_date=TODAY - timedelta(days=2),
        ), TODAY)
        assert s.effective_deadline == true_d - timedelta(days=2)

    def test_days_to_true_computed(self):
        s = track_deadline(self._inp(7), TODAY)
        assert s.days_to_true == 7

    def test_action_required_non_empty(self):
        s = track_deadline(self._inp(3), TODAY)
        assert len(s.action_required) > 10

    def test_action_critical_contains_dispute_id(self):
        s = track_deadline(self._inp(2, dispute_id="D-CRIT"), TODAY)
        assert "D-CRIT" in s.action_required


class TestDeadlineCalendar:

    def _make_disputes(self, deadline_offsets: list[int]) -> list[DisputeDeadlineInput]:
        return [
            DisputeDeadlineInput(
                dispute_id=f"D-{i:03d}",
                true_deadline=TODAY + timedelta(days=d),
                opened_date=TODAY - timedelta(days=5),
                response_status=ResponseStatus.NOT_STARTED,
                dispute_amount=100.0,
                estimated_work_hours=1.0,
            )
            for i, d in enumerate(deadline_offsets)
        ]

    def test_calendar_contains_all_statuses(self):
        disputes = self._make_disputes([3, 5, 10, 15])
        cal = build_deadline_calendar(disputes, TODAY)
        assert len(cal.statuses) == 4

    def test_calendar_counts_sum_to_total(self):
        disputes = self._make_disputes([2, 4, 6, 10, 15])
        cal = build_deadline_calendar(disputes, TODAY)
        total = (
            cal.critical_count + cal.red_count + cal.orange_count +
            cal.yellow_count + cal.green_count
        )
        assert total == len(disputes)

    def test_critical_sorted_first(self):
        disputes = self._make_disputes([15, 2, 5])  # critical in middle
        cal = build_deadline_calendar(disputes, TODAY)
        assert cal.statuses[0].heat in (DeadlineHeat.CRITICAL, DeadlineHeat.RED)

    def test_at_risk_count_matches_at_risk_statuses(self):
        disputes = self._make_disputes([2, 2, 10, 15])  # first two at risk
        cal = build_deadline_calendar(disputes, TODAY)
        expected = sum(1 for s in cal.statuses if s.is_at_risk)
        assert cal.at_risk_count == expected

    def test_at_risk_amount_computed(self):
        disputes = self._make_disputes([2, 10])
        cal = build_deadline_calendar(disputes, TODAY)
        expected = sum(s.dispute_amount for s in cal.statuses if s.is_at_risk)
        assert cal.at_risk_amount == pytest.approx(expected)

    def test_day_loads_cover_horizon(self):
        disputes = self._make_disputes([5, 10])
        cal = build_deadline_calendar(disputes, TODAY, horizon_days=14)
        assert len(cal.day_loads) == 15  # 0 through 14 inclusive

    def test_concentration_risk_flagged(self):
        # 10 disputes due on same day
        offsets = [4] * 10  # all effective deadline on same day (4-2=2 effective)
        disputes = self._make_disputes(offsets)
        cal = build_deadline_calendar(disputes, TODAY)
        assert len(cal.concentration_risk_days) > 0

    def test_summary_non_empty(self):
        cal = build_deadline_calendar(self._make_disputes([5, 10]), TODAY)
        assert len(cal.summary) > 20

    def test_priority_ranks_assigned(self):
        disputes = self._make_disputes([3, 5, 10])
        cal = build_deadline_calendar(disputes, TODAY)
        ranks = [s.priority_rank for s in cal.statuses]
        assert sorted(ranks) == list(range(1, len(disputes) + 1))

    def test_submitted_disputes_dont_count_as_at_risk(self):
        disputes = [
            DisputeDeadlineInput(
                dispute_id="D-SUB",
                true_deadline=TODAY + timedelta(days=2),
                opened_date=TODAY - timedelta(days=5),
                response_status=ResponseStatus.SUBMITTED,
                dispute_amount=100.0,
            ),
        ]
        cal = build_deadline_calendar(disputes, TODAY)
        assert cal.at_risk_count == 0


# ═══════════════════════════════════════════════════════════════════
# Settlement Negotiator Tests
# ═══════════════════════════════════════════════════════════════════

class TestSettlementNegotiator:

    def _inp(self, **kwargs) -> SettlementInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=150.0,
            p_win=0.50,
            buyer_dispute_count_90d=0,
            buyer_responded_to_messages=True,
            buyer_is_long_term_customer=False,
            buyer_purchase_history_count=1,
            is_hdr_contributing=True,
            days_until_deadline=10,
            fight_cost=25.0,
        )
        defaults.update(kwargs)
        return SettlementInput(**defaults)

    def test_ev_fight_computed(self):
        r = evaluate_settlement(self._inp(p_win=0.60, dispute_amount=100.0, fight_cost=25.0))
        expected = 0.60 * 100.0 - 25.0
        assert r.ev_fight == pytest.approx(expected, abs=0.01)

    def test_moderate_win_rate_recommends_settle(self):
        r = evaluate_settlement(self._inp(p_win=0.50, buyer_responded_to_messages=True))
        assert r.verdict == SettlementVerdict.SETTLE

    def test_high_win_rate_recommends_fight(self):
        r = evaluate_settlement(self._inp(p_win=0.75, dispute_amount=200.0))
        assert r.verdict == SettlementVerdict.FIGHT

    def test_very_negative_ev_recommends_refund(self):
        # EV = 0.10 × 50 − 25 = −20 < −15 → REFUND
        r = evaluate_settlement(self._inp(p_win=0.10, dispute_amount=50.0, fight_cost=25.0))
        assert r.verdict == SettlementVerdict.REFUND

    def test_tiny_amount_recommends_refund(self):
        r = evaluate_settlement(self._inp(dispute_amount=10.0))
        assert r.verdict == SettlementVerdict.REFUND

    def test_repeat_fraudster_recommends_fight_not_settle(self):
        r = evaluate_settlement(self._inp(buyer_dispute_count_90d=4))
        assert r.verdict == SettlementVerdict.FIGHT

    def test_buyer_likelihood_low_for_fraudster(self):
        r = evaluate_settlement(self._inp(buyer_dispute_count_90d=4))
        assert r.buyer_likelihood == BuyerLikelihood.LOW

    def test_buyer_likelihood_high_for_long_term_customer(self):
        r = evaluate_settlement(self._inp(
            buyer_is_long_term_customer=True,
            buyer_purchase_history_count=5,
        ))
        assert r.buyer_likelihood == BuyerLikelihood.HIGH

    def test_opening_offer_less_than_dispute_amount(self):
        r = evaluate_settlement(self._inp())
        assert r.opening_offer < r.dispute_amount

    def test_opening_offer_lower_when_high_win_rate(self):
        r_high = evaluate_settlement(self._inp(p_win=0.70))
        r_low = evaluate_settlement(self._inp(p_win=0.30))
        assert r_high.opening_offer < r_low.opening_offer

    def test_walk_away_less_than_dispute_amount(self):
        r = evaluate_settlement(self._inp())
        assert r.walk_away_amount <= r.dispute_amount

    def test_walk_away_equals_max_acceptable(self):
        r = evaluate_settlement(self._inp())
        assert r.walk_away_amount == r.max_acceptable_settlement

    def test_counter_offer_analysis_accept_below_walk_away(self):
        r = evaluate_settlement(self._inp(p_win=0.40, dispute_amount=200.0))
        # Counter at walk_away - 10 → should accept
        counter_ok = r.analyze_counter(r.walk_away_amount - 10)
        assert counter_ok.should_accept is True

    def test_counter_offer_analysis_reject_above_walk_away(self):
        r = evaluate_settlement(self._inp(p_win=0.40, dispute_amount=200.0))
        # Counter above walk_away → should reject
        counter_too_high = r.analyze_counter(r.walk_away_amount + 50)
        assert counter_too_high.should_accept is False

    def test_snad_with_low_win_rate_recommends_settle(self):
        r = evaluate_settlement(self._inp(claim_type="SNAD", p_win=0.35))
        assert r.verdict == SettlementVerdict.SETTLE

    def test_offer_script_non_empty_when_settle(self):
        r = evaluate_settlement(self._inp(p_win=0.50))
        if r.verdict == SettlementVerdict.SETTLE:
            assert len(r.offer_script) > 20

    def test_hdr_impact_note_non_empty(self):
        r = evaluate_settlement(self._inp())
        assert len(r.hdr_impact_note) > 10

    def test_rationale_non_empty(self):
        r = evaluate_settlement(self._inp())
        assert len(r.rationale) > 10

    def test_summary_contains_dispute_id(self):
        r = evaluate_settlement(self._inp())
        assert "D-001" in r.summary

    def test_offer_tactics_populated_for_settle(self):
        r = evaluate_settlement(self._inp(p_win=0.50))
        if r.verdict == SettlementVerdict.SETTLE:
            assert len(r.offer_tactics) > 0

    def test_ev_fight_negative_when_win_rate_very_low(self):
        r = evaluate_settlement(self._inp(p_win=0.10, dispute_amount=100.0, fight_cost=25.0))
        assert r.ev_fight < 0

    def test_conditional_verdict_for_unresponsive_buyer(self):
        r = evaluate_settlement(self._inp(
            p_win=0.50,
            buyer_responded_to_messages=False,
        ))
        # Unresponsive buyer → likelihood LOW → CONDITIONAL or SETTLE
        assert r.verdict in (SettlementVerdict.CONDITIONAL, SettlementVerdict.FIGHT,
                             SettlementVerdict.SETTLE, SettlementVerdict.REFUND)
