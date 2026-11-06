"""
Tests for Wave 28: dispute_timeline_tracker, network_rules_engine.
"""
import pytest
from oracle.dispute_timeline_tracker import (
    track_dispute_timeline, DisputeTimelineInput, DisputePhase,
    MilestoneStatus, MilestoneUrgency,
)
from oracle.network_rules_engine import (
    apply_network_rules, NetworkRulesInput, Network,
    EvidenceRequirementLevel,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Timeline Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeTimelineTracker:

    def _inp(self, **kw) -> DisputeTimelineInput:
        defaults = dict(
            dispute_id="D-001",
            network="PAYPAL",
            claim_type="INR",
            current_phase="AWAITING_MERCHANT",
            current_day=3,
            merchant_responded_day=None,
            evidence_submitted_day=None,
            ruling_issued_day=None,
            appeal_filed_day=None,
            is_escalated_to_network=False,
        )
        defaults.update(kw)
        return DisputeTimelineInput(**defaults)

    # ── Milestone counts ──────────────────────────────────────────

    def test_milestones_non_empty(self):
        r = track_dispute_timeline(self._inp())
        assert len(r.milestones) >= 2

    def test_no_overdue_at_day3(self):
        r = track_dispute_timeline(self._inp(current_day=3))
        assert r.overdue_milestones == 0

    def test_response_overdue_after_deadline(self):
        r = track_dispute_timeline(self._inp(current_day=12))  # > 10d PayPal deadline
        overdue_names = [m.name for m in r.milestones if m.status == MilestoneStatus.OVERDUE]
        assert "merchant_response" in overdue_names

    # ── Completed milestones ──────────────────────────────────────

    def test_responded_milestone_completed(self):
        r = track_dispute_timeline(self._inp(merchant_responded_day=2))
        response_m = next(m for m in r.milestones if m.name == "merchant_response")
        assert response_m.status == MilestoneStatus.COMPLETED

    def test_evidence_milestone_completed(self):
        r = track_dispute_timeline(self._inp(evidence_submitted_day=5))
        ev_m = next(m for m in r.milestones if m.name == "evidence_submission")
        assert ev_m.status == MilestoneStatus.COMPLETED

    def test_ruling_milestone_completed(self):
        r = track_dispute_timeline(self._inp(
            ruling_issued_day=20,
            current_day=25,
            current_phase="RULING_ISSUED",
        ))
        ruling_m = next(m for m in r.milestones if m.name == "ruling_expected")
        assert ruling_m.status == MilestoneStatus.COMPLETED

    # ── Appeal window ─────────────────────────────────────────────

    def test_appeal_milestone_created_after_ruling(self):
        r = track_dispute_timeline(self._inp(
            ruling_issued_day=20,
            current_day=22,
            current_phase="RULING_ISSUED",
        ))
        appeal_m = next((m for m in r.milestones if m.name == "appeal_window"), None)
        assert appeal_m is not None

    def test_appeal_overdue_after_paypal_window(self):
        r = track_dispute_timeline(self._inp(
            ruling_issued_day=10,
            current_day=25,   # 15 days after ruling, PayPal appeal is 10 days
            current_phase="RULING_ISSUED",
        ))
        appeal_m = next((m for m in r.milestones if m.name == "appeal_window"), None)
        if appeal_m:
            assert appeal_m.status == MilestoneStatus.OVERDUE

    # ── Network deadlines ─────────────────────────────────────────

    def test_visa_response_window_longer_than_paypal(self):
        paypal = track_dispute_timeline(self._inp(network="PAYPAL"))
        visa   = track_dispute_timeline(self._inp(network="VISA"))
        paypal_resp = next(m for m in paypal.milestones if m.name == "merchant_response")
        visa_resp   = next(m for m in visa.milestones   if m.name == "merchant_response")
        assert visa_resp.deadline_day > paypal_resp.deadline_day

    def test_mastercard_response_longest(self):
        mc = track_dispute_timeline(self._inp(network="MASTERCARD"))
        mc_resp = next(m for m in mc.milestones if m.name == "merchant_response")
        assert mc_resp.deadline_day == 45

    # ── Urgency ───────────────────────────────────────────────────

    def test_critical_urgency_near_deadline(self):
        r = track_dispute_timeline(self._inp(current_day=9))  # 1d before PayPal deadline
        resp_m = next(m for m in r.milestones if m.name == "merchant_response")
        if resp_m.status == MilestoneStatus.PENDING:
            assert resp_m.urgency in (MilestoneUrgency.CRITICAL, MilestoneUrgency.HIGH)

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = track_dispute_timeline(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_resolution_range_ordered(self):
        r = track_dispute_timeline(self._inp())
        low, high = r.estimated_resolution_range
        assert low <= high

    def test_immediate_actions_non_empty(self):
        r = track_dispute_timeline(self._inp())
        assert len(r.immediate_actions) >= 1

    def test_next_phase_set(self):
        r = track_dispute_timeline(self._inp(current_phase="AWAITING_MERCHANT"))
        assert r.next_expected_phase is not None


# ═══════════════════════════════════════════════════════════════════
# Network Rules Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestNetworkRulesEngine:

    def _inp(self, **kw) -> NetworkRulesInput:
        defaults = dict(
            dispute_id="D-001",
            network="VISA",
            claim_type="INR",
            prior_undisputed_transactions=0,
            same_device_prior_transactions=False,
            same_ip_prior_transactions=False,
            transaction_amount=100.0,
            is_card_present=False,
            is_international=False,
        )
        defaults.update(kw)
        return NetworkRulesInput(**defaults)

    # ── Dispute codes ─────────────────────────────────────────────

    def test_visa_inr_has_code(self):
        r = apply_network_rules(self._inp(network="VISA", claim_type="INR"))
        assert r.dispute_code is not None
        assert r.dispute_code.code == "13.1"

    def test_mastercard_duplicate_code(self):
        r = apply_network_rules(self._inp(network="MASTERCARD", claim_type="DUPLICATE"))
        assert r.dispute_code is not None
        assert r.dispute_code.code == "4834"

    def test_amex_snad_code(self):
        r = apply_network_rules(self._inp(network="AMEX", claim_type="SNAD"))
        assert r.dispute_code is not None
        assert "C31" in r.dispute_code.code

    def test_discover_unauthorized_code(self):
        r = apply_network_rules(self._inp(network="DISCOVER", claim_type="UNAUTHORIZED"))
        assert r.dispute_code is not None
        assert "UA" in r.dispute_code.code

    # ── Win rates ─────────────────────────────────────────────────

    def test_duplicate_win_rate_higher_than_snad(self):
        snad = apply_network_rules(self._inp(claim_type="SNAD"))
        dup  = apply_network_rules(self._inp(claim_type="DUPLICATE"))
        assert dup.typical_win_rate > snad.typical_win_rate

    def test_win_rate_in_range(self):
        r = apply_network_rules(self._inp())
        assert 0.0 <= r.typical_win_rate <= 1.0

    # ── CE3.0 eligibility ─────────────────────────────────────────

    def test_ce30_requires_visa_unauthorized_2plus(self):
        r = apply_network_rules(self._inp(
            network="VISA",
            claim_type="UNAUTHORIZED",
            prior_undisputed_transactions=2,
        ))
        assert r.ce30_applicable is True

    def test_ce30_not_on_mastercard(self):
        r = apply_network_rules(self._inp(
            network="MASTERCARD",
            claim_type="UNAUTHORIZED",
            prior_undisputed_transactions=5,
        ))
        assert r.ce30_applicable is False

    def test_ce30_not_on_inr(self):
        r = apply_network_rules(self._inp(
            network="VISA",
            claim_type="INR",
            prior_undisputed_transactions=5,
        ))
        assert r.ce30_applicable is False

    def test_ce30_requires_2_prior_transactions(self):
        one = apply_network_rules(self._inp(
            claim_type="UNAUTHORIZED", prior_undisputed_transactions=1))
        two = apply_network_rules(self._inp(
            claim_type="UNAUTHORIZED", prior_undisputed_transactions=2))
        assert one.ce30_applicable is False
        assert two.ce30_applicable is True

    def test_ce30_boost_18pp_when_applicable(self):
        r = apply_network_rules(self._inp(
            claim_type="UNAUTHORIZED", prior_undisputed_transactions=3))
        assert r.ce30_win_rate_boost == pytest.approx(0.18, abs=0.001)

    def test_ce30_boost_zero_when_not_applicable(self):
        r = apply_network_rules(self._inp(claim_type="INR"))
        assert r.ce30_win_rate_boost == pytest.approx(0.0)

    # ── Evidence requirements ─────────────────────────────────────

    def test_inr_requires_tracking(self):
        r = apply_network_rules(self._inp(claim_type="INR"))
        names = [e.name for e in r.evidence_requirements]
        assert "tracking_number" in names

    def test_snad_requires_product_listing(self):
        r = apply_network_rules(self._inp(claim_type="SNAD"))
        names = [e.name for e in r.evidence_requirements]
        assert "product_listing" in names

    def test_unauthorized_requires_ip_log(self):
        r = apply_network_rules(self._inp(claim_type="UNAUTHORIZED"))
        names = [e.name for e in r.evidence_requirements]
        assert "ip_address_log" in names

    def test_ce30_adds_extra_requirements(self):
        no_ce = apply_network_rules(self._inp(
            claim_type="UNAUTHORIZED", prior_undisputed_transactions=0))
        with_ce = apply_network_rules(self._inp(
            claim_type="UNAUTHORIZED", prior_undisputed_transactions=3))
        assert with_ce.required_items > no_ce.required_items

    # ── Deadlines ─────────────────────────────────────────────────

    def test_mastercard_45_day_window(self):
        r = apply_network_rules(self._inp(network="MASTERCARD"))
        assert r.response_window_days == 45

    def test_visa_20_day_window(self):
        r = apply_network_rules(self._inp(network="VISA"))
        assert r.response_window_days == 20

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = apply_network_rules(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_network_notes_present_for_visa_unauthorized(self):
        r = apply_network_rules(self._inp(claim_type="UNAUTHORIZED"))
        assert len(r.network_specific_notes) >= 1
