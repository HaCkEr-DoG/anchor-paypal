"""
Tests for Wave 23: evidence_quality_scorer, deadline_manager.
"""
import pytest
from oracle.evidence_quality_scorer import (
    score_evidence_quality, EvidenceQualityInput, EvidenceItem,
    EvidenceType, ClaimType, EvidenceStrength, EvidenceGapSeverity,
)
from oracle.deadline_manager import (
    compute_deadlines, DeadlineInput, NetworkType, DisputePhase,
    DeadlineType, DeadlineUrgency,
)


# ═══════════════════════════════════════════════════════════════════
# Evidence Quality Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidenceQualityScorer:

    def _full_inr_package(self) -> list[EvidenceItem]:
        return [
            EvidenceItem(EvidenceType.TRACKING_NUMBER,       True,  True,  True),
            EvidenceItem(EvidenceType.PROOF_OF_DELIVERY,     True,  True,  True),
            EvidenceItem(EvidenceType.CARRIER_CONFIRMATION,  True,  True,  True),
            EvidenceItem(EvidenceType.COMMUNICATION_LOG,     True,  True,  True),
        ]

    def _minimal_inr_package(self) -> list[EvidenceItem]:
        return [
            EvidenceItem(EvidenceType.TRACKING_NUMBER,  True, True, True),
            EvidenceItem(EvidenceType.PROOF_OF_DELIVERY, True, True, True),
        ]

    def _inp(self, claim_type=ClaimType.INR,
             evidence_items=None, **kw) -> EvidenceQualityInput:
        defaults = dict(
            dispute_id="D-001",
            dispute_amount=150.0,
            days_until_deadline=7,
        )
        defaults.update(kw)
        if evidence_items is None:
            evidence_items = self._full_inr_package()
        return EvidenceQualityInput(
            claim_type=claim_type,
            evidence_items=evidence_items,
            **defaults,
        )

    # ── Score range ───────────────────────────────────────────────

    def test_composite_score_range(self):
        r = score_evidence_quality(self._inp())
        assert 0 <= r.composite_score <= 100

    def test_full_inr_package_scores_high(self):
        r = score_evidence_quality(self._inp(evidence_items=self._full_inr_package()))
        assert r.composite_score >= 70

    def test_empty_evidence_scores_poor(self):
        r = score_evidence_quality(self._inp(evidence_items=[]))
        assert r.strength == EvidenceStrength.POOR

    def test_full_package_no_critical_gaps(self):
        r = score_evidence_quality(self._inp(evidence_items=self._full_inr_package()))
        assert r.critical_gaps == 0

    def test_missing_proof_of_delivery_critical_gap(self):
        items = [EvidenceItem(EvidenceType.TRACKING_NUMBER, True, True, True)]
        r = score_evidence_quality(self._inp(evidence_items=items))
        assert r.critical_gaps >= 1

    # ── Strength tiers ────────────────────────────────────────────

    def test_full_package_strong_or_adequate(self):
        r = score_evidence_quality(self._inp(evidence_items=self._full_inr_package()))
        assert r.strength in (EvidenceStrength.STRONG, EvidenceStrength.ADEQUATE)

    def test_no_evidence_poor_strength(self):
        r = score_evidence_quality(self._inp(evidence_items=[]))
        assert r.strength == EvidenceStrength.POOR

    def test_minimal_package_weak_or_better(self):
        r = score_evidence_quality(self._inp(evidence_items=self._minimal_inr_package()))
        assert r.strength in (EvidenceStrength.ADEQUATE, EvidenceStrength.WEAK)

    # ── Submit recommendation ────────────────────────────────────

    def test_full_package_submit_recommended(self):
        r = score_evidence_quality(self._inp(evidence_items=self._full_inr_package()))
        assert r.submit_recommended is True

    def test_no_evidence_submit_not_recommended(self):
        r = score_evidence_quality(self._inp(evidence_items=[]))
        assert r.submit_recommended is False

    def test_critical_gap_high_submission_risk(self):
        items = [EvidenceItem(EvidenceType.TRACKING_NUMBER, True, True, True)]
        r = score_evidence_quality(self._inp(evidence_items=items))
        assert r.submission_risk == "HIGH"

    # ── SNAD claim ───────────────────────────────────────────────

    def test_snad_missing_listing_critical_gap(self):
        items = [EvidenceItem(EvidenceType.ITEM_PHOTOS, True, True, True)]
        r = score_evidence_quality(self._inp(claim_type=ClaimType.SNAD, evidence_items=items))
        assert r.critical_gaps >= 1

    def test_snad_full_package_scores_high(self):
        items = [
            EvidenceItem(EvidenceType.PRODUCT_LISTING,   True, True, True),
            EvidenceItem(EvidenceType.DESCRIPTION_MATCH, True, True, True),
            EvidenceItem(EvidenceType.ITEM_PHOTOS,       True, True, True),
            EvidenceItem(EvidenceType.RETURN_POLICY,     True, True, True),
            EvidenceItem(EvidenceType.COMMUNICATION_LOG, True, True, True),
        ]
        r = score_evidence_quality(self._inp(claim_type=ClaimType.SNAD, evidence_items=items))
        assert r.composite_score >= 70

    # ── UNAUTHORIZED claim ───────────────────────────────────────

    def test_unauthorized_missing_ip_critical_gap(self):
        items = [EvidenceItem(EvidenceType.DEVICE_FINGERPRINT, True, True, True)]
        r = score_evidence_quality(self._inp(claim_type=ClaimType.UNAUTHORIZED, evidence_items=items))
        assert r.critical_gaps >= 1

    # ── High-value signature requirement ─────────────────────────

    def test_high_value_missing_signature_adds_gap(self):
        items = self._full_inr_package()
        r = score_evidence_quality(self._inp(
            evidence_items=items,
            order_above_signature_threshold=True,
        ))
        gap_types = [g.evidence_type for g in r.gaps]
        assert EvidenceType.SIGNATURE_CONFIRMATION in gap_types

    def test_high_value_with_signature_no_sig_gap(self):
        items = self._full_inr_package() + [
            EvidenceItem(EvidenceType.SIGNATURE_CONFIRMATION, True, True, True),
        ]
        r = score_evidence_quality(self._inp(
            evidence_items=items,
            order_above_signature_threshold=True,
        ))
        sig_gaps = [g for g in r.gaps
                    if g.evidence_type == EvidenceType.SIGNATURE_CONFIRMATION]
        assert len(sig_gaps) == 0

    # ── Report fields ─────────────────────────────────────────────

    def test_five_components_returned(self):
        r = score_evidence_quality(self._inp())
        assert len(r.components) == 5

    def test_weights_sum_to_one(self):
        r = score_evidence_quality(self._inp())
        total = sum(c.weight for c in r.components)
        assert abs(total - 1.0) < 0.001

    def test_summary_contains_dispute_id(self):
        r = score_evidence_quality(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_top_actions_non_empty_when_gaps(self):
        items = [EvidenceItem(EvidenceType.TRACKING_NUMBER, True, True, True)]
        r = score_evidence_quality(self._inp(evidence_items=items))
        assert len(r.top_actions) > 0

    def test_specific_items_boost_specificity_score(self):
        generic = [EvidenceItem(EvidenceType.TRACKING_NUMBER, True, False, False)]
        specific = [EvidenceItem(EvidenceType.TRACKING_NUMBER, True, True,  True)]
        r_generic  = score_evidence_quality(self._inp(evidence_items=generic))
        r_specific = score_evidence_quality(self._inp(evidence_items=specific))
        spec_score_g = next(c.score for c in r_generic.components  if c.dimension == "specificity")
        spec_score_s = next(c.score for c in r_specific.components if c.dimension == "specificity")
        assert spec_score_s > spec_score_g


# ═══════════════════════════════════════════════════════════════════
# Deadline Manager Tests
# ═══════════════════════════════════════════════════════════════════

class TestDeadlineManager:

    def _inp(self, **kw) -> DeadlineInput:
        defaults = dict(
            dispute_id="D-001",
            network=NetworkType.PAYPAL,
            phase=DisputePhase.OPEN,
            days_since_dispute_opened=3,
            has_responded=False,
            has_submitted_evidence=False,
            has_filed_appeal=False,
        )
        defaults.update(kw)
        return DeadlineInput(**defaults)

    # ── Basic functionality ───────────────────────────────────────

    def test_paypal_response_deadline_computed(self):
        r = compute_deadlines(self._inp())
        types = [d.deadline_type for d in r.deadlines]
        assert DeadlineType.MERCHANT_RESPONSE in types

    def test_paypal_10_day_response_window(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=3))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.days_remaining == 7   # 10 - 3

    def test_visa_20_day_response_window(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            days_since_dispute_opened=5,
        ))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.days_remaining == 15  # 20 - 5

    def test_mastercard_45_day_response_window(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.MASTERCARD,
            days_since_dispute_opened=10,
        ))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.days_remaining == 35  # 45 - 10

    # ── Overdue detection ─────────────────────────────────────────

    def test_overdue_response_detected(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=12))
        assert r.overdue_count >= 1

    def test_overdue_urgency_red(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=12))
        resp = next((d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE), None)
        if resp:
            assert resp.urgency == DeadlineUrgency.RED

    def test_overdue_negative_days_remaining(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=15))
        overdue = [d for d in r.deadlines if d.is_overdue]
        assert all(d.days_remaining < 0 for d in overdue)

    # ── Appeal deadlines ──────────────────────────────────────────

    def test_paypal_appeal_10_day_window(self):
        r = compute_deadlines(self._inp(
            phase=DisputePhase.RULING_ISSUED,
            days_since_ruling=3,
            has_filed_appeal=False,
        ))
        appeal = next((d for d in r.deadlines if d.deadline_type == DeadlineType.APPEAL_PAYPAL), None)
        assert appeal is not None
        assert appeal.days_remaining == 7   # 10 - 3

    def test_visa_appeal_45_day_window(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            phase=DisputePhase.RULING_ISSUED,
            days_since_ruling=10,
            has_filed_appeal=False,
        ))
        appeal = next((d for d in r.deadlines if d.deadline_type == DeadlineType.APPEAL_NETWORK), None)
        assert appeal is not None
        assert appeal.days_remaining == 35  # 45 - 10

    def test_no_appeal_deadline_without_ruling(self):
        r = compute_deadlines(self._inp(days_since_ruling=-1))
        types = [d.deadline_type for d in r.deadlines]
        assert DeadlineType.APPEAL_PAYPAL not in types
        assert DeadlineType.APPEAL_NETWORK not in types

    # ── CE3.0 deadline ────────────────────────────────────────────

    def test_ce30_deadline_computed_when_notified(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            is_ce30_notified=True,
            days_since_ce30_notification=5,
        ))
        types = [d.deadline_type for d in r.deadlines]
        assert DeadlineType.CE30_REBUTTAL in types

    def test_ce30_20_day_window(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            is_ce30_notified=True,
            days_since_ce30_notification=8,
        ))
        ce30 = next(d for d in r.deadlines if d.deadline_type == DeadlineType.CE30_REBUTTAL)
        assert ce30.days_remaining == 12   # 20 - 8

    # ── Urgency levels ────────────────────────────────────────────

    def test_1_day_remaining_is_red(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=9))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.urgency == DeadlineUrgency.RED

    def test_3_days_remaining_is_orange(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=7))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.urgency == DeadlineUrgency.ORANGE

    def test_8_days_remaining_is_green(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            days_since_dispute_opened=2,
        ))
        resp = next(d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE)
        assert resp.urgency == DeadlineUrgency.GREEN

    # ── Report structure ──────────────────────────────────────────

    def test_responded_dispute_no_response_deadline(self):
        r = compute_deadlines(self._inp(has_responded=True, has_submitted_evidence=True))
        types = [d.deadline_type for d in r.deadlines]
        assert DeadlineType.MERCHANT_RESPONSE not in types

    def test_schedule_non_empty(self):
        r = compute_deadlines(self._inp())
        assert len(r.schedule) > 0

    def test_summary_contains_dispute_id(self):
        r = compute_deadlines(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_most_urgent_is_first_in_deadlines(self):
        r = compute_deadlines(self._inp())
        if r.deadlines and r.most_urgent:
            assert r.deadlines[0].deadline_type == r.most_urgent.deadline_type

    def test_overall_urgency_red_when_overdue(self):
        r = compute_deadlines(self._inp(days_since_dispute_opened=15))
        assert r.overall_urgency == DeadlineUrgency.RED

    def test_visa_extended_response_window(self):
        r = compute_deadlines(self._inp(
            network=NetworkType.VISA,
            days_since_dispute_opened=22,
            response_extended=True,
        ))
        resp = next((d for d in r.deadlines if d.deadline_type == DeadlineType.MERCHANT_RESPONSE), None)
        if resp:
            assert resp.days_remaining == 23   # 45 - 22
