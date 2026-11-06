"""
Tests for Wave 47: shipment_evidence_validator, policy_defensibility_scorer.
"""
import pytest
from oracle.shipment_evidence_validator import (
    validate_shipment_evidence, ShipmentEvidenceInput,
    Carrier, CheckStatus, EvidenceReadiness,
)
from oracle.policy_defensibility_scorer import (
    score_policy_defensibility, PolicyEvidenceInput,
    PolicyType, DisclosureLevel, PolicyDefensibility,
)


# ═══════════════════════════════════════════════════════════════════
# Shipment Evidence Validator Tests
# ═══════════════════════════════════════════════════════════════════

BASE_TS = 1700000000
DAY = 86400


class TestShipmentEvidenceValidator:

    def _inp(self, amount=100.0, **kw) -> ShipmentEvidenceInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=amount,
        )
        defaults.update(kw)
        return ShipmentEvidenceInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = validate_shipment_evidence(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_score_bounded(self):
        r = validate_shipment_evidence(self._inp())
        assert 0 <= r.readiness_score <= 100

    def test_checks_non_empty(self):
        r = validate_shipment_evidence(self._inp())
        assert len(r.checks) >= 4

    # ── Tracking number ───────────────────────────────────────────

    def test_missing_tracking_is_critical_failure(self):
        r = validate_shipment_evidence(self._inp(tracking_number=""))
        critical = [c for c in r.checks
                    if c.name == "Tracking number present" and c.status == CheckStatus.FAIL]
        assert len(critical) == 1
        assert len(r.critical_failures) >= 1

    def test_tracking_present_passes(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="1Z999AA10123456784",
            carrier=Carrier.UPS,
        ))
        present = next(c for c in r.checks if c.name == "Tracking number present")
        assert present.status == CheckStatus.PASS

    def test_valid_ups_tracking_passes_format(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="1Z999AA10123456784",
            carrier=Carrier.UPS,
        ))
        fmt = next(c for c in r.checks if "format" in c.name.lower())
        assert fmt.status == CheckStatus.PASS

    def test_invalid_tracking_format_warns(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="BADFORMAT",
            carrier=Carrier.UPS,
        ))
        fmt = next(c for c in r.checks if "format" in c.name.lower())
        assert fmt.status in (CheckStatus.WARNING, CheckStatus.FAIL)

    def test_valid_usps_tracking_passes(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="9400100000010000000026",  # 22-digit USPS IMpb
            carrier=Carrier.USPS,
        ))
        fmt = next(c for c in r.checks if "format" in c.name.lower())
        assert fmt.status == CheckStatus.PASS

    # ── Ship date vs. transaction date ────────────────────────────

    def test_ship_after_transaction_passes(self):
        r = validate_shipment_evidence(self._inp(
            transaction_date_epoch=BASE_TS,
            ship_date_epoch=BASE_TS + DAY,
        ))
        date_check = next(c for c in r.checks if "Ship date after" in c.name)
        assert date_check.status == CheckStatus.PASS

    def test_ship_before_transaction_fails(self):
        r = validate_shipment_evidence(self._inp(
            transaction_date_epoch=BASE_TS,
            ship_date_epoch=BASE_TS - DAY,
        ))
        date_check = next(c for c in r.checks if "Ship date after" in c.name)
        assert date_check.status == CheckStatus.FAIL
        assert len(r.critical_failures) >= 1

    def test_missing_dates_skips_check(self):
        r = validate_shipment_evidence(self._inp(
            ship_date_epoch=None,
            transaction_date_epoch=None,
        ))
        date_check = next(c for c in r.checks if "Ship date after" in c.name)
        assert date_check.status == CheckStatus.SKIPPED

    # ── Delivery confirmation ─────────────────────────────────────

    def test_delivery_confirmation_passes(self):
        r = validate_shipment_evidence(self._inp(has_delivery_confirmation=True))
        dc = next(c for c in r.checks if "Delivery confirmation" in c.name)
        assert dc.status == CheckStatus.PASS

    def test_missing_delivery_confirmation_warns(self):
        r = validate_shipment_evidence(self._inp(has_delivery_confirmation=False))
        dc = next(c for c in r.checks if "Delivery confirmation" in c.name)
        assert dc.status == CheckStatus.WARNING

    # ── Signature for high-value ──────────────────────────────────

    def test_signature_required_above_750(self):
        r = validate_shipment_evidence(self._inp(
            amount=800.0,
            has_signature_confirmation=False,
        ))
        sig = next(c for c in r.checks if "Signature" in c.name)
        assert sig.status == CheckStatus.FAIL
        assert len(r.critical_failures) >= 1

    def test_signature_present_passes_above_750(self):
        r = validate_shipment_evidence(self._inp(
            amount=800.0,
            has_signature_confirmation=True,
        ))
        sig = next(c for c in r.checks if "Signature" in c.name)
        assert sig.status == CheckStatus.PASS

    def test_signature_skipped_below_750(self):
        r = validate_shipment_evidence(self._inp(
            amount=100.0,
            has_signature_confirmation=False,
        ))
        sig = next(c for c in r.checks if "Signature" in c.name)
        assert sig.status == CheckStatus.SKIPPED

    # ── Address match ─────────────────────────────────────────────

    def test_address_match_flag_passes(self):
        r = validate_shipment_evidence(self._inp(buyer_provided_address_match=True))
        addr = next(c for c in r.checks if "PayPal" in c.name)
        assert addr.status == CheckStatus.PASS

    def test_address_mismatch_fails(self):
        r = validate_shipment_evidence(self._inp(
            buyer_provided_address_match=False,
            shipped_to_address="123 Main St Anytown CA",
            paypal_confirmed_address="456 Oak Ave Different City TX",
        ))
        addr = next(c for c in r.checks if "PayPal" in c.name)
        assert addr.status in (CheckStatus.FAIL, CheckStatus.WARNING)

    # ── Readiness scoring ─────────────────────────────────────────

    def test_full_evidence_scores_high(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="1Z999AA10123456784",
            carrier=Carrier.UPS,
            transaction_date_epoch=BASE_TS,
            ship_date_epoch=BASE_TS + DAY,
            has_delivery_confirmation=True,
            has_shipment_label=True,
            buyer_provided_address_match=True,
        ))
        assert r.readiness in (EvidenceReadiness.STRONG, EvidenceReadiness.ADEQUATE)
        assert r.readiness_score >= 60

    def test_no_evidence_scores_low(self):
        r = validate_shipment_evidence(self._inp())
        assert r.readiness in (EvidenceReadiness.INSUFFICIENT, EvidenceReadiness.WEAK)

    def test_can_file_true_when_no_critical_failures(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="1Z999AA10123456784",
            carrier=Carrier.UPS,
            buyer_provided_address_match=True,
        ))
        assert r.can_file_with_current_evidence is True

    def test_cannot_file_on_critical_failure(self):
        r = validate_shipment_evidence(self._inp(
            tracking_number="",   # Missing tracking = critical
        ))
        assert r.can_file_with_current_evidence is False

    # ── Missing to strengthen ─────────────────────────────────────

    def test_missing_items_suggested_on_weak_evidence(self):
        r = validate_shipment_evidence(self._inp())
        assert len(r.missing_to_strengthen) >= 1


# ═══════════════════════════════════════════════════════════════════
# Policy Defensibility Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestPolicyDefensibilityScorer:

    def _inp(self, policy_type=PolicyType.RETURN_REFUND, **kw) -> PolicyEvidenceInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code="SNAD",
            policy_type=policy_type,
        )
        defaults.update(kw)
        return PolicyEvidenceInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = score_policy_defensibility(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_score_bounded(self):
        r = score_policy_defensibility(self._inp())
        assert 0 <= r.total_score <= 100

    def test_dimensions_non_empty(self):
        r = score_policy_defensibility(self._inp())
        assert len(r.dimensions) >= 4

    # ── Disclosure quality ────────────────────────────────────────

    def test_prominent_disclosure_scores_high(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.PROMINENT,
            buyer_checked_policy_box=True,
        ))
        disc = next(d for d in r.dimensions if "Disclosure" in d.dimension)
        assert disc.score >= 25

    def test_missing_disclosure_scores_zero(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.MISSING,
        ))
        disc = next(d for d in r.dimensions if "Disclosure" in d.dimension)
        assert disc.score == 0

    def test_buyer_acknowledgment_boosts_disclosure(self):
        no_ack = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.LINKED,
            buyer_checked_policy_box=False,
        ))
        with_ack = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.LINKED,
            buyer_checked_policy_box=True,
        ))
        no_ack_score = next(d.score for d in no_ack.dimensions if "Disclosure" in d.dimension)
        with_ack_score = next(d.score for d in with_ack.dimensions if "Disclosure" in d.dimension)
        assert with_ack_score > no_ack_score

    # ── Policy clarity ────────────────────────────────────────────

    def test_clear_timeframe_boosts_clarity(self):
        without = score_policy_defensibility(self._inp(has_clear_timeframe=False))
        with_tf  = score_policy_defensibility(self._inp(has_clear_timeframe=True))
        without_score = next(d.score for d in without.dimensions if "clarity" in d.dimension.lower())
        with_score    = next(d.score for d in with_tf.dimensions if "clarity" in d.dimension.lower())
        assert with_score > without_score

    def test_specific_conditions_boosts_clarity(self):
        without = score_policy_defensibility(self._inp(has_specific_conditions=False))
        with_sc  = score_policy_defensibility(self._inp(has_specific_conditions=True))
        without_s = next(d.score for d in without.dimensions if "clarity" in d.dimension.lower())
        with_s    = next(d.score for d in with_sc.dimensions if "clarity" in d.dimension.lower())
        assert with_s > without_s

    # ── Subscription specifics ────────────────────────────────────

    def test_subscription_policy_scored(self):
        r = score_policy_defensibility(self._inp(
            policy_type=PolicyType.SUBSCRIPTION,
            reason_code="SUBSCRIPTION",
            has_cancellation_instructions=True,
            has_renewal_reminder=True,
            cancellation_days_notice=7,
        ))
        sub_dim = next(d for d in r.dimensions if "Subscription" in d.dimension)
        assert sub_dim.score >= 15

    def test_subscription_missing_cancel_instructions_penalized(self):
        with_inst = score_policy_defensibility(self._inp(
            policy_type=PolicyType.SUBSCRIPTION,
            has_cancellation_instructions=True,
        ))
        without_inst = score_policy_defensibility(self._inp(
            policy_type=PolicyType.SUBSCRIPTION,
            has_cancellation_instructions=False,
        ))
        with_s = next(d.score for d in with_inst.dimensions if "Subscription" in d.dimension)
        without_s = next(d.score for d in without_inst.dimensions if "Subscription" in d.dimension)
        assert with_s > without_s

    # ── Network compliance ────────────────────────────────────────

    def test_network_override_penalized(self):
        r = score_policy_defensibility(self._inp(policy_overrides_network_rules=True))
        assert r.network_compliance_ok is False
        comp = next(d for d in r.dimensions if "compliance" in d.dimension.lower())
        assert comp.score < 0

    def test_network_compliant_passes(self):
        r = score_policy_defensibility(self._inp(policy_overrides_network_rules=False))
        assert r.network_compliance_ok is True

    # ── Overall defensibility ─────────────────────────────────────

    def test_strong_defensibility_on_full_policy(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.PROMINENT,
            buyer_checked_policy_box=True,
            has_clear_timeframe=True,
            has_specific_conditions=True,
            policy_applied_consistently=True,
            exceptions_made_before=False,
            policy_overrides_network_rules=False,
        ))
        assert r.defensibility in (PolicyDefensibility.STRONG, PolicyDefensibility.ADEQUATE)

    def test_unusable_defensibility_on_no_policy(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.MISSING,
            has_clear_timeframe=False,
            has_specific_conditions=False,
            policy_applied_consistently=False,
            exceptions_made_before=True,
            policy_overrides_network_rules=True,
        ))
        assert r.defensibility in (PolicyDefensibility.UNUSABLE, PolicyDefensibility.WEAK)

    # ── Win rate adjustment ───────────────────────────────────────

    def test_strong_policy_positive_win_adj(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.PROMINENT,
            buyer_checked_policy_box=True,
            has_clear_timeframe=True,
            has_specific_conditions=True,
            policy_applied_consistently=True,
            policy_overrides_network_rules=False,
        ))
        if r.defensibility == PolicyDefensibility.STRONG:
            assert r.win_rate_adjustment > 0

    def test_unusable_policy_negative_win_adj(self):
        r = score_policy_defensibility(self._inp(
            disclosure_level=DisclosureLevel.MISSING,
            has_clear_timeframe=False,
            has_specific_conditions=False,
            policy_overrides_network_rules=True,
        ))
        if r.defensibility == PolicyDefensibility.UNUSABLE:
            assert r.win_rate_adjustment < 0

    # ── Recommendation non-empty ──────────────────────────────────

    def test_recommendation_non_empty(self):
        r = score_policy_defensibility(self._inp())
        assert len(r.recommendation) > 0

    # ── Consistency scoring ───────────────────────────────────────

    def test_exceptions_made_reduce_consistency_score(self):
        no_exc = score_policy_defensibility(self._inp(
            policy_applied_consistently=True, exceptions_made_before=False))
        with_exc = score_policy_defensibility(self._inp(
            policy_applied_consistently=True, exceptions_made_before=True))
        no_exc_s = next(d.score for d in no_exc.dimensions if "onsist" in d.dimension)
        with_exc_s = next(d.score for d in with_exc.dimensions if "onsist" in d.dimension)
        assert no_exc_s > with_exc_s
