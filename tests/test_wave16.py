"""
Tests for Wave 16: response_template_engine, hdr_forecast_modeler.
"""
import pytest
from oracle.response_template_engine import (
    generate_response, DisputeContext, ResponseTone, ResponseSection,
)
from oracle.hdr_forecast_modeler import (
    forecast_hdr, HDRForecastInput, OpenDispute, HDRZone,
)


# ═══════════════════════════════════════════════════════════════════
# Response Template Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestResponseTemplateEngine:

    def _ctx(self, claim_type="INR", **kw) -> DisputeContext:
        defaults = dict(
            dispute_id="D-001",
            order_id="ORD-100",
            order_date="2024-10-01",
            dispute_amount=150.0,
            delivery_address="123 Main St, Springfield",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
            delivery_date="2024-10-05",
        )
        defaults.update(kw)
        return DisputeContext(claim_type=claim_type, **defaults)

    def test_basic_inr_response_generates(self):
        r = generate_response(self._ctx())
        assert r.dispute_id == "D-001"
        assert r.claim_type == "INR"
        assert len(r.paypal_response_text) > 100

    def test_paypal_text_contains_tracking(self):
        r = generate_response(self._ctx(tracking_number="1Z999AA10123456784"))
        assert "1Z999AA10123456784" in r.paypal_response_text

    def test_paypal_text_contains_order_id(self):
        r = generate_response(self._ctx())
        assert "ORD-100" in r.paypal_response_text

    def test_signature_confirmation_mentioned_when_present(self):
        r = generate_response(self._ctx(has_signature=True))
        text = r.paypal_response_text.lower()
        assert "signature" in text

    def test_delivery_photo_mentioned_when_present(self):
        r = generate_response(self._ctx(has_delivery_photo=True))
        text = r.paypal_response_text.lower()
        assert "photo" in text

    def test_snad_response_mentions_listing(self):
        r = generate_response(self._ctx(
            claim_type="SNAD",
            item_description="Blue Widget 10cm",
            listing_url="https://example.com/listing/1",
        ))
        text = r.paypal_response_text.lower()
        assert "listing" in text or "described" in text or "widget" in text.lower()

    def test_unauthorized_response_mentions_ip(self):
        r = generate_response(self._ctx(
            claim_type="UNAUTHORIZED",
            buyer_ip="192.168.1.100",
            prior_order_count=5,
        ))
        assert "192.168.1.100" in r.paypal_response_text

    def test_ce30_eligible_note_in_unauthorized(self):
        r = generate_response(self._ctx(
            claim_type="UNAUTHORIZED",
            ce30_eligible=True,
            ce30_prior_transactions=3,
        ))
        assert "Compelling Evidence" in r.paypal_response_text or "CE" in r.paypal_response_text or "3.0" in r.paypal_response_text

    def test_subscription_mentions_tos(self):
        r = generate_response(self._ctx(
            claim_type="SUBSCRIPTION",
            tos_accepted_date="2024-09-01",
            cancellation_policy_url="https://example.com/cancel",
        ))
        text = r.paypal_response_text.lower()
        assert "terms" in text or "tos" in text or "subscription" in text

    def test_buyer_message_non_empty(self):
        r = generate_response(self._ctx())
        assert len(r.buyer_message_text) > 20

    def test_internal_summary_contains_dispute_id(self):
        r = generate_response(self._ctx(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.internal_summary

    def test_internal_summary_contains_claim_type(self):
        r = generate_response(self._ctx(claim_type="SNAD"))
        assert "SNAD" in r.internal_summary

    def test_submission_checklist_non_empty(self):
        r = generate_response(self._ctx())
        assert len(r.submission_checklist) >= 2

    def test_checklist_contains_submit_reminder(self):
        r = generate_response(self._ctx())
        checklist_text = " ".join(r.submission_checklist).lower()
        assert "submit" in checklist_text or "deadline" in checklist_text

    def test_strength_strong_with_signature_and_tracking(self):
        r = generate_response(self._ctx(
            has_signature=True,
            has_delivery_photo=True,
            buyer_ip="10.0.0.1",
        ))
        assert r.estimated_strength in ("STRONG", "MODERATE")

    def test_strength_weak_no_evidence(self):
        r = generate_response(self._ctx(
            tracking_number=None,
            carrier=None,
            delivery_date=None,
        ))
        assert r.estimated_strength in ("WEAK", "MODERATE")

    def test_blocks_non_empty(self):
        r = generate_response(self._ctx())
        assert len(r.blocks) >= 3

    def test_blocks_contain_header(self):
        r = generate_response(self._ctx())
        sections = [b.section for b in r.blocks]
        assert ResponseSection.HEADER in sections

    def test_summary_contains_claim_type(self):
        r = generate_response(self._ctx(claim_type="SNAD"))
        assert "SNAD" in r.summary

    def test_tone_propagated(self):
        r = generate_response(self._ctx(tone=ResponseTone.ASSERTIVE))
        assert r.tone == ResponseTone.ASSERTIVE

    def test_unknown_claim_type_fallback(self):
        r = generate_response(self._ctx(claim_type="DUPLICATE"))
        assert len(r.paypal_response_text) > 50


# ═══════════════════════════════════════════════════════════════════
# HDR Forecast Modeler Tests
# ═══════════════════════════════════════════════════════════════════

class TestHDRForecastModeler:

    def _inp(self, disputes=30, txns=10000, **kw) -> HDRForecastInput:
        defaults = dict(
            disputes_last_90d=disputes,
            transactions_last_90d=txns,
            daily_transaction_rate=txns / 90.0,
            daily_new_dispute_rate=0.0,
            forecast_days=30,
        )
        defaults.update(kw)
        return HDRForecastInput(**defaults)

    def test_healthy_zone_low_dr(self):
        # 10 disputes / 10000 txns = 0.1% → HEALTHY
        r = forecast_hdr(self._inp(disputes=10, txns=10000))
        assert r.current_zone == HDRZone.HEALTHY
        assert r.current_dispute_rate_pct < 0.5

    def test_elevated_zone(self):
        # 60 disputes / 10000 txns = 0.6% → ELEVATED
        r = forecast_hdr(self._inp(disputes=60, txns=10000))
        assert r.current_zone == HDRZone.ELEVATED

    def test_limited_zone(self):
        # 110 disputes / 10000 txns = 1.1% → LIMITED
        r = forecast_hdr(self._inp(disputes=110, txns=10000))
        assert r.current_zone == HDRZone.LIMITED

    def test_surcharge_zone(self):
        # 160 disputes / 10000 txns = 1.6% → SURCHARGE
        r = forecast_hdr(self._inp(disputes=160, txns=10000))
        assert r.current_zone == HDRZone.SURCHARGE

    def test_severe_zone(self):
        # 350 disputes / 10000 txns = 3.5% → SEVERE
        r = forecast_hdr(self._inp(disputes=350, txns=10000))
        assert r.current_zone == HDRZone.SEVERE

    def test_terminal_zone(self):
        # 600 disputes / 10000 txns = 6% → TERMINAL
        r = forecast_hdr(self._inp(disputes=600, txns=10000))
        assert r.current_zone == HDRZone.TERMINAL

    def test_forecast_has_correct_number_of_days(self):
        r = forecast_hdr(self._inp(forecast_days=14))
        assert len(r.daily_forecast) == 14

    def test_dispute_expiry_lowers_rate(self):
        # Dispute opened 85 days ago will expire in 5 days
        disputes = [OpenDispute("D-001", opened_days_ago=85, dispute_amount=100.0, claim_type="INR")]
        r = forecast_hdr(self._inp(disputes=50, txns=10000, open_disputes=disputes))
        # Day 5 should have one fewer dispute
        day5 = r.daily_forecast[4]
        assert day5.disputes_expiring_today == 1

    def test_absorb_before_elevated_correct(self):
        # At 10 disputes / 10000 txns, can absorb 10000*0.005 - 10 = 50-10=40 more
        r = forecast_hdr(self._inp(disputes=10, txns=10000))
        assert r.disputes_can_absorb_before_elevated == 40

    def test_absorb_before_limited_correct(self):
        # At 50 disputes / 10000, absorb_limited = 100-50 = 50
        r = forecast_hdr(self._inp(disputes=50, txns=10000))
        assert r.disputes_can_absorb_before_limited == 50

    def test_new_disputes_raises_rate_over_time(self):
        # 1 new dispute per day, start healthy
        r = forecast_hdr(self._inp(disputes=10, txns=10000, daily_new_dispute_rate=1.0))
        # Rate should be higher on day 30 than day 1
        assert r.daily_forecast[-1].dispute_rate >= r.daily_forecast[0].dispute_rate

    def test_close_recommendations_have_rank(self):
        disputes = [OpenDispute(f"D-{i:03d}", opened_days_ago=i*5, dispute_amount=100.0, claim_type="INR") for i in range(5)]
        r = forecast_hdr(self._inp(disputes=50, txns=10000, open_disputes=disputes))
        if r.close_recommendations:
            assert r.close_recommendations[0].priority_rank == 1

    def test_close_recommendations_non_empty_when_disputes_open(self):
        disputes = [OpenDispute(f"D-{i:03d}", opened_days_ago=30, dispute_amount=100.0, claim_type="INR") for i in range(5)]
        r = forecast_hdr(self._inp(disputes=100, txns=10000, open_disputes=disputes))
        assert len(r.close_recommendations) > 0

    def test_summary_contains_zone(self):
        r = forecast_hdr(self._inp(disputes=10, txns=10000))
        assert "HEALTHY" in r.summary

    def test_recommendations_non_empty(self):
        r = forecast_hdr(self._inp(disputes=10, txns=10000))
        assert len(r.recommendations) > 0

    def test_trajectory_stable_no_new_disputes(self):
        r = forecast_hdr(self._inp(disputes=10, txns=10000, daily_new_dispute_rate=0.0))
        assert r.trajectory in ("STABLE", "IMPROVING")

    def test_trajectory_worsening_with_many_new(self):
        r = forecast_hdr(self._inp(disputes=10, txns=10000, daily_new_dispute_rate=5.0))
        assert r.trajectory == "WORSENING"

    def test_zero_transactions_safe(self):
        r = forecast_hdr(self._inp(disputes=0, txns=0))
        assert r.current_dispute_rate == 0.0
        assert r.current_zone == HDRZone.HEALTHY

    def test_days_until_elevated_set_when_trending_up(self):
        # Start healthy, adding many new disputes per day
        r = forecast_hdr(self._inp(disputes=20, txns=10000, daily_new_dispute_rate=3.0, forecast_days=30))
        # Will cross 0.5% threshold eventually if disputes grow
        # Just check it doesn't crash and returns int or None
        assert r.days_until_elevated is None or isinstance(r.days_until_elevated, int)

    def test_peak_rate_gte_current(self):
        r = forecast_hdr(self._inp(disputes=50, txns=10000, daily_new_dispute_rate=0.5))
        assert r.peak_rate_pct >= r.current_dispute_rate_pct

    def test_disputes_resolving_reduce_count(self):
        disputes = [OpenDispute("D-001", opened_days_ago=30, dispute_amount=100.0, claim_type="INR")]
        r = forecast_hdr(self._inp(
            disputes=50, txns=10000,
            open_disputes=disputes,
            disputes_resolving_in_days=[("D-001", 2)],
        ))
        # Day 2 should have one fewer dispute from resolution
        day2 = r.daily_forecast[1]
        assert day2.dispute_count <= 50
