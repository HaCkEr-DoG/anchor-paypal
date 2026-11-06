"""
Tests for Wave 51: refund_negotiation_advisor, evidence_gap_analyzer.
"""
import pytest
from oracle.refund_negotiation_advisor import (
    advise_negotiation, NegotiationInput,
    NegotiationStrategy, BuyerIntentSignal, LtvTier,
)
from oracle.evidence_gap_analyzer import (
    analyze_evidence_gaps, EvidenceGapInput,
    EvidenceReadiness, EvidenceStatus,
)


# ═══════════════════════════════════════════════════════════════════
# Refund Negotiation Advisor Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundNegotiationAdvisor:

    def _inp(self, amount=200.0, **kw) -> NegotiationInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=amount,
            reason_code="INR",
            network="VISA",
        )
        defaults.update(kw)
        return NegotiationInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = advise_negotiation(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_scenarios_count(self):
        r = advise_negotiation(self._inp())
        assert len(r.scenarios) == 3

    def test_counter_offer_pct_bounded(self):
        r = advise_negotiation(self._inp())
        assert 0.10 <= r.counter_offer_pct <= 0.70

    def test_counter_offer_amount_derived_from_pct(self):
        r = advise_negotiation(self._inp(amount=100.0))
        assert abs(r.counter_offer_amount - 100.0 * r.counter_offer_pct) < 0.01

    # ── Buyer intent classification ──────────────────────────────

    def test_serial_abuser_classification(self):
        r = advise_negotiation(self._inp(buyer_prior_disputes=6))
        assert r.buyer_intent == BuyerIntentSignal.SERIAL_ABUSER

    def test_opportunistic_classification(self):
        r = advise_negotiation(self._inp(buyer_prior_disputes=3, buyer_dispute_rate=0.12))
        assert r.buyer_intent == BuyerIntentSignal.OPPORTUNISTIC

    def test_legitimate_classification(self):
        r = advise_negotiation(self._inp(buyer_prior_disputes=0, buyer_dispute_rate=0.0))
        assert r.buyer_intent == BuyerIntentSignal.LEGITIMATE

    # ── LTV tier ─────────────────────────────────────────────────

    def test_vip_ltv_tier(self):
        r = advise_negotiation(self._inp(buyer_ltv=600.0))
        assert r.ltv_tier == LtvTier.VIP

    def test_negligible_ltv_tier(self):
        r = advise_negotiation(self._inp(buyer_ltv=5.0))
        assert r.ltv_tier == LtvTier.NEGLIGIBLE

    # ── Strategy selection ────────────────────────────────────────

    def test_auto_accept_on_tiny_amount(self):
        r = advise_negotiation(self._inp(amount=5.0))
        assert r.recommended_strategy == NegotiationStrategy.ACCEPT_FULL

    def test_fight_on_strong_evidence(self):
        r = advise_negotiation(self._inp(
            win_probability=0.80,
            evidence_completeness_pct=0.85,
        ))
        assert r.recommended_strategy == NegotiationStrategy.FIGHT_FULL

    def test_serial_abuser_fight_not_counter(self):
        r = advise_negotiation(self._inp(
            buyer_prior_disputes=7,
            win_probability=0.55,
        ))
        assert r.recommended_strategy == NegotiationStrategy.FIGHT_FULL

    def test_vip_prefers_counter_offer(self):
        r = advise_negotiation(self._inp(
            buyer_ltv=750.0,
            buyer_prior_disputes=0,
            win_probability=0.50,
        ))
        assert r.recommended_strategy == NegotiationStrategy.COUNTER_OFFER

    # ── Buyer offer handling ──────────────────────────────────────

    def test_accept_buyer_offer_when_above_counter(self):
        r = advise_negotiation(self._inp(
            amount=100.0,
            buyer_made_offer=True,
            buyer_offer_amount=60.0,  # above typical counter range
            win_probability=0.40,
        ))
        assert r.recommended_strategy == NegotiationStrategy.ACCEPT_PARTIAL

    def test_reject_low_buyer_offer_note_in_talking_points(self):
        r = advise_negotiation(self._inp(
            amount=200.0,
            buyer_made_offer=True,
            buyer_offer_amount=5.0,   # very low
            win_probability=0.60,
        ))
        # Should note counter-proposal
        assert any("counter" in tp.lower() or "counter-propose" in tp.lower() for tp in r.talking_points)

    # ── EV scenarios ─────────────────────────────────────────────

    def test_best_ev_scenario_in_scenarios(self):
        r = advise_negotiation(self._inp())
        names = {s.name for s in r.scenarios}
        assert r.best_ev_scenario in names

    def test_accept_ev_always_negative(self):
        r = advise_negotiation(self._inp(amount=100.0))
        accept = next(s for s in r.scenarios if s.name == "ACCEPT_FULL")
        assert accept.expected_value < 0

    # ── Urgency ───────────────────────────────────────────────────

    def test_critical_urgency_at_1_day(self):
        r = advise_negotiation(self._inp(days_until_deadline=1))
        assert r.urgency == "CRITICAL"
        assert any("CRITICAL" in rf or "2 days" in rf for rf in r.risk_factors)

    def test_moderate_urgency_at_10_days(self):
        r = advise_negotiation(self._inp(days_until_deadline=10))
        assert r.urgency == "MODERATE"

    # ── Talking points ────────────────────────────────────────────

    def test_talking_points_non_empty(self):
        r = advise_negotiation(self._inp())
        assert len(r.talking_points) >= 1

    def test_inr_tracking_talking_point(self):
        r = advise_negotiation(self._inp(reason_code="INR"))
        assert any("carrier" in tp.lower() or "tracking" in tp.lower() or "delivery" in tp.lower()
                   for tp in r.talking_points)


# ═══════════════════════════════════════════════════════════════════
# Evidence Gap Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidenceGapAnalyzer:

    def _inp(self, code="INR", network="VISA", amount=100.0, **kw) -> EvidenceGapInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=code,
            network=network,
            transaction_amount=amount,
        )
        defaults.update(kw)
        return EvidenceGapInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_evidence_gaps(self._inp(dispute_id="D-ABC"))
        assert "D-ABC" in r.summary

    def test_score_bounded(self):
        r = analyze_evidence_gaps(self._inp())
        assert 0 <= r.completeness_score <= 100

    def test_items_non_empty(self):
        r = analyze_evidence_gaps(self._inp())
        assert len(r.items) >= 2

    # ── INR evidence requirements ─────────────────────────────────

    def test_inr_tracking_missing_is_critical_gap(self):
        r = analyze_evidence_gaps(self._inp(code="INR"))
        assert any("Tracking" in g for g in r.critical_gaps)

    def test_inr_full_evidence_strong_readiness(self):
        r = analyze_evidence_gaps(self._inp(
            code="INR",
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_invoice=True,
            made_resolution_attempt=True,
        ))
        assert r.readiness in (EvidenceReadiness.STRONG, EvidenceReadiness.ADEQUATE)

    def test_inr_no_evidence_insufficient(self):
        r = analyze_evidence_gaps(self._inp(code="INR"))
        assert r.readiness in (EvidenceReadiness.INSUFFICIENT, EvidenceReadiness.WEAK)

    # ── UNAUTHORIZED evidence requirements ───────────────────────

    def test_unauthorized_3ds_in_items(self):
        r = analyze_evidence_gaps(self._inp(code="UNAUTHORIZED"))
        types = {i.evidence_type.value for i in r.items}
        assert "THREE_D_SECURE" in types

    def test_unauthorized_3ds_not_run_is_unavailable(self):
        r = analyze_evidence_gaps(self._inp(code="UNAUTHORIZED", three_d_secure_run=False))
        item = next(i for i in r.items if i.evidence_type.value == "THREE_D_SECURE")
        assert item.status == EvidenceStatus.UNAVAILABLE

    def test_unauthorized_3ds_passed_is_present(self):
        r = analyze_evidence_gaps(self._inp(
            code="UNAUTHORIZED",
            three_d_secure_run=True,
            three_d_secure_passed=True,
        ))
        item = next(i for i in r.items if i.evidence_type.value == "THREE_D_SECURE")
        assert item.status == EvidenceStatus.PRESENT

    # ── SUBSCRIPTION evidence requirements ───────────────────────

    def test_subscription_requires_renewal_notice(self):
        r = analyze_evidence_gaps(self._inp(code="SUBSCRIPTION"))
        assert any("Renewal" in g for g in r.critical_gaps)

    def test_subscription_full_evidence_adequate_plus(self):
        r = analyze_evidence_gaps(self._inp(
            code="SUBSCRIPTION",
            has_subscription_terms=True,
            has_renewal_notice=True,
            has_tos_accepted=True,
        ))
        assert r.readiness in (EvidenceReadiness.STRONG, EvidenceReadiness.ADEQUATE)

    # ── Score and readiness ───────────────────────────────────────

    def test_all_evidence_present_high_score(self):
        r = analyze_evidence_gaps(self._inp(
            code="INR",
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_invoice=True,
            made_resolution_attempt=True,
            has_buyer_correspondence=True,
            has_carrier_scan_log=True,
        ))
        assert r.completeness_score >= 60

    def test_win_adj_positive_on_strong(self):
        r = analyze_evidence_gaps(self._inp(
            code="INR",
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_invoice=True,
            has_buyer_correspondence=True,
            made_resolution_attempt=True,
        ))
        if r.readiness == EvidenceReadiness.STRONG:
            assert r.win_rate_adjustment > 0

    def test_win_adj_negative_on_insufficient(self):
        r = analyze_evidence_gaps(self._inp(code="INR"))
        if r.readiness == EvidenceReadiness.INSUFFICIENT:
            assert r.win_rate_adjustment < 0

    # ── Gap actions ────────────────────────────────────────────────

    def test_gap_actions_non_empty_when_gaps(self):
        r = analyze_evidence_gaps(self._inp(code="INR"))
        assert len(r.gap_actions) >= 1

    def test_critical_gap_action_marked(self):
        r = analyze_evidence_gaps(self._inp(code="INR"))
        assert any("[CRITICAL]" in a for a in r.gap_actions)

    # ── CE 3.0 / prior txns ───────────────────────────────────────

    def test_prior_txns_2_counts_as_present(self):
        r = analyze_evidence_gaps(self._inp(
            code="10.4",
            network="VISA",
            prior_non_disputed_txn_count=2,
        ))
        item = next((i for i in r.items if i.evidence_type.value == "PRIOR_NON_DISPUTED_TXNS"), None)
        if item:
            assert item.status == EvidenceStatus.PRESENT

    def test_prior_txns_1_counts_as_missing(self):
        r = analyze_evidence_gaps(self._inp(
            code="10.4",
            network="VISA",
            prior_non_disputed_txn_count=1,
        ))
        item = next((i for i in r.items if i.evidence_type.value == "PRIOR_NON_DISPUTED_TXNS"), None)
        if item:
            assert item.status == EvidenceStatus.MISSING
