"""
Tests for Wave 30: dispute_auto_responder, refund_impact_analyzer.
"""
import pytest
from oracle.dispute_auto_responder import (
    generate_response_plan, DisputeAutoResponderInput,
    ResponseStrategy, ResponseTone, EvidenceAvailability,
)
from oracle.refund_impact_analyzer import (
    analyze_refund_impact, RefundImpactInput, RefundRecommendation,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Auto-Responder Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeAutoResponder:

    def _inp(self, **kw) -> DisputeAutoResponderInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            transaction_amount=150.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            p_win=0.65,
            fight_cost=10.0,
            chargeback_fee=20.0,
        )
        defaults.update(kw)
        return DisputeAutoResponderInput(**defaults)

    # ── Strategy selection ────────────────────────────────────────

    def test_fight_when_evidence_complete_high_pwin(self):
        r = generate_response_plan(self._inp())
        assert r.strategy == ResponseStrategy.FIGHT

    def test_accept_tiny_amount(self):
        r = generate_response_plan(self._inp(
            transaction_amount=5.0,
            has_tracking_number=False,
            has_delivery_confirmation=False,
            p_win=0.30,
        ))
        assert r.strategy in (ResponseStrategy.ACCEPT, ResponseStrategy.SETTLE)

    def test_settle_no_evidence_large_amount(self):
        r = generate_response_plan(self._inp(
            has_tracking_number=False,
            has_delivery_confirmation=False,
            transaction_amount=200.0,
            p_win=0.30,
        ))
        assert r.strategy in (ResponseStrategy.SETTLE, ResponseStrategy.ACCEPT)

    def test_escalate_when_ce30_applicable(self):
        r = generate_response_plan(self._inp(
            claim_type="UNAUTHORIZED",
            has_ip_log=True,
            has_two_fa_record=True,
            ce30_applicable=True,
            p_win=0.76,
        ))
        assert r.strategy == ResponseStrategy.ESCALATE

    # ── Evidence assessment ───────────────────────────────────────

    def test_all_present_inr_with_full_evidence(self):
        r = generate_response_plan(self._inp(
            has_tracking_number=True, has_delivery_confirmation=True))
        assert r.evidence_availability == EvidenceAvailability.ALL_PRESENT

    def test_partial_evidence_one_item_missing(self):
        r = generate_response_plan(self._inp(
            has_tracking_number=True, has_delivery_confirmation=False))
        assert r.evidence_availability == EvidenceAvailability.PARTIAL

    def test_no_evidence_inr(self):
        r = generate_response_plan(self._inp(
            has_tracking_number=False, has_delivery_confirmation=False))
        assert r.evidence_availability == EvidenceAvailability.NONE

    # ── Tone selection ────────────────────────────────────────────

    def test_de_escalate_tone_for_hostile_buyer(self):
        r = generate_response_plan(self._inp(buyer_hostile=True))
        assert r.tone == ResponseTone.DE_ESCALATE

    def test_formal_tone_for_unauthorized(self):
        r = generate_response_plan(self._inp(
            claim_type="UNAUTHORIZED", has_ip_log=True, buyer_hostile=False))
        assert r.tone == ResponseTone.FORMAL

    # ── Key points ────────────────────────────────────────────────

    def test_key_points_non_empty(self):
        r = generate_response_plan(self._inp())
        assert len(r.key_points) >= 1

    def test_tracking_key_point_when_present(self):
        r = generate_response_plan(self._inp(has_tracking_number=True))
        combined = " ".join(r.key_points).lower()
        assert "tracking" in combined

    def test_ip_log_key_point_for_unauthorized(self):
        r = generate_response_plan(self._inp(
            claim_type="UNAUTHORIZED", has_ip_log=True, p_win=0.65))
        combined = " ".join(r.key_points).lower()
        assert "ip" in combined

    def test_ce30_key_point_when_applicable(self):
        r = generate_response_plan(self._inp(
            claim_type="UNAUTHORIZED",
            has_ip_log=True, has_two_fa_record=True,
            ce30_applicable=True, p_win=0.76,
        ))
        combined = " ".join(r.key_points).lower()
        assert "ce3" in combined or "prior" in combined

    # ── Evidence checklist ────────────────────────────────────────

    def test_checklist_non_empty(self):
        r = generate_response_plan(self._inp())
        assert len(r.evidence_checklist) >= 1

    def test_checklist_shows_missing(self):
        r = generate_response_plan(self._inp(has_delivery_confirmation=False))
        missing = [c for c in r.evidence_checklist if "MISSING" in c]
        assert len(missing) >= 1

    def test_checklist_shows_ready(self):
        r = generate_response_plan(self._inp(has_tracking_number=True))
        ready = [c for c in r.evidence_checklist if "READY" in c]
        assert len(ready) >= 1

    # ── Deadline warning ──────────────────────────────────────────

    def test_deadline_warning_at_1d(self):
        r = generate_response_plan(self._inp(days_until_deadline=1))
        assert r.deadline_warning is not None
        assert "CRITICAL" in r.deadline_warning.upper()

    def test_no_deadline_warning_at_10d(self):
        r = generate_response_plan(self._inp(days_until_deadline=10))
        assert r.deadline_warning is None

    # ── SP / CE3.0 notes ──────────────────────────────────────────

    def test_sp_note_when_eligible(self):
        r = generate_response_plan(self._inp(sp_eligible=True))
        assert r.sp_note is not None

    def test_no_sp_note_when_ineligible(self):
        r = generate_response_plan(self._inp(sp_eligible=False))
        assert r.sp_note is None

    def test_ce30_note_when_applicable(self):
        r = generate_response_plan(self._inp(
            claim_type="UNAUTHORIZED", ce30_applicable=True,
            has_ip_log=True, p_win=0.76))
        assert r.ce30_note is not None

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = generate_response_plan(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary


# ═══════════════════════════════════════════════════════════════════
# Refund Impact Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundImpactAnalyzer:

    def _inp(self, **kw) -> RefundImpactInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=100.0,
            gross_margin_pct=0.40,
            chargeback_fee=20.0,
            fight_cost=10.0,
            p_win=0.60,
        )
        defaults.update(kw)
        return RefundImpactInput(**defaults)

    # ── Cost calculations ─────────────────────────────────────────

    def test_full_refund_cost_equals_amount(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, paypal_processing_fee=0.0))
        assert r.cost_full_refund == pytest.approx(100.0)

    def test_full_refund_includes_processing_fee(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, paypal_processing_fee=5.0))
        assert r.cost_full_refund == pytest.approx(105.0)

    def test_fight_win_cost_is_just_fight_cost(self):
        r = analyze_refund_impact(self._inp(fight_cost=10.0))
        assert r.cost_fight_win == pytest.approx(10.0)

    def test_fight_lose_cost_includes_chargeback_fee(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, chargeback_fee=20.0,
            fight_cost=10.0, paypal_processing_fee=0.0))
        assert r.cost_fight_lose == pytest.approx(130.0)

    def test_ev_fight_between_win_and_lose_costs(self):
        r = analyze_refund_impact(self._inp())
        assert r.cost_fight_win <= r.ev_fight <= r.cost_fight_lose

    # ── COGS ─────────────────────────────────────────────────────

    def test_cogs_from_explicit_field(self):
        r = analyze_refund_impact(self._inp(cogs=30.0))
        assert r.cogs == pytest.approx(30.0)

    def test_cogs_derived_from_margin(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, gross_margin_pct=0.40, cogs=0.0))
        assert r.cogs == pytest.approx(60.0)  # 100 * (1 - 0.40)

    def test_gross_margin_correct(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, gross_margin_pct=0.40))
        assert r.gross_margin == pytest.approx(40.0)

    # ── Recommendations ───────────────────────────────────────────

    def test_fight_recommended_high_pwin(self):
        r = analyze_refund_impact(self._inp(p_win=0.80))
        assert r.recommendation == RefundRecommendation.FIGHT

    def test_no_fight_on_tiny_amount(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=5.0, fight_cost=10.0, chargeback_fee=20.0))
        # Tiny amount — should not fight (too expensive); accept or partial are both valid
        assert r.recommendation in (
            RefundRecommendation.FULL_REFUND, RefundRecommendation.PARTIAL_REFUND)

    def test_fight_always_on_fraud(self):
        r = analyze_refund_impact(self._inp(
            is_fraud_risk=True, p_win=0.40))
        assert r.recommendation == RefundRecommendation.FIGHT

    # ── LTV adjustments ───────────────────────────────────────────

    def test_vip_customer_has_ltv_adjustment(self):
        r = analyze_refund_impact(self._inp(is_vip=True))
        assert r.ltv_adjustment > 0

    def test_no_ltv_for_new_customer(self):
        r = analyze_refund_impact(self._inp(
            is_vip=False, is_repeat_customer=False, customer_ltv=0.0))
        assert r.ltv_adjustment == 0.0

    # ── HDR fee ───────────────────────────────────────────────────

    def test_hdr_fee_applied(self):
        r = analyze_refund_impact(self._inp(hdr_fee_per_dispute=30.0))
        assert r.hdr_fee_applicable is True
        assert r.hdr_note is not None

    def test_no_hdr_fee_note_when_zero(self):
        r = analyze_refund_impact(self._inp(hdr_fee_per_dispute=0.0))
        assert r.hdr_note is None

    # ── Breakdowns ────────────────────────────────────────────────

    def test_breakdowns_non_empty(self):
        r = analyze_refund_impact(self._inp())
        assert len(r.breakdowns) >= 3

    def test_all_breakdowns_have_positive_cost(self):
        r = analyze_refund_impact(self._inp())
        for b in r.breakdowns:
            assert b.gross_cost >= 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_refund_impact(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_actions_non_empty(self):
        r = analyze_refund_impact(self._inp())
        assert len(r.immediate_actions) >= 1

    def test_partial_amount_matches_pct(self):
        r = analyze_refund_impact(self._inp(
            transaction_amount=100.0, partial_refund_pct=0.60))
        assert r.partial_refund_amount == pytest.approx(60.0)
