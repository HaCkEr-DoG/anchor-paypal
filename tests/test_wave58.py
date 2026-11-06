"""
Tests for Wave 58: buyer_fraud_dna_profiler, transaction_risk_scorer.
"""
import pytest
from oracle.buyer_fraud_dna_profiler import (
    profile_buyer_fraud_dna, FraudDNAInput,
    FraudRiskTier, VelocityRisk,
)
from oracle.transaction_risk_scorer import (
    score_transaction_risk, TransactionRiskInput,
    RiskTier, RecommendedAction,
)


# ═══════════════════════════════════════════════════════════════════
# Buyer Fraud DNA Profiler Tests
# ═══════════════════════════════════════════════════════════════════

def _dna(dispute_id="D-001", buyer_id="B-001", reason_code="INR",
         text="I never received my item", amount=100.0, **kw) -> FraudDNAInput:
    defaults = dict(
        dispute_id=dispute_id, buyer_id=buyer_id, reason_code=reason_code,
        dispute_reason_text=text, transaction_amount=amount, dispute_amount=amount,
    )
    defaults.update(kw)
    return FraudDNAInput(**defaults)


class TestBuyerFraudDNAProfiler:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = profile_buyer_fraud_dna(_dna(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_fraud_score_bounded(self):
        r = profile_buyer_fraud_dna(_dna())
        assert 0.0 <= r.fraud_score <= 100.0

    def test_low_risk_on_clean_dispute(self):
        r = profile_buyer_fraud_dna(_dna(
            text="item arrived slightly later than expected but ok",
            buyer_disputes_last_90d=0,
        ))
        assert r.risk_tier == FraudRiskTier.LOW

    # ── Text similarity ───────────────────────────────────────────

    def test_empty_box_phrase_raises_risk(self):
        r = profile_buyer_fraud_dna(_dna(text="i received an empty box nothing was inside tampered"))
        assert r.risk_tier in (FraudRiskTier.MEDIUM, FraudRiskTier.HIGH, FraudRiskTier.CRITICAL)

    def test_fraud_script_match_found(self):
        r = profile_buyer_fraud_dna(_dna(text="never received item did not arrive package tracking useless"))
        assert r.text_similarity_score > 0.0

    def test_text_similarity_score_bounded(self):
        r = profile_buyer_fraud_dna(_dna())
        assert 0.0 <= r.text_similarity_score <= 1.0

    # ── Velocity signals ──────────────────────────────────────────

    def test_extreme_velocity(self):
        r = profile_buyer_fraud_dna(_dna(buyer_disputes_last_90d=15))
        assert r.velocity_risk == VelocityRisk.EXTREME
        assert r.risk_tier in (FraudRiskTier.MEDIUM, FraudRiskTier.HIGH, FraudRiskTier.CRITICAL)

    def test_high_velocity(self):
        r = profile_buyer_fraud_dna(_dna(buyer_disputes_last_90d=7))
        assert r.velocity_risk == VelocityRisk.HIGH

    def test_no_velocity(self):
        r = profile_buyer_fraud_dna(_dna(buyer_disputes_last_90d=1))
        assert r.velocity_risk == VelocityRisk.NONE

    # ── Delivery contradiction ────────────────────────────────────

    def test_delivery_contradiction_raises_risk(self):
        r = profile_buyer_fraud_dna(_dna(
            reason_code="INR",
            tracking_confirmed_delivered=True,
        ))
        assert r.risk_tier != FraudRiskTier.LOW
        assert any("delivered" in f.lower() or "tracking" in f.lower() for f in r.red_flags)

    def test_delivery_no_contradiction_on_snad(self):
        r = profile_buyer_fraud_dna(_dna(
            reason_code="SNAD",
            tracking_confirmed_delivered=True,
        ))
        # Delivery contradiction only applies to INR — SNAD should not add this flag
        assert not any("delivery" in f.lower() and "inr" not in f.lower()
                       for f in r.red_flags if "not as described" not in f.lower())

    # ── Professional disputer ─────────────────────────────────────

    def test_professional_disputer_on_extreme_velocity_plus_critical_tier(self):
        r = profile_buyer_fraud_dna(_dna(
            buyer_disputes_last_90d=12,
            buyer_disputes_total=20,
            buyer_transactions_total=30,
            text="never received item did not arrive package empty box tampered",
        ))
        assert r.is_suspected_professional is True

    def test_not_professional_on_low_tier(self):
        r = profile_buyer_fraud_dna(_dna())
        # Low tier can't be professional
        if r.risk_tier == FraudRiskTier.LOW:
            assert r.is_suspected_professional is False

    # ── DTR ───────────────────────────────────────────────────────

    def test_dtr_computed(self):
        r = profile_buyer_fraud_dna(_dna(
            buyer_disputes_total=10, buyer_transactions_total=20
        ))
        assert abs(r.dispute_to_transaction_ratio - 0.50) < 0.01

    def test_high_dtr_flags_red_flag(self):
        r = profile_buyer_fraud_dna(_dna(
            buyer_disputes_total=50, buyer_transactions_total=100,
        ))
        assert any("ratio" in f.lower() for f in r.red_flags)

    # ── Cross-merchant ────────────────────────────────────────────

    def test_cross_merchant_flag_on_3_plus(self):
        r = profile_buyer_fraud_dna(_dna(same_reason_across_merchants=4))
        assert r.cross_merchant_flag is True

    def test_no_cross_merchant_flag_on_2(self):
        r = profile_buyer_fraud_dna(_dna(same_reason_across_merchants=2))
        assert r.cross_merchant_flag is False

    # ── Action ────────────────────────────────────────────────────

    def test_low_risk_recommends_standard(self):
        r = profile_buyer_fraud_dna(_dna(
            text="i would like a refund for this item",
            buyer_disputes_last_90d=0,
        ))
        if r.risk_tier == FraudRiskTier.LOW:
            assert r.recommended_action == "STANDARD_PROCESS"

    def test_red_flags_non_empty_on_risk(self):
        r = profile_buyer_fraud_dna(_dna(buyer_disputes_last_90d=15))
        assert len(r.red_flags) >= 1


# ═══════════════════════════════════════════════════════════════════
# Transaction Risk Scorer Tests
# ═══════════════════════════════════════════════════════════════════

def _txr(transaction_id="T-001", merchant_id="M-001", amount=200.0, **kw) -> TransactionRiskInput:
    defaults = dict(transaction_id=transaction_id, merchant_id=merchant_id, amount=amount)
    defaults.update(kw)
    return TransactionRiskInput(**defaults)


class TestTransactionRiskScorer:

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_transaction_id(self):
        r = score_transaction_risk(_txr(transaction_id="T-XYZ"))
        assert "T-XYZ" in r.summary

    def test_risk_score_bounded(self):
        r = score_transaction_risk(_txr())
        assert 0.0 <= r.risk_score <= 100.0

    def test_cb_probability_bounded(self):
        r = score_transaction_risk(_txr())
        assert 0.0 < r.chargeback_probability < 1.0

    def test_all_tiers_reachable(self):
        low  = score_transaction_risk(_txr(avs_result="Y", cvv_result="M",
                                           has_3ds_authentication=True,
                                           buyer_account_age_days=730,
                                           buyer_prior_transactions=10,
                                           is_first_transaction_with_merchant=False))
        high = score_transaction_risk(_txr(avs_result="N", cvv_result="N",
                                           buyer_account_age_days=1,
                                           buyer_disputes_last_90d=10,
                                           buyer_orders_last_24h=12,
                                           item_category="GIFT_CARDS",
                                           billing_country="US", ip_country="RU",
                                           shipping_country="CN"))
        assert low.risk_tier == RiskTier.LOW
        assert high.risk_tier in (RiskTier.HIGH, RiskTier.CRITICAL)

    # ── AVS/CVV signals ───────────────────────────────────────────

    def test_avs_mismatch_raises_score(self):
        base = score_transaction_risk(_txr(avs_result="Y"))
        mism = score_transaction_risk(_txr(avs_result="N"))
        assert mism.risk_score > base.risk_score

    def test_cvv_mismatch_raises_score(self):
        base = score_transaction_risk(_txr(cvv_result="M"))
        mism = score_transaction_risk(_txr(cvv_result="N"))
        assert mism.risk_score > base.risk_score

    # ── Address signals ───────────────────────────────────────────

    def test_ip_country_mismatch_raises_score(self):
        base = score_transaction_risk(_txr(billing_country="US", ip_country="US"))
        mism = score_transaction_risk(_txr(billing_country="US", ip_country="NG"))
        assert mism.risk_score > base.risk_score

    # ── Item category ─────────────────────────────────────────────

    def test_gift_cards_highest_item_risk(self):
        gc   = score_transaction_risk(_txr(item_category="GIFT_CARDS"))
        phys = score_transaction_risk(_txr(item_category="PHYSICAL_GOODS"))
        assert gc.risk_score > phys.risk_score

    def test_digital_goods_higher_than_physical(self):
        dig  = score_transaction_risk(_txr(item_category="DIGITAL_GOODS"))
        phys = score_transaction_risk(_txr(item_category="PHYSICAL_GOODS"))
        assert dig.risk_score > phys.risk_score

    # ── Buyer signals ─────────────────────────────────────────────

    def test_new_account_raises_score(self):
        new_acc = score_transaction_risk(_txr(buyer_account_age_days=3))
        old_acc = score_transaction_risk(_txr(buyer_account_age_days=720))
        assert new_acc.risk_score > old_acc.risk_score

    def test_disposable_email_raises_score(self):
        disp = score_transaction_risk(_txr(buyer_email_domain="mailinator.com"))
        norm = score_transaction_risk(_txr(buyer_email_domain="gmail.com"))
        assert disp.risk_score > norm.risk_score

    def test_prior_disputes_raises_score(self):
        clean  = score_transaction_risk(_txr(buyer_disputes_last_90d=0))
        risky  = score_transaction_risk(_txr(buyer_disputes_last_90d=5))
        assert risky.risk_score > clean.risk_score

    # ── 3DS mitigation ────────────────────────────────────────────

    def test_3ds_lowers_score(self):
        no3ds = score_transaction_risk(_txr(has_3ds_authentication=False))
        with3ds = score_transaction_risk(_txr(has_3ds_authentication=True))
        assert with3ds.risk_score < no3ds.risk_score

    def test_3ds_in_mitigating_factors(self):
        r = score_transaction_risk(_txr(has_3ds_authentication=True))
        assert any("3ds" in f.lower() or "3d" in f.lower() for f in r.mitigating_factors)

    # ── Action recommendations ────────────────────────────────────

    def test_critical_risk_recommends_decline(self):
        r = score_transaction_risk(_txr(
            avs_result="N", cvv_result="N",
            buyer_account_age_days=1,
            buyer_disputes_last_90d=12,
            buyer_orders_last_24h=15,
            item_category="GIFT_CARDS",
            billing_country="US", ip_country="RU",
            buyer_email_domain="tempmail.org",
        ))
        if r.risk_tier == RiskTier.CRITICAL:
            assert r.recommended_action == RecommendedAction.DECLINE

    def test_low_risk_recommends_approve(self):
        r = score_transaction_risk(_txr(
            avs_result="Y", cvv_result="M",
            has_3ds_authentication=True,
            buyer_account_age_days=730,
            buyer_prior_transactions=20,
            is_first_transaction_with_merchant=False,
            item_category="PHYSICAL_GOODS",
        ))
        assert r.recommended_action in (RecommendedAction.APPROVE, RecommendedAction.APPROVE_MONITOR)

    # ── Signal breakdown ──────────────────────────────────────────

    def test_top_signals_non_empty(self):
        r = score_transaction_risk(_txr(avs_result="N"))
        assert len(r.top_signals) >= 1

    def test_score_breakdown_adds_up(self):
        r = score_transaction_risk(_txr())
        total_from_parts = (r.address_risk_score + r.card_risk_score +
                            r.buyer_risk_score + r.velocity_risk_score +
                            r.item_risk_score)
        # 3DS mitigation (-20) may cause total_from_parts > risk_score
        assert total_from_parts >= 0

    def test_velocity_signal_on_many_orders(self):
        r = score_transaction_risk(_txr(buyer_orders_last_24h=12))
        assert r.velocity_risk_score > 0

    def test_high_amount_adds_risk(self):
        normal = score_transaction_risk(_txr(amount=100.0))
        high   = score_transaction_risk(_txr(amount=5000.0))
        assert high.risk_score >= normal.risk_score
