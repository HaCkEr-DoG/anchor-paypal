"""
Undefeatable v3 — tests for 4 new oracle modules:
  oracle/buyer_intelligence.py    — buyer risk tier + serial disputer detection
  oracle/order_risk.py            — order-time risk scoring (8 dimensions)
  oracle/settlement_negotiator.py — negotiation strategy + message templates
  oracle/dispute_dna.py           — historical dispute pattern analytics
"""

import pytest

from oracle.buyer_intelligence import (
    analyze_buyer,
    BuyerProfile,
    BuyerRiskTier,
    BuyerStrategy,
)
from oracle.order_risk import (
    score_order_risk,
    OrderSignals,
    ProductCategory,
    CarrierRiskProfile,
    RiskLevel,
)
from oracle.settlement_negotiator import (
    plan_negotiation,
    SettlementOffer,
)
from oracle.dispute_dna import (
    analyze_dispute_dna,
    DisputeEvent,
    DisputeOutcome,
    RootCause,
)


# ── Buyer Intelligence ────────────────────────────────────────────────────────

class TestBuyerIntelligence:

    def test_new_account_is_tier5_fraud(self):
        """Account < 14 days old = TIER_5_FRAUD."""
        profile = BuyerProfile(account_age_days=10, lifetime_transactions=2, dispute_rate=0.5)
        result = analyze_buyer(profile, 80.0)
        assert result.risk_tier == BuyerRiskTier.TIER_5_FRAUD
        assert result.fight_recommendation is True
        assert result.strategy == BuyerStrategy.REPORT_FRAUD

    def test_serial_disputer_3_merchants(self):
        """3+ disputes with other merchants = TIER_4_SERIAL."""
        profile = BuyerProfile(
            account_age_days=200,
            disputes_with_other_merchants=3,
            dispute_rate=0.05,
        )
        result = analyze_buyer(profile, 150.0)
        assert result.risk_tier == BuyerRiskTier.TIER_4_SERIAL
        assert result.win_probability_adjustment >= 0.15
        assert result.fight_recommendation is True

    def test_high_dispute_rate_tier4(self):
        """Dispute rate > 8% = TIER_4_SERIAL."""
        profile = BuyerProfile(
            account_age_days=365,
            lifetime_transactions=50,
            lifetime_disputes_filed=5,
            dispute_rate=0.10,
        )
        result = analyze_buyer(profile, 200.0)
        assert result.risk_tier == BuyerRiskTier.TIER_4_SERIAL
        assert result.strategy == BuyerStrategy.ESCALATE_TO_PAYPAL

    def test_normal_buyer_low_adjustment(self):
        """Normal buyer = TIER_1, low win prob adjustment."""
        profile = BuyerProfile(
            account_age_days=500,
            lifetime_transactions=100,
            dispute_rate=0.005,
            account_verified=True,
            billing_country="US",
        )
        result = analyze_buyer(profile, 60.0)
        assert result.risk_tier == BuyerRiskTier.TIER_1_NORMAL
        assert result.win_probability_adjustment <= 0.05
        assert result.strategy == BuyerStrategy.GOODWILL_GESTURE

    def test_digital_goods_accessed_before_dispute(self):
        """Digital goods downloaded before dispute = TIER_4+ + friendly fraud flag."""
        profile = BuyerProfile(account_age_days=180, dispute_rate=0.02)
        result = analyze_buyer(
            profile,
            49.0,
            is_digital_goods=True,
            buyer_downloaded_before_dispute=True,
        )
        assert result.risk_tier in (BuyerRiskTier.TIER_4_SERIAL, BuyerRiskTier.TIER_5_FRAUD)
        assert any("BEFORE" in f.upper() or "FRIENDLY" in f.upper() for f in result.red_flags)

    def test_prior_disputes_with_us_increases_adjustment(self):
        """Each prior dispute with this merchant adds to win probability adjustment."""
        profile_no_prior = BuyerProfile(account_age_days=200, prior_disputes_with_us=0)
        profile_with_prior = BuyerProfile(account_age_days=200, prior_disputes_with_us=2)
        r1 = analyze_buyer(profile_no_prior, 100.0)
        r2 = analyze_buyer(profile_with_prior, 100.0)
        assert r2.win_probability_adjustment > r1.win_probability_adjustment

    def test_high_risk_country_elevates_tier(self):
        """Billing country NG (Nigeria) should elevate risk."""
        profile = BuyerProfile(
            account_age_days=365,
            dispute_rate=0.005,
            billing_country="NG",
        )
        result = analyze_buyer(profile, 80.0)
        assert result.risk_tier in (BuyerRiskTier.TIER_2_ELEVATED, BuyerRiskTier.TIER_3_HIGH, BuyerRiskTier.TIER_4_SERIAL)
        assert result.win_probability_adjustment >= 0.08

    def test_buyer_risk_statement_included_for_serial(self):
        """High-risk buyer should have a PayPal-ready risk statement."""
        profile = BuyerProfile(
            account_age_days=90,
            disputes_with_other_merchants=4,
            dispute_rate=0.12,
        )
        result = analyze_buyer(profile, 200.0)
        assert len(result.buyer_risk_statement) > 20

    def test_fraud_account_has_maximum_win_probability_cap(self):
        """Win probability adjustment capped at 0.35."""
        profile = BuyerProfile(
            account_age_days=5,              # TIER_5
            lifetime_transactions=10,
            dispute_rate=0.90,              # +0.30
            disputes_with_other_merchants=5, # +0.20
            has_paypal_buyer_protection_abuse=True,  # +0.20
        )
        result = analyze_buyer(profile, 500.0)
        assert result.win_probability_adjustment <= 0.35

    def test_dispute_filed_first_possible_day(self):
        """Filing on first possible day adds scripted fraud signal."""
        profile = BuyerProfile(account_age_days=180, dispute_rate=0.01)
        result = analyze_buyer(
            profile, 90.0, dispute_filed_on_first_possible_day=True
        )
        assert result.win_probability_adjustment > 0.05
        assert any("first" in f.lower() or "script" in f.lower() for f in result.red_flags)


# ── Order Risk Scorer ─────────────────────────────────────────────────────────

class TestOrderRiskScorer:

    def test_gift_card_always_high_risk(self):
        """Gift cards = GIFT_CARDS category = highest risk category."""
        result = score_order_risk(OrderSignals(
            order_value=100.0,
            product_category=ProductCategory.GIFT_CARDS,
        ))
        assert result.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        assert result.risk_score >= 38

    def test_household_low_value_low_risk(self):
        """Low-value household goods = LOW risk."""
        result = score_order_risk(OrderSignals(
            order_value=25.0,
            product_category=ProductCategory.HOUSEHOLD,
            buyer_prior_orders_with_merchant=5,
            billing_matches_shipping=True,
            carrier_risk=CarrierRiskProfile.LOW,
        ))
        assert result.risk_level == RiskLevel.LOW

    def test_electronics_first_time_buyer_medium_plus(self):
        """Electronics + first-time buyer = MEDIUM or higher."""
        result = score_order_risk(OrderSignals(
            order_value=200.0,
            product_category=ProductCategory.ELECTRONICS,
            buyer_prior_orders_with_merchant=0,
        ))
        assert result.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)

    def test_billing_ne_shipping_adds_points(self):
        """Billing ≠ shipping address adds significantly more points than a match."""
        result_match = score_order_risk(OrderSignals(
            order_value=100.0,
            product_category=ProductCategory.APPAREL,
            billing_matches_shipping=True,
        ))
        result_mismatch = score_order_risk(OrderSignals(
            order_value=100.0,
            product_category=ProductCategory.APPAREL,
            billing_matches_shipping=False,
        ))
        # Mismatch adds 12 pts, matching adds only 3 pts (card vs balance diff) — net +9
        assert result_mismatch.risk_score >= result_match.risk_score + 8

    def test_high_value_requires_insurance(self):
        """HIGH/CRITICAL risk order should recommend insurance."""
        result = score_order_risk(OrderSignals(
            order_value=800.0,
            product_category=ProductCategory.ELECTRONICS,
            buyer_prior_orders_with_merchant=0,
        ))
        assert result.insurance_recommended is True

    def test_critical_risk_triggers_manual_review(self):
        """CRITICAL risk should flag manual review."""
        result = score_order_risk(OrderSignals(
            order_value=1200.0,
            product_category=ProductCategory.ELECTRONICS,
            buyer_prior_orders_with_merchant=0,
            billing_matches_shipping=False,
            billing_country="NG",
            is_international=True,
        ))
        assert result.risk_level == RiskLevel.CRITICAL
        assert result.manual_review_flag is True

    def test_preventive_actions_non_empty_for_high_risk(self):
        """HIGH risk should have non-empty preventive actions."""
        result = score_order_risk(OrderSignals(
            order_value=500.0,
            product_category=ProductCategory.LUXURY_GOODS,
            carrier_risk=CarrierRiskProfile.HIGH,
        ))
        assert len(result.preventive_actions) > 0

    def test_signature_required_above_750(self):
        """$750+ order should mention signature confirmation requirement."""
        result = score_order_risk(OrderSignals(
            order_value=800.0,
            product_category=ProductCategory.ELECTRONICS,
        ))
        actions_text = " ".join(result.preventive_actions)
        assert "signature" in actions_text.lower() or "750" in actions_text

    def test_dispute_probability_scales_with_score(self):
        """Higher risk score = higher dispute probability."""
        low_risk = score_order_risk(OrderSignals(
            order_value=20.0,
            product_category=ProductCategory.HOUSEHOLD,
        ))
        high_risk = score_order_risk(OrderSignals(
            order_value=1000.0,
            product_category=ProductCategory.ELECTRONICS,
            buyer_prior_orders_with_merchant=0,
            billing_matches_shipping=False,
        ))
        assert high_risk.dispute_probability > low_risk.dispute_probability

    def test_carrier_routing_recommends_fedex_ups_for_high_value(self):
        """High-value orders should recommend FedEx/UPS."""
        result = score_order_risk(OrderSignals(
            order_value=600.0,
            product_category=ProductCategory.ELECTRONICS,
        ))
        assert "FedEx" in result.routing_recommendation or "UPS" in result.routing_recommendation


# ── Settlement Negotiator ─────────────────────────────────────────────────────

class TestSettlementNegotiator:

    def test_fraud_buyer_do_not_negotiate(self):
        """TIER_5_FRAUD buyers should get FIGHT, not an offer."""
        plan = plan_negotiation(
            100.0,
            buyer_risk_tier="TIER_5_FRAUD",
        )
        assert plan.opening_offer == SettlementOffer.FIGHT
        assert plan.should_negotiate is False

    def test_serial_disputer_fight(self):
        """TIER_4_SERIAL buyers should not be negotiated with."""
        plan = plan_negotiation(
            200.0,
            buyer_risk_tier="TIER_4_SERIAL",
        )
        assert plan.opening_offer == SettlementOffer.FIGHT

    def test_small_amount_full_refund(self):
        """Amounts < $30 should get full refund (not worth negotiating)."""
        plan = plan_negotiation(20.0)
        assert plan.opening_offer == SettlementOffer.FULL_REFUND_KEEP

    def test_delivered_inr_should_fight(self):
        """Delivered item + INR claim = fight, not negotiate."""
        plan = plan_negotiation(
            120.0,
            claim_type="ITEM_NOT_RECEIVED",
            item_already_delivered=True,
        )
        assert plan.opening_offer == SettlementOffer.FIGHT
        assert plan.should_negotiate is False

    def test_digital_goods_accessed_fight_with_message(self):
        """Digital goods accessed before dispute = fight with specific message."""
        plan = plan_negotiation(
            49.0,
            digital_goods_accessed=True,
        )
        assert plan.opening_offer == SettlementOffer.FIGHT
        assert len(plan.opening_message) > 20
        assert "access" in plan.opening_message.lower() or "digital" in plan.opening_message.lower()

    def test_repeat_amicable_buyer_store_credit(self):
        """Repeat + amicable buyer = store credit opening offer."""
        plan = plan_negotiation(
            120.0,
            is_repeat_customer=True,
            buyer_has_engaged_positively=True,
        )
        assert plan.opening_offer == SettlementOffer.STORE_CREDIT_120

    def test_amicable_buyer_partial_30(self):
        """Amicable (non-repeat) buyer = 30% partial refund opening."""
        plan = plan_negotiation(
            100.0,
            buyer_has_engaged_positively=True,
        )
        assert plan.opening_offer == SettlementOffer.PARTIAL_30

    def test_snad_partial_refund_opening(self):
        """SNAD claim starts at partial refund."""
        plan = plan_negotiation(
            80.0,
            claim_type="SIGNIFICANTLY_NOT_AS_DESCRIBED",
        )
        assert plan.opening_offer in (SettlementOffer.PARTIAL_30, SettlementOffer.PARTIAL_50)

    def test_escalation_sequence_non_empty(self):
        """Negotiation plan should have an escalation path."""
        plan = plan_negotiation(100.0)
        assert len(plan.escalation_sequence) > 0

    def test_opening_message_includes_amount(self):
        """Opening message should reference a dollar amount from the dispute."""
        plan = plan_negotiation(85.0, buyer_has_engaged_positively=True)
        # Opening offer is 30% = $25.50; check that opening_message or escalations mention an amount
        all_text = plan.opening_message + " ".join(plan.escalation_messages.values())
        assert "$" in all_text

    def test_ev_comparison_has_fight_key(self):
        """EV comparison must include 'fight' key."""
        plan = plan_negotiation(200.0, fight_win_probability=0.70)
        assert "fight" in plan.ev_comparison

    def test_walkaway_threshold_positive(self):
        """Walk-away threshold should be positive for a viable negotiation."""
        plan = plan_negotiation(200.0, fight_win_probability=0.60)
        if plan.should_negotiate:
            assert plan.walkaway_threshold > 0

    def test_escalation_messages_keyed_by_offer(self):
        """Escalation messages dict must be keyed by SettlementOffer."""
        plan = plan_negotiation(100.0, buyer_has_engaged_positively=True)
        for offer in plan.escalation_sequence:
            assert offer in plan.escalation_messages


# ── Dispute DNA ───────────────────────────────────────────────────────────────

class TestDisputeDNA:

    def _make_disputes(self):
        return [
            DisputeEvent("D001", "SKU-A", "ITEM_NOT_RECEIVED", "USPS", "10001", "US", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
            DisputeEvent("D002", "SKU-A", "ITEM_NOT_RECEIVED", "USPS", "10002", "US", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
            DisputeEvent("D003", "SKU-A", "SIGNIFICANTLY_NOT_AS_DESCRIBED", "FedEx", "90210", "US", 89.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.LISTING_MISMATCH),
            DisputeEvent("D004", "SKU-B", "ITEM_NOT_RECEIVED", "USPS", "60601", "US", 25.0, DisputeOutcome.SETTLED, settlement_cost=12.5, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
            DisputeEvent("D005", "SKU-B", "ITEM_NOT_RECEIVED", "FedEx", "60602", "US", 25.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.FRAUD_FRIENDLY),
            DisputeEvent("D006", "SKU-C", "UNAUTHORIZED", "FedEx", "77001", "US", 850.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.FRAUD_SERIAL_DISPUTER),
            DisputeEvent("D007", "SKU-A", "ITEM_NOT_RECEIVED", "USPS", "10003", "BR", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.FRAUD_SERIAL_DISPUTER),
        ]

    def test_total_disputes_count(self):
        """analyze_dispute_dna should count all disputes."""
        dna = analyze_dispute_dna(self._make_disputes())
        assert dna.total_disputes_analyzed == 7

    def test_total_loss_calculation(self):
        """Total loss = sum of all settlement costs."""
        dna = analyze_dispute_dna(self._make_disputes())
        assert dna.total_loss == pytest.approx(89 + 89 + 12.5 + 89, abs=0.01)

    def test_overall_win_rate(self):
        """Win rate = won disputes / total."""
        dna = analyze_dispute_dna(self._make_disputes())
        # Won: D003, D005, D006 = 3 out of 7
        assert dna.overall_win_rate == pytest.approx(3 / 7, abs=0.01)

    def test_top_sku_cluster_sorted_by_loss(self):
        """Top SKU clusters should be sorted by loss (highest first)."""
        dna = analyze_dispute_dna(self._make_disputes())
        assert len(dna.top_sku_clusters) > 0
        # SKU-A has the most loss
        assert dna.top_sku_clusters[0].sku == "SKU-A"

    def test_sku_cluster_has_recommendation(self):
        """Each SKU cluster must have a non-empty recommendation."""
        dna = analyze_dispute_dna(self._make_disputes())
        for cluster in dna.top_sku_clusters:
            assert len(cluster.recommendation) > 10

    def test_carrier_profile_usps_has_high_inr_rate(self):
        """USPS should appear as a carrier and have elevated INR rate."""
        dna = analyze_dispute_dna(self._make_disputes())
        carriers = {c.carrier: c for c in dna.carrier_profiles}
        assert "USPS" in carriers
        assert carriers["USPS"].inr_rate > 0.5

    def test_win_rate_by_claim_type_present(self):
        """win_rate_by_claim_type must include at least one claim type."""
        dna = analyze_dispute_dna(self._make_disputes())
        assert len(dna.win_rate_by_claim_type) > 0

    def test_root_cause_distribution_sums_to_1(self):
        """Root cause distribution fractions should sum approximately to 1."""
        dna = analyze_dispute_dna(self._make_disputes())
        total = sum(dna.root_cause_distribution.values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_insights_non_empty(self):
        """At least one insight should be generated."""
        dna = analyze_dispute_dna(self._make_disputes())
        assert len(dna.top_insights) > 0

    def test_empty_disputes_handled_gracefully(self):
        """Empty dispute list should not crash."""
        dna = analyze_dispute_dna([])
        assert dna.total_disputes_analyzed == 0
        assert dna.overall_win_rate == 0.0
        assert dna.total_loss == 0.0

    def test_geographic_cluster_international_flagged(self):
        """Brazil (BR) billing country should appear as a geographic cluster."""
        dna = analyze_dispute_dna(self._make_disputes(), total_orders_30d=100)
        regions = {g.region for g in dna.geographic_clusters}
        assert "BR" in regions

    def test_fraud_dispute_pattern_generates_action(self):
        """High fraud rate should generate a buyer screening action item."""
        # Create mostly-fraud disputes
        fraud_disputes = [
            DisputeEvent(f"DF{i}", "SKU-X", "ITEM_NOT_RECEIVED", "USPS", "10001", "US",
                         50.0, DisputeOutcome.LOST, settlement_cost=50.0,
                         root_cause=RootCause.FRAUD_SERIAL_DISPUTER)
            for i in range(10)
        ]
        dna = analyze_dispute_dna(fraud_disputes)
        actions_text = " ".join(dna.action_items + dna.top_insights)
        assert "fraud" in actions_text.lower() or "buyer" in actions_text.lower()
