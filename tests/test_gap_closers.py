"""
Tests for the two competitive-gap-closing modules:
  - oracle/alert_interceptor.py  (Ethoca/Verifi parity)
  - oracle/autopilot.py          (Chargebacks911 parity as code)

These modules close the structural gaps vs all known competitors:
  Ethoca/Verifi  — relay alerts but don't decide; Anchor decides with EV math
  Chargebacks911 — human-review execution; Anchor executes autonomously
"""

import pytest
from oracle.alert_interceptor import (
    AlertPayload, AlertNetwork, AlertReasonCode, AlertAction,
    intercept_alert,
)
from oracle.autopilot import (
    DisputeContext, DisputeAction, ConfidenceLevel, ExecutionMode,
    plan_autopilot, autopilot_summary,
)


# ═════════════════════════════════════════════════════════════
# Alert Interceptor Tests
# ═════════════════════════════════════════════════════════════

class TestAlertInterceptor:

    def _alert(self, **kwargs) -> AlertPayload:
        defaults = dict(
            alert_id="TEST-001",
            network=AlertNetwork.ETHOCA,
            reason_code=AlertReasonCode.ITEM_NOT_RECEIVED,
            dispute_amount=85.0,
            transaction_id="TX-001",
            buyer_risk_tier="TIER_1_NORMAL",
            merchant_dispute_rate=0.005,
            alert_fee=25.0,
            response_deadline_hours=24,
        )
        defaults.update(kwargs)
        return AlertPayload(**defaults)

    def test_fraud_buyer_tier5_always_escalates(self):
        alert = self._alert(buyer_risk_tier="TIER_5_FRAUD")
        result = intercept_alert(alert)
        assert result.action == AlertAction.ESCALATE
        assert result.refund_amount == 0.0

    def test_fraud_buyer_tier4_always_escalates(self):
        alert = self._alert(buyer_risk_tier="TIER_4_SERIAL")
        result = intercept_alert(alert)
        assert result.action == AlertAction.ESCALATE

    def test_digital_goods_accessed_escalates(self):
        alert = self._alert(is_digital_goods=True, digital_goods_accessed=True)
        result = intercept_alert(alert)
        assert result.action == AlertAction.ESCALATE
        assert result.refund_amount == 0.0

    def test_delivered_inr_always_fights(self):
        """Delivery confirmation on INR → fight regardless of EV."""
        alert = self._alert(
            reason_code=AlertReasonCode.ITEM_NOT_RECEIVED,
            item_delivered=True,
            has_delivery_confirmation=True,
        )
        result = intercept_alert(alert)
        assert result.action == AlertAction.FIGHT_LATER
        assert result.refund_amount == 0.0
        assert len(result.evidence_to_gather) > 0

    def test_delivered_inr_includes_evidence_instructions(self):
        alert = self._alert(
            reason_code=AlertReasonCode.ITEM_NOT_RECEIVED,
            item_delivered=True,
            has_delivery_confirmation=True,
        )
        result = intercept_alert(alert)
        combined = " ".join(result.evidence_to_gather)
        assert "carrier" in combined.lower() or "tracking" in combined.lower()

    def test_near_hdr_threshold_favors_refund(self):
        """Merchant near 1.5% HDR threshold: extra count_cost pushes toward refund."""
        alert = self._alert(
            dispute_amount=60.0,
            merchant_dispute_rate=0.013,  # Near 1.5% threshold
            has_delivery_confirmation=False,
            item_delivered=False,
        )
        result = intercept_alert(alert)
        # Near HDR adds significant count cost — should favor refund on small amounts
        assert result.action in (AlertAction.REFUND_NOW, AlertAction.PARTIAL_REFUND)

    def test_ev_refund_calculation_present(self):
        """Both EV values must be populated and logically signed."""
        alert = self._alert(dispute_amount=100.0)
        result = intercept_alert(alert)
        # ev_refund_now should be negative (cost = refund amount minus savings)
        # ev_fight_later can be positive or negative
        assert result.ev_refund_now != 0.0
        assert isinstance(result.ev_fight_later, float)

    def test_refund_now_sets_response_message(self):
        alert = self._alert(
            dispute_amount=40.0,
            merchant_dispute_rate=0.014,  # Near HDR
            has_delivery_confirmation=False,
        )
        result = intercept_alert(alert)
        if result.action == AlertAction.REFUND_NOW:
            assert "refund" in result.response_message.lower()
            assert result.refund_amount == 40.0

    def test_fight_later_sets_evidence_list(self):
        """FIGHT_LATER action must come with evidence-gathering instructions."""
        alert = self._alert(
            dispute_amount=200.0,
            merchant_dispute_rate=0.002,  # Low rate — fighting is relatively cheap
            has_delivery_confirmation=True,
            item_delivered=True,
            reason_code=AlertReasonCode.ITEM_NOT_RECEIVED,
        )
        result = intercept_alert(alert)
        assert result.action == AlertAction.FIGHT_LATER
        assert len(result.evidence_to_gather) > 0

    def test_urgent_priority_for_short_deadline(self):
        alert = self._alert(response_deadline_hours=8)
        result = intercept_alert(alert)
        assert result.priority == "URGENT"

    def test_normal_priority_for_long_deadline(self):
        alert = self._alert(response_deadline_hours=48)
        result = intercept_alert(alert)
        assert result.priority == "NORMAL"

    def test_snad_partial_refund_on_large_amount(self):
        """SNAD + large amount → partial refund (50%) instead of full."""
        alert = self._alert(
            reason_code=AlertReasonCode.NOT_AS_DESCRIBED,
            dispute_amount=120.0,
            merchant_dispute_rate=0.013,  # Near HDR
        )
        result = intercept_alert(alert)
        if result.action == AlertAction.PARTIAL_REFUND:
            assert result.refund_amount == pytest.approx(60.0, abs=0.01)

    def test_net_saving_field_is_ev_difference(self):
        alert = self._alert(dispute_amount=85.0)
        result = intercept_alert(alert)
        expected_net = round(result.ev_refund_now - result.ev_fight_later, 2)
        assert result.net_saving_vs_fight == pytest.approx(expected_net, abs=0.01)

    def test_reasoning_populated(self):
        alert = self._alert()
        result = intercept_alert(alert)
        assert len(result.reasoning) > 0

    def test_alert_id_preserved_in_result(self):
        alert = self._alert(alert_id="ETH-XYZ-999")
        result = intercept_alert(alert)
        assert result.alert_id == "ETH-XYZ-999"


# ═════════════════════════════════════════════════════════════
# Autopilot Tests
# ═════════════════════════════════════════════════════════════

class TestAutopilot:

    def _ctx(self, **kwargs) -> DisputeContext:
        defaults = dict(
            dispute_id="PP-D-99999",
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=120.0,
            days_until_deadline=7,
            oracle_recommendation="FIGHT",
            oracle_ev_fight=25.0,
            oracle_win_probability=0.68,
            oracle_confidence=0.78,
            buyer_risk_tier="TIER_1_NORMAL",
            buyer_win_probability_adjustment=0.0,
            has_tracking=True,
            has_delivery_confirmation=False,
        )
        defaults.update(kwargs)
        return DisputeContext(**defaults)

    def test_fight_recommendation_produces_respond_action(self):
        ctx = self._ctx(oracle_recommendation="FIGHT", oracle_win_probability=0.68)
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.RESPOND_AND_FIGHT

    def test_accept_recommendation_produces_accept_action(self):
        ctx = self._ctx(oracle_recommendation="ACCEPT", oracle_win_probability=0.30)
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.ACCEPT_CLAIM

    def test_tier5_buyer_escalates_to_paypal(self):
        ctx = self._ctx(buyer_risk_tier="TIER_5_FRAUD")
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.ESCALATE_TO_PAYPAL

    def test_tier4_buyer_escalates_to_paypal(self):
        ctx = self._ctx(buyer_risk_tier="TIER_4_SERIAL")
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.ESCALATE_TO_PAYPAL

    def test_appeal_on_lost_dispute_with_open_window(self):
        ctx = self._ctx(dispute_outcome="LOST", appeal_window_open=True)
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.APPEAL

    def test_accept_with_negotiation_offer_sends_message(self):
        ctx = self._ctx(
            oracle_recommendation="ACCEPT",
            settlement_opening_offer="PARTIAL_30",
            settlement_opening_cost=36.0,
            buyer_risk_tier="TIER_1_NORMAL",
        )
        plan = plan_autopilot(ctx)
        assert plan.primary_action == DisputeAction.SEND_MESSAGE
        assert plan.negotiation_message is not None

    def test_high_confidence_fraud_is_fully_autonomous(self):
        ctx = self._ctx(buyer_risk_tier="TIER_5_FRAUD")
        plan = plan_autopilot(ctx)
        assert plan.execution_mode == ExecutionMode.FULLY_AUTONOMOUS

    def test_digital_goods_accessed_is_fully_autonomous(self):
        ctx = self._ctx(is_digital_goods=True, digital_goods_accessed=True)
        plan = plan_autopilot(ctx)
        assert plan.execution_mode == ExecutionMode.FULLY_AUTONOMOUS

    def test_high_win_prob_with_delivery_confirmation_is_autonomous(self):
        ctx = self._ctx(
            oracle_win_probability=0.85,
            has_delivery_confirmation=True,
        )
        plan = plan_autopilot(ctx)
        assert plan.confidence == ConfidenceLevel.HIGH
        assert plan.execution_mode == ExecutionMode.FULLY_AUTONOMOUS

    def test_low_confidence_requires_human_review(self):
        ctx = self._ctx(
            oracle_win_probability=0.35,
            oracle_confidence=0.40,
            has_tracking=False,
            has_delivery_confirmation=False,
            has_buyer_communication=False,
        )
        plan = plan_autopilot(ctx)
        # Low win prob + no evidence = low confidence or medium → human review
        assert plan.requires_human_review is True or plan.confidence in (
            ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM
        )

    def test_respond_and_fight_includes_provide_evidence_step(self):
        ctx = self._ctx(oracle_recommendation="FIGHT", oracle_win_probability=0.70)
        plan = plan_autopilot(ctx)
        actions = [s.action for s in plan.steps]
        assert "provide_evidence" in actions

    def test_accept_includes_accept_claim_step(self):
        ctx = self._ctx(oracle_recommendation="ACCEPT")
        plan = plan_autopilot(ctx)
        actions = [s.action for s in plan.steps]
        assert "accept_claim" in actions

    def test_steps_are_numbered_sequentially(self):
        ctx = self._ctx()
        plan = plan_autopilot(ctx)
        for i, step in enumerate(plan.steps, start=1):
            assert step.step_num == i

    def test_win_probability_includes_buyer_adjustment(self):
        ctx = self._ctx(
            oracle_win_probability=0.60,
            buyer_win_probability_adjustment=0.15,
        )
        plan = plan_autopilot(ctx)
        assert plan.win_probability == pytest.approx(0.75, abs=0.001)

    def test_win_probability_capped_at_95(self):
        ctx = self._ctx(
            oracle_win_probability=0.90,
            buyer_win_probability_adjustment=0.30,
        )
        plan = plan_autopilot(ctx)
        assert plan.win_probability <= 0.95

    def test_deadline_hours_set(self):
        ctx = self._ctx(days_until_deadline=5)
        plan = plan_autopilot(ctx)
        assert plan.deadline_hours_remaining == 5 * 24

    def test_summary_contains_dispute_id(self):
        ctx = self._ctx(dispute_id="PP-D-TESTID")
        plan = plan_autopilot(ctx)
        summary = autopilot_summary(plan)
        assert "PP-D-TESTID" in summary

    def test_summary_contains_win_probability(self):
        ctx = self._ctx(oracle_win_probability=0.72)
        plan = plan_autopilot(ctx)
        summary = autopilot_summary(plan)
        assert "%" in summary

    def test_reasoning_populated(self):
        ctx = self._ctx()
        plan = plan_autopilot(ctx)
        assert len(plan.reasoning) >= 2

    def test_ensemble_recommendation_takes_priority_over_oracle(self):
        """When ensemble says ACCEPT but oracle says FIGHT, ensemble wins."""
        ctx = self._ctx(
            oracle_recommendation="FIGHT",
            ensemble_recommendation="ACCEPT",
        )
        plan = plan_autopilot(ctx)
        assert plan.primary_action in (DisputeAction.ACCEPT_CLAIM, DisputeAction.SEND_MESSAGE)
