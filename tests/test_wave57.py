"""
Tests for Wave 57: appeal_window_tracker, reason_code_strategy_advisor.
"""
import pytest
from oracle.appeal_window_tracker import (
    track_appeal_windows, AppealWindowInput,
    WindowStatus, DisputeStage, DAY_SECS,
)
from oracle.reason_code_strategy_advisor import (
    advise_reason_code_strategy, ReasonCodeStrategyInput,
    StrategyType, RiskFactor,
)


# ═══════════════════════════════════════════════════════════════════
# Appeal Window Tracker Tests
# ═══════════════════════════════════════════════════════════════════

def _aw(dispute_id="D-001", network="VISA", stage="CHARGEBACK",
        days_elapsed=5, **kw) -> AppealWindowInput:
    """Factory: stage_started_epoch anchored to 'days_elapsed days ago'."""
    base_now = 1_700_000_000
    start = base_now - days_elapsed * DAY_SECS
    defaults = dict(
        dispute_id=dispute_id,
        network=network,
        stage=stage,
        stage_started_epoch=start,
        now_epoch=base_now,
    )
    defaults.update(kw)
    return AppealWindowInput(**defaults)


class TestAppealWindowTracker:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = track_appeal_windows(_aw(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_network_in_summary(self):
        r = track_appeal_windows(_aw(network="VISA"))
        assert "VISA" in r.summary

    def test_current_stage_matches_input(self):
        r = track_appeal_windows(_aw(stage="CHARGEBACK"))
        assert r.current_stage == DisputeStage.CHARGEBACK

    # ── VISA chargeback window (20 days) ──────────────────────────

    def test_visa_cb_open_at_day5(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=5))
        assert r.current_window.status == WindowStatus.OPEN

    def test_visa_cb_days_remaining_correct(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=5))
        assert r.current_window.days_remaining == 15

    def test_visa_cb_expiring_at_day18(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=18))
        assert r.current_window.status in (WindowStatus.EXPIRING, WindowStatus.OPEN)

    def test_visa_cb_expiring_at_day19(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=19))
        assert r.current_window.status == WindowStatus.EXPIRING

    def test_visa_cb_expired_at_day21(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=21))
        assert r.current_window.status == WindowStatus.EXPIRED

    # ── PayPal — no pre-arb, no arb ───────────────────────────────

    def test_paypal_no_pre_arb(self):
        r = track_appeal_windows(_aw(network="PAYPAL", stage="CHARGEBACK"))
        assert r.has_pre_arb is False

    def test_paypal_no_arb(self):
        r = track_appeal_windows(_aw(network="PAYPAL", stage="CHARGEBACK"))
        assert r.has_arb is False

    def test_paypal_cb_window_10_days(self):
        r = track_appeal_windows(_aw(network="PAYPAL", stage="CHARGEBACK", days_elapsed=5))
        assert r.current_window.total_days == 10
        assert r.current_window.days_remaining == 5

    def test_paypal_cb_expired_at_day11(self):
        r = track_appeal_windows(_aw(network="PAYPAL", stage="CHARGEBACK", days_elapsed=11))
        assert r.current_window.status == WindowStatus.EXPIRED

    # ── Amex — no pre-arb ─────────────────────────────────────────

    def test_amex_no_pre_arb(self):
        r = track_appeal_windows(_aw(network="AMEX", stage="CHARGEBACK"))
        assert r.has_pre_arb is False

    def test_amex_has_arb(self):
        r = track_appeal_windows(_aw(network="AMEX", stage="CHARGEBACK"))
        assert r.has_arb is True

    def test_amex_cb_window_20_days(self):
        r = track_appeal_windows(_aw(network="AMEX", stage="CHARGEBACK", days_elapsed=5))
        assert r.current_window.total_days == 20

    # ── VISA/MC/Discover — pre-arb + arb ─────────────────────────

    def test_visa_has_pre_arb(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK"))
        assert r.has_pre_arb is True

    def test_visa_has_arb(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK"))
        assert r.has_arb is True

    def test_mc_has_pre_arb(self):
        r = track_appeal_windows(_aw(network="MC", stage="CHARGEBACK"))
        assert r.has_pre_arb is True

    def test_discover_has_pre_arb(self):
        r = track_appeal_windows(_aw(network="DISCOVER", stage="CHARGEBACK"))
        assert r.has_pre_arb is True

    # ── Pre-arb stage ─────────────────────────────────────────────

    def test_pre_arb_stage_window_total_30_days(self):
        r = track_appeal_windows(_aw(network="VISA", stage="PRE_ARB", days_elapsed=5))
        assert r.current_window.total_days == 30

    def test_pre_arb_stage_open_at_day5(self):
        r = track_appeal_windows(_aw(network="VISA", stage="PRE_ARB", days_elapsed=5))
        assert r.current_window.status == WindowStatus.OPEN

    # ── Arbitration stage ─────────────────────────────────────────

    def test_arb_stage_window_total_45_days(self):
        r = track_appeal_windows(_aw(network="VISA", stage="ARBITRATION", days_elapsed=10))
        assert r.current_window.total_days == 45

    def test_arb_stage_open_at_day10(self):
        r = track_appeal_windows(_aw(network="VISA", stage="ARBITRATION", days_elapsed=10))
        assert r.current_window.status == WindowStatus.OPEN

    # ── Critical actions ──────────────────────────────────────────

    def test_critical_actions_non_empty(self):
        r = track_appeal_windows(_aw())
        assert len(r.critical_actions) >= 1

    def test_days_to_act_matches_current_window(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=5))
        assert r.days_to_act == r.current_window.days_remaining

    def test_overall_status_expired_on_expired_window(self):
        r = track_appeal_windows(_aw(network="PAYPAL", stage="CHARGEBACK", days_elapsed=15))
        assert r.overall_status == WindowStatus.EXPIRED

    # ── Pct elapsed ───────────────────────────────────────────────

    def test_pct_elapsed_between_0_and_1(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=10))
        assert 0.0 <= r.current_window.pct_elapsed <= 1.0

    def test_pct_elapsed_half_at_day10_of_20(self):
        r = track_appeal_windows(_aw(network="VISA", stage="CHARGEBACK", days_elapsed=10))
        assert abs(r.current_window.pct_elapsed - 0.5) < 0.1


# ═══════════════════════════════════════════════════════════════════
# Reason Code Strategy Advisor Tests
# ═══════════════════════════════════════════════════════════════════

def _rc(dispute_id="D-001", reason_code="INR", network="VISA", amount=200.0) -> ReasonCodeStrategyInput:
    return ReasonCodeStrategyInput(
        dispute_id=dispute_id,
        reason_code=reason_code,
        network=network,
        transaction_amount=amount,
    )


class TestReasonCodeStrategyAdvisor:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = advise_reason_code_strategy(_rc(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_summary_contains_network(self):
        r = advise_reason_code_strategy(_rc(network="VISA"))
        assert "VISA" in r.summary

    def test_reason_code_uppercased(self):
        r = advise_reason_code_strategy(_rc(reason_code="inr"))
        assert r.reason_code == "INR"

    def test_network_uppercased(self):
        r = advise_reason_code_strategy(_rc(network="visa"))
        assert r.network == "VISA"

    # ── INR strategy ──────────────────────────────────────────────

    def test_inr_sp_eligible(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert r.sp_eligible is True

    def test_inr_not_ce3_eligible(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert r.ce3_eligible is False

    def test_inr_strategy_type_fight_sp(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert r.strategy_type == StrategyType.FIGHT_SP

    def test_inr_base_win_rate(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert abs(r.base_win_rate - 0.52) < 0.001

    def test_inr_evidence_has_tracking(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "TRACKING_NUMBER" in types

    def test_inr_signature_required_above_750(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR", amount=800.0))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "SIGNATURE_CONFIRMATION" in types
        assert RiskFactor.SIGNATURE_REQUIRED in r.risk_factors

    def test_inr_no_signature_below_750(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR", amount=100.0))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "SIGNATURE_CONFIRMATION" not in types

    # ── SNAD strategy ─────────────────────────────────────────────

    def test_snad_not_sp_eligible(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        assert r.sp_eligible is False

    def test_snad_sp_never_risk_factor(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        assert RiskFactor.SP_NEVER_APPLIES in r.risk_factors

    def test_snad_hard_to_win_flag(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        assert RiskFactor.HARD_TO_WIN in r.risk_factors

    def test_snad_base_win_rate_38(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        assert abs(r.base_win_rate - 0.38) < 0.001

    def test_snad_evidence_has_photos(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "ITEM_PHOTOS" in types

    # ── UNAUTHORIZED strategy ─────────────────────────────────────

    def test_unauthorized_on_visa_is_ce3(self):
        r = advise_reason_code_strategy(_rc(reason_code="UNAUTHORIZED", network="VISA"))
        assert r.ce3_eligible is True
        assert r.strategy_type == StrategyType.FIGHT_CE3

    def test_unauthorized_on_mc_not_ce3(self):
        r = advise_reason_code_strategy(_rc(reason_code="UNAUTHORIZED", network="MC"))
        assert r.ce3_eligible is False
        assert r.strategy_type == StrategyType.FIGHT_EVIDENCE

    def test_unauthorized_ce3_risk_factor_visa(self):
        r = advise_reason_code_strategy(_rc(reason_code="UNAUTHORIZED", network="VISA"))
        assert RiskFactor.CE3_AVAILABLE in r.risk_factors

    def test_unauthorized_has_3ds_evidence(self):
        r = advise_reason_code_strategy(_rc(reason_code="UNAUTHORIZED"))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "3DS_AUTHENTICATION" in types

    # ── SUBSCRIPTION strategy ─────────────────────────────────────

    def test_subscription_base_win_rate_55(self):
        r = advise_reason_code_strategy(_rc(reason_code="SUBSCRIPTION"))
        assert abs(r.base_win_rate - 0.55) < 0.001

    def test_subscription_has_sub_terms_evidence(self):
        r = advise_reason_code_strategy(_rc(reason_code="SUBSCRIPTION"))
        types = [e.evidence_type for e in r.evidence_requirements]
        assert "SUBSCRIPTION_TERMS" in types

    # ── 4807 strategy ─────────────────────────────────────────────

    def test_4807_easy_to_win_flag(self):
        r = advise_reason_code_strategy(_rc(reason_code="4807"))
        assert RiskFactor.EASY_TO_WIN in r.risk_factors

    def test_4807_base_win_rate_70(self):
        r = advise_reason_code_strategy(_rc(reason_code="4807"))
        assert abs(r.base_win_rate - 0.70) < 0.001

    # ── Alias codes ───────────────────────────────────────────────

    def test_13_1_maps_to_snad_strategy(self):
        r = advise_reason_code_strategy(_rc(reason_code="13.1"))
        assert r.sp_eligible is False
        assert RiskFactor.HARD_TO_WIN in r.risk_factors

    def test_10_4_maps_to_unauthorized_strategy(self):
        r = advise_reason_code_strategy(_rc(reason_code="10.4", network="VISA"))
        assert r.ce3_eligible is True

    # ── Unknown code fallback ─────────────────────────────────────

    def test_unknown_code_returns_generic_strategy(self):
        r = advise_reason_code_strategy(_rc(reason_code="UNKNOWN_XYZ"))
        assert r.strategy_type == StrategyType.FIGHT_EVIDENCE
        assert len(r.action_checklist) >= 1

    # ── Action checklist ──────────────────────────────────────────

    def test_action_checklist_non_empty(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert len(r.action_checklist) >= 3

    def test_evidence_requirements_non_empty(self):
        r = advise_reason_code_strategy(_rc(reason_code="INR"))
        assert len(r.evidence_requirements) >= 2

    def test_escalation_tip_non_empty(self):
        r = advise_reason_code_strategy(_rc(reason_code="SNAD"))
        assert len(r.escalation_tip) > 0
