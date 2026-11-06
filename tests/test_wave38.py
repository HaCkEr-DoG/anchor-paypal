"""
Tests for Wave 38: dispute_cost_calculator, chargeback_risk_scorer.
"""
import pytest
from oracle.dispute_cost_calculator import (
    calculate_dispute_cost, DisputeCostInput,
    DisputeNetwork, MerchantHDRTier,
)
from oracle.chargeback_risk_scorer import (
    score_chargeback_risk, ChargebackRiskInput,
    ProductCategory, RiskTier, RiskAction,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Cost Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeCostCalculator:

    def _inp(self, **kw) -> DisputeCostInput:
        defaults = dict(
            dispute_id="D-001",
            network=DisputeNetwork.PAYPAL,
            disputed_amount=100.0,
            p_win=0.60,
            hdr_tier=MerchantHDRTier.NORMAL,
            fight_cost=0.0,
            hourly_staff_cost=35.0,
            customer_ltv=0.0,
            churn_probability_if_lost=0.3,
        )
        defaults.update(kw)
        return DisputeCostInput(**defaults)

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calculate_dispute_cost(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_accept_cost_includes_disputed_amount(self):
        r = calculate_dispute_cost(self._inp(disputed_amount=200.0))
        assert r.cost_if_accept.total >= 200.0

    def test_fight_ev_cost_includes_disputed_amount_weighted(self):
        r = calculate_dispute_cost(self._inp(disputed_amount=100.0, p_win=0.0))
        # p_win=0 means certain loss — fight cost ≥ accept cost
        assert r.cost_if_fight.total >= r.cost_if_accept.total - 1.0

    def test_costs_non_negative(self):
        r = calculate_dispute_cost(self._inp())
        assert r.cost_if_accept.total >= 0
        assert r.cost_if_fight.total >= 0

    # ── HDR fee ───────────────────────────────────────────────────

    def test_hdr_fee_zero_when_normal_tier(self):
        r = calculate_dispute_cost(self._inp(hdr_tier=MerchantHDRTier.NORMAL))
        assert r.hdr_fee == pytest.approx(0.0)

    def test_hdr_fee_30_when_fee_tier(self):
        r = calculate_dispute_cost(self._inp(hdr_tier=MerchantHDRTier.FEE_TIER))
        assert r.hdr_fee == pytest.approx(30.0)

    def test_hdr_fee_included_in_fight_cost_on_loss(self):
        r = calculate_dispute_cost(self._inp(
            hdr_tier=MerchantHDRTier.FEE_TIER,
            p_win=0.0,
            disputed_amount=100.0))
        # With p_win=0, full HDR fee should be in EV
        assert r.cost_if_fight.hdr_fee > 0

    # ── Chargeback fee ────────────────────────────────────────────

    def test_paypal_cb_fee_20(self):
        r = calculate_dispute_cost(self._inp(network=DisputeNetwork.PAYPAL))
        assert r.chargeback_fee == pytest.approx(20.0)

    def test_visa_cb_fee_25(self):
        r = calculate_dispute_cost(self._inp(network=DisputeNetwork.VISA))
        assert r.chargeback_fee == pytest.approx(25.0)

    def test_custom_cb_fee_overrides(self):
        r = calculate_dispute_cost(self._inp(custom_chargeback_fee=10.0))
        assert r.chargeback_fee == pytest.approx(10.0)

    # ── Accept scenario — no chargeback fee ───────────────────────

    def test_accept_has_no_chargeback_fee(self):
        r = calculate_dispute_cost(self._inp())
        assert r.cost_if_accept.chargeback_fee == pytest.approx(0.0)

    def test_accept_has_no_hdr_fee(self):
        r = calculate_dispute_cost(self._inp(hdr_tier=MerchantHDRTier.FEE_TIER))
        assert r.cost_if_accept.hdr_fee == pytest.approx(0.0)

    # ── EV analysis ───────────────────────────────────────────────

    def test_ev_fight_better_at_high_win_rate(self):
        r = calculate_dispute_cost(self._inp(p_win=0.95, fight_cost=5.0,
                                              disputed_amount=100.0))
        assert r.ev_fight > r.ev_accept

    def test_ev_accept_better_at_zero_win_rate(self):
        r = calculate_dispute_cost(self._inp(p_win=0.0, fight_cost=10.0,
                                              disputed_amount=100.0))
        assert r.ev_accept >= r.ev_fight

    def test_break_even_in_range(self):
        r = calculate_dispute_cost(self._inp())
        assert 0.0 <= r.break_even_win_rate <= 1.0

    def test_recommendation_fight_at_high_win(self):
        r = calculate_dispute_cost(self._inp(p_win=0.90, fight_cost=0.0,
                                              disputed_amount=200.0))
        assert r.recommendation in ("FIGHT", "BORDERLINE")

    def test_recommendation_accept_at_zero_win(self):
        r = calculate_dispute_cost(self._inp(p_win=0.0, fight_cost=50.0))
        assert r.recommendation in ("ACCEPT", "BORDERLINE")

    # ── Opportunity cost ──────────────────────────────────────────

    def test_opportunity_cost_included_in_fight_on_loss(self):
        r_no_ltv = calculate_dispute_cost(self._inp(customer_ltv=0.0, p_win=0.0))
        r_ltv    = calculate_dispute_cost(self._inp(customer_ltv=500.0, p_win=0.0))
        assert r_ltv.cost_if_fight.total > r_no_ltv.cost_if_fight.total


# ═══════════════════════════════════════════════════════════════════
# Chargeback Risk Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackRiskScorer:

    def _inp(self, **kw) -> ChargebackRiskInput:
        defaults = dict(
            transaction_id="T-001",
            amount=100.0,
            product_category=ProductCategory.PHYSICAL_GENERAL,
            account_age_days=365,
            account_prior_orders=5,
            account_prior_disputes=0,
            billing_shipping_match=True,
            email_domain="mycompany.com",
            ip_country_matches_billing=True,
            is_vpn_or_proxy=False,
            device_fingerprint_new=False,
            is_international_card=False,
            is_prepaid_card=False,
            order_hour_utc=12,
            orders_last_24h=1,
        )
        defaults.update(kw)
        return ChargebackRiskInput(**defaults)

    # ── Low-risk baseline ─────────────────────────────────────────

    def test_low_risk_baseline_no_flags(self):
        r = score_chargeback_risk(self._inp())
        assert r.risk_tier in (RiskTier.MINIMAL, RiskTier.LOW)

    def test_summary_contains_transaction_id(self):
        r = score_chargeback_risk(self._inp(transaction_id="T-XYZ"))
        assert "T-XYZ" in r.summary

    def test_score_in_range(self):
        r = score_chargeback_risk(self._inp())
        assert 0 <= r.risk_score <= 100

    def test_approve_on_low_risk(self):
        r = score_chargeback_risk(self._inp())
        assert r.recommended_action in (RiskAction.APPROVE, RiskAction.REVIEW)

    # ── Digital goods ─────────────────────────────────────────────

    def test_digital_goods_higher_score_than_physical(self):
        physical = score_chargeback_risk(self._inp(
            product_category=ProductCategory.PHYSICAL_GENERAL))
        digital  = score_chargeback_risk(self._inp(
            product_category=ProductCategory.DIGITAL_GOODS))
        assert digital.risk_score > physical.risk_score

    # ── New account risk ──────────────────────────────────────────

    def test_very_new_account_raises_score(self):
        old = score_chargeback_risk(self._inp(account_age_days=365))
        new = score_chargeback_risk(self._inp(account_age_days=3))
        assert new.risk_score > old.risk_score

    def test_account_with_disputes_raises_score(self):
        no_disp = score_chargeback_risk(self._inp(account_prior_disputes=0))
        disp    = score_chargeback_risk(self._inp(account_prior_disputes=3))
        assert disp.risk_score > no_disp.risk_score

    def test_first_order_raises_score(self):
        repeat = score_chargeback_risk(self._inp(account_prior_orders=5))
        first  = score_chargeback_risk(self._inp(account_prior_orders=0))
        assert first.risk_score > repeat.risk_score

    # ── Address mismatch ──────────────────────────────────────────

    def test_address_mismatch_raises_score(self):
        match    = score_chargeback_risk(self._inp(billing_shipping_match=True))
        mismatch = score_chargeback_risk(self._inp(
            billing_shipping_match=False,
            billing_address_hash="BILL",
            shipping_address_hash="SHIP",
            amount=200.0))
        assert mismatch.risk_score > match.risk_score

    # ── VPN / proxy ───────────────────────────────────────────────

    def test_vpn_raises_score(self):
        no_vpn = score_chargeback_risk(self._inp(is_vpn_or_proxy=False))
        vpn    = score_chargeback_risk(self._inp(is_vpn_or_proxy=True))
        assert vpn.risk_score > no_vpn.risk_score

    def test_ip_country_mismatch_raises_score(self):
        match    = score_chargeback_risk(self._inp(ip_country_matches_billing=True))
        mismatch = score_chargeback_risk(self._inp(ip_country_matches_billing=False))
        assert mismatch.risk_score > match.risk_score

    # ── Prepaid card ──────────────────────────────────────────────

    def test_prepaid_card_raises_score(self):
        normal  = score_chargeback_risk(self._inp(is_prepaid_card=False))
        prepaid = score_chargeback_risk(self._inp(is_prepaid_card=True))
        assert prepaid.risk_score > normal.risk_score

    # ── Velocity ──────────────────────────────────────────────────

    def test_high_velocity_raises_score(self):
        low_vel  = score_chargeback_risk(self._inp(orders_last_24h=1))
        high_vel = score_chargeback_risk(self._inp(orders_last_24h=6))
        assert high_vel.risk_score > low_vel.risk_score

    # ── Disposable email ──────────────────────────────────────────

    def test_disposable_email_raises_score(self):
        corp  = score_chargeback_risk(self._inp(email_domain="company.com"))
        disp  = score_chargeback_risk(self._inp(email_domain="mailinator.com"))
        assert disp.risk_score > corp.risk_score

    # ── Combined high-risk ────────────────────────────────────────

    def test_many_risk_factors_very_high_tier(self):
        r = score_chargeback_risk(self._inp(
            product_category=ProductCategory.DIGITAL_GOODS,
            account_age_days=2,
            account_prior_disputes=4,
            account_prior_orders=0,
            billing_shipping_match=False,
            billing_address_hash="B",
            shipping_address_hash="S",
            is_vpn_or_proxy=True,
            is_prepaid_card=True,
            orders_last_24h=6,
            amount=500.0,
        ))
        assert r.risk_tier in (RiskTier.VERY_HIGH, RiskTier.HIGH)

    def test_high_risk_triggers_verify_or_decline(self):
        r = score_chargeback_risk(self._inp(
            product_category=ProductCategory.DIGITAL_GOODS,
            account_age_days=2,
            account_prior_disputes=4,
            is_vpn_or_proxy=True,
            is_prepaid_card=True,
        ))
        assert r.recommended_action in (RiskAction.VERIFY, RiskAction.DECLINE,
                                         RiskAction.REVIEW)

    # ── Report fields ─────────────────────────────────────────────

    def test_top_reason_non_empty(self):
        r = score_chargeback_risk(self._inp(is_vpn_or_proxy=True))
        assert len(r.top_reason) > 0

    def test_risk_factors_non_empty_when_flags_present(self):
        r = score_chargeback_risk(self._inp(is_vpn_or_proxy=True, account_age_days=3))
        assert len(r.risk_factors) >= 1
