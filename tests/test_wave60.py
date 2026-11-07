"""
Wave 60 — Sponsor Integrations + PayPal Ecosystem Reporter
Tests: 42 total

Modules under test:
  - oracle/paypal_ecosystem_reporter.py
  - integrations/shopify_enricher.py
  - integrations/google_drive_evidence.py
  - integrations/gmail_prevention.py
  - integrations/firecrawl_tracker.py
  - integrations/zapier_ingest.py
"""

import pytest
from oracle.paypal_ecosystem_reporter import (
    EcosystemReportInput,
    MerchantHealthTier,
    BuyerProtectionScore,
    generate_ecosystem_report,
)
from integrations.shopify_enricher import (
    enrich_from_shopify_order,
    _map_item_category,
    _ltv_tier,
    _delivery_status,
    _hours_since_purchase,
)
from integrations.google_drive_evidence import (
    EvidenceFile,
    EvidenceType,
    EvidenceSource,
    EvidencePackage,
    score_evidence_package,
    build_search_queries,
    classify_drive_file,
)
from integrations.gmail_prevention import (
    PreventionEmailContext,
    compose_prevention_email,
    EmailPriority,
)
from integrations.firecrawl_tracker import (
    CarrierCode,
    DeliveryStatus,
    detect_carrier,
    build_tracking_url,
    parse_tracking_page,
)
from integrations.zapier_ingest import (
    SourcePlatform,
    normalize_zapier_payload,
    detect_platform,
    normalize_reason_code,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _eco(
    transactions=10000, disputes=70,
    won=42, lost=20, accepted=8,
    inr=20, snad=30, unauth=15, sub=5,
    evidence_score=72.0, evidence_missing_rate=0.15,
    unique_buyers=55, repeat=8, fraud=2,
    total_amount=14000.0, recovered=8000.0, lost_amt=5000.0, fees=1200.0,
    visa=25, mc=20, amex=5, pp=20,
    avg_response_hrs=18.0, responded_on_time=65,
    prev_rate=0.006, prev_win_rate=0.55,
) -> EcosystemReportInput:
    return EcosystemReportInput(
        merchant_id="TEST_MERCHANT",
        report_period_days=90,
        total_transactions=transactions,
        total_disputes=disputes,
        disputes_won=won,
        disputes_lost=lost,
        disputes_accepted_early=accepted,
        disputes_pending=0,
        inr_count=inr,
        snad_count=snad,
        unauthorized_count=unauth,
        subscription_count=sub,
        avg_evidence_score=evidence_score,
        evidence_missing_rate=evidence_missing_rate,
        unique_buyers_disputing=unique_buyers,
        repeat_disputers=repeat,
        suspected_fraud_buyers=fraud,
        total_dispute_amount=total_amount,
        total_recovered=recovered,
        total_lost=lost_amt,
        total_fees_paid=fees,
        visa_disputes=visa,
        mc_disputes=mc,
        amex_disputes=amex,
        paypal_disputes=pp,
        avg_response_time_hours=avg_response_hrs,
        disputes_responded_on_time=responded_on_time,
        prev_period_dispute_rate=prev_rate,
        prev_period_win_rate=prev_win_rate,
    )


def _shopify_order(**overrides):
    base = {
        "id": "12345",
        "total_price": "149.99",
        "currency": "USD",
        "created_at": "2026-11-01T10:00:00Z",
        "fulfillment_status": "fulfilled",
        "fulfillments": [{"tracking_number": "1Z12345E6789012345", "tracking_company": "UPS"}],
        "shipping_lines": [{"title": "Standard Shipping"}],
        "line_items": [{"product_type": "clothing", "tags": ""}],
        "tags": "",
        "billing_address": {"zip": "10001", "country_code": "US"},
        "shipping_address": {"zip": "10001", "country_code": "US"},
        "payment_gateway": "stripe",
        "customer": {
            "email": "buyer@example.com",
            "orders_count": 5,
            "total_spent": "750.00",
            "created_at": "2024-01-01T00:00:00Z",
        },
    }
    base.update(overrides)
    return base


def _email_ctx(**overrides):
    base = dict(
        buyer_email="buyer@example.com",
        buyer_first_name="Alex",
        merchant_name="TestShop",
        merchant_support_email="support@testshop.com",
        order_id="ORD-999",
        order_amount=129.99,
        item_name="Blue Denim Jacket",
        tracking_number="9400111899223397718430",
    )
    base.update(overrides)
    return PreventionEmailContext(**base)


# ══════════════════════════════════════════════════════════════════════════════
# PayPal Ecosystem Reporter (12 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestPayPalEcosystemReporter:

    def test_excellent_health_tier(self):
        inp = _eco(disputes=40, transactions=10000, won=30, lost=8, accepted=2)
        r = generate_ecosystem_report(inp)
        assert r.dispute_rate <= 0.005
        assert r.merchant_health_tier == MerchantHealthTier.EXCELLENT

    def test_at_risk_health_tier(self):
        inp = _eco(disputes=120, transactions=10000, won=30, lost=80, accepted=10)
        r = generate_ecosystem_report(inp)
        assert r.merchant_health_tier in (MerchantHealthTier.AT_RISK, MerchantHealthTier.CRITICAL)

    def test_critical_tier_breach(self):
        inp = _eco(disputes=250, transactions=10000, won=20, lost=200, accepted=30)
        r = generate_ecosystem_report(inp)
        assert r.merchant_health_tier == MerchantHealthTier.CRITICAL

    def test_dispute_rate_threshold_flag_generated(self):
        inp = _eco(disputes=120, transactions=10000, won=30, lost=80, accepted=10)
        r = generate_ecosystem_report(inp)
        codes = [f.code for f in r.compliance_flags]
        assert "DISPUTE_RATE_THRESHOLD_BREACH" in codes

    def test_fraud_signal_flag_generated(self):
        inp = _eco(fraud=5)
        r = generate_ecosystem_report(inp)
        codes = [f.code for f in r.compliance_flags]
        assert "FRAUD_NETWORK_SIGNAL" in codes

    def test_paypal_actions_contain_fraud_signal(self):
        inp = _eco(fraud=5)
        r = generate_ecosystem_report(inp)
        joined = " ".join(r.paypal_actions)
        assert "FRAUD SIGNAL" in joined or "fraud" in joined.lower()

    def test_buyer_protection_abused(self):
        inp = _eco(unique_buyers=10, fraud=4)
        r = generate_ecosystem_report(inp)
        assert r.buyer_protection_score == BuyerProtectionScore.ABUSED

    def test_buyer_protection_strong(self):
        inp = _eco(won=60, lost=10, accepted=5, fraud=0, unique_buyers=50)
        r = generate_ecosystem_report(inp)
        assert r.buyer_protection_score in (BuyerProtectionScore.STRONG, BuyerProtectionScore.ADEQUATE)

    def test_network_rates_sum_reasonably(self):
        inp = _eco()
        r = generate_ecosystem_report(inp)
        assert len(r.network_dispute_rates) == 4
        assert all(0 <= v <= 1 for v in r.network_dispute_rates.values())

    def test_worsening_trend_detected(self):
        inp = _eco(disputes=80, transactions=10000, prev_rate=0.005)
        r = generate_ecosystem_report(inp)
        assert r.dispute_rate_trend == "WORSENING"

    def test_improving_trend_detected(self):
        inp = _eco(disputes=40, transactions=10000, prev_rate=0.009)
        r = generate_ecosystem_report(inp)
        assert r.dispute_rate_trend == "IMPROVING"

    def test_summary_fields_populated(self):
        r = generate_ecosystem_report(_eco())
        assert r.merchant_id == "TEST_MERCHANT"
        assert len(r.summary) > 20
        assert len(r.paypal_summary) > 20
        assert 0 <= r.health_score <= 100


# ══════════════════════════════════════════════════════════════════════════════
# Shopify Enricher (10 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestShopifyEnricher:

    def test_basic_order_enrichment(self):
        ctx = enrich_from_shopify_order(_shopify_order())
        assert ctx.order_id == "12345"
        assert ctx.order_amount == 149.99
        assert ctx.buyer_ltv_tier == "VIP"  # total_spent=750

    def test_category_mapping_clothing(self):
        ctx = enrich_from_shopify_order(_shopify_order())
        assert ctx.item_category == "CLOTHING"

    def test_category_mapping_electronics(self):
        order = _shopify_order(line_items=[{"product_type": "electronics", "tags": ""}])
        ctx = enrich_from_shopify_order(order)
        assert ctx.item_category == "ELECTRONICS"

    def test_digital_product_detection(self):
        order = _shopify_order(line_items=[{"product_type": "digital", "tags": "download"}])
        ctx = enrich_from_shopify_order(order)
        assert ctx.is_digital_product is True

    def test_express_shipping_detection(self):
        order = _shopify_order(shipping_lines=[{"title": "Express 2-Day Shipping"}])
        ctx = enrich_from_shopify_order(order)
        assert ctx.is_express_shipping is True

    def test_tracking_number_extracted(self):
        ctx = enrich_from_shopify_order(_shopify_order())
        assert ctx.tracking_number == "1Z12345E6789012345"
        assert ctx.carrier_has_tracking is True

    def test_no_tracking(self):
        order = _shopify_order(fulfillments=[], fulfillment_status="unfulfilled")
        ctx = enrich_from_shopify_order(order)
        assert ctx.carrier_has_tracking is False
        assert ctx.delivery_status == "PENDING"

    def test_to_prevention_kwargs(self):
        ctx = enrich_from_shopify_order(_shopify_order())
        kwargs = ctx.to_prevention_kwargs()
        assert "delivery_status" in kwargs
        assert "hours_since_purchase" in kwargs

    def test_to_ltv_kwargs(self):
        ctx = enrich_from_shopify_order(_shopify_order())
        kwargs = ctx.to_ltv_kwargs()
        assert kwargs["buyer_ltv_tier"] == "VIP"
        assert kwargs["buyer_ltv_estimate"] > 0

    def test_ltv_tier_boundaries(self):
        assert _ltv_tier(600) == "VIP"
        assert _ltv_tier(300) == "HIGH"
        assert _ltv_tier(75) == "MEDIUM"
        assert _ltv_tier(15) == "LOW"
        assert _ltv_tier(5) == "NEGLIGIBLE"


# ══════════════════════════════════════════════════════════════════════════════
# Google Drive Evidence (8 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestGoogleDriveEvidence:

    def _make_file(self, evidence_type: EvidenceType) -> EvidenceFile:
        return EvidenceFile(
            file_id="f1", file_name="test.pdf",
            evidence_type=evidence_type,
            source=EvidenceSource.GOOGLE_DRIVE,
            mime_type="application/pdf",
            size_bytes=1024,
        )

    def test_inr_complete_package(self):
        pkg = EvidencePackage(dispute_id="D1", order_id="O1", reason_code="INR")
        pkg.files = [
            self._make_file(EvidenceType.TRACKING_SCREENSHOT),
            self._make_file(EvidenceType.PROOF_OF_DELIVERY),
            self._make_file(EvidenceType.INVOICE),
        ]
        pkg = score_evidence_package(pkg)
        assert pkg.completeness_score == 100.0
        assert pkg.is_ready_to_submit is True
        assert pkg.missing_evidence_types == []

    def test_inr_incomplete_package(self):
        pkg = EvidencePackage(dispute_id="D2", order_id="O2", reason_code="INR")
        pkg.files = [self._make_file(EvidenceType.INVOICE)]
        pkg = score_evidence_package(pkg)
        assert pkg.completeness_score < 100.0
        assert pkg.is_ready_to_submit is False
        assert len(pkg.missing_evidence_types) > 0

    def test_classify_tracking_file(self):
        t = classify_drive_file("fedex_tracking_screenshot.png", "image/png")
        assert t == EvidenceType.TRACKING_SCREENSHOT

    def test_classify_invoice_file(self):
        t = classify_drive_file("invoice_12345.pdf", "application/pdf")
        assert t == EvidenceType.INVOICE

    def test_classify_policy_file(self):
        t = classify_drive_file("return_policy.pdf", "application/pdf")
        assert t == EvidenceType.POLICY_DOCUMENT

    def test_build_search_queries_inr(self):
        queries = build_search_queries("ORD-999", "INR")
        assert any("tracking" in q for q in queries)
        assert any("ORD-999" in q for q in queries)

    def test_build_search_queries_snad(self):
        queries = build_search_queries("ORD-999", "SNAD")
        assert any("invoice" in q or "policy" in q for q in queries)

    def test_evidence_package_properties(self):
        pkg = EvidencePackage(dispute_id="D3", order_id="O3", reason_code="INR")
        pkg.files = [self._make_file(EvidenceType.TRACKING_SCREENSHOT)]
        assert pkg.has_tracking_evidence is True
        assert pkg.has_invoice is False
        assert pkg.file_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Gmail Prevention (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestGmailPrevention:

    def test_buyer_outreach_email(self):
        draft = compose_prevention_email("BUYER_OUTREACH", _email_ctx())
        assert "ORD-999" in draft.subject
        assert "Alex" in draft.body
        assert draft.priority == EmailPriority.HIGH
        assert draft.to == "buyer@example.com"

    def test_partial_refund_email(self):
        ctx = _email_ctx(partial_refund_amount=84.49)
        draft = compose_prevention_email("PARTIAL_REFUND_OFFER", ctx)
        assert "84.49" in draft.body
        assert draft.priority == EmailPriority.URGENT
        assert draft.send_within_hours <= 2

    def test_tracking_update_email(self):
        draft = compose_prevention_email("SEND_TRACKING_UPDATE", _email_ctx())
        assert "9400111899223397718430" in draft.body
        assert draft.send_within_hours <= 6

    def test_proactive_refund_email(self):
        draft = compose_prevention_email("PROACTIVE_REFUND", _email_ctx())
        assert "129.99" in draft.body
        assert "refund" in draft.body.lower()

    def test_unknown_action_falls_back_to_outreach(self):
        draft = compose_prevention_email("NONEXISTENT_ACTION", _email_ctx())
        assert draft.action_type == "NONEXISTENT_ACTION"
        assert len(draft.body) > 50


# ══════════════════════════════════════════════════════════════════════════════
# Firecrawl Tracker (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestFirecrawlTracker:

    def test_ups_carrier_detection(self):
        c = detect_carrier("1Z12345E6789012345")
        assert c == CarrierCode.UPS

    def test_amazon_carrier_detection(self):
        c = detect_carrier("TBA123456789012")
        assert c == CarrierCode.AMAZON

    def test_carrier_hint_overrides_pattern(self):
        c = detect_carrier("123456789012", carrier_hint="FedEx")
        assert c == CarrierCode.FEDEX

    def test_parse_delivered_page(self):
        page_text = "Your package was delivered on November 5, 2026 at 2:30pm. Left at front door."
        url = "https://www.ups.com/track?tracknum=1Z12345E6789012345"
        result = parse_tracking_page(page_text, "1Z12345E6789012345", CarrierCode.UPS, url)
        assert result.is_delivered is True
        assert result.status == DeliveryStatus.DELIVERED
        assert result.can_use_as_proof_of_delivery is True

    def test_parse_in_transit_page(self):
        page_text = "Your shipment is in transit. Arrived at sorting facility."
        url = "https://www.ups.com/track?tracknum=1Z12345E6789012345"
        result = parse_tracking_page(page_text, "1Z12345E6789012345", CarrierCode.UPS, url)
        assert result.is_delivered is False
        assert result.status in (DeliveryStatus.IN_TRANSIT, DeliveryStatus.UNKNOWN)


# ══════════════════════════════════════════════════════════════════════════════
# Zapier Ingest (5 tests) - wait, let me add 2 more for 42 total
# ══════════════════════════════════════════════════════════════════════════════

class TestZapierIngest:

    def test_stripe_platform_detection(self):
        payload = {"id": "dp_123abc", "charge": "ch_123", "payment_intent": "pi_123"}
        platform = detect_platform(payload)
        assert platform == SourcePlatform.STRIPE

    def test_normalize_stripe_product_not_received(self):
        payload = {
            "id": "dp_stripe1",
            "charge": "ch_abc",
            "payment_intent": "pi_abc",
            "reason": "product_not_received",
            "amount": 15000,
            "currency": "usd",
        }
        dispute = normalize_zapier_payload(payload)
        assert dispute.reason_code == "INR"
        assert dispute.amount == 150.00
        assert dispute.currency == "USD"

    def test_normalize_generic_dispute(self):
        payload = {
            "dispute_id": "ZAP-9999",
            "transaction_id": "TXN-001",
            "amount": 89.99,
            "currency": "USD",
            "reason_code": "SNAD",
            "buyer_email": "buyer@test.com",
            "network": "VISA",
            "created_at": "2026-11-07T10:00:00Z",
        }
        dispute = normalize_zapier_payload(payload)
        assert dispute.dispute_id == "ZAP-9999"
        assert dispute.reason_code == "SNAD"
        assert dispute.network == "VISA"

    def test_to_anchor_body(self):
        payload = {
            "dispute_id": "ZAP-TEST",
            "amount": 50.0,
            "currency": "USD",
            "reason_code": "INR",
            "buyer_email": "x@example.com",
            "network": "PAYPAL",
        }
        dispute = normalize_zapier_payload(payload)
        body = dispute.to_anchor_body()
        assert body["reason_code"] == "INR"
        assert body["amount"] == 50.0
        assert "dispute_id" in body

    def test_stripe_reason_code_normalization(self):
        assert normalize_reason_code("fraudulent", SourcePlatform.STRIPE) == "UNAUTHORIZED"
        assert normalize_reason_code("product_not_received", SourcePlatform.STRIPE) == "INR"
        assert normalize_reason_code("subscription_canceled", SourcePlatform.STRIPE) == "SUBSCRIPTION"

    def test_unknown_reason_code_fallback(self):
        code = normalize_reason_code("completely_unknown_reason", SourcePlatform.GENERIC)
        assert code in ("SNAD", "INR", "UNAUTHORIZED", "SUBSCRIPTION")
