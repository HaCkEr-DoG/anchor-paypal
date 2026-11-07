"""
Wave 65 — Dispute Response Writer
Tests: 32 total
"""

import pytest
from oracle.dispute_response_writer import (
    ResponseContext,
    EvidenceItem,
    ResponseTone,
    DisputeResponseDraft,
    write_dispute_response,
)


def _ctx(**kwargs):
    defaults = dict(
        dispute_id="DISP_001",
        merchant_id="MERCH_X",
        merchant_name="Test Shop",
        reason_code="INR",
        order_id="ORD_123",
        order_date="2026-11-01",
        order_amount=150.0,
        currency="USD",
        item_description="Blue Running Shoes Size 10",
        buyer_name="John Doe",
        ship_date="2026-11-02",
        carrier="USPS",
        tracking_number="9400111899223369159720",
        delivery_date="2026-11-07",
        delivery_status="DELIVERED",
        win_probability=0.75,
        evidence_items=[
            EvidenceItem.TRACKING_NUMBER,
            EvidenceItem.PROOF_OF_DELIVERY,
            EvidenceItem.INVOICE,
        ],
    )
    defaults.update(kwargs)
    return ResponseContext(**defaults)


class TestDisputeResponseWriter:

    # ─── Basic structure ──────────────────────────────────────────────────────

    def test_response_has_five_sections(self):
        r = write_dispute_response(_ctx())
        assert len(r.sections) == 5

    def test_section_names_correct(self):
        r = write_dispute_response(_ctx())
        names = [s.section for s in r.sections]
        assert names == ["HEADER", "FACTS", "EVIDENCE", "REBUTTAL", "REQUEST"]

    def test_full_response_contains_all_sections(self):
        r = write_dispute_response(_ctx())
        for section in ["HEADER", "FACTS", "EVIDENCE", "REBUTTAL", "REQUEST"]:
            assert section in r.full_response

    def test_full_response_not_empty(self):
        r = write_dispute_response(_ctx())
        assert r.word_count > 50

    def test_dispute_id_returned(self):
        r = write_dispute_response(_ctx(dispute_id="TEST_DISP_XYZ"))
        assert r.dispute_id == "TEST_DISP_XYZ"

    def test_merchant_id_returned(self):
        r = write_dispute_response(_ctx(merchant_id="SHOP_ABC"))
        assert r.merchant_id == "SHOP_ABC"

    # ─── Tone selection ───────────────────────────────────────────────────────

    def test_assertive_tone_on_high_win_prob(self):
        r = write_dispute_response(_ctx(win_probability=0.80))
        assert r.tone == ResponseTone.ASSERTIVE

    def test_cooperative_tone_on_medium_win_prob(self):
        r = write_dispute_response(_ctx(win_probability=0.55))
        assert r.tone == ResponseTone.COOPERATIVE

    def test_conciliatory_tone_on_low_win_prob(self):
        r = write_dispute_response(_ctx(win_probability=0.25))
        assert r.tone == ResponseTone.CONCILIATORY

    # ─── INR response ─────────────────────────────────────────────────────────

    def test_inr_tracking_number_in_facts(self):
        r = write_dispute_response(_ctx(reason_code="INR"))
        facts = next(s.content for s in r.sections if s.section == "FACTS")
        assert "9400111899223369159720" in facts

    def test_inr_delivery_date_in_facts(self):
        r = write_dispute_response(_ctx(reason_code="INR"))
        facts = next(s.content for s in r.sections if s.section == "FACTS")
        assert "2026-11-07" in facts

    def test_inr_no_tracking_header_mentions_investigating(self):
        r = write_dispute_response(_ctx(
            reason_code="INR", tracking_number="",
            evidence_items=[EvidenceItem.INVOICE]
        ))
        header = next(s.content for s in r.sections if s.section == "HEADER")
        assert "investigat" in header.lower() or "carrier" in header.lower()

    def test_inr_proof_of_delivery_assertive_request(self):
        r = write_dispute_response(_ctx(
            reason_code="INR", win_probability=0.85,
            evidence_items=[EvidenceItem.TRACKING_NUMBER, EvidenceItem.PROOF_OF_DELIVERY]
        ))
        request = next(s.content for s in r.sections if s.section == "REQUEST")
        assert "favor" in request.lower() or "close" in request.lower()

    def test_inr_exception_mentions_carrier_claim(self):
        r = write_dispute_response(_ctx(
            reason_code="INR", delivery_status="EXCEPTION",
            evidence_items=[EvidenceItem.TRACKING_NUMBER]
        ))
        full = r.full_response.lower()
        assert "exception" in full or "carrier" in full

    # ─── SNAD response ─────────────────────────────────────────────────────────

    def test_snad_response_has_five_sections(self):
        r = write_dispute_response(_ctx(
            reason_code="SNAD",
            evidence_items=[EvidenceItem.PRODUCT_PHOTO, EvidenceItem.INVOICE, EvidenceItem.POLICY_DOCUMENT]
        ))
        assert len(r.sections) == 5

    def test_snad_product_photo_mentioned_in_rebuttal(self):
        r = write_dispute_response(_ctx(
            reason_code="SNAD",
            evidence_items=[EvidenceItem.PRODUCT_PHOTO, EvidenceItem.INVOICE]
        ))
        rebuttal = next(s.content for s in r.sections if s.section == "REBUTTAL")
        assert "photo" in rebuttal.lower()

    def test_snad_policy_mentioned_when_available(self):
        r = write_dispute_response(_ctx(
            reason_code="SNAD",
            evidence_items=[EvidenceItem.PRODUCT_PHOTO, EvidenceItem.INVOICE, EvidenceItem.POLICY_DOCUMENT]
        ))
        rebuttal = next(s.content for s in r.sections if s.section == "REBUTTAL")
        assert "return policy" in rebuttal.lower() or "return" in rebuttal.lower()

    def test_snad_cooperative_tone_mentions_return(self):
        r = write_dispute_response(_ctx(
            reason_code="SNAD", win_probability=0.50,
            evidence_items=[EvidenceItem.INVOICE]
        ))
        request = next(s.content for s in r.sections if s.section == "REQUEST")
        assert "return" in request.lower() or "refund" in request.lower()

    # ─── UNAUTHORIZED response ────────────────────────────────────────────────

    def test_unauthorized_response_has_five_sections(self):
        r = write_dispute_response(_ctx(
            reason_code="UNAUTHORIZED",
            evidence_items=[EvidenceItem.TRANSACTION_RECORD, EvidenceItem.IP_ADDRESS_LOG]
        ))
        assert len(r.sections) == 5

    def test_unauthorized_ip_log_mentioned_in_rebuttal(self):
        r = write_dispute_response(_ctx(
            reason_code="UNAUTHORIZED",
            evidence_items=[EvidenceItem.IP_ADDRESS_LOG, EvidenceItem.TRANSACTION_RECORD]
        ))
        rebuttal = next(s.content for s in r.sections if s.section == "REBUTTAL")
        assert "ip" in rebuttal.lower() or "address" in rebuttal.lower()

    def test_unauthorized_transaction_record_mentioned(self):
        r = write_dispute_response(_ctx(
            reason_code="UNAUTHORIZED",
            evidence_items=[EvidenceItem.TRANSACTION_RECORD]
        ))
        rebuttal = next(s.content for s in r.sections if s.section == "REBUTTAL")
        assert "transaction" in rebuttal.lower() or "authoriz" in rebuttal.lower()

    # ─── SUBSCRIPTION response ────────────────────────────────────────────────

    def test_subscription_response_has_five_sections(self):
        r = write_dispute_response(_ctx(
            reason_code="SUBSCRIPTION",
            evidence_items=[EvidenceItem.SUBSCRIPTION_AGREEMENT],
            subscription_start="2026-01-01",
            last_billing_date="2026-11-01",
        ))
        assert len(r.sections) == 5

    def test_subscription_agreement_mentioned(self):
        r = write_dispute_response(_ctx(
            reason_code="SUBSCRIPTION",
            evidence_items=[EvidenceItem.SUBSCRIPTION_AGREEMENT]
        ))
        rebuttal = next(s.content for s in r.sections if s.section == "REBUTTAL")
        assert "agreement" in rebuttal.lower() or "subscri" in rebuttal.lower()

    def test_subscription_cancellation_acknowledged_when_requested(self):
        r = write_dispute_response(_ctx(
            reason_code="SUBSCRIPTION",
            evidence_items=[EvidenceItem.SUBSCRIPTION_AGREEMENT, EvidenceItem.CANCELLATION_RECORD],
            cancellation_requested=True,
            cancellation_date="2026-11-05",
        ))
        request = next(s.content for s in r.sections if s.section == "REQUEST")
        assert "cancell" in request.lower() or "refund" in request.lower()

    # ─── Submission readiness ────────────────────────────────────────────────

    def test_ready_when_essential_evidence_present(self):
        r = write_dispute_response(_ctx(
            reason_code="INR",
            evidence_items=[EvidenceItem.TRACKING_NUMBER, EvidenceItem.PROOF_OF_DELIVERY]
        ))
        assert r.is_submission_ready is True

    def test_not_ready_when_missing_essential_evidence(self):
        r = write_dispute_response(_ctx(
            reason_code="INR",
            evidence_items=[EvidenceItem.INVOICE]  # missing tracking + POD
        ))
        assert r.is_submission_ready is False

    def test_submission_note_mentions_missing_item(self):
        r = write_dispute_response(_ctx(
            reason_code="SNAD",
            evidence_items=[EvidenceItem.POLICY_DOCUMENT]  # missing invoice + photo
        ))
        assert len(r.submission_note) > 10

    # ─── Evidence list ────────────────────────────────────────────────────────

    def test_evidence_count_matches_items(self):
        evidence = [EvidenceItem.TRACKING_NUMBER, EvidenceItem.PROOF_OF_DELIVERY, EvidenceItem.INVOICE]
        r = write_dispute_response(_ctx(evidence_items=evidence))
        assert r.evidence_count == 3

    def test_evidence_items_in_evidence_section(self):
        r = write_dispute_response(_ctx(
            evidence_items=[EvidenceItem.TRACKING_NUMBER, EvidenceItem.PROOF_OF_DELIVERY]
        ))
        ev_section = next(s.content for s in r.sections if s.section == "EVIDENCE")
        assert "Tracking Number" in ev_section or "Proof Of Delivery" in ev_section

    # ─── Summary / metadata ───────────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = write_dispute_response(_ctx(dispute_id="DISP_SUMMARY_TEST"))
        assert "DISP_SUMMARY_TEST" in r.summary

    def test_summary_contains_reason_code(self):
        r = write_dispute_response(_ctx(reason_code="SNAD"))
        assert "SNAD" in r.summary
