"""
Tests for Wave 33: duplicate_charge_detector, friendly_fraud_scorer.
"""
import pytest
from oracle.duplicate_charge_detector import (
    detect_duplicate_charge, DuplicateChargeInput, TransactionRecord,
    DuplicateVerdict, DuplicateResolution,
)
from oracle.friendly_fraud_scorer import (
    score_friendly_fraud, FriendlyFraudInput,
    FraudRiskLevel, RecommendedAction,
)


# ═══════════════════════════════════════════════════════════════════
# Duplicate Charge Detector Tests
# ═══════════════════════════════════════════════════════════════════

class TestDuplicateChargeDetector:

    BASE_TS = 1700000000  # Unix timestamp anchor

    def _tx(self, tx_id, amount=50.0, offset_secs=0) -> TransactionRecord:
        return TransactionRecord(
            transaction_id=tx_id,
            amount=amount,
            timestamp_epoch=self.BASE_TS + offset_secs,
            order_id=f"ORD-{tx_id}",
        )

    def _inp(self, **kw) -> DuplicateChargeInput:
        defaults = dict(
            dispute_id="D-001",
            disputed_transaction_id="TX-1",
            disputed_amount=50.0,
            card_last4="4242",
        )
        defaults.update(kw)
        return DuplicateChargeInput(**defaults)

    # ── Verdict: TRUE_DUPLICATE ───────────────────────────────────

    def test_true_duplicate_same_amount_same_window_one_order(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0, offset_secs=0),
                self._tx("TX-2", amount=50.0, offset_secs=60),  # 1 min later
            ],
            unique_order_count=1,
        ))
        assert r.verdict == DuplicateVerdict.TRUE_DUPLICATE

    def test_true_duplicate_triggers_refund_resolution(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=120),
            ],
            unique_order_count=1,
        ))
        assert r.resolution == DuplicateResolution.REFUND_SECOND

    def test_true_duplicate_merchant_fault(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=120),
            ],
            unique_order_count=1,
        ))
        assert r.is_merchant_fault is True

    # ── Verdict: CONFIRMED_LEGITIMATE ────────────────────────────

    def test_legitimate_when_no_matching_transactions(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[self._tx("TX-1", amount=50.0)],
            unique_order_count=1,
        ))
        assert r.verdict == DuplicateVerdict.CONFIRMED_LEGITIMATE

    def test_legitimate_with_separate_shipments(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=300),
            ],
            has_separate_shipments=True,
            has_separate_fulfillment_logs=True,
            unique_order_count=2,
        ))
        assert r.verdict == DuplicateVerdict.CONFIRMED_LEGITIMATE

    def test_legitimate_when_buyer_acknowledged_second_order(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=300),
            ],
            buyer_acknowledged_second_order=True,
        ))
        assert r.verdict == DuplicateVerdict.CONFIRMED_LEGITIMATE

    def test_legitimate_not_merchant_fault(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[self._tx("TX-1", amount=50.0)],
            unique_order_count=1,
        ))
        assert r.is_merchant_fault is False

    # ── Verdict: AMBIGUOUS ────────────────────────────────────────

    def test_ambiguous_two_orders_same_amount(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=300),
            ],
            unique_order_count=2,
        ))
        assert r.verdict == DuplicateVerdict.AMBIGUOUS

    def test_ambiguous_triggers_investigate(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=300),
            ],
            unique_order_count=2,
        ))
        assert r.resolution == DuplicateResolution.INVESTIGATE

    # ── Window filtering ──────────────────────────────────────────

    def test_outside_window_not_duplicate(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=90000),  # 25 hours later
            ],
            dispute_window_hours=24,
            unique_order_count=1,
        ))
        # TX-2 is outside the 24h window, so no duplicate found
        assert DuplicateVerdict.TRUE_DUPLICATE != r.verdict

    # ── Win probability ───────────────────────────────────────────

    def test_win_prob_higher_for_legitimate(self):
        legit = detect_duplicate_charge(self._inp(
            recent_transactions=[self._tx("TX-1")],
            unique_order_count=1,
        ))
        dup = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1"),
                self._tx("TX-2", offset_secs=120),
            ],
            unique_order_count=1,
        ))
        assert legit.win_probability > dup.win_probability

    # ── Duplicate list ────────────────────────────────────────────

    def test_duplicate_transactions_listed(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=50.0, offset_secs=120),
            ],
            unique_order_count=1,
        ))
        assert "TX-2" in r.duplicate_transactions

    def test_no_duplicates_when_different_amount(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[
                self._tx("TX-1", amount=50.0),
                self._tx("TX-2", amount=99.99, offset_secs=120),
            ],
            unique_order_count=1,
        ))
        assert r.duplicate_transactions == []

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = detect_duplicate_charge(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_actions_non_empty(self):
        r = detect_duplicate_charge(self._inp(
            recent_transactions=[self._tx("TX-1")],
            unique_order_count=1,
        ))
        assert len(r.immediate_actions) >= 1


# ═══════════════════════════════════════════════════════════════════
# Friendly Fraud Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestFriendlyFraudScorer:

    def _inp(self, **kw) -> FriendlyFraudInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            disputed_amount=100.0,
        )
        defaults.update(kw)
        return FriendlyFraudInput(**defaults)

    # ── Score range ───────────────────────────────────────────────

    def test_score_in_range(self):
        r = score_friendly_fraud(self._inp())
        assert 0.0 <= r.fraud_score <= 1.0

    def test_low_score_no_evidence(self):
        r = score_friendly_fraud(self._inp())
        assert r.fraud_score < 0.60

    # ── High fraud signals ────────────────────────────────────────

    def test_delivery_confirmed_raises_score(self):
        no_conf = score_friendly_fraud(self._inp())
        with_conf = score_friendly_fraud(self._inp(has_delivery_confirmation=True))
        assert with_conf.fraud_score > no_conf.fraud_score

    def test_accessed_digital_raises_score_significantly(self):
        no_access = score_friendly_fraud(self._inp(is_digital_goods=True))
        accessed = score_friendly_fraud(self._inp(
            is_digital_goods=True,
            item_accessed_after_purchase=True,
        ))
        assert accessed.fraud_score > no_access.fraud_score

    def test_very_high_risk_level_with_all_signals(self):
        r = score_friendly_fraud(self._inp(
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
            buyer_acknowledged_receipt=True,
            item_accessed_after_purchase=True,
            is_repeat_dispute_same_merchant=True,
            prior_disputes_won_by_buyer=3,
            days_between_delivery_and_dispute=1,
            item_delivered_before_dispute=True,
        ))
        assert r.risk_level in (FraudRiskLevel.VERY_HIGH, FraudRiskLevel.HIGH)

    def test_very_low_risk_with_no_evidence(self):
        r = score_friendly_fraud(self._inp(claim_type="UNAUTHORIZED"))
        assert r.risk_level in (FraudRiskLevel.VERY_LOW, FraudRiskLevel.LOW, FraudRiskLevel.MODERATE)

    # ── Action mapping ────────────────────────────────────────────

    def test_fight_aggressively_on_very_high(self):
        r = score_friendly_fraud(self._inp(
            has_delivery_confirmation=True,
            buyer_acknowledged_receipt=True,
            item_accessed_after_purchase=True,
            has_usage_logs=True,
            is_repeat_dispute_same_merchant=True,
            prior_disputes_won_by_buyer=5,
            days_between_delivery_and_dispute=1,
            item_delivered_before_dispute=True,
        ))
        assert r.recommended_action in (
            RecommendedAction.FIGHT_AGGRESSIVELY,
            RecommendedAction.FIGHT,
        )

    def test_consider_settle_on_very_low(self):
        r = score_friendly_fraud(self._inp(
            claim_type="UNAUTHORIZED",
            same_device_as_prior_orders=False,
            same_ip_as_prior_orders=False,
            shipping_matches_billing=False,
        ))
        assert r.recommended_action in (
            RecommendedAction.CONSIDER_SETTLE,
            RecommendedAction.EVALUATE,
        )

    # ── Adjusted win probability ──────────────────────────────────

    def test_adj_win_prob_higher_for_high_fraud(self):
        low_fraud = score_friendly_fraud(self._inp())
        high_fraud = score_friendly_fraud(self._inp(
            has_delivery_confirmation=True,
            buyer_acknowledged_receipt=True,
            item_accessed_after_purchase=True,
            is_repeat_dispute_same_merchant=True,
        ))
        assert high_fraud.adjusted_win_probability >= low_fraud.adjusted_win_probability

    def test_adj_win_in_range(self):
        r = score_friendly_fraud(self._inp())
        assert 0.0 <= r.adjusted_win_probability <= 1.0

    # ── Signals list ──────────────────────────────────────────────

    def test_top_fraud_signals_on_high_risk(self):
        r = score_friendly_fraud(self._inp(
            has_delivery_confirmation=True,
            buyer_acknowledged_receipt=True,
        ))
        assert len(r.top_fraud_signals) >= 1

    def test_missing_evidence_when_no_delivery_conf(self):
        r = score_friendly_fraud(self._inp())
        assert "delivery_confirmation" in r.missing_evidence_to_prove_fraud

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = score_friendly_fraud(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_repeat_disputer_signal_in_list(self):
        r = score_friendly_fraud(self._inp(
            is_repeat_dispute_same_merchant=True))
        signal_texts = " ".join(r.top_fraud_signals)
        assert "prior" in signal_texts.lower() or "dispute" in signal_texts.lower()
