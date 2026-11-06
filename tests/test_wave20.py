"""
Tests for Wave 20: inquiry_triage_engine, return_merchandise_authorizer.
"""
import pytest
from oracle.inquiry_triage_engine import (
    triage_inquiry, InquiryInput, InquiryCategory, InquiryUrgency,
    ResponseStrategy, EscalationRisk,
)
from oracle.return_merchandise_authorizer import (
    authorize_return, ReturnInput, ReturnDecision, ReturnReason,
    FraudRisk,
)


# ═══════════════════════════════════════════════════════════════════
# Inquiry Triage Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestInquiryTriageEngine:

    def _inp(self, keywords=None, **kw) -> InquiryInput:
        defaults = dict(
            inquiry_id="IQ-001",
            order_id="ORD-001",
            message_keywords=keywords or ["where", "package"],
            order_amount=150.0,
            order_age_days=5,
            is_shipped=True,
            has_tracking=True,
        )
        defaults.update(kw)
        return InquiryInput(**defaults)

    # ── Category classification ──────────────────────────────────

    def test_delivery_keywords_classify_delivery(self):
        r = triage_inquiry(self._inp(keywords=["where", "tracking", "package"]))
        assert r.category == InquiryCategory.DELIVERY_INQUIRY

    def test_refund_keywords_classify_refund(self):
        r = triage_inquiry(self._inp(keywords=["refund", "money back"]))
        assert r.category == InquiryCategory.REFUND_REQUEST

    def test_cancel_keywords_classify_cancellation(self):
        r = triage_inquiry(self._inp(keywords=["cancel", "cancellation"]))
        assert r.category == InquiryCategory.CANCELLATION_REQUEST

    def test_item_concern_keywords(self):
        r = triage_inquiry(self._inp(keywords=["broken", "damaged", "not as described"]))
        assert r.category == InquiryCategory.ITEM_CONCERN

    def test_fraud_signal_keywords_take_priority(self):
        # Fraud keywords override delivery keywords
        r = triage_inquiry(self._inp(keywords=["tracking", "hacked", "not me"]))
        assert r.category == InquiryCategory.FRAUD_SIGNAL

    def test_payment_issue_keywords(self):
        r = triage_inquiry(self._inp(keywords=["charged twice", "overcharged"]))
        assert r.category == InquiryCategory.PAYMENT_ISSUE

    def test_unknown_keywords_general_inquiry(self):
        r = triage_inquiry(self._inp(keywords=["hello", "question"]))
        assert r.category == InquiryCategory.GENERAL_INQUIRY

    # ── Escalation probability ──────────────────────────────────

    def test_p_escalation_range(self):
        r = triage_inquiry(self._inp())
        assert 0.0 <= r.p_escalation <= 1.0

    def test_negative_tone_raises_p_escalation(self):
        base = triage_inquiry(self._inp(buyer_tone_negative=False))
        neg  = triage_inquiry(self._inp(buyer_tone_negative=True))
        assert neg.p_escalation > base.p_escalation

    def test_serial_filer_raises_p_escalation(self):
        base = triage_inquiry(self._inp(buyer_dispute_history=0))
        high = triage_inquiry(self._inp(buyer_dispute_history=5))
        assert high.p_escalation > base.p_escalation

    def test_fraud_signal_has_high_base_escalation(self):
        r = triage_inquiry(self._inp(keywords=["hacked", "not me", "fraud"]))
        assert r.p_escalation >= 0.60

    def test_delivered_tracking_lowers_delivery_escalation(self):
        base = triage_inquiry(self._inp(tracking_shows_delivered=False))
        deliv = triage_inquiry(self._inp(tracking_shows_delivered=True))
        assert deliv.p_escalation <= base.p_escalation

    # ── Urgency ──────────────────────────────────────────────────

    def test_high_value_high_risk_p1(self):
        r = triage_inquiry(self._inp(
            keywords=["refund", "hacked"], order_amount=600.0,
            buyer_tone_negative=True,
        ))
        assert r.urgency == InquiryUrgency.P1

    def test_low_value_low_risk_p3_or_p4(self):
        r = triage_inquiry(self._inp(
            keywords=["hello", "question"], order_amount=20.0,
        ))
        assert r.urgency in (InquiryUrgency.P3, InquiryUrgency.P4)

    def test_respond_within_hours_p1_is_2(self):
        r = triage_inquiry(self._inp(
            keywords=["fraud", "hacked"], order_amount=600.0,
            buyer_tone_negative=True,
        ))
        if r.urgency == InquiryUrgency.P1:
            assert r.respond_within_hours == 2

    # ── Strategy ─────────────────────────────────────────────────

    def test_has_tracking_uses_provide_tracking(self):
        r = triage_inquiry(self._inp(
            keywords=["where", "tracking"], has_tracking=True,
        ))
        assert r.strategy == ResponseStrategy.PROVIDE_TRACKING

    def test_no_tracking_delivery_uses_investigate_first(self):
        r = triage_inquiry(self._inp(
            keywords=["where", "package"], has_tracking=False,
        ))
        assert r.strategy == ResponseStrategy.INVESTIGATE_FIRST

    def test_cancellation_not_shipped_accept(self):
        r = triage_inquiry(self._inp(
            keywords=["cancel"], is_shipped=False,
        ))
        assert r.strategy == ResponseStrategy.ACCEPT_CANCELLATION

    def test_cancellation_already_shipped_deny(self):
        r = triage_inquiry(self._inp(
            keywords=["cancel"], is_shipped=True,
        ))
        assert r.strategy == ResponseStrategy.DENY_CANCELLATION

    def test_fraud_signal_escalates_internally(self):
        r = triage_inquiry(self._inp(keywords=["hacked", "unauthorized", "not me"]))
        assert r.strategy == ResponseStrategy.ESCALATE_INTERNALLY

    def test_small_refund_request_offers_refund(self):
        r = triage_inquiry(self._inp(keywords=["refund"], order_amount=40.0))
        assert r.strategy == ResponseStrategy.OFFER_REFUND

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_inquiry_id(self):
        r = triage_inquiry(self._inp(inquiry_id="IQ-XYZ"))
        assert "IQ-XYZ" in r.summary

    def test_response_template_hint_non_empty(self):
        r = triage_inquiry(self._inp())
        assert len(r.response_template_hint) > 10

    def test_hdr_zone_adds_proactive_action(self):
        r = triage_inquiry(self._inp(is_in_hdr_zone=True))
        assert any("hdr" in a.lower() for a in r.proactive_actions)

    def test_risk_factors_non_empty_for_negative_tone(self):
        r = triage_inquiry(self._inp(buyer_tone_negative=True))
        assert len(r.risk_factors) > 0

    def test_escalation_risk_enum_matches_p(self):
        r = triage_inquiry(self._inp(keywords=["hacked", "unauthorized"], buyer_tone_negative=True))
        if r.p_escalation >= 0.60:
            assert r.escalation_risk == EscalationRisk.HIGH
        elif r.p_escalation >= 0.30:
            assert r.escalation_risk == EscalationRisk.MEDIUM
        else:
            assert r.escalation_risk == EscalationRisk.LOW


# ═══════════════════════════════════════════════════════════════════
# Return Merchandise Authorizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestReturnMerchandiseAuthorizer:

    def _inp(self, **kw) -> ReturnInput:
        defaults = dict(
            dispute_id="D-001",
            order_id="ORD-001",
            claim_type="SNAD",
            order_amount=100.0,
            item_description="Blue T-shirt, size L",
            cogs_pct=0.40,
            return_reason=ReturnReason.NOT_AS_DESCRIBED,
            buyer_has_sent_photos=True,
            buyer_dispute_history=0,
            buyer_account_age_days=365,
            item_can_be_resold=True,
            p_win_if_fight=0.38,
            fight_cost=25.0,
        )
        defaults.update(kw)
        return ReturnInput(**defaults)

    # ── Decision logic ────────────────────────────────────────────

    def test_low_value_authorizes_full_return(self):
        r = authorize_return(self._inp(order_amount=30.0))
        assert r.decision in (ReturnDecision.AUTHORIZE_FULL, ReturnDecision.AUTHORIZE_PARTIAL)

    def test_digital_item_denied(self):
        r = authorize_return(self._inp(item_is_digital=True))
        assert r.decision == ReturnDecision.DENY_RETURN

    def test_custom_made_denied(self):
        r = authorize_return(self._inp(item_is_custom_made=True))
        assert r.decision == ReturnDecision.DENY_RETURN

    def test_no_photos_high_value_requests_info(self):
        r = authorize_return(self._inp(buyer_has_sent_photos=False, order_amount=100.0))
        assert r.decision == ReturnDecision.REQUEST_MORE_INFO

    def test_missing_parts_gives_partial_refund(self):
        r = authorize_return(self._inp(
            return_reason=ReturnReason.MISSING_PARTS,
            buyer_has_sent_photos=True,
        ))
        assert r.decision == ReturnDecision.AUTHORIZE_PARTIAL
        assert r.partial_refund_pct is not None

    def test_damaged_in_transit_gives_partial(self):
        r = authorize_return(self._inp(
            return_reason=ReturnReason.DAMAGED_IN_TRANSIT,
            buyer_has_sent_photos=True,
        ))
        assert r.decision == ReturnDecision.AUTHORIZE_PARTIAL

    def test_high_fraud_risk_conditional_auth(self):
        r = authorize_return(self._inp(
            buyer_dispute_history=5,
            is_first_order_from_buyer=True,
            buyer_account_age_days=15,
        ))
        assert r.decision == ReturnDecision.AUTHORIZE_CONDITIONAL
        assert r.fraud_risk == FraudRisk.HIGH

    # ── Cost analysis ─────────────────────────────────────────────

    def test_cost_accept_includes_order_amount(self):
        r = authorize_return(self._inp(order_amount=100.0, item_can_be_resold=False))
        assert r.cost_accept_return >= 100.0

    def test_resale_reduces_accept_cost(self):
        no_resale = authorize_return(self._inp(item_can_be_resold=False))
        with_resale = authorize_return(self._inp(item_can_be_resold=True, item_resale_pct=0.70))
        assert with_resale.cost_accept_return < no_resale.cost_accept_return

    def test_ev_fight_negative_low_p_win(self):
        r = authorize_return(self._inp(p_win_if_fight=0.10, order_amount=50.0, fight_cost=25.0))
        assert r.ev_fight < 0

    def test_ev_fight_positive_high_p_win(self):
        r = authorize_return(self._inp(p_win_if_fight=0.90, order_amount=500.0, fight_cost=25.0))
        assert r.ev_fight > 0

    # ── Fraud signals ─────────────────────────────────────────────

    def test_new_account_first_order_adds_fraud_flag(self):
        r = authorize_return(self._inp(
            buyer_account_age_days=15,
            is_first_order_from_buyer=True,
            order_amount=150.0,
        ))
        assert len(r.fraud_flags) > 0

    def test_serial_filer_adds_fraud_flag(self):
        r = authorize_return(self._inp(buyer_dispute_history=4))
        assert any("serial" in f.lower() or "disputes" in f.lower() for f in r.fraud_flags)

    def test_low_fraud_risk_no_flags(self):
        r = authorize_return(self._inp())
        assert r.fraud_risk == FraudRisk.LOW

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = authorize_return(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_response_to_buyer_non_empty(self):
        r = authorize_return(self._inp())
        assert len(r.response_to_buyer) > 10

    def test_action_items_deny_includes_dispute_evidence(self):
        r = authorize_return(self._inp(item_is_digital=True))
        assert any("dispute" in a.lower() or "evidence" in a.lower() for a in r.action_items)

    def test_merchant_pays_shipping_for_wrong_item(self):
        r = authorize_return(self._inp(
            return_reason=ReturnReason.WRONG_ITEM,
            order_amount=100.0,
        ))
        # Wrong item → merchant's fault → merchant should pay
        if r.decision == ReturnDecision.AUTHORIZE_FULL:
            assert r.merchant_pays_shipping is True

    def test_inspection_required_for_high_fraud_risk(self):
        r = authorize_return(self._inp(
            buyer_dispute_history=5,
            is_first_order_from_buyer=True,
            buyer_account_age_days=10,
            order_amount=200.0,
        ))
        if r.decision in (ReturnDecision.AUTHORIZE_FULL, ReturnDecision.AUTHORIZE_CONDITIONAL):
            assert r.inspection_required is True
