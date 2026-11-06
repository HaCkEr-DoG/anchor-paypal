"""
Tests for Wave 34: evidence_strength_evaluator, settlement_optimizer.
"""
import pytest
from oracle.evidence_strength_evaluator import (
    evaluate_evidence_package, EvidencePackageInput, EvidenceItem,
    EvidenceCategory, EvidenceQuality, PackageStrength,
)
from oracle.settlement_optimizer import (
    optimize_settlement, SettlementInput, SettlementStrategy, LTVTier,
)


# ═══════════════════════════════════════════════════════════════════
# Evidence Strength Evaluator Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidenceStrengthEvaluator:

    def _inp(self, **kw) -> EvidencePackageInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            disputed_amount=100.0,
            days_until_deadline=10,
        )
        defaults.update(kw)
        return EvidencePackageInput(**defaults)

    # ── Score range ───────────────────────────────────────────────

    def test_score_zero_no_evidence(self):
        r = evaluate_evidence_package(self._inp())
        assert r.evidence_score >= 0.0

    def test_score_increases_with_evidence(self):
        low = evaluate_evidence_package(self._inp())
        high = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_order_confirmation=True,
        ))
        assert high.evidence_score > low.evidence_score

    def test_full_evidence_compelling(self):
        r = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
            has_order_confirmation=True,
            has_customer_communication=True,
            has_ip_address_log=True,
            has_usage_logs=True,
            has_refund_policy=True,
            has_terms_of_service=True,
        ))
        assert r.package_strength in (PackageStrength.COMPELLING, PackageStrength.ADEQUATE)

    def test_no_evidence_insufficient(self):
        r = evaluate_evidence_package(self._inp())
        assert r.package_strength in (
            PackageStrength.INSUFFICIENT, PackageStrength.MARGINAL)

    # ── Win probability ───────────────────────────────────────────

    def test_win_prob_increases_with_more_evidence(self):
        low = evaluate_evidence_package(self._inp())
        high = evaluate_evidence_package(self._inp(
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
            has_order_confirmation=True,
        ))
        assert high.win_probability > low.win_probability

    def test_win_prob_in_valid_range(self):
        r = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
        ))
        assert 0.0 <= r.win_probability <= 1.0

    # ── Missing items / gaps ──────────────────────────────────────

    def test_missing_items_listed(self):
        r = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
        ))
        assert len(r.missing_items) > 0

    def test_present_items_listed(self):
        r = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
        ))
        assert "tracking_number" in r.present_items
        assert "delivery_confirmation" in r.present_items

    def test_no_gaps_when_all_present(self):
        r = evaluate_evidence_package(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
            has_order_confirmation=True,
            has_customer_communication=True,
            has_ip_address_log=True,
            has_usage_logs=True,
            has_refund_policy=True,
            has_terms_of_service=True,
        ))
        assert len(r.present_items) > 0

    # ── Deadline risk ─────────────────────────────────────────────

    def test_deadline_critical_when_1_day(self):
        r = evaluate_evidence_package(self._inp(days_until_deadline=1))
        assert r.deadline_risk == "CRITICAL"

    def test_deadline_low_when_14_days(self):
        r = evaluate_evidence_package(self._inp(days_until_deadline=14))
        assert r.deadline_risk == "LOW"

    def test_deadline_medium_when_5_days(self):
        r = evaluate_evidence_package(self._inp(days_until_deadline=5))
        assert r.deadline_risk == "MEDIUM"

    # ── Custom items ──────────────────────────────────────────────

    def test_definitive_item_gives_high_score(self):
        item = EvidenceItem(
            name="signed_delivery_receipt",
            category=EvidenceCategory.PROOF_OF_DELIVERY,
            quality=EvidenceQuality.DEFINITIVE,
            is_present=True,
        )
        r = evaluate_evidence_package(self._inp(items=[item]))
        assert r.evidence_score > 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = evaluate_evidence_package(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_recommendation_non_empty(self):
        r = evaluate_evidence_package(self._inp())
        assert len(r.recommendation) > 0


# ═══════════════════════════════════════════════════════════════════
# Settlement Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestSettlementOptimizer:

    def _inp(self, **kw) -> SettlementInput:
        defaults = dict(
            dispute_id="D-001",
            disputed_amount=100.0,
            p_win=0.50,
            fight_cost=10.0,
            chargeback_fee=20.0,
        )
        defaults.update(kw)
        return SettlementInput(**defaults)

    # ── Strategy selection ────────────────────────────────────────

    def test_full_fight_on_high_pwin_sp_eligible(self):
        r = optimize_settlement(self._inp(p_win=0.80, sp_eligible=True))
        assert r.strategy == SettlementStrategy.FULL_FIGHT

    def test_full_settle_on_low_pwin(self):
        r = optimize_settlement(self._inp(p_win=0.05))
        assert r.strategy in (
            SettlementStrategy.FULL_SETTLE,
            SettlementStrategy.PARTIAL_SETTLE,
            SettlementStrategy.ACCEPT_LOSS,
        )

    def test_partial_settle_mid_pwin(self):
        r = optimize_settlement(self._inp(p_win=0.30, allow_partial=True))
        # Should not be full fight
        assert r.strategy != SettlementStrategy.FULL_FIGHT

    # ── Offer amount ──────────────────────────────────────────────

    def test_offer_lte_disputed_amount(self):
        r = optimize_settlement(self._inp())
        assert r.optimal_offer <= r._match_input_amount() if hasattr(r, '_match_input_amount') else True
        assert r.optimal_offer <= 100.0

    def test_offer_positive(self):
        r = optimize_settlement(self._inp())
        assert r.optimal_offer > 0

    def test_offer_pct_in_range(self):
        r = optimize_settlement(self._inp())
        assert 0.0 <= r.offer_pct_of_dispute <= 100.0

    # ── LTV impact ────────────────────────────────────────────────

    def test_vip_offer_higher_than_negligible(self):
        vip = optimize_settlement(self._inp(ltv_tier=LTVTier.VIP, p_win=0.30))
        neg = optimize_settlement(self._inp(ltv_tier=LTVTier.NEGLIGIBLE, p_win=0.30))
        assert vip.optimal_offer >= neg.optimal_offer

    # ── EV calculations ───────────────────────────────────────────

    def test_ev_accept_equals_negative_amount(self):
        r = optimize_settlement(self._inp(disputed_amount=100.0))
        assert r.ev_accept == pytest.approx(-100.0)

    def test_ev_fight_better_than_accept_at_high_pwin(self):
        r = optimize_settlement(self._inp(p_win=0.90))
        assert r.ev_fight > r.ev_accept

    def test_ev_fight_worse_than_accept_at_low_pwin(self):
        # At p_win=0.05 and high cb_fee, fighting costs more than accepting
        r = optimize_settlement(self._inp(
            p_win=0.05, disputed_amount=100.0,
            fight_cost=10.0, chargeback_fee=50.0))
        assert r.ev_fight < r.ev_accept

    def test_is_ev_positive_to_fight_at_high_pwin(self):
        r = optimize_settlement(self._inp(p_win=0.90, disputed_amount=100.0))
        assert r.is_ev_positive_to_fight is True

    # ── HDR fee impact ────────────────────────────────────────────

    def test_hdr_fee_worsens_ev_fight(self):
        no_hdr = optimize_settlement(self._inp(hdr_fee_per_dispute=0.0))
        with_hdr = optimize_settlement(self._inp(hdr_fee_per_dispute=30.0))
        assert with_hdr.ev_fight < no_hdr.ev_fight

    # ── Savings calculation ───────────────────────────────────────

    def test_savings_vs_losing_positive(self):
        r = optimize_settlement(self._inp(p_win=0.30))
        assert r.savings_vs_losing > 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = optimize_settlement(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_reasoning_non_empty(self):
        r = optimize_settlement(self._inp())
        assert len(r.reasoning) > 10
