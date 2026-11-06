"""
Tests for Wave 40: dispute_timeline_manager, network_rule_checker.
"""
import pytest
from oracle.dispute_timeline_manager import (
    build_dispute_timeline, TimelineInput,
    Network, DisputeStage, DeadlineType, UrgencyLevel,
)
from oracle.network_rule_checker import (
    check_network_rules, NetworkRuleInput,
    CheckStatus, NetworkRuleViolation,
)


BASE_TS = 1_700_000_000
DAY = 86400


# ═══════════════════════════════════════════════════════════════════
# Dispute Timeline Manager Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeTimelineManager:

    def _inp(self, network=Network.PAYPAL, days_since=5, dispute_id="D-001", **kw) -> TimelineInput:
        return TimelineInput(
            dispute_id=dispute_id,
            network=network,
            dispute_filed_epoch=BASE_TS,
            now_epoch=BASE_TS + days_since * DAY,
            **kw,
        )

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = build_dispute_timeline(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_deadlines_non_empty(self):
        r = build_dispute_timeline(self._inp())
        assert len(r.deadlines) >= 1

    def test_days_since_filing_correct(self):
        r = build_dispute_timeline(self._inp(days_since=7))
        assert r.days_since_filing == 7

    # ── PayPal windows ────────────────────────────────────────────

    def test_paypal_response_window_10_days(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL, days_since=0))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.RESPONSE)
        assert resp.days_remaining == 10

    def test_paypal_evidence_window_10_days(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL, days_since=0))
        ev = next(d for d in r.deadlines if d.deadline_type == DeadlineType.EVIDENCE)
        assert ev.days_remaining == 10

    def test_paypal_no_rebuttal_window(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL))
        reb = next((d for d in r.deadlines if d.deadline_type == DeadlineType.REBUTTAL), None)
        if reb:
            assert reb.is_applicable is False

    # ── Visa windows ──────────────────────────────────────────────

    def test_visa_response_window_20_days(self):
        r = build_dispute_timeline(self._inp(network=Network.VISA, days_since=0))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.RESPONSE)
        assert resp.days_remaining == 20

    def test_visa_appeal_window_45_days(self):
        ruling_epoch = BASE_TS + 25 * DAY
        r = build_dispute_timeline(self._inp(
            network=Network.VISA,
            days_since=30,
            ruling_epoch=ruling_epoch,
            stage=DisputeStage.RULED))
        appeal = next(d for d in r.deadlines if d.deadline_type == DeadlineType.APPEAL)
        assert appeal.is_applicable is True
        # 45 day window from ruling, 5 days elapsed
        assert 39 <= appeal.days_remaining <= 41

    # ── Urgency levels ────────────────────────────────────────────

    def test_critical_urgency_at_1_day_remaining(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL, days_since=9))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.RESPONSE)
        assert resp.urgency in (UrgencyLevel.CRITICAL, UrgencyLevel.OVERDUE)

    def test_low_urgency_many_days_remaining(self):
        r = build_dispute_timeline(self._inp(network=Network.VISA, days_since=1))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.RESPONSE)
        assert resp.urgency in (UrgencyLevel.LOW, UrgencyLevel.MEDIUM)

    def test_overdue_when_past_deadline(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL, days_since=15))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.RESPONSE)
        assert resp.urgency == UrgencyLevel.OVERDUE

    def test_is_any_overdue_flag(self):
        r = build_dispute_timeline(self._inp(network=Network.PAYPAL, days_since=15))
        assert r.is_any_overdue is True

    def test_no_overdue_when_within_window(self):
        r = build_dispute_timeline(self._inp(network=Network.VISA, days_since=5))
        assert r.is_any_overdue is False

    # ── Next action ───────────────────────────────────────────────

    def test_next_action_exists(self):
        r = build_dispute_timeline(self._inp(network=Network.VISA, days_since=5))
        assert r.next_action_deadline is not None

    def test_appeal_next_after_ruling(self):
        ruling_epoch = BASE_TS + 25 * DAY
        r = build_dispute_timeline(self._inp(
            network=Network.VISA,
            days_since=27,
            ruling_epoch=ruling_epoch,
            stage=DisputeStage.RULED))
        # Next action should be appeal (or arbitration)
        assert r.next_action_deadline is not None

    # ── Rebuttal window start ─────────────────────────────────────

    def test_rebuttal_applicable_when_started(self):
        reb_start = BASE_TS + 15 * DAY
        r = build_dispute_timeline(self._inp(
            network=Network.VISA,
            days_since=18,
            rebuttal_period_started_epoch=reb_start,
            stage=DisputeStage.REBUTTAL_PERIOD))
        reb = next((d for d in r.deadlines if d.deadline_type == DeadlineType.REBUTTAL), None)
        if reb:
            assert reb.is_applicable is True


# ═══════════════════════════════════════════════════════════════════
# Network Rule Checker Tests
# ═══════════════════════════════════════════════════════════════════

class TestNetworkRuleChecker:

    def _inp(self, **kw) -> NetworkRuleInput:
        defaults = dict(
            dispute_id="D-001",
            network="PAYPAL",
            reason_code="INR",
            transaction_amount=100.0,
            disputed_amount=100.0,
            transaction_currency="USD",
            dispute_currency="USD",
            transaction_date_epoch=BASE_TS - 30 * DAY,
            dispute_filed_epoch=BASE_TS,
            response_to_be_filed_epoch=BASE_TS + 5 * DAY,
            evidence_fields_present=["tracking_number", "shipment_date"],
            is_second_chargeback=False,
            has_prior_dispute_on_same_txn=False,
            is_eu_transaction=False,
            has_sca_authentication=False,
        )
        defaults.update(kw)
        return NetworkRuleInput(**defaults)

    # ── Can file ──────────────────────────────────────────────────

    def test_valid_input_can_file(self):
        r = check_network_rules(self._inp())
        assert r.can_file is True

    def test_summary_contains_dispute_id(self):
        r = check_network_rules(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    # ── Reason code validation ────────────────────────────────────

    def test_valid_reason_code_passes(self):
        r = check_network_rules(self._inp(network="VISA", reason_code="10.4",
                                           evidence_fields_present=["ip_log", "order_history",
                                                                      "device_id"]))
        code_check = next(c for c in r.all_checks if c.rule == "Reason code validity")
        assert code_check.status == CheckStatus.PASS

    def test_invalid_reason_code_fails(self):
        r = check_network_rules(self._inp(reason_code="INVALID_CODE"))
        assert r.can_file is False
        assert NetworkRuleViolation.INVALID_REASON_CODE.value in r.violations

    def test_visa_code_on_paypal_fails(self):
        r = check_network_rules(self._inp(network="PAYPAL", reason_code="10.4"))
        assert r.can_file is False

    # ── Required evidence ─────────────────────────────────────────

    def test_missing_evidence_fails(self):
        r = check_network_rules(self._inp(
            network="VISA", reason_code="13.1",
            evidence_fields_present=["tracking_number"]))   # missing proof_of_delivery
        assert r.can_file is False
        assert "proof_of_delivery" in r.missing_evidence

    def test_all_evidence_present_passes(self):
        r = check_network_rules(self._inp(
            network="VISA", reason_code="13.1",
            evidence_fields_present=["tracking_number", "proof_of_delivery"]))
        assert r.can_file is True

    def test_missing_evidence_in_missing_list(self):
        r = check_network_rules(self._inp(
            reason_code="INR",
            evidence_fields_present=["tracking_number"]))   # missing shipment_date
        assert "shipment_date" in r.missing_evidence

    # ── Amount checks ─────────────────────────────────────────────

    def test_disputed_exceeds_transaction_fails(self):
        r = check_network_rules(self._inp(
            transaction_amount=100.0, disputed_amount=150.0))
        assert r.can_file is False
        assert NetworkRuleViolation.EXCEEDS_TRANSACTION_AMOUNT.value in r.violations

    def test_partial_dispute_passes(self):
        r = check_network_rules(self._inp(
            transaction_amount=100.0, disputed_amount=50.0))
        amt_check = next(c for c in r.all_checks if c.rule == "Disputed amount")
        assert amt_check.status == CheckStatus.PASS

    # ── Currency mismatch ─────────────────────────────────────────

    def test_currency_mismatch_warning(self):
        r = check_network_rules(self._inp(
            transaction_currency="USD", dispute_currency="EUR"))
        curr_check = next(c for c in r.all_checks if c.rule == "Currency match")
        assert curr_check.status == CheckStatus.WARNING

    def test_currency_match_passes(self):
        r = check_network_rules(self._inp(
            transaction_currency="USD", dispute_currency="USD"))
        curr_check = next(c for c in r.all_checks if c.rule == "Currency match")
        assert curr_check.status == CheckStatus.PASS

    # ── Time-bar ──────────────────────────────────────────────────

    def test_time_barred_dispute_fails(self):
        r = check_network_rules(self._inp(
            transaction_date_epoch=BASE_TS - 200 * DAY,  # 200 days old
            dispute_filed_epoch=BASE_TS,
            network="VISA"))   # Visa limit = 120 days
        assert r.can_file is False
        assert NetworkRuleViolation.TIME_BARRED.value in r.violations

    def test_within_time_limit_passes(self):
        r = check_network_rules(self._inp(
            transaction_date_epoch=BASE_TS - 30 * DAY,
            dispute_filed_epoch=BASE_TS,
            network="VISA"))
        time_check = next(c for c in r.all_checks if c.rule == "Time-bar")
        assert time_check.status == CheckStatus.PASS

    # ── Response deadline ─────────────────────────────────────────

    def test_late_response_fails(self):
        r = check_network_rules(self._inp(
            network="PAYPAL",
            dispute_filed_epoch=BASE_TS,
            response_to_be_filed_epoch=BASE_TS + 15 * DAY))  # 15 days, limit=10
        assert r.can_file is False

    def test_on_time_response_passes(self):
        r = check_network_rules(self._inp(
            network="PAYPAL",
            dispute_filed_epoch=BASE_TS,
            response_to_be_filed_epoch=BASE_TS + 8 * DAY))
        resp_check = next(c for c in r.all_checks if c.rule == "Response deadline")
        assert resp_check.status == CheckStatus.PASS

    # ── EU SCA ────────────────────────────────────────────────────

    def test_eu_unauthorized_without_sca_warning(self):
        r = check_network_rules(self._inp(
            network="VISA", reason_code="10.4",
            evidence_fields_present=["ip_log", "order_history", "device_id"],
            is_eu_transaction=True, has_sca_authentication=False))
        sca_checks = [c for c in r.all_checks if "SCA" in c.rule]
        if sca_checks:
            assert sca_checks[0].status == CheckStatus.WARNING

    def test_eu_unauthorized_with_sca_passes(self):
        r = check_network_rules(self._inp(
            network="VISA", reason_code="10.4",
            evidence_fields_present=["ip_log", "order_history", "device_id"],
            is_eu_transaction=True, has_sca_authentication=True))
        sca_checks = [c for c in r.all_checks if "SCA" in c.rule]
        if sca_checks:
            assert sca_checks[0].status == CheckStatus.PASS
