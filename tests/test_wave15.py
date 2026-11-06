"""
Tests for Wave 15: evidence_quality_scorer, dispute_batch_prioritizer.
"""
import pytest
from oracle.evidence_quality_scorer import (
    score_evidence, EvidencePackage, EvidenceItem, EvidenceTier,
)
from oracle.dispute_batch_prioritizer import (
    prioritize_batch, DisputeBatchItem, PriorityBand, WorkAction,
)


# ═══════════════════════════════════════════════════════════════════
# Evidence Quality Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidenceQualityScorer:

    def _pkg(self, claim_type="INR", items=None, dispute_id="D-001") -> EvidencePackage:
        return EvidencePackage(
            dispute_id=dispute_id,
            claim_type=claim_type,
            items=items or [],
        )

    def test_no_evidence_insufficient(self):
        r = score_evidence(self._pkg())
        assert r.tier == EvidenceTier.INSUFFICIENT

    def test_normalized_score_zero_to_100(self):
        r = score_evidence(self._pkg())
        assert 0 <= r.normalized_score <= 100

    def test_excellent_tier_with_strong_inr_evidence(self):
        r = score_evidence(self._pkg(items=[
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.SIGNATURE_CONFIRMATION,
            EvidenceItem.DELIVERY_PHOTO,
            EvidenceItem.CARRIER_AFFIDAVIT,
            EvidenceItem.BUYER_MESSAGES_LOG,
        ]))
        assert r.tier in (EvidenceTier.EXCELLENT, EvidenceTier.GOOD)

    def test_good_tier_with_tracking_and_comms(self):
        r = score_evidence(self._pkg(items=[
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.BUYER_MESSAGES_LOG,
            EvidenceItem.SHIPPING_LABEL,
        ]))
        assert r.tier in (EvidenceTier.GOOD, EvidenceTier.ADEQUATE)

    def test_win_rate_boost_negative_for_no_evidence(self):
        r = score_evidence(self._pkg())
        assert r.win_rate_boost < 0

    def test_win_rate_boost_positive_for_excellent(self):
        r = score_evidence(self._pkg(items=[
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.SIGNATURE_CONFIRMATION,
            EvidenceItem.DELIVERY_PHOTO,
            EvidenceItem.CARRIER_AFFIDAVIT,
            EvidenceItem.BUYER_MESSAGES_LOG,
        ]))
        assert r.win_rate_boost > 0

    def test_critical_gaps_for_inr_without_tracking(self):
        r = score_evidence(self._pkg(claim_type="INR", items=[]))
        assert EvidenceItem.TRACKING_SHOWS_DELIVERED in r.critical_gaps or \
               EvidenceItem.TRACKING_NUMBER in r.critical_gaps

    def test_critical_gaps_empty_for_full_evidence(self):
        r = score_evidence(self._pkg(items=[
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.TRACKING_NUMBER,
            EvidenceItem.SIGNATURE_CONFIRMATION,
        ]))
        assert len(r.critical_gaps) == 0

    def test_snad_evidence_product_photos_score_high(self):
        r_snad = score_evidence(self._pkg(claim_type="SNAD", items=[
            EvidenceItem.PRODUCT_PHOTOS,
            EvidenceItem.LISTING_SCREENSHOT,
        ]))
        r_inr = score_evidence(self._pkg(claim_type="INR", items=[
            EvidenceItem.PRODUCT_PHOTOS,
            EvidenceItem.LISTING_SCREENSHOT,
        ]))
        assert r_snad.normalized_score > r_inr.normalized_score

    def test_tracking_delivered_scores_higher_for_inr(self):
        r_inr = score_evidence(self._pkg(claim_type="INR", items=[EvidenceItem.TRACKING_SHOWS_DELIVERED]))
        r_snad = score_evidence(self._pkg(claim_type="SNAD", items=[EvidenceItem.TRACKING_SHOWS_DELIVERED]))
        assert r_inr.normalized_score > r_snad.normalized_score

    def test_gaps_contain_recommended_missing_items(self):
        r = score_evidence(self._pkg(items=[]))
        gap_items = [g.item for g in r.gaps]
        assert len(gap_items) > 0

    def test_items_present_count_matches(self):
        items = [EvidenceItem.TRACKING_NUMBER, EvidenceItem.BUYER_MESSAGES_LOG]
        r = score_evidence(self._pkg(items=items))
        assert r.items_present_count == 2

    def test_top_evidence_items_sorted_by_score(self):
        r = score_evidence(self._pkg(items=[
            EvidenceItem.TRACKING_SHOWS_DELIVERED,
            EvidenceItem.TRACKING_NUMBER,
            EvidenceItem.BUYER_MESSAGES_LOG,
        ]))
        if len(r.top_evidence_items) >= 2:
            assert r.top_evidence_items[0][1] >= r.top_evidence_items[1][1]

    def test_collection_priority_non_empty(self):
        r = score_evidence(self._pkg())
        assert len(r.collection_priority) > 0

    def test_summary_contains_dispute_id(self):
        r = score_evidence(self._pkg(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_unauthorized_ip_scores_high(self):
        r = score_evidence(self._pkg(claim_type="UNAUTHORIZED", items=[
            EvidenceItem.MATCHING_IP_ADDRESS,
            EvidenceItem.MATCHING_DEVICE,
            EvidenceItem.PRIOR_SUCCESSFUL_ORDERS,
        ]))
        assert r.tier in (EvidenceTier.EXCELLENT, EvidenceTier.GOOD)

    def test_subscription_tos_critical(self):
        r = score_evidence(self._pkg(claim_type="SUBSCRIPTION", items=[]))
        critical = r.critical_gaps
        assert EvidenceItem.TOS_ACCEPTANCE in critical or EvidenceItem.CANCELLATION_POLICY in critical


# ═══════════════════════════════════════════════════════════════════
# Dispute Batch Prioritizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeBatchPrioritizer:

    def _item(self, dispute_id="D-001", days=10, ev=20.0, **kw) -> DisputeBatchItem:
        return DisputeBatchItem(
            dispute_id=dispute_id,
            claim_type="INR",
            dispute_amount=kw.pop("amount", 150.0),
            days_until_deadline=days,
            ev_fight=ev,
            is_hdr_contributing=kw.pop("hdr", True),
            evidence_ready=kw.pop("evidence_ready", True),
            evidence_score=kw.pop("evidence_score", 70.0),
            response_status=kw.pop("response_status", "NOT_STARTED"),
            recommended_action=kw.pop("recommended_action", "AUTO_FIGHT"),
            is_fraud_flagged=kw.pop("is_fraud_flagged", False),
            estimated_work_hours=kw.pop("work_hours", 1.0),
            buyer_tier="STANDARD_BUYER",
        )

    def test_empty_batch_returns_empty_queue(self):
        r = prioritize_batch([])
        assert r.total_disputes == 0
        assert len(r.queue) == 0

    def test_p0_for_deadline_zero(self):
        r = prioritize_batch([self._item(days=0)])
        assert r.queue[0].priority_band == PriorityBand.P0_FIRE

    def test_p1_for_deadline_2_days(self):
        r = prioritize_batch([self._item(days=2)])
        assert r.queue[0].priority_band in (PriorityBand.P0_FIRE, PriorityBand.P1_TODAY)

    def test_p3_for_deadline_15_days(self):
        # Low EV, no HDR, no evidence ready → lower score → P3/P4
        r = prioritize_batch([self._item(days=15, ev=5.0, hdr=False, evidence_ready=False, evidence_score=10.0)])
        assert r.queue[0].priority_band in (PriorityBand.P2_THIS_WEEK, PriorityBand.P3_QUEUE, PriorityBand.P4_DEFER)

    def test_urgent_dispute_sorted_first(self):
        urgent = self._item("D-URGENT", days=1)
        normal = self._item("D-NORMAL", days=15)
        r = prioritize_batch([normal, urgent])
        assert r.queue[0].dispute_id == "D-URGENT"

    def test_band_counts_sum_to_total(self):
        items = [self._item(f"D-{i:03d}", days=d) for i, d in enumerate([0, 2, 5, 10, 20])]
        r = prioritize_batch(items)
        total = r.p0_count + r.p1_count + r.p2_count + r.p3_count + r.p4_count
        assert total == len(items)

    def test_work_action_submit_evidence_when_ready(self):
        item = self._item(evidence_ready=True, evidence_score=75.0, recommended_action="AUTO_FIGHT")
        r = prioritize_batch([item])
        assert r.queue[0].work_action == WorkAction.SUBMIT_EVIDENCE

    def test_work_action_collect_evidence_when_not_ready(self):
        item = self._item(evidence_ready=False, evidence_score=20.0, recommended_action="AUTO_FIGHT")
        r = prioritize_batch([item])
        assert r.queue[0].work_action == WorkAction.COLLECT_EVIDENCE

    def test_work_action_process_refund(self):
        item = self._item(recommended_action="AUTO_REFUND")
        r = prioritize_batch([item])
        assert r.queue[0].work_action == WorkAction.PROCESS_REFUND

    def test_work_action_escalate_for_fraud(self):
        item = self._item(is_fraud_flagged=True)
        r = prioritize_batch([item])
        assert r.queue[0].work_action == WorkAction.ESCALATE

    def test_work_action_monitor_for_submitted(self):
        item = self._item(response_status="SUBMITTED")
        r = prioritize_batch([item])
        assert r.queue[0].work_action == WorkAction.MONITOR

    def test_total_ev_sum_computed(self):
        items = [self._item(f"D-{i:03d}", ev=float(i * 10)) for i in range(5)]
        r = prioritize_batch(items)
        expected = sum(float(i * 10) for i in range(5))
        assert r.total_ev_at_stake == pytest.approx(expected, abs=0.01)

    def test_hdr_dispute_count(self):
        items = [
            self._item("D-001", hdr=True),
            self._item("D-002", hdr=False),
            self._item("D-003", hdr=True),
        ]
        r = prioritize_batch(items)
        assert r.hdr_dispute_count == 2

    def test_can_complete_today_lte_p0_p1_count(self):
        items = [self._item(f"D-{i:03d}", days=d) for i, d in enumerate([0, 1, 2, 10, 20])]
        r = prioritize_batch(items)
        assert r.can_complete_today <= r.p0_count + r.p1_count

    def test_next_steps_non_empty(self):
        item = self._item()
        r = prioritize_batch([item])
        assert len(r.queue[0].next_steps) > 0

    def test_summary_contains_total_count(self):
        items = [self._item(f"D-{i:03d}") for i in range(5)]
        r = prioritize_batch(items)
        assert "5" in r.summary

    def test_priority_scores_sorted_within_band(self):
        items = [self._item(f"D-{i:03d}", days=5, ev=float(i * 15)) for i in range(5)]
        r = prioritize_batch(items)
        # All in same band — verify descending score within that band
        band = r.queue[0].priority_band
        band_scores = [q.priority_score for q in r.queue if q.priority_band == band]
        assert band_scores == sorted(band_scores, reverse=True)
