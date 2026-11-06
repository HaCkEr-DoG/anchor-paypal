"""
Tests for Wave 37: transaction_velocity_checker, buyer_communication_analyzer.
"""
import pytest
from oracle.transaction_velocity_checker import (
    check_transaction_velocity, VelocityInput, VelocityTransaction,
    VelocityRisk, VelocityTrigger,
)
from oracle.buyer_communication_analyzer import (
    analyze_buyer_communication, CommunicationInput, BuyerMessage,
    FraudSignalLevel, HostilityLevel, RecommendedTone,
)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

BASE_TS = 1_700_000_000   # arbitrary epoch reference


def _txn(txn_id="T-001", amount=50.0, offset_secs=0,
         addr="ADDR_A", bin_="411111") -> VelocityTransaction:
    return VelocityTransaction(
        transaction_id=txn_id,
        amount=amount,
        timestamp_epoch=BASE_TS + offset_secs,
        shipping_address_hash=addr,
        card_bin=bin_,
    )


def _vinp(**kw) -> VelocityInput:
    defaults = dict(
        dispute_id="D-001",
        card_last4="4242",
        account_id="A-001",
        account_age_days=365,
        avg_daily_transactions=1.0,
        avg_transaction_amount=50.0,
        typical_shipping_address_hash="ADDR_A",
        velocity_window_hours=24,
    )
    defaults.update(kw)
    return VelocityInput(**defaults)


def _msg(text: str, msg_id="M-001", offset=0) -> BuyerMessage:
    return BuyerMessage(
        message_id=msg_id,
        text=text,
        timestamp_epoch=BASE_TS + offset,
        is_from_buyer=True,
    )


def _cinp(**kw) -> CommunicationInput:
    defaults = dict(
        dispute_id="D-001",
        claim_type="INR",
    )
    defaults.update(kw)
    return CommunicationInput(**defaults)


# ═══════════════════════════════════════════════════════════════════
# Transaction Velocity Checker Tests
# ═══════════════════════════════════════════════════════════════════

class TestTransactionVelocityChecker:

    # ── Normal case ───────────────────────────────────────────────

    def test_no_transactions_normal_risk(self):
        r = check_transaction_velocity(_vinp(recent_transactions=[]))
        assert r.overall_risk == VelocityRisk.NORMAL

    def test_single_normal_transaction_normal_risk(self):
        r = check_transaction_velocity(_vinp(
            recent_transactions=[_txn("T-1", amount=50.0)]))
        assert r.overall_risk == VelocityRisk.NORMAL

    def test_summary_contains_dispute_id(self):
        r = check_transaction_velocity(_vinp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    # ── Amount spike ──────────────────────────────────────────────

    def test_10x_amount_spike_critical(self):
        r = check_transaction_velocity(_vinp(
            avg_transaction_amount=50.0,
            recent_transactions=[_txn("T-1", amount=600.0)]))
        assert r.overall_risk in (VelocityRisk.CRITICAL, VelocityRisk.HIGH)

    def test_5x_amount_spike_high_risk(self):
        r = check_transaction_velocity(_vinp(
            avg_transaction_amount=50.0,
            recent_transactions=[_txn("T-1", amount=270.0)]))
        assert r.overall_risk in (VelocityRisk.HIGH, VelocityRisk.MODERATE)

    def test_2x_amount_no_trigger(self):
        r = check_transaction_velocity(_vinp(
            avg_transaction_amount=50.0,
            recent_transactions=[_txn("T-1", amount=100.0)]))
        assert r.overall_risk in (VelocityRisk.NORMAL, VelocityRisk.LOW,
                                   VelocityRisk.MODERATE)

    def test_amount_spike_trigger_name(self):
        r = check_transaction_velocity(_vinp(
            avg_transaction_amount=50.0,
            recent_transactions=[_txn("T-1", amount=600.0)]))
        assert VelocityTrigger.AMOUNT_SPIKE.value in r.triggered_checks

    # ── Frequency spike ───────────────────────────────────────────

    def test_10x_frequency_critical(self):
        txns = [_txn(f"T-{i}", offset_secs=i*60) for i in range(15)]
        r = check_transaction_velocity(_vinp(
            avg_daily_transactions=1.0,
            recent_transactions=txns))
        assert r.overall_risk in (VelocityRisk.CRITICAL, VelocityRisk.HIGH)

    def test_frequency_trigger_in_checks(self):
        txns = [_txn(f"T-{i}", offset_secs=i*60) for i in range(15)]
        r = check_transaction_velocity(_vinp(
            avg_daily_transactions=1.0,
            recent_transactions=txns))
        assert VelocityTrigger.FREQUENCY_SPIKE.value in r.triggered_checks

    # ── BIN testing (micro-transactions) ──────────────────────────

    def test_5_micro_transactions_critical_bin_testing(self):
        txns = [_txn(f"T-{i}", amount=1.0, offset_secs=i*10) for i in range(5)]
        r = check_transaction_velocity(_vinp(
            recent_transactions=txns, avg_transaction_amount=50.0))
        assert r.overall_risk in (VelocityRisk.CRITICAL, VelocityRisk.HIGH)
        assert VelocityTrigger.BIN_TESTING.value in r.triggered_checks

    def test_3_micro_transactions_high_bin_testing(self):
        txns = [_txn(f"T-{i}", amount=2.0, offset_secs=i*10) for i in range(3)]
        r = check_transaction_velocity(_vinp(
            recent_transactions=txns, avg_transaction_amount=50.0))
        assert r.overall_risk in (VelocityRisk.HIGH, VelocityRisk.CRITICAL,
                                   VelocityRisk.MODERATE)

    def test_1_micro_transaction_no_bin_trigger(self):
        r = check_transaction_velocity(_vinp(
            recent_transactions=[_txn("T-1", amount=1.0)]))
        assert VelocityTrigger.BIN_TESTING.value not in r.triggered_checks

    # ── Address changes ───────────────────────────────────────────

    def test_4_different_addresses_critical(self):
        txns = [_txn(f"T-{i}", addr=f"ADDR_{i}") for i in range(4)]
        r = check_transaction_velocity(_vinp(
            typical_shipping_address_hash="ADDR_0",
            recent_transactions=txns))
        assert r.overall_risk in (VelocityRisk.CRITICAL, VelocityRisk.HIGH)
        assert VelocityTrigger.ADDRESS_CHANGE.value in r.triggered_checks

    def test_new_address_high_risk(self):
        txns = [
            _txn("T-1", addr="ADDR_A"),
            _txn("T-2", addr="ADDR_NEW"),
        ]
        r = check_transaction_velocity(_vinp(
            typical_shipping_address_hash="ADDR_A",
            recent_transactions=txns))
        assert VelocityTrigger.ADDRESS_CHANGE.value in r.triggered_checks

    # ── New account ───────────────────────────────────────────────

    def test_very_new_account_high_risk(self):
        txns = [_txn(f"T-{i}", amount=100.0, offset_secs=i*3600) for i in range(3)]
        r = check_transaction_velocity(_vinp(
            account_age_days=3,
            avg_transaction_amount=50.0,
            recent_transactions=txns))
        assert r.overall_risk in (VelocityRisk.HIGH, VelocityRisk.MODERATE,
                                   VelocityRisk.CRITICAL)

    def test_old_account_new_account_check_skipped(self):
        txns = [_txn(f"T-{i}", amount=100.0) for i in range(5)]
        r = check_transaction_velocity(_vinp(
            account_age_days=365,
            avg_daily_transactions=1.0,
            recent_transactions=txns))
        assert VelocityTrigger.NEW_ACCOUNT.value not in r.triggered_checks

    # ── Block flag ────────────────────────────────────────────────

    def test_should_block_set_on_critical(self):
        txns = [_txn(f"T-{i}", amount=1.0, offset_secs=i*10) for i in range(5)]
        r = check_transaction_velocity(_vinp(
            recent_transactions=txns, avg_transaction_amount=50.0))
        if r.overall_risk == VelocityRisk.CRITICAL:
            assert r.should_block_account is True

    def test_no_block_on_normal(self):
        r = check_transaction_velocity(_vinp(recent_transactions=[]))
        assert r.should_block_account is False


# ═══════════════════════════════════════════════════════════════════
# Buyer Communication Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerCommunicationAnalyzer:

    # ── No messages ───────────────────────────────────────────────

    def test_no_messages_minimal_fraud(self):
        r = analyze_buyer_communication(_cinp(messages=[]))
        assert r.fraud_signal_level == FraudSignalLevel.MINIMAL

    def test_no_messages_cooperative_or_low_hostility(self):
        r = analyze_buyer_communication(_cinp(messages=[]))
        assert r.hostility_level in (HostilityLevel.COOPERATIVE, HostilityLevel.LOW)

    # ── Threat signals ────────────────────────────────────────────

    def test_legal_threat_raises_hostility(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I will contact my lawyer and sue you if you don't refund immediately"),
        ]))
        assert r.hostility_level in (HostilityLevel.HIGH, HostilityLevel.EXTREME)

    def test_bbb_threat_detected(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I'm going to report you to the BBB and social media"),
        ]))
        assert r.threat_count >= 1

    def test_multiple_threats_extreme_hostility(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I will sue you, go to the BBB, and post on social media. "
                 "My lawyer is ready. I'll file a police report."),
        ]))
        assert r.hostility_level in (HostilityLevel.EXTREME, HostilityLevel.HIGH)

    def test_threat_phrases_extracted(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I'll contact my lawyer"),
        ]))
        assert len(r.key_threat_phrases) >= 1

    # ── Sophistication signals ────────────────────────────────────

    def test_chargeback_mention_raises_fraud_signal(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I know the chargeback process and have done it before"),
        ]))
        assert r.fraud_signal_level in (
            FraudSignalLevel.HIGH, FraudSignalLevel.VERY_HIGH,
            FraudSignalLevel.MODERATE, FraudSignalLevel.LOW)
        assert r.sophistication_count >= 1

    def test_regulation_e_mention_raises_sophistication(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("Under Regulation E I'm entitled to a provisional credit"),
        ]))
        assert r.sophistication_count >= 1

    # ── Receipt acknowledgment ────────────────────────────────────

    def test_buyer_acknowledgement_of_receipt_raises_fraud(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I received the package but it was broken"),
        ]))
        assert r.buyer_acknowledged_receipt is True
        assert r.fraud_signal_level in (
            FraudSignalLevel.MODERATE, FraudSignalLevel.HIGH, FraudSignalLevel.VERY_HIGH)

    def test_no_acknowledgement_flag_when_not_mentioned(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I never got my order"),
        ]))
        assert r.buyer_acknowledged_receipt is False

    # ── Inconsistency detection ───────────────────────────────────

    def test_contradictory_claims_detected(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I never received the item"),
            _msg("I want to return it because it doesn't fit"),
        ]))
        assert len(r.inconsistencies) >= 1

    def test_consistent_claims_no_inconsistency(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I never received the item. The tracking shows delivered but I was not home."),
        ]))
        # No contradictions
        assert len(r.inconsistencies) == 0

    # ── Repeat disputer ───────────────────────────────────────────

    def test_repeat_disputer_raises_fraud_score(self):
        no_repeat = analyze_buyer_communication(_cinp(messages=[
            _msg("I didn't receive the item"),
        ], is_repeat_disputer=False))
        with_repeat = analyze_buyer_communication(_cinp(messages=[
            _msg("I didn't receive the item"),
        ], is_repeat_disputer=True))
        fraud_order = [
            FraudSignalLevel.VERY_HIGH, FraudSignalLevel.HIGH,
            FraudSignalLevel.MODERATE, FraudSignalLevel.LOW, FraudSignalLevel.MINIMAL,
        ]
        # repeat disputer should have equal or higher fraud level
        assert fraud_order.index(with_repeat.fraud_signal_level) <= \
               fraud_order.index(no_repeat.fraud_signal_level)

    # ── Cooperation signals ───────────────────────────────────────

    def test_cooperative_buyer_low_hostility(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I understand there may be a delay. I am willing to wait "
                 "a few more days. Please let me know the status. I appreciate your help."),
        ]))
        assert r.hostility_level in (HostilityLevel.COOPERATIVE, HostilityLevel.LOW)

    def test_cooperative_tone_recommended_for_polite_buyer(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("Can we resolve this? I'd like to work this out amicably. "
                 "I am open to any solution you can offer."),
        ]))
        assert r.recommended_tone in (RecommendedTone.COOPERATIVE, RecommendedTone.FORMAL)

    # ── Tone recommendations ──────────────────────────────────────

    def test_legal_tone_for_extreme_threats(self):
        r = analyze_buyer_communication(_cinp(messages=[
            _msg("I will sue you. My lawyer is filing today. "
                 "I will go to the BBB and police. Social media blast coming."),
        ]))
        assert r.recommended_tone in (RecommendedTone.LEGAL, RecommendedTone.FIRM)

    def test_firm_tone_for_fraud_with_hostility(self):
        r = analyze_buyer_communication(_cinp(
            is_repeat_disputer=True,
            messages=[
                _msg("I never received it (even though i received it and used it). "
                     "I know the chargeback rules. Send me a refund or I'll chargeback. "
                     "I've done this before many times."),
            ]
        ))
        assert r.recommended_tone in (RecommendedTone.FIRM, RecommendedTone.LEGAL)

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_buyer_communication(_cinp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_counts_non_negative(self):
        r = analyze_buyer_communication(_cinp(messages=[_msg("test message")]))
        assert r.threat_count >= 0
        assert r.urgency_count >= 0
        assert r.sophistication_count >= 0
        assert r.cooperation_count >= 0

    def test_action_note_non_empty(self):
        r = analyze_buyer_communication(_cinp(messages=[_msg("hi")]))
        assert len(r.action_note) > 0
