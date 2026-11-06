"""
Tests for Wave 44: hdr_forecast_engine, dispute_response_composer.
"""
import pytest
from oracle.hdr_forecast_engine import (
    forecast_hdr, HDRForecastInput, HDRTier,
)
from oracle.dispute_response_composer import (
    compose_dispute_response, DisputeResponseInput,
    ResponseTone, ResponseSection,
)


# ═══════════════════════════════════════════════════════════════════
# HDR Forecast Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestHDRForecastEngine:

    def _inp(self, txn=10000, disputes=30, **kw) -> HDRForecastInput:
        defaults = dict(
            merchant_id="M-001",
            transactions_last_90_days=txn,
            disputes_last_90_days=disputes,
        )
        defaults.update(kw)
        return HDRForecastInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = forecast_hdr(self._inp())
        assert "M-001" in r.summary

    def test_rate_computed_correctly(self):
        r = forecast_hdr(self._inp(txn=1000, disputes=5))
        assert r.current_rate == pytest.approx(0.005, abs=1e-6)

    def test_current_tier_normal(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=30))
        # 30/10000 = 0.30% → NORMAL
        assert r.current_tier == HDRTier.NORMAL

    def test_current_tier_elevated(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=60))
        # 60/10000 = 0.60% → ELEVATED
        assert r.current_tier == HDRTier.ELEVATED

    def test_current_tier_limited(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=120))
        # 120/10000 = 1.20% → LIMITED
        assert r.current_tier == HDRTier.LIMITED

    def test_current_tier_fee_tier(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=200))
        # 200/10000 = 2.0% → FEE_TIER
        assert r.current_tier == HDRTier.FEE_TIER

    def test_current_tier_severe(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=350))
        # 350/10000 = 3.5% → SEVERE
        assert r.current_tier == HDRTier.SEVERE

    # ── Headroom ──────────────────────────────────────────────────

    def test_headroom_positive_below_threshold(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=30))
        assert r.hdr_budget_remaining >= 0
        assert r.disputes_until_next_tier is not None

    def test_headroom_zero_at_severe(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=400))
        # Severe → no headroom
        assert r.hdr_budget_remaining == 0

    def test_next_tier_field_set(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=30))
        assert r.next_tier is not None
        assert r.next_tier_rate is not None

    # ── Scenario: fight all ───────────────────────────────────────

    def test_fight_all_scenario_present(self):
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
            pending_will_fight=15,
            pending_fight_expected_wins=10,
        ))
        s = r.scenario_fight_all
        assert s.label == "fight_all"
        assert s.projected_disputes_total >= 30

    def test_fight_all_wins_reduce_hdr_impact(self):
        # 10 wins → only 5 losses + 5 accepts count
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
            pending_will_fight=15,
            pending_fight_expected_wins=10,
        ))
        s_fight = r.scenario_fight_all
        s_accept = r.scenario_accept_all
        # Fighting with wins should result in fewer counted disputes than accepting all
        assert s_fight.projected_disputes_total <= s_accept.projected_disputes_total

    def test_fight_all_tier_fee_applied(self):
        r = forecast_hdr(self._inp(
            txn=1000, disputes=14,
            pending_disputes_count=5,
            pending_will_fight=5,
            pending_fight_expected_wins=0,
        ))
        s = r.scenario_fight_all
        if s.projected_tier in (HDRTier.FEE_TIER, HDRTier.SEVERE):
            assert s.tier_fee_per_dispute == 30.0
        else:
            assert s.tier_fee_per_dispute == 0.0

    # ── Scenario: accept all ──────────────────────────────────────

    def test_accept_all_scenario_adds_all_pending(self):
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
        ))
        assert r.scenario_accept_all.disputes_added == 20
        assert r.scenario_accept_all.projected_disputes_total == 50

    def test_accept_all_rate_computed(self):
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
        ))
        assert r.scenario_accept_all.projected_rate == pytest.approx(50 / 10000, abs=1e-6)

    # ── Scenario: strategic ───────────────────────────────────────

    def test_strategic_scenario_absent_when_no_deferral(self):
        r = forecast_hdr(self._inp())
        assert r.scenario_strategic is None

    def test_strategic_scenario_present_when_deferral_possible(self):
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
            pending_will_fight=15,
            pending_fight_expected_wins=5,
            can_defer_some_disputes=True,
            max_deferrable=5,
        ))
        assert r.scenario_strategic is not None
        assert r.scenario_strategic.label == "strategic_deferral"

    def test_strategic_fewer_disputes_than_fight_all(self):
        r = forecast_hdr(self._inp(
            txn=10000, disputes=30,
            pending_disputes_count=20,
            pending_will_fight=20,
            pending_fight_expected_wins=5,
            can_defer_some_disputes=True,
            max_deferrable=10,
        ))
        assert (r.scenario_strategic.projected_disputes_total
                <= r.scenario_fight_all.projected_disputes_total)

    # ── Recommendation ────────────────────────────────────────────

    def test_recommendation_non_empty(self):
        r = forecast_hdr(self._inp())
        assert len(r.recommendation) > 0

    def test_severe_recommends_urgent_action(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=400))
        assert "URGENT" in r.recommendation or "accept" in r.recommendation.lower()

    def test_normal_with_headroom_recommends_normal_operations(self):
        r = forecast_hdr(self._inp(txn=100000, disputes=100))
        # 100/100000 = 0.1% → very low; headroom is high
        assert "normal" in r.recommendation.lower() or "EV" in r.recommendation

    # ── Zero-division guard ───────────────────────────────────────

    def test_zero_transactions_does_not_crash(self):
        r = forecast_hdr(self._inp(txn=0, disputes=0))
        assert r is not None

    # ── Fee computation ───────────────────────────────────────────

    def test_total_fees_at_fee_tier(self):
        r = forecast_hdr(self._inp(txn=1000, disputes=20))
        # 20/1000 = 2.0% → FEE_TIER
        s = r.scenario_accept_all
        if s.projected_tier in (HDRTier.FEE_TIER, HDRTier.SEVERE):
            assert s.total_tier_fees == pytest.approx(30.0 * s.projected_disputes_total, abs=0.01)

    def test_no_fees_in_normal_tier(self):
        r = forecast_hdr(self._inp(txn=10000, disputes=30))
        assert r.scenario_fight_all.tier_fee_per_dispute == 0.0


# ═══════════════════════════════════════════════════════════════════
# Dispute Response Composer Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeResponseComposer:

    def _inp(self, reason_code="INR", **kw) -> DisputeResponseInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason_code,
            network="PAYPAL",
            transaction_amount=100.0,
            buyer_claim="I did not receive my order",
        )
        defaults.update(kw)
        return DisputeResponseInput(**defaults)

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = compose_dispute_response(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_four_paragraphs_present(self):
        r = compose_dispute_response(self._inp())
        assert len(r.paragraphs) == 4

    def test_all_sections_present(self):
        r = compose_dispute_response(self._inp())
        sections = {p.section for p in r.paragraphs}
        assert ResponseSection.OPENING in sections
        assert ResponseSection.EVIDENCE_SUMMARY in sections
        assert ResponseSection.REBUTTAL in sections
        assert ResponseSection.CLOSING in sections

    def test_headings_non_empty(self):
        r = compose_dispute_response(self._inp())
        for p in r.paragraphs:
            assert len(p.heading) > 0

    def test_body_non_empty(self):
        r = compose_dispute_response(self._inp())
        for p in r.paragraphs:
            assert len(p.body) > 0

    # ── Reason code templates ─────────────────────────────────────

    def test_inr_opening_mentions_shipment(self):
        r = compose_dispute_response(self._inp(reason_code="INR"))
        opening = next(p for p in r.paragraphs if p.section == ResponseSection.OPENING)
        body_lower = opening.body.lower()
        assert "ship" in body_lower or "deliver" in body_lower or "fulfilled" in body_lower

    def test_snad_opening_mentions_listing(self):
        r = compose_dispute_response(self._inp(
            reason_code="SNAD",
            buyer_claim="Item not as described",
        ))
        opening = next(p for p in r.paragraphs if p.section == ResponseSection.OPENING)
        body_lower = opening.body.lower()
        assert "listing" in body_lower or "described" in body_lower or "matches" in body_lower

    def test_unauthorized_opening_mentions_authentication(self):
        r = compose_dispute_response(self._inp(
            reason_code="UNAUTHORIZED",
            buyer_claim="I did not authorize this transaction",
        ))
        opening = next(p for p in r.paragraphs if p.section == ResponseSection.OPENING)
        body_lower = opening.body.lower()
        assert "account" in body_lower or "verified" in body_lower or "legitimate" in body_lower

    def test_unknown_reason_code_falls_back(self):
        r = compose_dispute_response(self._inp(reason_code="UNKNOWN_CODE"))
        assert len(r.paragraphs) == 4

    # ── Attachments ───────────────────────────────────────────────

    def test_required_attachments_present_for_inr(self):
        r = compose_dispute_response(self._inp(reason_code="INR"))
        required = [a for a in r.attachments if a.is_required]
        assert len(required) >= 2

    def test_tracking_has_it_true_when_provided(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            tracking_number="1Z999AA10123456784",
        ))
        tracking_attaches = [
            a for a in r.attachments if "rack" in a.name.lower() and a.is_required
        ]
        for a in tracking_attaches:
            assert a.has_it is True

    def test_missing_attachments_listed(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=False,
            has_delivery_confirmation=False,
        ))
        assert len(r.missing_critical_attachments) > 0

    def test_can_file_true_when_all_required_present(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
        ))
        # Order details is always has_it=True
        # tracking and proof of shipment both backed by has_tracking_number
        assert r.can_file_complete is True

    def test_can_file_false_when_missing_evidence(self):
        r = compose_dispute_response(self._inp(
            reason_code="UNAUTHORIZED",
            has_ip_log=False,
            has_order_history=False,
        ))
        assert r.can_file_complete is False

    # ── Tone ──────────────────────────────────────────────────────

    def test_assertive_tone_on_high_win_prob(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            win_probability=0.80,
        ))
        assert r.tone == ResponseTone.ASSERTIVE

    def test_conciliatory_tone_on_low_win_prob(self):
        r = compose_dispute_response(self._inp(
            reason_code="SNAD",
            win_probability=0.30,
        ))
        assert r.tone == ResponseTone.CONCILIATORY

    def test_professional_tone_on_medium_win_complete(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            win_probability=0.60,
        ))
        assert r.tone == ResponseTone.PROFESSIONAL

    def test_factual_tone_when_incomplete_filing(self):
        r = compose_dispute_response(self._inp(
            reason_code="UNAUTHORIZED",
            has_ip_log=False,
            has_order_history=False,
            win_probability=0.60,
        ))
        assert r.tone == ResponseTone.FACTUAL

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce30_in_evidence_summary_when_applicable(self):
        r = compose_dispute_response(self._inp(
            reason_code="UNAUTHORIZED",
            ce30_applicable=True,
        ))
        ev_body = next(
            p.body for p in r.paragraphs if p.section == ResponseSection.EVIDENCE_SUMMARY
        )
        assert "CE3.0" in ev_body or "ce3" in ev_body.lower()

    # ── Tracking number substitution ─────────────────────────────

    def test_tracking_number_in_rebuttal(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            tracking_number="TRACK12345",
        ))
        rebuttal = next(p for p in r.paragraphs if p.section == ResponseSection.REBUTTAL)
        assert "TRACK12345" in rebuttal.body

    def test_see_attached_fallback_in_rebuttal(self):
        r = compose_dispute_response(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            tracking_number="",
        ))
        rebuttal = next(p for p in r.paragraphs if p.section == ResponseSection.REBUTTAL)
        assert "see attached" in rebuttal.body.lower()

    # ── Word count ────────────────────────────────────────────────

    def test_word_count_positive(self):
        r = compose_dispute_response(self._inp())
        assert r.word_count_estimate > 0

    # ── SNAD attachments ──────────────────────────────────────────

    def test_snad_requires_product_listing_attachment(self):
        r = compose_dispute_response(self._inp(reason_code="SNAD"))
        names = [a.name for a in r.attachments if a.is_required]
        assert any("listing" in n.lower() or "product" in n.lower() for n in names)

    # ── Subscription ─────────────────────────────────────────────

    def test_subscription_response_structured(self):
        r = compose_dispute_response(self._inp(
            reason_code="SUBSCRIPTION",
            buyer_claim="I cancelled my subscription",
        ))
        opening = next(p for p in r.paragraphs if p.section == ResponseSection.OPENING)
        assert "subscription" in opening.body.lower()
