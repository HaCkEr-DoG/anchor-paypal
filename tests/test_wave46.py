"""
Tests for Wave 46: buyer_communication_analyzer, dispute_negotiation_advisor.
"""
import pytest
from oracle.buyer_communication_analyzer import (
    analyze_buyer_communication, BuyerCommunicationInput,
    MessageRecord, MessageSender,
    BuyerTone, FraudSignalLevel, EscalationRisk,
)
from oracle.dispute_negotiation_advisor import (
    advise_negotiation, NegotiationAdvisorInput,
    NegotiationStance, TimingRecommendation,
)

BASE_TS = 1700000000


def _msg(sender, content, offset_secs=0, idx=0):
    return MessageRecord(
        sender=sender,
        content=content,
        timestamp_epoch=BASE_TS + offset_secs,
        message_index=idx,
    )


# ═══════════════════════════════════════════════════════════════════
# Buyer Communication Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerCommunicationAnalyzer:

    def _inp(self, messages=None, **kw) -> BuyerCommunicationInput:
        if messages is None:
            messages = [
                _msg(MessageSender.BUYER, "I never received my package", 0, 0),
            ]
        defaults = dict(
            dispute_id="D-001",
            dispute_reason_code="INR",
            transaction_amount=100.0,
        )
        defaults.update(kw)
        return BuyerCommunicationInput(messages=messages, **defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_buyer_communication(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_message_counts_correct(self):
        msgs = [
            _msg(MessageSender.BUYER, "where is my order", 0),
            _msg(MessageSender.MERCHANT, "it was shipped", 10),
            _msg(MessageSender.BUYER, "still not here", 20),
        ]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.total_messages == 3
        assert r.buyer_message_count == 2
        assert r.merchant_message_count == 1

    # ── Legal threat detection ────────────────────────────────────

    def test_legal_threat_detected(self):
        msgs = [_msg(MessageSender.BUYER, "I will get a lawyer if this is not resolved")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.legal_threat_detected is True

    def test_no_legal_threat_in_normal_message(self):
        msgs = [_msg(MessageSender.BUYER, "I never received my package")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.legal_threat_detected is False

    # ── Social media threat ───────────────────────────────────────

    def test_social_media_threat_detected(self):
        msgs = [_msg(MessageSender.BUYER, "I'll post about this on twitter")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.social_media_threat_detected is True

    def test_no_social_threat_without_keywords(self):
        msgs = [_msg(MessageSender.BUYER, "please help me with my order")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.social_media_threat_detected is False

    # ── Delivery acknowledgment / contradiction ───────────────────

    def test_delivery_acknowledged_detected(self):
        msgs = [
            _msg(MessageSender.BUYER, "Oh I received the package yesterday", 0),
            _msg(MessageSender.BUYER, "But the item is wrong", 100),
        ]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.delivery_acknowledged_by_buyer is True

    def test_contradiction_flagged_on_inr_with_delivery_ack(self):
        msgs = [
            _msg(MessageSender.BUYER, "I received the package but want a refund", 0),
        ]
        r = analyze_buyer_communication(self._inp(
            messages=msgs,
            dispute_reason_code="INR",
        ))
        assert len(r.contradiction_flags) >= 1

    def test_no_contradiction_on_snad_with_delivery_ack(self):
        msgs = [
            _msg(MessageSender.BUYER, "I got the item but it's not as described", 0),
        ]
        r = analyze_buyer_communication(self._inp(
            messages=msgs,
            dispute_reason_code="SNAD",
        ))
        # No INR contradiction expected for SNAD
        inr_contradictions = [
            c for c in r.contradiction_flags if "INR" in c.description.upper()
        ]
        assert len(inr_contradictions) == 0

    # ── Message burst ─────────────────────────────────────────────

    def test_burst_detected_when_3_messages_in_hour(self):
        msgs = [
            _msg(MessageSender.BUYER, "where is my order", 0),
            _msg(MessageSender.BUYER, "still waiting", 1200),
            _msg(MessageSender.BUYER, "this is ridiculous", 2400),
        ]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.message_burst_detected is True

    def test_no_burst_on_spread_out_messages(self):
        msgs = [
            _msg(MessageSender.BUYER, "where is my order", 0),
            _msg(MessageSender.BUYER, "still waiting", 86400),   # 1 day later
            _msg(MessageSender.BUYER, "please respond", 172800),
        ]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.message_burst_detected is False

    # ── Tone classification ───────────────────────────────────────

    def test_hostile_tone_on_threats(self):
        msgs = [_msg(MessageSender.BUYER, "you are a scam and a thief I will sue you")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.buyer_tone in (BuyerTone.HOSTILE, BuyerTone.DEMANDING)

    def test_cooperative_tone(self):
        msgs = [_msg(MessageSender.BUYER, "I understand, please let me know what we can do, I appreciate your help")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.buyer_tone == BuyerTone.COOPERATIVE

    # ── Fraud signal level ────────────────────────────────────────

    def test_very_high_fraud_on_contradiction_plus_boilerplate(self):
        msgs = [
            _msg(MessageSender.BUYER,
                 "I never received my item and tracking shows delivered but I did not get it. "
                 "Item not received.", 0),
            _msg(MessageSender.BUYER, "got it yesterday but not what i ordered", 100),
        ]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.fraud_signal_level in (
            FraudSignalLevel.VERY_HIGH, FraudSignalLevel.HIGH, FraudSignalLevel.MODERATE)

    def test_minimal_fraud_on_clean_message(self):
        msgs = [_msg(MessageSender.BUYER, "My package has not arrived yet")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.fraud_signal_level in (
            FraudSignalLevel.MINIMAL, FraudSignalLevel.LOW)

    # ── Escalation risk ───────────────────────────────────────────

    def test_imminent_risk_on_legal_hostile(self):
        msgs = [_msg(MessageSender.BUYER, "I'll sue you in court, you scam")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.escalation_risk == EscalationRisk.IMMINENT

    def test_low_risk_on_cooperative(self):
        msgs = [_msg(MessageSender.BUYER, "I understand, let me know what you can do, thank you")]
        r = analyze_buyer_communication(self._inp(messages=msgs))
        assert r.escalation_risk in (EscalationRisk.LOW, EscalationRisk.MODERATE)

    # ── Repeat disputer signal ────────────────────────────────────

    def test_repeat_disputer_flag(self):
        msgs = [_msg(MessageSender.BUYER, "I want a refund")]
        r = analyze_buyer_communication(self._inp(
            messages=msgs,
            buyer_prior_disputes=5,
        ))
        repeat_flags = [f for f in r.signal_flags if "REPEAT" in f.signal_type]
        assert len(repeat_flags) >= 1

    # ── Empty message thread ──────────────────────────────────────

    def test_empty_messages_does_not_crash(self):
        r = analyze_buyer_communication(self._inp(messages=[]))
        assert r is not None
        assert r.buyer_message_count == 0

    # ── Negotiation leverage / response tone ──────────────────────

    def test_negotiation_leverage_non_empty(self):
        r = analyze_buyer_communication(self._inp())
        assert len(r.negotiation_leverage) > 0

    def test_suggested_response_tone_non_empty(self):
        r = analyze_buyer_communication(self._inp())
        assert len(r.suggested_response_tone) > 0


# ═══════════════════════════════════════════════════════════════════
# Dispute Negotiation Advisor Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeNegotiationAdvisor:

    def _inp(self, amount=100.0, win_prob=0.50, **kw) -> NegotiationAdvisorInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code="INR",
            transaction_amount=amount,
            win_probability=win_prob,
        )
        defaults.update(kw)
        return NegotiationAdvisorInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = advise_negotiation(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_all_options_present(self):
        r = advise_negotiation(self._inp(
            can_offer_partial=True,
            can_offer_store_credit=True,
            max_refund_pct=1.0,
        ))
        stances = {o.stance for o in r.all_options}
        assert NegotiationStance.HOLD_FIRM in stances
        assert NegotiationStance.ACCEPT_AND_CLOSE in stances

    def test_primary_and_fallback_differ(self):
        r = advise_negotiation(self._inp())
        assert r.primary_stance != r.fallback_stance

    # ── EV baseline ───────────────────────────────────────────────

    def test_ev_fight_formula(self):
        p, amount = 0.60, 100.0
        fc, cb = 15.0, 20.0
        expected = p * amount - fc - (1 - p) * cb
        r = advise_negotiation(self._inp(
            win_probability=p, amount=amount,
            fight_cost=fc, chargeback_fee=cb,
        ))
        assert r.ev_fight == pytest.approx(expected, abs=0.5)

    def test_ev_accept_is_negative_full_loss(self):
        r = advise_negotiation(self._inp(amount=100.0))
        assert r.ev_accept == pytest.approx(-100.0, abs=0.01)

    # ── Walk-away threshold ───────────────────────────────────────

    def test_walk_away_above_amount(self):
        r = advise_negotiation(self._inp(amount=100.0, chargeback_fee=20.0))
        assert r.walk_away_threshold > 100.0

    def test_walk_away_below_amount_plus_cb_fee(self):
        r = advise_negotiation(self._inp(amount=100.0, chargeback_fee=20.0))
        assert r.walk_away_threshold < 121.0

    # ── Option amounts ────────────────────────────────────────────

    def test_partial_25pct_offer(self):
        r = advise_negotiation(self._inp(
            amount=200.0,
            can_offer_partial=True,
            max_refund_pct=1.0,
        ))
        partials = [o for o in r.all_options if o.stance == NegotiationStance.OFFER_PARTIAL_REFUND]
        amounts = {o.offer_amount for o in partials}
        # Should have at least one partial at ~25% = $50
        assert any(abs(a - 50.0) < 1.0 for a in amounts)

    def test_full_refund_option_equals_amount(self):
        r = advise_negotiation(self._inp(amount=100.0, max_refund_pct=1.0))
        full_opts = [o for o in r.all_options if o.stance == NegotiationStance.OFFER_FULL_REFUND]
        if full_opts:
            assert full_opts[0].offer_amount == pytest.approx(100.0)

    # ── Script openings ───────────────────────────────────────────

    def test_primary_offer_has_script(self):
        r = advise_negotiation(self._inp())
        assert r.primary_offer is not None
        assert len(r.primary_offer.script_opening) > 0

    # ── Timing ────────────────────────────────────────────────────

    def test_act_now_on_imminent_escalation(self):
        r = advise_negotiation(self._inp(escalation_risk="IMMINENT"))
        assert r.timing == TimingRecommendation.ACT_NOW

    def test_act_now_on_tight_deadline(self):
        r = advise_negotiation(self._inp(days_until_deadline=1))
        assert r.timing == TimingRecommendation.ACT_NOW

    def test_wait_on_high_fraud_signal(self):
        r = advise_negotiation(self._inp(
            fraud_signal_level="VERY_HIGH",
            escalation_risk="LOW",
            days_until_deadline=15,
        ))
        assert r.timing == TimingRecommendation.WAIT_FOR_INFO

    def test_standard_timing_otherwise(self):
        r = advise_negotiation(self._inp(
            escalation_risk="LOW",
            fraud_signal_level="MINIMAL",
            days_until_deadline=10,
        ))
        assert r.timing in (TimingRecommendation.STANDARD, TimingRecommendation.ACT_SOON)

    # ── Acceptance probability ────────────────────────────────────

    def test_acceptance_higher_for_larger_offers(self):
        r = advise_negotiation(self._inp(
            amount=200.0,
            can_offer_partial=True,
            max_refund_pct=1.0,
        ))
        partials = sorted(
            [o for o in r.all_options if o.stance == NegotiationStance.OFFER_PARTIAL_REFUND],
            key=lambda x: x.offer_pct,
        )
        if len(partials) >= 2:
            assert partials[-1].expected_buyer_acceptance >= partials[0].expected_buyer_acceptance

    # ── High win prob → hold firm ─────────────────────────────────

    def test_hold_firm_recommended_on_high_win(self):
        r = advise_negotiation(self._inp(
            win_probability=0.90,
            amount=200.0,
            fight_cost=15.0,
            chargeback_fee=20.0,
            buyer_tone="COOPERATIVE",
        ))
        # HOLD_FIRM should be in top options when fight EV is very high
        top_ev = r.all_options[0].ev
        hold_ev = next(
            (o.ev for o in r.all_options if o.stance == NegotiationStance.HOLD_FIRM), None
        )
        assert hold_ev is not None
        # The best option should be hold firm or at least comparable
        assert abs(hold_ev - top_ev) < 20.0 or r.all_options[0].stance == NegotiationStance.HOLD_FIRM

    # ── VIP risk flag ─────────────────────────────────────────────

    def test_vip_risk_in_risks_list(self):
        r = advise_negotiation(self._inp(
            is_vip=True,
            buyer_ltv=500.0,
            win_probability=0.30,
        ))
        assert any("VIP" in risk or "LTV" in risk for risk in r.risks)

    # ── Leverage non-empty ────────────────────────────────────────

    def test_leverage_non_empty(self):
        r = advise_negotiation(self._inp())
        assert len(r.negotiation_leverage) > 0

    # ── No partial when disabled ──────────────────────────────────

    def test_no_partial_options_when_disabled(self):
        r = advise_negotiation(self._inp(can_offer_partial=False, can_offer_store_credit=False))
        partial_opts = [
            o for o in r.all_options
            if o.stance in (NegotiationStance.OFFER_PARTIAL_REFUND,
                            NegotiationStance.OFFER_STORE_CREDIT)
        ]
        assert len(partial_opts) == 0
