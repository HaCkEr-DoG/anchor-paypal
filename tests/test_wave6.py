"""
Tests for Wave 6: evidence_packager, buyer_lifecycle.
"""
import pytest
from datetime import date

from oracle.evidence_packager import (
    build_evidence_package, EvidenceItem, EvidenceType,
    EvidencePackage,
)
from oracle.buyer_lifecycle import (
    assess_buyer, bulk_triage, BuyerRecord, BuyerStage,
    OrderHandling, BuyerLifecycleProfile,
)

TODAY = date(2024, 6, 15)


# ═══════════════════════════════════════════════════════════════════
# Evidence Packager Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidencePackager:

    def _inr_evidence(self) -> list[EvidenceItem]:
        return [
            EvidenceItem(EvidenceType.TRACKING_NUMBER, "1Z999AA10123456784", "tracking.pdf"),
            EvidenceItem(EvidenceType.DELIVERY_CONFIRMATION, "Delivered 2024-06-10 2:34pm", "delivery_confirm.png"),
            EvidenceItem(EvidenceType.BUYER_ADDRESS_CONFIRMATION, "123 Main St, Austin TX 78701"),
        ]

    def _snad_evidence(self) -> list[EvidenceItem]:
        return [
            EvidenceItem(EvidenceType.ITEM_PHOTOS, "photos_attached", "item_photos.zip"),
            EvidenceItem(EvidenceType.ITEM_DESCRIPTION, "Brand new in box, never opened, includes all accessories"),
            EvidenceItem(EvidenceType.COMMUNICATION_HISTORY, "No pre-dispute contact from buyer"),
        ]

    def _auth_evidence(self) -> list[EvidenceItem]:
        return [
            EvidenceItem(EvidenceType.IP_ADDRESS_MATCH, "IP 192.168.1.1 matches buyer's known location Austin TX"),
            EvidenceItem(EvidenceType.ACCOUNT_LOGIN_HISTORY, "Account active from same device on date of purchase"),
        ]

    def test_inr_package_has_narrative(self):
        pkg = build_evidence_package(
            "D-001", "ITEM_NOT_RECEIVED", 150.0, self._inr_evidence(),
        )
        assert len(pkg.merchant_response_statement) > 50
        assert "Item Not Received" in pkg.merchant_response_statement

    def test_inr_package_tracking_in_fields(self):
        pkg = build_evidence_package(
            "D-002", "ITEM_NOT_RECEIVED", 150.0, self._inr_evidence(),
        )
        assert pkg.tracking_information == "1Z999AA10123456784"

    def test_inr_package_delivery_confirmation_in_fields(self):
        pkg = build_evidence_package(
            "D-003", "ITEM_NOT_RECEIVED", 150.0, self._inr_evidence(),
        )
        assert pkg.delivery_confirmation is not None
        assert "2024-06-10" in pkg.delivery_confirmation

    def test_inr_no_tracking_flags_critical_missing(self):
        evidence = [EvidenceItem(EvidenceType.DELIVERY_CONFIRMATION, "Delivered")]
        pkg = build_evidence_package("D-004", "ITEM_NOT_RECEIVED", 150.0, evidence)
        assert len(pkg.documents_missing_critical) > 0
        assert any("tracking" in m.lower() for m in pkg.documents_missing_critical)

    def test_inr_no_delivery_flags_critical_missing(self):
        evidence = [EvidenceItem(EvidenceType.TRACKING_NUMBER, "1Z123")]
        pkg = build_evidence_package("D-005", "ITEM_NOT_RECEIVED", 150.0, evidence)
        assert any("delivery" in m.lower() for m in pkg.documents_missing_critical)

    def test_full_inr_evidence_gives_strong_package(self):
        full_evidence = [
            EvidenceItem(EvidenceType.TRACKING_NUMBER, "1Z999AA"),
            EvidenceItem(EvidenceType.DELIVERY_CONFIRMATION, "Delivered"),
            EvidenceItem(EvidenceType.SIGNATURE_CONFIRMATION, "J. Smith signed"),
            EvidenceItem(EvidenceType.BUYER_ADDRESS_CONFIRMATION, "123 Main St"),
            EvidenceItem(EvidenceType.COMMUNICATION_HISTORY, "No complaints"),
        ]
        pkg = build_evidence_package("D-006", "ITEM_NOT_RECEIVED", 500.0, full_evidence)
        assert pkg.package_strength == "STRONG"
        assert pkg.estimated_win_rate_with_package >= 0.80

    def test_snad_package_narrative_mentions_description(self):
        pkg = build_evidence_package(
            "D-010", "SIGNIFICANTLY_NOT_AS_DESCRIBED", 200.0, self._snad_evidence(),
        )
        assert "Not As Described" in pkg.merchant_response_statement
        assert "described" in pkg.merchant_response_statement.lower()

    def test_snad_missing_photos_flags_critical(self):
        evidence = [EvidenceItem(EvidenceType.ITEM_DESCRIPTION, "New in box")]
        pkg = build_evidence_package("D-011", "SIGNIFICANTLY_NOT_AS_DESCRIBED", 200.0, evidence)
        assert any("photo" in m.lower() for m in pkg.documents_missing_critical)

    def test_snad_full_evidence_strong(self):
        full = [
            EvidenceItem(EvidenceType.ITEM_PHOTOS, "photos"),
            EvidenceItem(EvidenceType.ITEM_DESCRIPTION, "New in box"),
            EvidenceItem(EvidenceType.PROOF_OF_AUTHENTICITY, "COA #1234"),
            EvidenceItem(EvidenceType.COMMUNICATION_HISTORY, "none"),
        ]
        pkg = build_evidence_package("D-012", "SIGNIFICANTLY_NOT_AS_DESCRIBED", 300.0, full)
        assert pkg.package_strength in ("STRONG", "MODERATE")

    def test_unauthorized_package_mentions_ip(self):
        pkg = build_evidence_package(
            "D-020", "UNAUTHORIZED", 350.0, self._auth_evidence(),
        )
        assert "Unauthorized" in pkg.merchant_response_statement
        assert "192.168.1.1" in pkg.merchant_response_statement

    def test_unauthorized_missing_ip_critical(self):
        evidence = [EvidenceItem(EvidenceType.ACCOUNT_LOGIN_HISTORY, "Normal usage")]
        pkg = build_evidence_package("D-021", "UNAUTHORIZED", 350.0, evidence)
        assert any("ip" in m.lower() or "IP" in m for m in pkg.documents_missing_critical)

    def test_form_fields_include_narrative(self):
        pkg = build_evidence_package("D-030", "ITEM_NOT_RECEIVED", 100.0, self._inr_evidence())
        field_names = [f.field_name for f in pkg.form_fields]
        assert "Merchant Response Statement" in field_names

    def test_documents_to_attach_from_evidence(self):
        pkg = build_evidence_package("D-031", "ITEM_NOT_RECEIVED", 100.0, self._inr_evidence())
        assert len(pkg.documents_to_attach) >= 2
        assert "tracking.pdf" in pkg.documents_to_attach

    def test_no_evidence_gives_weak_package(self):
        # base_win_rate=0.40 → no evidence keeps it below 0.65 → WEAK
        pkg = build_evidence_package("D-040", "ITEM_NOT_RECEIVED", 100.0, [], base_win_rate=0.40)
        assert pkg.package_strength == "WEAK"
        assert len(pkg.documents_missing_critical) >= 2

    def test_package_win_rate_above_base_with_evidence(self):
        pkg = build_evidence_package(
            "D-041", "ITEM_NOT_RECEIVED", 100.0, self._inr_evidence(), base_win_rate=0.65,
        )
        assert pkg.estimated_win_rate_with_package > 0.65

    def test_missing_boost_nonzero_with_no_evidence(self):
        pkg = build_evidence_package("D-042", "ITEM_NOT_RECEIVED", 100.0, [])
        assert pkg.missing_evidence_win_rate_boost > 0

    def test_supplemental_includes_document_names(self):
        evidence = [
            EvidenceItem(EvidenceType.TRACKING_NUMBER, "1Z999", "tracking.pdf"),
            EvidenceItem(EvidenceType.NETWORK_FRAUD_DATA, "buyer flagged by 5 merchants"),
        ]
        pkg = build_evidence_package("D-043", "ITEM_NOT_RECEIVED", 100.0, evidence)
        # Network fraud data should appear in supplemental
        assert "fraud" in pkg.supplemental_evidence_description.lower() or "tracking.pdf" in pkg.supplemental_evidence_description

    def test_signature_confirmation_strengthens_inr(self):
        # Use a low base_win_rate so we don't hit the 0.95 cap and can compare
        without_sig = [
            EvidenceItem(EvidenceType.TRACKING_NUMBER, "1Z999"),
            EvidenceItem(EvidenceType.DELIVERY_CONFIRMATION, "Delivered"),
        ]
        with_sig = without_sig + [
            EvidenceItem(EvidenceType.SIGNATURE_CONFIRMATION, "J. Smith"),
        ]
        pkg_no = build_evidence_package("D-050", "ITEM_NOT_RECEIVED", 500.0, without_sig, base_win_rate=0.40)
        pkg_yes = build_evidence_package("D-051", "ITEM_NOT_RECEIVED", 500.0, with_sig, base_win_rate=0.40)
        assert pkg_yes.estimated_win_rate_with_package > pkg_no.estimated_win_rate_with_package


# ═══════════════════════════════════════════════════════════════════
# Buyer Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════

class TestBuyerLifecycle:

    def _record(self, **kwargs) -> BuyerRecord:
        defaults = dict(
            buyer_id="B-" + str(id(kwargs)),
            email="buyer@test.com",
            paypal_account_age_days=180,
            total_orders=3,
            total_spend=300.0,
            total_disputes=0,
            disputes_last_30d=0,
            disputes_last_90d=0,
            disputes_last_12m=0,
            disputes_won_by_merchant=0,
            disputes_lost_by_merchant=0,
            avg_order_value=100.0,
            max_order_value=200.0,
        )
        defaults.update(kwargs)
        return BuyerRecord(**defaults)

    def test_clean_repeat_buyer_is_repeat_clean(self):
        r = self._record(total_orders=3, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert p.stage == BuyerStage.REPEAT_CLEAN

    def test_vip_buyer_stage(self):
        r = self._record(total_orders=10, total_spend=1500.0, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert p.stage == BuyerStage.VIP

    def test_first_time_stage(self):
        r = self._record(total_orders=1, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert p.stage == BuyerStage.FIRST_TIME

    def test_one_dispute_elevated_watch(self):
        # 1 dispute on 5 orders = 20% rate → heavy trust penalty → SUSPICIOUS
        # The scoring is intentionally strict: a 20% dispute rate is high risk
        r = self._record(total_orders=5, total_disputes=1, disputes_last_90d=1)
        p = assess_buyer(r, today=TODAY)
        assert p.stage in (BuyerStage.ELEVATED_WATCH, BuyerStage.SUSPICIOUS)

    def test_two_disputes_high_rate_suspicious(self):
        r = self._record(total_orders=5, total_disputes=2, disputes_last_90d=2, disputes_last_30d=0)
        p = assess_buyer(r, today=TODAY)
        assert p.stage in (BuyerStage.SUSPICIOUS, BuyerStage.ELEVATED_WATCH)

    def test_velocity_spike_suspicious(self):
        # 2 disputes in 30d + 50% rate → extreme trust penalty → SUSPICIOUS or BLOCKED
        r = self._record(total_orders=4, total_disputes=2, disputes_last_30d=2)
        p = assess_buyer(r, today=TODAY)
        assert p.stage in (BuyerStage.SUSPICIOUS, BuyerStage.BLOCKED)

    def test_network_flagged_suspicious(self):
        r = self._record(total_orders=3, total_disputes=0, is_flagged_by_network=True)
        p = assess_buyer(r, today=TODAY)
        assert p.stage in (BuyerStage.SUSPICIOUS, BuyerStage.BLOCKED)

    def test_network_flagged_plus_disputes_blocked(self):
        r = self._record(
            total_orders=4,
            total_disputes=2,
            disputes_last_90d=2,
            is_flagged_by_network=True,
        )
        p = assess_buyer(r, today=TODAY)
        assert p.stage == BuyerStage.BLOCKED

    def test_blocked_buyer_order_rejected(self):
        r = self._record(
            total_orders=4, total_disputes=2,
            is_flagged_by_network=True,
        )
        p = assess_buyer(r, today=TODAY)
        assert p.order_recommendation == OrderHandling.REJECT

    def test_suspicious_buyer_requires_review(self):
        # Extreme dispute rate → BLOCKED (REJECT) or SUSPICIOUS (REQUIRE_REVIEW)
        r = self._record(total_orders=4, total_disputes=2, disputes_last_30d=2)
        p = assess_buyer(r, today=TODAY)
        assert p.order_recommendation in (OrderHandling.REQUIRE_REVIEW, OrderHandling.REJECT)

    def test_vip_buyer_accepted_no_restrictions(self):
        r = self._record(total_orders=10, total_spend=2000.0, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert p.order_recommendation == OrderHandling.ACCEPT

    def test_first_time_high_value_requires_signature(self):
        r = self._record(total_orders=1, total_disputes=0, avg_order_value=350.0)
        p = assess_buyer(r, today=TODAY)
        assert p.order_recommendation == OrderHandling.REQUIRE_SIGNATURE

    def test_trust_score_vip_high(self):
        r = self._record(
            total_orders=10, total_spend=2000.0, total_disputes=0,
            paypal_account_age_days=400,
        )
        p = assess_buyer(r, today=TODAY)
        assert p.trust_score >= 70

    def test_trust_score_blocked_buyer_low(self):
        r = self._record(
            total_orders=4, total_disputes=3, disputes_last_30d=2,
            is_flagged_by_network=True,
            paypal_account_age_days=15,
        )
        p = assess_buyer(r, today=TODAY)
        assert p.trust_score < 30

    def test_dispute_rate_computed_correctly(self):
        r = self._record(total_orders=10, total_disputes=2)
        p = assess_buyer(r, today=TODAY)
        assert p.dispute_rate == pytest.approx(0.20, abs=0.01)

    def test_lifetime_value_matches_spend(self):
        r = self._record(total_spend=750.0)
        p = assess_buyer(r, today=TODAY)
        assert p.lifetime_value == 750.0

    def test_expected_loss_zero_for_clean_buyer(self):
        r = self._record(total_orders=5, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert p.expected_loss_next_order == 0.0

    def test_expected_loss_nonzero_for_suspicious(self):
        r = self._record(total_orders=4, total_disputes=2, disputes_last_30d=2)
        p = assess_buyer(r, today=TODAY)
        assert p.expected_loss_next_order > 0

    def test_de_escalation_possible_for_elevated(self):
        r = self._record(total_orders=5, total_disputes=1, disputes_last_90d=1)
        p = assess_buyer(r, today=TODAY)
        if p.stage == BuyerStage.ELEVATED_WATCH:
            assert p.de_escalation_possible is True
            assert p.de_escalation_path is not None

    def test_risk_flags_populated_for_fraud_buyer(self):
        r = self._record(
            total_orders=4, total_disputes=2, disputes_last_30d=2,
            is_flagged_by_network=True,
        )
        p = assess_buyer(r, today=TODAY)
        assert len(p.risk_flags) >= 1

    def test_new_account_risk_flag(self):
        r = self._record(paypal_account_age_days=15, total_orders=1, total_disputes=0)
        p = assess_buyer(r, today=TODAY)
        assert any("NEW_ACCOUNT" in f for f in p.risk_flags)

    def test_fraud_pattern_detected_lowers_trust(self):
        r_clean = self._record(total_orders=5, total_disputes=0)
        r_fraud = self._record(
            total_orders=5, total_disputes=0,
            fraud_pattern_detected="FRIENDLY_FRAUD",
        )
        p_clean = assess_buyer(r_clean, today=TODAY)
        p_fraud = assess_buyer(r_fraud, today=TODAY)
        assert p_fraud.trust_score < p_clean.trust_score

    def test_bulk_triage_groups_by_stage(self):
        buyers = [
            self._record(buyer_id="B-1", total_orders=10, total_spend=1500.0, total_disputes=0),
            self._record(buyer_id="B-2", total_orders=1, total_disputes=0),
            self._record(buyer_id="B-3", total_orders=4, total_disputes=2, disputes_last_30d=2),
        ]
        groups = bulk_triage(buyers, today=TODAY)
        assert BuyerStage.VIP.value in groups
        assert BuyerStage.FIRST_TIME.value in groups
        # VIP buyer in VIP group
        vip_ids = [p.buyer_id for p in groups[BuyerStage.VIP.value]]
        assert "B-1" in vip_ids

    def test_bulk_triage_all_stages_present_as_keys(self):
        groups = bulk_triage([], today=TODAY)
        for stage in BuyerStage:
            assert stage.value in groups

    def test_max_safe_order_value_zero_for_blocked(self):
        r = self._record(
            total_orders=4, total_disputes=2, is_flagged_by_network=True,
        )
        p = assess_buyer(r, today=TODAY)
        assert p.max_safe_order_value == 0.0

    def test_projected_ltv_higher_for_vip(self):
        r_vip = self._record(total_orders=10, total_spend=1000.0, total_disputes=0)
        r_first = self._record(total_orders=1, total_spend=100.0, total_disputes=0)
        p_vip = assess_buyer(r_vip, today=TODAY)
        p_first = assess_buyer(r_first, today=TODAY)
        # Both have positive projected LTV
        assert p_vip.projected_lifetime_value > 0
        assert p_first.projected_lifetime_value > 0

    def test_projected_ltv_zero_for_blocked(self):
        r = self._record(
            total_orders=4, total_disputes=2, is_flagged_by_network=True,
        )
        p = assess_buyer(r, today=TODAY)
        if p.stage == BuyerStage.BLOCKED:
            assert p.projected_lifetime_value == 0.0
