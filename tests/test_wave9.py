"""
Tests for Wave 9: seller_protection_advisor, case_summarizer.
"""
import pytest
from datetime import date, timedelta

from oracle.seller_protection_advisor import (
    assess_seller_protection, batch_sp_check,
    SPAdvisoryInput, ItemCategory, ProtectionLevel,
    SPAdvisoryReport,
)
from oracle.case_summarizer import (
    summarize_case, batch_summarize,
    DisputeCaseInput, DisputePhase, ActionUrgency, FightRecommendation,
    CaseSummary,
)

TODAY = date(2024, 6, 18)


# ═══════════════════════════════════════════════════════════════════
# Seller Protection Advisor Tests
# ═══════════════════════════════════════════════════════════════════

class TestSellerProtectionAdvisor:

    def _inp(self, **kwargs) -> SPAdvisoryInput:
        defaults = dict(
            order_id="O-SP-1",
            order_amount=200.0,
            payment_confirmed=True,
            payment_method="PAYPAL_DIRECT",
            is_partial_payment=False,
            item_category=ItemCategory.TANGIBLE_GOODS,
            has_tracking_number=True,
            shipped_to_paypal_address=True,
            has_signature_confirmation=False,
            carrier_provides_tracking=True,
            buyer_on_ineligible_list=False,
        )
        defaults.update(kwargs)
        return SPAdvisoryInput(**defaults)

    def test_fully_eligible_tangible_goods(self):
        r = assess_seller_protection(self._inp())
        assert r.protection_level == ProtectionLevel.FULL
        assert r.sp_eligible is True

    def test_digital_goods_no_sp(self):
        r = assess_seller_protection(self._inp(item_category=ItemCategory.DIGITAL_GOODS))
        assert r.protection_level == ProtectionLevel.NONE
        assert r.sp_eligible is False

    def test_services_no_sp(self):
        r = assess_seller_protection(self._inp(item_category=ItemCategory.SERVICES))
        assert r.protection_level == ProtectionLevel.NONE

    def test_gift_card_no_sp(self):
        r = assess_seller_protection(self._inp(item_category=ItemCategory.GIFT_CARD))
        assert r.protection_level == ProtectionLevel.NONE

    def test_custom_built_no_sp(self):
        r = assess_seller_protection(self._inp(is_custom_built=True))
        assert r.protection_level == ProtectionLevel.NONE

    def test_echeque_no_sp(self):
        r = assess_seller_protection(self._inp(payment_method="ECHEQUE"))
        assert r.protection_level == ProtectionLevel.NONE

    def test_partial_payment_no_sp(self):
        r = assess_seller_protection(self._inp(is_partial_payment=True))
        assert r.protection_level == ProtectionLevel.NONE

    def test_wrong_address_no_sp(self):
        r = assess_seller_protection(self._inp(shipped_to_paypal_address=False))
        assert r.protection_level == ProtectionLevel.NONE

    def test_buyer_flagged_no_sp(self):
        r = assess_seller_protection(self._inp(buyer_on_ineligible_list=True))
        assert r.protection_level == ProtectionLevel.NONE

    def test_missing_tracking_gives_partial(self):
        r = assess_seller_protection(self._inp(has_tracking_number=False))
        assert r.protection_level == ProtectionLevel.PARTIAL
        assert r.sp_eligible is True

    def test_no_signature_on_high_value_gives_partial(self):
        r = assess_seller_protection(self._inp(order_amount=800.0, has_signature_confirmation=False))
        assert r.protection_level == ProtectionLevel.PARTIAL

    def test_signature_on_high_value_gives_full(self):
        r = assess_seller_protection(self._inp(order_amount=800.0, has_signature_confirmation=True))
        assert r.protection_level == ProtectionLevel.FULL

    def test_signature_not_required_below_750(self):
        r = assess_seller_protection(self._inp(order_amount=600.0, has_signature_confirmation=False))
        assert r.protection_level == ProtectionLevel.FULL

    def test_seven_requirements_returned(self):
        r = assess_seller_protection(self._inp())
        assert len(r.requirements) == 7

    def test_snad_risk_flag_present(self):
        r = assess_seller_protection(self._inp(expect_snad_risk=True))
        assert any("SNAD" in f for f in r.risk_flags)

    def test_missing_requirements_list_empty_when_full(self):
        r = assess_seller_protection(self._inp())
        assert r.missing_requirements == []

    def test_recommendation_populated(self):
        r = assess_seller_protection(self._inp())
        assert len(r.recommendation) > 30

    def test_shipping_checklist_populated(self):
        r = assess_seller_protection(self._inp())
        assert len(r.shipping_checklist) >= 4

    def test_signature_checklist_item_on_high_value(self):
        r = assess_seller_protection(self._inp(order_amount=800.0))
        sig_items = [c for c in r.shipping_checklist if "SIGNATURE" in c.upper()]
        assert len(sig_items) >= 1

    def test_snad_note_always_present(self):
        r = assess_seller_protection(self._inp())
        assert "SNAD" in r.snad_note
        assert any(w in r.snad_note.lower() for w in ("never", "always", "excluded"))

    def test_summary_contains_order_id(self):
        r = assess_seller_protection(self._inp(order_id="O-TEST-99"))
        assert "O-TEST-99" in r.summary

    def test_batch_sorted_none_first(self):
        orders = [
            self._inp(order_id="O-full"),
            self._inp(order_id="O-none", item_category=ItemCategory.DIGITAL_GOODS),
            self._inp(order_id="O-partial", has_tracking_number=False),
        ]
        reports = batch_sp_check(orders)
        assert reports[0].protection_level == ProtectionLevel.NONE
        assert reports[-1].protection_level == ProtectionLevel.FULL


# ═══════════════════════════════════════════════════════════════════
# Case Summarizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestCaseSummarizer:

    def _inp(self, **kwargs) -> DisputeCaseInput:
        defaults = dict(
            dispute_id="D-CS-1",
            order_id="O-CS-1",
            opened_date=TODAY - timedelta(days=5),
            deadline=TODAY + timedelta(days=10),
            dispute_amount=200.0,
            order_amount=200.0,
            merchant_win_rate_90d=0.65,
            claim_type="ITEM_NOT_RECEIVED",
            phase=DisputePhase.IN_PROGRESS,
            evidence_package_strength="MODERATE",
            response_ready=False,
            sp_protection_level="FULL",
        )
        defaults.update(kwargs)
        return DisputeCaseInput(**defaults)

    def test_days_remaining_computed(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert s.days_remaining == 10

    def test_fight_score_in_range(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert 0 <= s.fight_score <= 100

    def test_ev_fight_computed(self):
        s = summarize_case(self._inp(), today=TODAY)
        # Should be a real number (positive for good fight)
        assert isinstance(s.ev_fight, float)

    def test_expected_loss_if_uncontested_positive(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert s.expected_loss_if_uncontested > 0

    def test_full_sp_gives_auto_fight_tendency(self):
        s = summarize_case(self._inp(
            sp_protection_level="FULL",
            evidence_package_strength="STRONG",
            merchant_win_rate_90d=0.80,
        ), today=TODAY)
        assert s.fight_recommendation in (FightRecommendation.AUTO_FIGHT, FightRecommendation.FIGHT)

    def test_low_amount_weak_evidence_refund(self):
        s = summarize_case(self._inp(
            dispute_amount=20.0,
            evidence_package_strength="WEAK",
            sp_protection_level="NONE",
            merchant_win_rate_90d=0.40,
        ), today=TODAY)
        assert s.fight_recommendation in (FightRecommendation.REFUND, FightRecommendation.SETTLE, FightRecommendation.PARTIAL_REFUND, FightRecommendation.REVIEW)

    def test_critical_urgency_when_deadline_tomorrow(self):
        s = summarize_case(self._inp(deadline=TODAY + timedelta(days=1)), today=TODAY)
        assert s.action_urgency == ActionUrgency.CRITICAL

    def test_low_urgency_when_plenty_of_time(self):
        s = summarize_case(self._inp(deadline=TODAY + timedelta(days=20)), today=TODAY)
        assert s.action_urgency == ActionUrgency.LOW

    def test_submitted_phase_monitor_urgency(self):
        s = summarize_case(self._inp(phase=DisputePhase.SUBMITTED), today=TODAY)
        assert s.action_urgency == ActionUrgency.MONITOR

    def test_next_action_populated(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert len(s.next_action) > 20

    def test_headline_contains_amount(self):
        s = summarize_case(self._inp(dispute_amount=350.0), today=TODAY)
        assert "350" in s.headline

    def test_narrative_populated(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert len(s.narrative) > 50
        assert s.dispute_id in s.narrative

    def test_key_facts_populated(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert len(s.key_facts) >= 3

    def test_deadline_warning_when_imminent(self):
        s = summarize_case(self._inp(deadline=TODAY + timedelta(days=1)), today=TODAY)
        assert any("DEADLINE" in w or "day" in w.lower() for w in s.warnings)

    def test_snad_warning_when_snad_claim(self):
        s = summarize_case(self._inp(claim_type="SNAD"), today=TODAY)
        assert any("SNAD" in w for w in s.warnings)

    def test_no_sp_warning_when_no_protection(self):
        s = summarize_case(self._inp(sp_protection_level="NONE"), today=TODAY)
        assert any("Seller Protection" in w or "SP" in w for w in s.warnings)

    def test_confidence_in_range(self):
        s = summarize_case(self._inp(), today=TODAY)
        assert 0 <= s.confidence <= 1.0

    def test_higher_confidence_with_more_signals(self):
        s_few = summarize_case(self._inp(fight_score=None, sp_protection_level="UNKNOWN"), today=TODAY)
        s_many = summarize_case(self._inp(fight_score=75.0, sp_protection_level="FULL", response_ready=True), today=TODAY)
        assert s_many.confidence >= s_few.confidence

    def test_summary_line_csv_format(self):
        s = summarize_case(self._inp(), today=TODAY)
        parts = s.summary_line.split(",")
        assert len(parts) == 8

    def test_batch_summarize_sorted_critical_first(self):
        cases = [
            self._inp(dispute_id="D-low", deadline=TODAY + timedelta(days=20)),
            self._inp(dispute_id="D-critical", deadline=TODAY + timedelta(days=1)),
            self._inp(dispute_id="D-high", deadline=TODAY + timedelta(days=3)),
        ]
        summaries = batch_summarize(cases, today=TODAY)
        urgencies = [s.action_urgency for s in summaries]
        assert urgencies[0] == ActionUrgency.CRITICAL

    def test_batch_all_included(self):
        cases = [self._inp(dispute_id=f"D-{i}") for i in range(5)]
        summaries = batch_summarize(cases, today=TODAY)
        assert len(summaries) == 5

    def test_fight_score_uses_explicit_value_when_provided(self):
        s = summarize_case(self._inp(fight_score=92.0), today=TODAY)
        assert s.fight_score == pytest.approx(92.0, abs=1.0)

    def test_resolved_win_gives_monitor_urgency(self):
        s = summarize_case(self._inp(phase=DisputePhase.RESOLVED_WIN), today=TODAY)
        assert s.action_urgency == ActionUrgency.MONITOR
