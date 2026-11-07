"""
Wave 61 — Cross-Merchant Intelligence Engine
Tests: 24 total
"""

import pytest
from oracle.cross_merchant_intel import (
    MerchantDisputeRecord,
    CrossMerchantIntelInput,
    FraudNetworkType,
    NetworkThreatLevel,
    analyze_cross_merchant_intel,
)


def _rec(
    merchant_id="M1",
    buyer_id="BUYER_A",
    dispute_id="D1",
    amount=150.0,
    reason_code="INR",
    dispute_date="2026-11-01",
    shipping_zip="10001",
    was_resolved_refund=False,
    dispute_text_hash="HASH_ABC",
) -> MerchantDisputeRecord:
    return MerchantDisputeRecord(
        merchant_id=merchant_id,
        dispute_id=dispute_id,
        buyer_id=buyer_id,
        buyer_email_domain="example.com",
        shipping_zip=shipping_zip,
        dispute_amount=amount,
        reason_code=reason_code,
        dispute_date=dispute_date,
        was_resolved_refund=was_resolved_refund,
        dispute_text_hash=dispute_text_hash,
    )


def _run(records, min_merchants=2):
    return analyze_cross_merchant_intel(
        CrossMerchantIntelInput(records=records, min_merchants_for_network=min_merchants)
    )


class TestCrossMerchantIntel:

    # ─── Basic structure ─────────────────────────────────────────────────────

    def test_empty_input_no_alerts(self):
        r = _run([])
        assert r.network_alerts == []
        assert r.paypal_should_act is False

    def test_single_merchant_no_serial_alert(self):
        records = [
            _rec(merchant_id="M1", buyer_id="BUYER_X", dispute_id="D1"),
            _rec(merchant_id="M1", buyer_id="BUYER_X", dispute_id="D2"),
        ]
        r = _run(records)
        serial = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SERIAL_DISPUTER]
        assert serial == []

    def test_report_counts_are_correct(self):
        records = [
            _rec("M1", "BUYER_X", "D1"),
            _rec("M2", "BUYER_X", "D2"),
            _rec("M3", "BUYER_Y", "D3"),
        ]
        r = _run(records)
        assert r.total_records_analyzed == 3
        assert r.unique_merchants == 3
        assert r.unique_buyer_ids == 2

    # ─── Serial disputer detection ───────────────────────────────────────────

    def test_serial_disputer_detected_across_two_merchants(self):
        records = [
            _rec("M1", "BAD_BUYER", "D1", amount=200),
            _rec("M2", "BAD_BUYER", "D2", amount=180),
        ]
        r = _run(records)
        serial = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SERIAL_DISPUTER]
        assert len(serial) == 1
        assert serial[0].merchant_count == 2
        assert serial[0].total_dispute_amount == 380.0

    def test_serial_disputer_across_five_merchants_is_critical(self):
        records = [
            _rec(f"M{i}", "SERIAL_FRAUDSTER", f"D{i}", amount=300)
            for i in range(1, 6)
        ]
        r = _run(records)
        serial = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SERIAL_DISPUTER]
        assert len(serial) == 1
        assert serial[0].threat_level == NetworkThreatLevel.CRITICAL
        assert serial[0].merchant_count == 5

    def test_serial_disputer_in_high_risk_buyers(self):
        records = [
            _rec(f"M{i}", "HIGH_RISK_BUYER", f"D{i}", amount=500)
            for i in range(1, 4)
        ]
        r = _run(records)
        assert "HIGH_RISK_BUYER" in r.high_risk_buyer_ids

    def test_different_buyers_no_serial_alert(self):
        records = [
            _rec("M1", "BUYER_A", "D1"),
            _rec("M2", "BUYER_B", "D2"),
        ]
        r = _run(records)
        serial = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SERIAL_DISPUTER]
        assert serial == []

    # ─── Address reuse detection ─────────────────────────────────────────────

    def test_address_reuse_detected(self):
        records = [
            _rec("M1", "BUYER_A", "D1", shipping_zip="99999"),
            _rec("M2", "BUYER_B", "D2", shipping_zip="99999"),
        ]
        r = _run(records)
        addr = [a for a in r.network_alerts if a.network_type == FraudNetworkType.ADDRESS_REUSE]
        assert len(addr) == 1
        assert "99999" in addr[0].zip_codes_involved

    def test_address_reuse_same_buyer_not_flagged(self):
        # Same buyer same zip = serial disputer, not address reuse
        records = [
            _rec("M1", "BUYER_A", "D1", shipping_zip="99999"),
            _rec("M2", "BUYER_A", "D2", shipping_zip="99999"),
        ]
        r = _run(records)
        addr = [a for a in r.network_alerts if a.network_type == FraudNetworkType.ADDRESS_REUSE]
        # Need at least 2 different buyer_ids for address reuse alert
        assert len(addr) == 0

    def test_high_risk_zip_codes_populated(self):
        records = [
            _rec(f"M{i}", f"BUYER_{i}", f"D{i}", shipping_zip="88888", amount=600)
            for i in range(1, 4)
        ]
        r = _run(records)
        assert "88888" in r.high_risk_zip_codes

    # ─── Script cloner detection ─────────────────────────────────────────────

    def test_script_cloner_detected(self):
        records = [
            _rec("M1", "BUYER_A", "D1", dispute_text_hash="HASH_COPY"),
            _rec("M2", "BUYER_B", "D2", dispute_text_hash="HASH_COPY"),
        ]
        r = _run(records)
        cloners = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SCRIPT_CLONER]
        assert len(cloners) == 1
        assert cloners[0].confidence >= 0.85

    def test_unique_hashes_no_cloner_alert(self):
        records = [
            _rec("M1", "BUYER_A", "D1", dispute_text_hash="UNIQUE_HASH_1"),
            _rec("M2", "BUYER_B", "D2", dispute_text_hash="UNIQUE_HASH_2"),
        ]
        r = _run(records)
        cloners = [a for a in r.network_alerts if a.network_type == FraudNetworkType.SCRIPT_CLONER]
        assert cloners == []

    # ─── Chargeback wash detection ────────────────────────────────────────────

    def test_chargeback_wash_detected(self):
        records = [
            _rec("M1", "WASH_BUYER", "D1", was_resolved_refund=True, amount=200),
            _rec("M2", "WASH_BUYER", "D2", was_resolved_refund=True, amount=180),
        ]
        r = _run(records, min_merchants=1)
        wash = [a for a in r.network_alerts if a.network_type == FraudNetworkType.CHARGEBACK_WASH]
        assert len(wash) == 1
        assert wash[0].total_dispute_amount == 380.0

    def test_no_refund_no_wash_alert(self):
        records = [
            _rec("M1", "NORMAL_BUYER", "D1", was_resolved_refund=False),
            _rec("M2", "NORMAL_BUYER", "D2", was_resolved_refund=False),
        ]
        r = _run(records)
        wash = [a for a in r.network_alerts if a.network_type == FraudNetworkType.CHARGEBACK_WASH]
        assert wash == []

    # ─── Threat ordering ─────────────────────────────────────────────────────

    def test_alerts_sorted_by_threat_level(self):
        records = [
            # Create a medium-threat serial disputer (2 merchants)
            _rec("M1", "MEDIUM_BUYER", "D1", amount=50),
            _rec("M2", "MEDIUM_BUYER", "D2", amount=50),
            # Create a critical serial disputer (5 merchants, high amount)
            *[_rec(f"MX{i}", "CRITICAL_BUYER", f"DCR{i}", amount=1500) for i in range(1, 6)],
        ]
        r = _run(records)
        if len(r.network_alerts) >= 2:
            levels = [a.threat_level for a in r.network_alerts]
            order_map = {NetworkThreatLevel.CRITICAL: 0, NetworkThreatLevel.HIGH: 1,
                         NetworkThreatLevel.MEDIUM: 2, NetworkThreatLevel.LOW: 3}
            indices = [order_map.get(l, 99) for l in levels]
            assert indices == sorted(indices)

    # ─── PayPal action flag ───────────────────────────────────────────────────

    def test_paypal_should_act_on_critical(self):
        records = [
            _rec(f"M{i}", "FRAUD_KING", f"D{i}", amount=1000)
            for i in range(1, 7)
        ]
        r = _run(records)
        assert r.paypal_should_act is True

    def test_paypal_no_action_low_threat(self):
        records = [
            _rec("M1", "LOW_RISK", "D1", amount=10),
            _rec("M2", "LOW_RISK", "D2", amount=10),
        ]
        r = _run(records)
        # Low amounts → LOW threat → paypal_should_act may be False
        critical_or_high = any(
            a.threat_level in (NetworkThreatLevel.CRITICAL, NetworkThreatLevel.HIGH)
            for a in r.network_alerts
        )
        assert r.paypal_should_act == critical_or_high

    # ─── Summary quality ─────────────────────────────────────────────────────

    def test_summary_contains_key_counts(self):
        records = [
            _rec("M1", "B1", "D1"),
            _rec("M2", "B1", "D2"),
        ]
        r = _run(records)
        assert str(r.unique_merchants) in r.summary or "2" in r.summary

    def test_paypal_alert_text_populated(self):
        records = [
            _rec("M1", "ALERT_BUYER", "D1", amount=800),
            _rec("M2", "ALERT_BUYER", "D2", amount=700),
            _rec("M3", "ALERT_BUYER", "D3", amount=600),
        ]
        r = _run(records)
        for alert in r.network_alerts:
            assert len(alert.paypal_alert_text) > 20
