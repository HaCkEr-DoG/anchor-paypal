"""
Tests for Wave 53: buyer_behavior_profiler, chargeback_cost_calculator.
"""
import pytest
from oracle.buyer_behavior_profiler import (
    profile_buyer, BuyerProfileInput, DisputeHistoryEntry,
    BuyerRiskClass, BehaviorPattern, HandlingTier,
)
from oracle.chargeback_cost_calculator import (
    calculate_chargeback_cost, ChargebackCostInput,
    OutcomePath, Network,
)

BASE_NOW = 1700000000
DAY = 86400


# ═══════════════════════════════════════════════════════════════════
# Buyer Behavior Profiler Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerBehaviorProfiler:

    def _entry(self, dispute_id="D-001", code="INR", amount=50.0,
               outcome="LOST", days_after=10, offset_days=30) -> DisputeHistoryEntry:
        return DisputeHistoryEntry(
            dispute_id=dispute_id,
            reason_code=code,
            amount=amount,
            outcome=outcome,
            days_after_purchase=days_after,
            epoch=BASE_NOW - offset_days * DAY,
        )

    def _inp(self, orders=50, spend=1000.0, disputes=0, age=365, **kw) -> BuyerProfileInput:
        defaults = dict(
            buyer_id="B-001",
            total_orders=orders,
            total_spend=spend,
            account_age_days=age,
            total_disputes=disputes,
        )
        defaults.update(kw)
        return BuyerProfileInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_buyer_id(self):
        r = profile_buyer(self._inp(buyer_id="B-XYZ"))
        assert "B-XYZ" in r.summary

    def test_score_bounded(self):
        r = profile_buyer(self._inp())
        assert 0 <= r.risk_score <= 100

    def test_patterns_non_empty(self):
        r = profile_buyer(self._inp())
        assert len(r.patterns_detected) >= 1

    # ── Risk classification ───────────────────────────────────────

    def test_trusted_buyer_zero_disputes(self):
        r = profile_buyer(self._inp(orders=100, disputes=0, age=730, spend=5000.0))
        assert r.risk_class == BuyerRiskClass.TRUSTED

    def test_fraudster_on_high_dispute_rate(self):
        history = [self._entry(f"D-{i}", offset_days=10 * i) for i in range(8)]
        r = profile_buyer(self._inp(
            orders=20, disputes=8,
            dispute_history=history,
        ))
        assert r.risk_class in (BuyerRiskClass.HIGH_RISK, BuyerRiskClass.FRAUDSTER)

    def test_elevated_on_moderate_disputes(self):
        history = [self._entry(f"D-{i}") for i in range(3)]
        r = profile_buyer(self._inp(orders=30, disputes=3, dispute_history=history))
        assert r.risk_class in (BuyerRiskClass.ELEVATED, BuyerRiskClass.HIGH_RISK)

    # ── Pattern detection ─────────────────────────────────────────

    def test_serial_returner_detected_on_multiple_inr(self):
        history = [
            self._entry("D-1", code="INR"),
            self._entry("D-2", code="INR"),
            self._entry("D-3", code="SNAD"),
        ]
        r = profile_buyer(self._inp(orders=30, disputes=3, dispute_history=history))
        patterns = {p.pattern for p in r.patterns_detected}
        assert BehaviorPattern.SERIAL_RETURNER in patterns

    def test_friendly_fraud_on_late_disputes(self):
        history = [
            self._entry("D-1", days_after=28),
            self._entry("D-2", days_after=27),
        ]
        r = profile_buyer(self._inp(orders=20, disputes=2, dispute_history=history))
        patterns = {p.pattern for p in r.patterns_detected}
        assert BehaviorPattern.FRIENDLY_FRAUD in patterns

    def test_subscription_abuser_detected(self):
        history = [
            self._entry("D-1", code="SUBSCRIPTION"),
            self._entry("D-2", code="SUBSCRIPTION"),
        ]
        r = profile_buyer(self._inp(orders=10, disputes=2, dispute_history=history))
        patterns = {p.pattern for p in r.patterns_detected}
        assert BehaviorPattern.SUBSCRIPTION_ABUSER in patterns

    def test_unauthorized_repeat_detected(self):
        history = [
            self._entry("D-1", code="UNAUTHORIZED"),
            self._entry("D-2", code="10.4"),
        ]
        r = profile_buyer(self._inp(orders=15, disputes=2, dispute_history=history))
        patterns = {p.pattern for p in r.patterns_detected}
        assert BehaviorPattern.UNAUTHORIZED_REPEAT in patterns

    def test_clean_on_no_disputes(self):
        r = profile_buyer(self._inp(orders=50, disputes=0))
        assert r.primary_pattern == BehaviorPattern.CLEAN

    # ── Handling tier ─────────────────────────────────────────────

    def test_trusted_gets_standard_handling(self):
        r = profile_buyer(self._inp(orders=100, disputes=0, age=730, spend=5000.0))
        assert r.handling_tier == HandlingTier.STANDARD

    def test_high_risk_gets_flag_account(self):
        history = [self._entry(f"D-{i}", offset_days=10 * i) for i in range(6)]
        r = profile_buyer(self._inp(orders=10, disputes=6, dispute_history=history))
        assert r.handling_tier in (HandlingTier.FLAG_ACCOUNT, HandlingTier.BLOCK_SALES)

    # ── Win rate modifier ─────────────────────────────────────────

    def test_trusted_has_positive_modifier(self):
        r = profile_buyer(self._inp(orders=100, disputes=0, age=730, spend=5000.0))
        assert r.win_rate_modifier >= 0

    def test_high_risk_has_positive_modifier(self):
        # High-risk → fight harder → positive modifier
        history = [self._entry(f"D-{i}", offset_days=10 * i) for i in range(6)]
        r = profile_buyer(self._inp(orders=10, disputes=6, dispute_history=history))
        assert r.win_rate_modifier >= 0  # Fight assertively

    # ── Dispute rate ──────────────────────────────────────────────

    def test_dispute_rate_computed(self):
        r = profile_buyer(self._inp(orders=100, disputes=5))
        assert abs(r.dispute_rate - 0.05) < 0.0001


# ═══════════════════════════════════════════════════════════════════
# Chargeback Cost Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackCostCalculator:

    def _inp(self, amount=100.0, network="VISA", win_prob=0.60, **kw) -> ChargebackCostInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=amount,
            network=network,
            win_prob_response=win_prob,
        )
        defaults.update(kw)
        return ChargebackCostInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calculate_chargeback_cost(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_seven_paths_computed(self):
        r = calculate_chargeback_cost(self._inp())
        assert len(r.paths) == 7

    def test_all_outcome_paths_present(self):
        r = calculate_chargeback_cost(self._inp())
        path_names = {p.path for p in r.paths}
        assert OutcomePath.ACCEPT_NOW in path_names
        assert OutcomePath.RESPOND_WIN in path_names
        assert OutcomePath.ARBITRATION_LOSE in path_names

    # ── Network fee defaults ──────────────────────────────────────

    def test_paypal_cb_fee_20(self):
        r = calculate_chargeback_cost(self._inp(network="PAYPAL"))
        assert r.chargeback_fee == 20.0

    def test_visa_cb_fee_15(self):
        r = calculate_chargeback_cost(self._inp(network="VISA"))
        assert r.chargeback_fee == 15.0

    def test_amex_cb_fee_25(self):
        r = calculate_chargeback_cost(self._inp(network="AMEX"))
        assert r.chargeback_fee == 25.0

    def test_visa_pre_arb_fee_150(self):
        r = calculate_chargeback_cost(self._inp(network="VISA"))
        assert r.pre_arb_fee == 150.0

    def test_paypal_no_arb_fee(self):
        r = calculate_chargeback_cost(self._inp(network="PAYPAL"))
        assert r.arbitration_fee == 0.0

    def test_visa_arb_fee_500(self):
        r = calculate_chargeback_cost(self._inp(network="VISA"))
        assert r.arbitration_fee == 500.0

    # ── Cost calculations ─────────────────────────────────────────

    def test_accept_cost_includes_cb_fee(self):
        r = calculate_chargeback_cost(self._inp(amount=100.0, network="PAYPAL"))
        assert r.cost_to_accept == pytest.approx(120.0)  # 100 + 20

    def test_respond_win_path_net_recovery_positive(self):
        r = calculate_chargeback_cost(self._inp(amount=200.0))
        win_path = next(p for p in r.paths if p.path == OutcomePath.RESPOND_WIN)
        assert win_path.net_recovery > 0

    def test_respond_lose_path_net_recovery_negative(self):
        r = calculate_chargeback_cost(self._inp())
        lose_path = next(p for p in r.paths if p.path == OutcomePath.RESPOND_LOSE)
        assert lose_path.net_recovery < 0

    def test_arb_win_costs_more_than_respond_win(self):
        r = calculate_chargeback_cost(self._inp(network="VISA"))
        arb_win = next(p for p in r.paths if p.path == OutcomePath.ARBITRATION_WIN)
        cb_win  = next(p for p in r.paths if p.path == OutcomePath.RESPOND_WIN)
        assert arb_win.total_cost > cb_win.total_cost

    # ── EV and decision ───────────────────────────────────────────

    def test_should_fight_at_high_win_prob(self):
        r = calculate_chargeback_cost(self._inp(amount=200.0, win_prob=0.80))
        assert r.should_fight is True

    def test_should_not_fight_at_low_win_prob(self):
        # Small amount: EV(fight) < EV(accept) at 10% win prob
        r = calculate_chargeback_cost(self._inp(amount=10.0, network="PAYPAL", win_prob=0.10))
        assert r.should_fight is False

    def test_break_even_bounded(self):
        r = calculate_chargeback_cost(self._inp())
        assert 0.0 <= r.break_even_win_prob <= 1.0

    def test_optimal_path_in_paths(self):
        r = calculate_chargeback_cost(self._inp())
        path_names = {p.path for p in r.paths}
        assert r.optimal_path in path_names

    def test_ev_respond_better_at_high_win_prob(self):
        low  = calculate_chargeback_cost(self._inp(win_prob=0.30))
        high = calculate_chargeback_cost(self._inp(win_prob=0.80))
        assert high.ev_respond > low.ev_respond

    # ── Fee overrides ─────────────────────────────────────────────

    def test_fee_override_respected(self):
        r = calculate_chargeback_cost(self._inp(
            network="VISA",
            chargeback_fee_override=10.0,
        ))
        assert r.chargeback_fee == 10.0

    def test_recommendation_non_empty(self):
        r = calculate_chargeback_cost(self._inp())
        assert len(r.recommendation) > 0
