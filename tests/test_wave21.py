"""
Tests for Wave 21: chargeback_reason_classifier, dispute_batch_prioritizer.
"""
import pytest
from oracle.chargeback_reason_classifier import (
    classify_reason_code, ReasonCodeInput, CardNetwork,
    DisputeCategory, PayPalClaimType,
)
from oracle.dispute_batch_prioritizer import (
    prioritize_disputes, BatchDisputeItem, PriorityTier, DisputeStatus,
)


# ═══════════════════════════════════════════════════════════════════
# Chargeback Reason Code Classifier Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackReasonClassifier:

    def _inp(self, network=CardNetwork.VISA, code="13.1", **kw) -> ReasonCodeInput:
        defaults = dict(
            dispute_id="D-001",
            dispute_amount=200.0,
            prior_non_disputed_count=0,
            transaction_days_ago=10,
        )
        defaults.update(kw)
        return ReasonCodeInput(network=network, reason_code=code, **defaults)

    # ── Visa codes ───────────────────────────────────────────────

    def test_visa_13_1_is_inr(self):
        r = classify_reason_code(self._inp(code="13.1"))
        assert r.category == DisputeCategory.ITEM_NOT_RECEIVED
        assert r.paypal_claim_type == PayPalClaimType.INR

    def test_visa_13_3_is_snad(self):
        r = classify_reason_code(self._inp(code="13.3"))
        assert r.category == DisputeCategory.NOT_AS_DESCRIBED
        assert r.paypal_claim_type == PayPalClaimType.SNAD

    def test_visa_10_4_is_unauthorized(self):
        r = classify_reason_code(self._inp(code="10.4"))
        assert r.category == DisputeCategory.UNAUTHORIZED
        assert r.paypal_claim_type == PayPalClaimType.UNAUTHORIZED

    def test_visa_13_7_is_subscription(self):
        r = classify_reason_code(self._inp(code="13.7"))
        assert r.paypal_claim_type == PayPalClaimType.SUBSCRIPTION

    def test_visa_12_1_is_duplicate(self):
        r = classify_reason_code(self._inp(code="12.1"))
        assert r.paypal_claim_type == PayPalClaimType.DUPLICATE

    # ── Mastercard codes ─────────────────────────────────────────

    def test_mc_4853_is_snad(self):
        r = classify_reason_code(self._inp(network=CardNetwork.MASTERCARD, code="4853"))
        assert r.category == DisputeCategory.NOT_AS_DESCRIBED

    def test_mc_4855_is_inr(self):
        r = classify_reason_code(self._inp(network=CardNetwork.MASTERCARD, code="4855"))
        assert r.category == DisputeCategory.ITEM_NOT_RECEIVED

    def test_mc_response_deadline_45_days(self):
        r = classify_reason_code(self._inp(network=CardNetwork.MASTERCARD, code="4853"))
        assert r.response_deadline_days == 45

    # ── Amex codes ───────────────────────────────────────────────

    def test_amex_c08_is_inr(self):
        r = classify_reason_code(self._inp(network=CardNetwork.AMEX, code="C08"))
        assert r.category == DisputeCategory.ITEM_NOT_RECEIVED

    def test_amex_c31_is_snad(self):
        r = classify_reason_code(self._inp(network=CardNetwork.AMEX, code="C31"))
        assert r.category == DisputeCategory.NOT_AS_DESCRIBED

    def test_amex_f29_is_unauthorized(self):
        r = classify_reason_code(self._inp(network=CardNetwork.AMEX, code="F29"))
        assert r.category == DisputeCategory.UNAUTHORIZED

    # ── Discover codes ───────────────────────────────────────────

    def test_discover_nf_is_inr(self):
        r = classify_reason_code(self._inp(network=CardNetwork.DISCOVER, code="NF"))
        assert r.category == DisputeCategory.ITEM_NOT_RECEIVED

    def test_discover_rm_is_snad(self):
        r = classify_reason_code(self._inp(network=CardNetwork.DISCOVER, code="RM"))
        assert r.category == DisputeCategory.NOT_AS_DESCRIBED

    # ── PayPal internal codes ────────────────────────────────────

    def test_paypal_inr(self):
        r = classify_reason_code(self._inp(network=CardNetwork.PAYPAL, code="INR"))
        assert r.paypal_claim_type == PayPalClaimType.INR

    def test_paypal_snad(self):
        r = classify_reason_code(self._inp(network=CardNetwork.PAYPAL, code="SNAD"))
        assert r.paypal_claim_type == PayPalClaimType.SNAD

    # ── CE3.0 eligibility ────────────────────────────────────────

    def test_ce30_eligible_visa_unauthorized_2_prior(self):
        r = classify_reason_code(self._inp(
            code="10.4",
            prior_non_disputed_count=2,
        ))
        assert r.ce30_eligible is True

    def test_ce30_not_eligible_fewer_than_2_prior(self):
        r = classify_reason_code(self._inp(
            code="10.4",
            prior_non_disputed_count=1,
        ))
        assert r.ce30_eligible is False

    def test_ce30_not_eligible_non_visa(self):
        r = classify_reason_code(self._inp(
            network=CardNetwork.MASTERCARD, code="4863",
            prior_non_disputed_count=5,
        ))
        assert r.ce30_eligible is False

    def test_ce30_not_eligible_inr_code(self):
        # CE3.0 is only for unauthorized, not INR
        r = classify_reason_code(self._inp(
            code="13.1",
            prior_non_disputed_count=5,
        ))
        assert r.ce30_eligible is False

    # ── Report fields ─────────────────────────────────────────────

    def test_unknown_code_returns_other(self):
        r = classify_reason_code(self._inp(code="ZZZZ"))
        assert r.category == DisputeCategory.OTHER

    def test_win_difficulty_range(self):
        r = classify_reason_code(self._inp(code="13.1"))
        assert 1 <= r.win_difficulty <= 5

    def test_required_evidence_non_empty(self):
        r = classify_reason_code(self._inp(code="13.1"))
        assert len(r.required_evidence) > 0

    def test_summary_contains_dispute_id(self):
        r = classify_reason_code(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_sp_applicable_inr(self):
        r = classify_reason_code(self._inp(code="13.1"))
        assert r.seller_protection_applicable is True

    def test_sp_not_applicable_snad(self):
        r = classify_reason_code(self._inp(code="13.3"))
        assert r.seller_protection_applicable is False


# ═══════════════════════════════════════════════════════════════════
# Dispute Batch Prioritizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeBatchPrioritizer:

    def _item(self, dispute_id="D-001", **kw) -> BatchDisputeItem:
        defaults = dict(
            claim_type="INR",
            dispute_amount=100.0,
            days_until_deadline=7,
            status=DisputeStatus.NEEDS_RESPONSE,
            p_win=0.65,
            is_hdr_contributing=True,
        )
        defaults.update(kw)
        return BatchDisputeItem(dispute_id=dispute_id, **defaults)

    def test_empty_batch_returns_empty_queue(self):
        r = prioritize_disputes([])
        assert r.total_disputes == 0
        assert r.queue == []

    def test_single_dispute_ranks_first(self):
        r = prioritize_disputes([self._item()])
        assert r.queue[0].rank == 1

    def test_overdue_dispute_is_critical(self):
        r = prioritize_disputes([self._item(days_until_deadline=-2)])
        assert r.queue[0].priority_tier == PriorityTier.CRITICAL

    def test_anomaly_dispute_is_critical(self):
        r = prioritize_disputes([self._item(has_lifecycle_anomaly=True)])
        assert r.queue[0].priority_tier == PriorityTier.CRITICAL

    def test_high_score_dispute_ranks_before_low(self):
        urgent = self._item("D-URGENT", days_until_deadline=1, dispute_amount=500.0)
        routine = self._item("D-ROUTINE", days_until_deadline=14, dispute_amount=20.0)
        r = prioritize_disputes([routine, urgent])
        assert r.queue[0].dispute_id == "D-URGENT"

    def test_total_disputes_count_correct(self):
        items = [self._item(f"D-{i}") for i in range(5)]
        r = prioritize_disputes(items)
        assert r.total_disputes == 5

    def test_total_amount_at_risk_correct(self):
        items = [self._item("D-1", dispute_amount=100.0), self._item("D-2", dispute_amount=200.0)]
        r = prioritize_disputes(items)
        assert r.total_amount_at_risk == pytest.approx(300.0, abs=0.01)

    def test_hdr_contributing_count_correct(self):
        items = [
            self._item("D-1", is_hdr_contributing=True),
            self._item("D-2", is_hdr_contributing=False),
            self._item("D-3", is_hdr_contributing=True),
        ]
        r = prioritize_disputes(items)
        assert r.total_hdr_contributing == 2

    def test_tier_counts_sum_to_total(self):
        items = [self._item(f"D-{i}") for i in range(6)]
        r = prioritize_disputes(items)
        assert r.critical_count + r.high_count + r.medium_count + r.low_count == len(items)

    def test_ranks_are_sequential(self):
        items = [self._item(f"D-{i}") for i in range(4)]
        r = prioritize_disputes(items)
        ranks = [q.rank for q in r.queue]
        assert ranks == list(range(1, len(items) + 1))

    def test_overdue_in_immediate_actions(self):
        items = [self._item("D-OVER", days_until_deadline=-3)]
        r = prioritize_disputes(items)
        assert any("overdue" in a.lower() or "D-OVER" in a for a in r.immediate_actions)

    def test_work_estimate_positive(self):
        r = prioritize_disputes([self._item()])
        assert r.queue[0].work_estimate_minutes > 0

    def test_scoring_breakdown_has_expected_keys(self):
        r = prioritize_disputes([self._item()])
        breakdown = r.queue[0].scoring_breakdown
        for key in ("deadline", "amount", "p_win", "claim_type", "hdr", "sp", "anomaly"):
            assert key in breakdown

    def test_sp_eligible_adds_score(self):
        no_sp = self._item("D-NSP", sp_eligible=False, days_until_deadline=10)
        sp    = self._item("D-SP",  sp_eligible=True,  days_until_deadline=10)
        r = prioritize_disputes([no_sp, sp])
        # Same everything except SP — SP should score higher
        sp_score   = next(q for q in r.queue if q.dispute_id == "D-SP").priority_score
        nosp_score = next(q for q in r.queue if q.dispute_id == "D-NSP").priority_score
        assert sp_score > nosp_score

    def test_snad_ranks_below_inr_equal_params(self):
        inr  = self._item("D-INR",  claim_type="INR",  days_until_deadline=5)
        snad = self._item("D-SNAD", claim_type="SNAD", days_until_deadline=5)
        r = prioritize_disputes([snad, inr])
        inr_rank  = next(q for q in r.queue if q.dispute_id == "D-INR").rank
        snad_rank = next(q for q in r.queue if q.dispute_id == "D-SNAD").rank
        assert inr_rank < snad_rank  # Lower rank number = higher priority

    def test_summary_contains_total(self):
        items = [self._item(f"D-{i}") for i in range(3)]
        r = prioritize_disputes(items)
        assert "3" in r.summary

    def test_estimated_work_hours_positive(self):
        items = [self._item(f"D-{i}") for i in range(3)]
        r = prioritize_disputes(items)
        assert r.estimated_total_work_hours > 0
