"""
Tests for Wave 25: buyer_fraud_detector, communication_sentiment_analyzer.
"""
import pytest
from oracle.buyer_fraud_detector import (
    detect_buyer_fraud, BuyerFraudInput, FraudRiskLevel, FraudSignalType,
)
from oracle.communication_sentiment_analyzer import (
    analyze_communication_sentiment, CommunicationSentimentInput,
    MessageTone, UrgencyLevel, ThreatLevel, ResolutionOpenness,
)


# ═══════════════════════════════════════════════════════════════════
# Buyer Fraud Detector Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerFraudDetector:

    def _inp(self, **kw) -> BuyerFraudInput:
        defaults = dict(
            dispute_id="D-001",
            buyer_id="B-001",
            claim_type="INR",
            dispute_amount=100.0,
            total_lifetime_orders=10,
            total_lifetime_disputes=1,
            months_as_buyer=18,
            hours_until_dispute_filed=72,
            has_delivery_confirmation=False,
            delivery_address_matches_account=True,
            buyer_story_changed=False,
            escalated_within_24h=False,
            dispute_rate_last_12m=0.10,
            orders_above_threshold=0,
            networks_used=1,
        )
        defaults.update(kw)
        return BuyerFraudInput(**defaults)

    # ── Fraud score range ──────────────────────────────────────────

    def test_fraud_score_range(self):
        r = detect_buyer_fraud(self._inp())
        assert 0 <= r.fraud_score <= 100

    def test_clean_buyer_low_risk(self):
        r = detect_buyer_fraud(self._inp(
            total_lifetime_disputes=0,
            months_as_buyer=24,
            dispute_rate_last_12m=0.0,
        ))
        assert r.risk_level == FraudRiskLevel.LOW_RISK

    def test_serial_disputer_high_risk(self):
        r = detect_buyer_fraud(self._inp(
            total_lifetime_orders=10,
            total_lifetime_disputes=4,   # 40% rate
            dispute_rate_last_12m=0.30,
            has_prior_fraud_flag=True,   # 25 (serial) + 15 (prior flag) = 40 → MEDIUM_RISK
        ))
        assert r.risk_level in (FraudRiskLevel.HIGH_RISK, FraudRiskLevel.MEDIUM_RISK)

    def test_immediate_dispute_increases_score(self):
        normal = detect_buyer_fraud(self._inp(hours_until_dispute_filed=72))
        fast   = detect_buyer_fraud(self._inp(hours_until_dispute_filed=5))
        assert fast.fraud_score > normal.fraud_score

    def test_prior_fraud_flag_increases_score(self):
        no_flag = detect_buyer_fraud(self._inp(has_prior_fraud_flag=False))
        flag    = detect_buyer_fraud(self._inp(has_prior_fraud_flag=True))
        assert flag.fraud_score > no_flag.fraud_score

    def test_inconsistent_story_increases_score(self):
        consistent   = detect_buyer_fraud(self._inp(buyer_story_changed=False))
        inconsistent = detect_buyer_fraud(self._inp(buyer_story_changed=True))
        assert inconsistent.fraud_score > consistent.fraud_score

    def test_delivery_confirmed_inr_boosts_score(self):
        no_confirm = detect_buyer_fraud(self._inp(
            claim_type="INR",
            has_delivery_confirmation=False,
        ))
        confirm = detect_buyer_fraud(self._inp(
            claim_type="INR",
            has_delivery_confirmation=True,
        ))
        assert confirm.fraud_score > no_confirm.fraud_score

    # ── Anti-fraud signals ────────────────────────────────────────

    def test_long_loyal_customer_reduces_score(self):
        new_buyer   = detect_buyer_fraud(self._inp(months_as_buyer=2))
        loyal_buyer = detect_buyer_fraud(self._inp(
            months_as_buyer=36,
            total_lifetime_disputes=1,
            total_lifetime_orders=50,
            dispute_rate_last_12m=0.02,
        ))
        assert loyal_buyer.fraud_score < new_buyer.fraud_score

    def test_new_account_increases_score(self):
        old = detect_buyer_fraud(self._inp(months_as_buyer=24))
        new = detect_buyer_fraud(self._inp(months_as_buyer=2))
        assert new.fraud_score > old.fraud_score

    # ── Win probability adjustment ────────────────────────────────

    def test_high_risk_boosts_p_win(self):
        r = detect_buyer_fraud(self._inp(
            total_lifetime_orders=10,
            total_lifetime_disputes=5,
            dispute_rate_last_12m=0.40,
            has_prior_fraud_flag=True,
            escalated_within_24h=True,
        ), base_p_win=0.55)
        assert r.adjusted_p_win > r.base_p_win

    def test_low_risk_no_p_win_boost(self):
        r = detect_buyer_fraud(self._inp(
            total_lifetime_disputes=0,
            months_as_buyer=36,
            dispute_rate_last_12m=0.0,
        ), base_p_win=0.55)
        assert r.p_win_boost == 0.0

    def test_adjusted_p_win_capped_at_0_97(self):
        r = detect_buyer_fraud(self._inp(
            total_lifetime_orders=5,
            total_lifetime_disputes=4,
            has_prior_fraud_flag=True,
        ), base_p_win=0.95)
        assert r.adjusted_p_win <= 0.97

    # ── Report fields ─────────────────────────────────────────────

    def test_all_signal_types_present(self):
        r = detect_buyer_fraud(self._inp())
        signal_types = {s.signal_type for s in r.signals}
        assert FraudSignalType.SERIAL_DISPUTER in signal_types
        assert FraudSignalType.IMMEDIATE_DISPUTE in signal_types

    def test_summary_contains_dispute_id(self):
        r = detect_buyer_fraud(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_recommendation_non_empty(self):
        r = detect_buyer_fraud(self._inp())
        assert len(r.recommendation) > 10

    def test_active_signals_non_negative(self):
        r = detect_buyer_fraud(self._inp())
        assert r.active_signals >= 0


# ═══════════════════════════════════════════════════════════════════
# Communication Sentiment Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestCommunicationSentimentAnalyzer:

    def _inp(self, messages=None, **kw) -> CommunicationSentimentInput:
        defaults = dict(
            dispute_id="D-001",
            buyer_id="B-001",
            claim_type="INR",
            days_since_first_message=1,
        )
        defaults.update(kw)
        if messages is None:
            messages = ["I did not receive my order, please help me resolve this."]
        return CommunicationSentimentInput(messages=messages, **defaults)

    # ── Tone classification ───────────────────────────────────────

    def test_neutral_message_neutral_tone(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Where is my package?"]
        ))
        assert r.overall_tone in (MessageTone.NEUTRAL, MessageTone.NEGATIVE)

    def test_positive_message_positive_tone(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Thank you so much, I appreciate your great help!"]
        ))
        assert r.overall_tone == MessageTone.POSITIVE

    def test_hostile_message_hostile_tone(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["This is a scam and fraud! I will sue you and report you to the FTC!"]
        ))
        assert r.overall_tone == MessageTone.HOSTILE

    def test_negative_message_negative_tone(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["I am very disappointed and frustrated. This is unacceptable."]
        ))
        assert r.overall_tone in (MessageTone.NEGATIVE, MessageTone.HOSTILE)

    # ── Threat detection ──────────────────────────────────────────

    def test_chargeback_threat_explicit(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["I will file a chargeback and dispute this with my bank immediately."]
        ))
        assert r.peak_threat_level in (ThreatLevel.IMPLIED, ThreatLevel.EXPLICIT)

    def test_no_threat_message_none(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Can you please check on my order status?"]
        ))
        assert r.peak_threat_level == ThreatLevel.NONE

    # ── Urgency detection ─────────────────────────────────────────

    def test_urgent_language_high_urgency(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["I need this resolved immediately! This is urgent and I need it today!"]
        ))
        assert r.peak_urgency in (UrgencyLevel.HIGH, UrgencyLevel.EXTREME)

    def test_calm_message_low_urgency(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Hello, I ordered something last week and have not seen it yet."]
        ))
        assert r.peak_urgency in (UrgencyLevel.LOW, UrgencyLevel.MEDIUM)

    # ── Escalation risk ───────────────────────────────────────────

    def test_hostile_messages_high_escalation_risk(self):
        r = analyze_communication_sentiment(self._inp(
            messages=[
                "This is a scam and fraud! You are criminal! Illegal! Lawsuit!",
                "I will file a chargeback and report you now! Fraud criminal scam! Sue you! Illegal!",
            ]
        ))
        assert r.escalation_risk_score >= 50

    def test_positive_messages_low_escalation_risk(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Thank you for your help!", "I appreciate your quick response."]
        ))
        assert r.escalation_risk_score < 20

    def test_escalation_probability_in_range(self):
        r = analyze_communication_sentiment(self._inp())
        assert 0.0 <= r.escalation_probability <= 1.0

    # ── Resolution openness ───────────────────────────────────────

    def test_helpful_buyer_open_resolution(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["Hi, I'd like help getting a refund or replacement please."]
        ))
        assert r.overall_resolution_openness in (
            ResolutionOpenness.OPEN, ResolutionOpenness.CONDITIONAL)

    def test_adversarial_buyer_adversarial_openness(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["I will sue you and report to FTC. This is fraud and scam!"]
        ))
        assert r.overall_resolution_openness == ResolutionOpenness.ADVERSARIAL

    # ── Tone deterioration ────────────────────────────────────────

    def test_deteriorating_tone_detected(self):
        r = analyze_communication_sentiment(self._inp(
            messages=[
                "Hello, can you help with my order?",
                "I am disappointed with no response.",
                "This is unacceptable! Fraud scam! I will file chargeback!",
                "I am filing now! You are criminals and liars!",
            ]
        ))
        assert r.tone_deteriorating is True

    def test_stable_positive_tone_not_deteriorating(self):
        r = analyze_communication_sentiment(self._inp(
            messages=[
                "Thank you, appreciate your help.",
                "Great customer service!",
            ]
        ))
        assert r.tone_deteriorating is False

    # ── Edge cases ────────────────────────────────────────────────

    def test_empty_messages_no_crash(self):
        r = analyze_communication_sentiment(self._inp(messages=[]))
        assert r.escalation_risk_score == 0.0
        assert r.overall_tone == MessageTone.NEUTRAL

    def test_per_message_analyses_count_matches(self):
        msgs = ["message one", "message two", "message three"]
        r = analyze_communication_sentiment(self._inp(messages=msgs))
        assert len(r.message_analyses) == 3

    def test_summary_contains_dispute_id(self):
        r = analyze_communication_sentiment(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_priority_flag_urgent_for_hostile(self):
        r = analyze_communication_sentiment(self._inp(
            messages=["This is a scam and fraud! I will sue you! Chargeback now!"]
        ))
        assert r.priority_flag == "URGENT"
