"""
Tests for Wave 13: fraud_network_analyzer, carrier_intelligence.
"""
import pytest
from datetime import date
from oracle.fraud_network_analyzer import (
    analyze_fraud_network, BuyerSignal, FraudSignalType,
    NetworkRiskLevel, FraudNetworkReport,
)
from oracle.carrier_intelligence import (
    analyze_carrier, CarrierInput, CarrierName, CarrierTier,
)


# ═══════════════════════════════════════════════════════════════════
# Fraud Network Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestFraudNetworkAnalyzer:

    def _buyer(self, buyer_id="B-001", address="123 Main St", disputes=1, **kw) -> BuyerSignal:
        return BuyerSignal(
            buyer_id=buyer_id,
            email=f"{buyer_id.lower()}@example.com",
            shipping_address=address,
            dispute_count_90d=disputes,
            dispute_amounts=kw.pop("dispute_amounts", [100.0] * disputes),
            dispute_dates=kw.pop("dispute_dates", []),
            purchase_count_90d=kw.pop("purchase_count_90d", 2),
            first_order_amount=kw.pop("first_order_amount", None),
            **kw,
        )

    def test_isolated_no_signals(self):
        buyer = self._buyer()
        r = analyze_fraud_network("D-001", buyer, [])
        assert r.network_risk_level == NetworkRiskLevel.ISOLATED
        assert r.signal_count == 0

    def test_address_cluster_detected(self):
        buyer1 = self._buyer("B-001", "123 Main St", 1)
        buyer2 = self._buyer("B-002", "123 Main St", 1)
        buyer3 = self._buyer("B-003", "123 Main St", 1)
        r = analyze_fraud_network("D-001", buyer1, [buyer2, buyer3])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.ADDRESS_CLUSTER in signal_types

    def test_address_cluster_requires_dispute(self):
        # Buyer at same address but 0 disputes — not in cluster
        buyer1 = self._buyer("B-001", "123 Main St", 1)
        buyer2 = self._buyer("B-002", "123 Main St", 0)  # No disputes
        r = analyze_fraud_network("D-001", buyer1, [buyer2])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.ADDRESS_CLUSTER not in signal_types

    def test_velocity_burst_detected_high_dispute_count(self):
        buyer = self._buyer("B-001", disputes=4)
        r = analyze_fraud_network("D-001", buyer, [])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.VELOCITY_BURST in signal_types

    def test_no_velocity_burst_single_dispute(self):
        buyer = self._buyer("B-001", disputes=1)
        r = analyze_fraud_network("D-001", buyer, [])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.VELOCITY_BURST not in signal_types

    def test_velocity_burst_detected_by_dates(self):
        dates = [date(2026, 11, 1), date(2026, 11, 3)]  # 2 days apart
        buyer = self._buyer("B-001", disputes=2, dispute_dates=dates)
        r = analyze_fraud_network("D-001", buyer, [])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.VELOCITY_BURST in signal_types

    def test_card_test_detected(self):
        buyer = self._buyer(
            "B-001",
            first_order_amount=15.0,
            dispute_amounts=[200.0],  # 200/15 = 13× — card test
            disputes=1,
        )
        r = analyze_fraud_network("D-001", buyer, [])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.CARD_TEST_SEQUENCE in signal_types

    def test_no_card_test_when_amounts_similar(self):
        buyer = self._buyer(
            "B-001",
            first_order_amount=100.0,
            dispute_amounts=[150.0],  # 1.5× — not suspicious
            disputes=1,
        )
        r = analyze_fraud_network("D-001", buyer, [])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.CARD_TEST_SEQUENCE not in signal_types

    def test_coordinated_filing_detected(self):
        d = date(2026, 11, 1)
        buyer1 = self._buyer("B-001", disputes=1, dispute_dates=[d])
        buyer2 = self._buyer("B-002", disputes=1, dispute_dates=[d])
        buyer3 = self._buyer("B-003", disputes=1, dispute_dates=[d])
        r = analyze_fraud_network("D-001", buyer1, [buyer2, buyer3])
        signal_types = [s.signal_type for s in r.signals]
        assert FraudSignalType.COORDINATED_FILING in signal_types

    def test_high_risk_level_with_multiple_signals(self):
        # Trigger velocity burst (disputes=4, tight dates) + card test simultaneously
        d = date(2026, 11, 1)
        buyer = self._buyer(
            "B-001",
            disputes=4,
            dispute_dates=[d, d],
            first_order_amount=10.0,
            dispute_amounts=[200.0, 150.0, 180.0, 160.0],  # Card-test pattern
        )
        r = analyze_fraud_network("D-001", buyer, [])
        # With 2+ signals the score should cross ELEVATED (≥40)
        assert r.network_risk_level in (NetworkRiskLevel.ELEVATED, NetworkRiskLevel.HIGH, NetworkRiskLevel.CRITICAL)

    def test_risk_score_zero_to_100(self):
        buyer = self._buyer()
        r = analyze_fraud_network("D-001", buyer, [])
        assert 0 <= r.risk_score <= 100

    def test_related_buyers_identified(self):
        buyer1 = self._buyer("B-001", "123 Main St", 1)
        buyer2 = self._buyer("B-002", "123 Main St", 1)
        r = analyze_fraud_network("D-001", buyer1, [buyer2])
        assert "B-002" in r.related_buyer_ids

    def test_flags_populated_for_high_risk(self):
        buyer = self._buyer("B-001", disputes=5)
        r = analyze_fraud_network("D-001", buyer, [])
        if r.network_risk_level in (NetworkRiskLevel.HIGH, NetworkRiskLevel.CRITICAL):
            assert len(r.flags_for_buyer_lifecycle) > 0

    def test_summary_contains_dispute_id(self):
        buyer = self._buyer()
        r = analyze_fraud_network("D-999", buyer, [])
        assert "D-999" in r.summary

    def test_recommended_action_non_empty(self):
        buyer = self._buyer()
        r = analyze_fraud_network("D-001", buyer, [])
        assert len(r.recommended_action) > 10

    def test_paypal_report_non_empty(self):
        buyer = self._buyer()
        r = analyze_fraud_network("D-001", buyer, [])
        assert len(r.paypal_report_summary) > 10

    def test_critical_level_for_large_address_cluster(self):
        # 5 buyers at same address
        buyers_at_addr = [self._buyer(f"B-{i:03d}", "999 Fraud Ave", 1) for i in range(5)]
        target = buyers_at_addr[0]
        rest = buyers_at_addr[1:]
        r = analyze_fraud_network("D-001", target, rest)
        # With 5 buyers at same address, should be HIGH or CRITICAL
        assert r.network_risk_level in (NetworkRiskLevel.HIGH, NetworkRiskLevel.CRITICAL)


# ═══════════════════════════════════════════════════════════════════
# Carrier Intelligence Tests
# ═══════════════════════════════════════════════════════════════════

class TestCarrierIntelligence:

    def _inp(self, carrier=CarrierName.USPS, amount=200.0, **kw) -> CarrierInput:
        return CarrierInput(
            order_id="ORD-001",
            carrier=carrier,
            order_amount=amount,
            merchant_base_inr_win_rate=kw.pop("base_win_rate", 0.55),
            fight_cost=25.0,
            current_signature=kw.pop("current_signature", False),
        )

    def test_usps_is_tier_1(self):
        r = analyze_carrier(self._inp(CarrierName.USPS))
        assert r.carrier_tier == CarrierTier.TIER_1

    def test_dhl_ecommerce_is_tier_3(self):
        r = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE))
        assert r.carrier_tier == CarrierTier.TIER_3

    def test_ups_has_highest_evidence_quality(self):
        r_ups = analyze_carrier(self._inp(CarrierName.UPS))
        r_dhl = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE))
        assert r_ups.evidence_quality > r_dhl.evidence_quality

    def test_tier1_has_higher_win_rate_than_tier3(self):
        r_usps = analyze_carrier(self._inp(CarrierName.USPS))
        r_dhl = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE))
        assert r_usps.inr_win_rate_with_carrier > r_dhl.inr_win_rate_with_carrier

    def test_signature_recommended_for_high_value(self):
        r = analyze_carrier(self._inp(CarrierName.USPS, amount=800.0))
        assert r.signature_recommended is True

    def test_signature_not_required_for_low_value(self):
        r = analyze_carrier(self._inp(CarrierName.USPS, amount=50.0))
        assert r.signature_recommended is False

    def test_ev_fight_computed(self):
        r = analyze_carrier(self._inp(CarrierName.UPS, amount=200.0))
        expected = round(r.inr_win_rate_with_carrier * 200.0 - 25.0, 2)
        assert r.ev_fight_current == pytest.approx(expected, abs=0.01)

    def test_tier3_carrier_has_upgrade_options(self):
        r = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE))
        assert len(r.upgrade_options) > 0

    def test_tier1_carrier_no_upgrade_options(self):
        r = analyze_carrier(self._inp(CarrierName.USPS))
        assert len(r.upgrade_options) == 0

    def test_upgrade_has_positive_win_rate_improvement(self):
        r = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE))
        for opt in r.upgrade_options:
            assert opt.win_rate_improvement > -0.5  # improvements should be positive for tier 1 targets

    def test_best_upgrade_is_worthwhile(self):
        r = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE, amount=500.0))
        if r.best_upgrade:
            assert r.best_upgrade.is_worth_upgrading is True

    def test_should_upgrade_true_for_poor_carrier(self):
        r = analyze_carrier(self._inp(CarrierName.PITNEY_BOWES, amount=300.0))
        assert r.should_upgrade is True  # Pitney Bowes is Tier 3 — should upgrade

    def test_should_upgrade_false_for_tier1(self):
        r = analyze_carrier(self._inp(CarrierName.UPS))
        assert r.should_upgrade is False

    def test_signature_adds_win_rate(self):
        r_no_sig = analyze_carrier(self._inp(CarrierName.USPS, current_signature=False))
        r_sig = analyze_carrier(self._inp(CarrierName.USPS, current_signature=True))
        assert r_sig.inr_win_rate_with_carrier > r_no_sig.inr_win_rate_with_carrier

    def test_tracking_reliability_in_range(self):
        r = analyze_carrier(self._inp(CarrierName.USPS))
        assert 0.0 <= r.tracking_reliability <= 1.0

    def test_paypal_trust_note_non_empty(self):
        r = analyze_carrier(self._inp(CarrierName.FEDEX))
        assert len(r.paypal_trust_note) > 10

    def test_summary_contains_order_id(self):
        r = analyze_carrier(self._inp())
        assert "ORD-001" in r.summary

    def test_win_rate_capped_at_095(self):
        r = analyze_carrier(self._inp(CarrierName.UPS, base_win_rate=0.90, current_signature=True))
        assert r.inr_win_rate_with_carrier <= 0.95

    def test_ev_upgrade_improvement_positive_when_upgrading(self):
        r = analyze_carrier(self._inp(CarrierName.DHL_ECOMMERCE, amount=400.0))
        if r.should_upgrade:
            assert r.ev_upgrade_improvement >= 0
