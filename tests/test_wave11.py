"""
Tests for Wave 11: chargeback_shield, claim_router.
"""
import pytest
from oracle.chargeback_shield import (
    assess_chargeback_defense, ChargebackInput, CardNetwork,
    ChargebackReasonCategory, DefenseStrategy, PriorTransaction,
)
from oracle.claim_router import (
    route_dispute, route_portfolio, RoutingPolicy, DisputeRoutingInput,
    Route, RoutingReason, PortfolioRoutingPlan,
)


# ═══════════════════════════════════════════════════════════════════
# Chargeback Shield Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackShield:

    def _inp(self, **kwargs) -> ChargebackInput:
        defaults = dict(
            chargeback_id="CB-001",
            order_id="ORD-001",
            chargeback_amount=200.0,
            card_network=CardNetwork.VISA,
            reason_code="10.4",
            has_tracking=True,
            has_delivery_confirmation=False,
            has_signature_confirmation=False,
            has_buyer_communications=True,
            has_account_login_history=False,
            prior_transactions=[],
            merchant_chargeback_win_rate=0.40,
            fight_cost=25.0,
        )
        defaults.update(kwargs)
        return ChargebackInput(**defaults)

    def test_visa_10_4_classified_as_unauthorized(self):
        r = assess_chargeback_defense(self._inp(reason_code="10.4"))
        assert r.reason_category == ChargebackReasonCategory.UNAUTHORIZED

    def test_visa_13_1_classified_as_inr(self):
        r = assess_chargeback_defense(self._inp(reason_code="13.1"))
        assert r.reason_category == ChargebackReasonCategory.ITEM_NOT_RECEIVED

    def test_visa_13_3_classified_as_snad(self):
        r = assess_chargeback_defense(self._inp(reason_code="13.3"))
        assert r.reason_category == ChargebackReasonCategory.SIGNIFICANTLY_NAS

    def test_visa_12_2_classified_as_duplicate(self):
        r = assess_chargeback_defense(self._inp(reason_code="12.2"))
        assert r.reason_category == ChargebackReasonCategory.DUPLICATE

    def test_explicit_category_overrides_reason_code(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="10.4",
            reason_category=ChargebackReasonCategory.SUBSCRIPTION,
        ))
        assert r.reason_category == ChargebackReasonCategory.SUBSCRIPTION

    def test_base_win_rate_unauthorized_is_low(self):
        r = assess_chargeback_defense(self._inp(reason_code="10.4"))
        assert r.base_win_rate == pytest.approx(0.28)

    def test_base_win_rate_duplicate_is_high(self):
        r = assess_chargeback_defense(self._inp(reason_code="12.2"))
        assert r.base_win_rate == pytest.approx(0.65)

    def test_amex_lowers_win_rate(self):
        r_visa = assess_chargeback_defense(self._inp(card_network=CardNetwork.VISA))
        r_amex = assess_chargeback_defense(self._inp(card_network=CardNetwork.AMEX))
        assert r_amex.adjusted_win_rate < r_visa.adjusted_win_rate

    def test_signature_confirmation_raises_win_rate(self):
        r_no_sig = assess_chargeback_defense(self._inp(has_signature_confirmation=False))
        r_sig = assess_chargeback_defense(self._inp(has_signature_confirmation=True))
        assert r_sig.adjusted_win_rate > r_no_sig.adjusted_win_rate

    def test_no_tracking_destroys_inr_win_rate(self):
        r_track = assess_chargeback_defense(self._inp(reason_code="13.1", has_tracking=True))
        r_no_track = assess_chargeback_defense(self._inp(reason_code="13.1", has_tracking=False))
        assert r_no_track.adjusted_win_rate < r_track.adjusted_win_rate

    def test_ev_contest_computed_correctly(self):
        r = assess_chargeback_defense(self._inp(chargeback_amount=100.0, fight_cost=25.0))
        expected_ev = round(r.adjusted_win_rate * 100.0 - 25.0, 2)
        assert r.ev_contest == pytest.approx(expected_ev, abs=0.01)

    def test_should_contest_false_when_ev_negative(self):
        # Very small amount with high fight cost → negative EV
        r = assess_chargeback_defense(self._inp(chargeback_amount=20.0, fight_cost=25.0))
        assert r.ev_contest < 0
        assert r.should_contest is False

    def test_should_contest_true_when_ev_positive(self):
        r = assess_chargeback_defense(self._inp(chargeback_amount=500.0))
        assert r.ev_contest > 0
        assert r.should_contest is True

    def test_ce30_none_for_mastercard(self):
        r = assess_chargeback_defense(self._inp(card_network=CardNetwork.MASTERCARD))
        assert r.ce30 is None

    def test_ce30_not_eligible_without_prior_transactions(self):
        r = assess_chargeback_defense(self._inp(
            card_network=CardNetwork.VISA,
            reason_code="10.4",
            prior_transactions=[],
        ))
        assert r.ce30 is not None
        assert r.ce30.eligible is False

    def test_ce30_eligible_with_two_prior_transactions(self):
        priors = [
            PriorTransaction("T-1", 100.0, is_disputed=False, days_before_chargeback=30, same_device=True, same_ip=True),
            PriorTransaction("T-2", 80.0, is_disputed=False, days_before_chargeback=60, same_device=True, same_ip=True),
        ]
        r = assess_chargeback_defense(self._inp(prior_transactions=priors))
        assert r.ce30.eligible is True
        assert r.ce30.qualifying_prior_transactions == 2
        assert r.strategy == DefenseStrategy.CE30_CONTEST

    def test_ce30_not_eligible_if_all_disputed(self):
        priors = [
            PriorTransaction("T-1", 100.0, is_disputed=True, days_before_chargeback=30),
            PriorTransaction("T-2", 80.0, is_disputed=True, days_before_chargeback=60),
        ]
        r = assess_chargeback_defense(self._inp(prior_transactions=priors))
        assert r.ce30.eligible is False

    def test_ce30_excludes_transactions_older_than_120_days(self):
        priors = [
            PriorTransaction("T-1", 100.0, days_before_chargeback=130),
            PriorTransaction("T-2", 80.0, days_before_chargeback=150),
        ]
        r = assess_chargeback_defense(self._inp(prior_transactions=priors))
        assert r.ce30.eligible is False

    def test_accept_strategy_when_ev_negative_non_duplicate(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="13.3",
            reason_category=ChargebackReasonCategory.SIGNIFICANTLY_NAS,
            chargeback_amount=20.0,
            fight_cost=25.0,
        ))
        assert r.strategy == DefenseStrategy.ACCEPT

    def test_contest_strategy_for_positive_ev_no_ce30(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="13.1",
            chargeback_amount=400.0,
            has_tracking=True,
            has_delivery_confirmation=True,
        ))
        assert r.strategy in (DefenseStrategy.CONTEST, DefenseStrategy.CE30_CONTEST)

    def test_required_documents_populated(self):
        r = assess_chargeback_defense(self._inp(reason_code="13.1"))
        assert len(r.required_documents) > 0

    def test_missing_documents_identified(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="13.1",
            has_tracking=False,
            has_delivery_confirmation=False,
        ))
        assert len(r.missing_documents) > 0

    def test_document_completeness_zero_to_one(self):
        r = assess_chargeback_defense(self._inp())
        assert 0.0 <= r.document_completeness <= 1.0

    def test_win_likely_label(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="12.2",
            chargeback_amount=500.0,
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
        ))
        assert r.expected_outcome == "WIN_LIKELY"

    def test_loss_likely_label_for_low_win_rate(self):
        r = assess_chargeback_defense(self._inp(
            reason_code="13.3",
            reason_category=ChargebackReasonCategory.SIGNIFICANTLY_NAS,
            chargeback_amount=100.0,
            has_buyer_communications=False,
            merchant_chargeback_win_rate=0.15,
        ))
        assert r.expected_outcome in ("LOSS_LIKELY", "COIN_FLIP")

    def test_summary_contains_key_fields(self):
        r = assess_chargeback_defense(self._inp())
        assert "CB-001" in r.summary
        assert "VISA" in r.summary

    def test_submission_notes_non_empty(self):
        r = assess_chargeback_defense(self._inp())
        assert len(r.submission_notes) > 0

    def test_high_value_no_signature_warning(self):
        r = assess_chargeback_defense(self._inp(
            chargeback_amount=1000.0,
            has_signature_confirmation=False,
        ))
        warnings = " ".join(r.submission_notes)
        assert "signature" in warnings.lower() or "WARNING" in warnings


# ═══════════════════════════════════════════════════════════════════
# Claim Router Tests
# ═══════════════════════════════════════════════════════════════════

class TestClaimRouter:

    def _inp(self, **kwargs) -> DisputeRoutingInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="ITEM_NOT_RECEIVED",
            fight_score=75.0,
            ev_fight=50.0,
            dispute_amount=150.0,
            evidence_ready=True,
            evidence_strength="STRONG",
            has_tracking=True,
            has_delivery_confirmation=True,
            buyer_dispute_count_90d=0,
            is_hdr_contributing=True,
            days_remaining=10,
            sp_protection="FULL",
        )
        defaults.update(kwargs)
        return DisputeRoutingInput(**defaults)

    def _policy(self, **kwargs) -> RoutingPolicy:
        return RoutingPolicy(**kwargs)

    def test_high_score_evidence_ready_routes_auto_fight(self):
        d = route_dispute(self._inp(fight_score=80.0, ev_fight=60.0), self._policy())
        assert d.route == Route.AUTO_FIGHT

    def test_borderline_score_routes_human_review(self):
        d = route_dispute(self._inp(fight_score=55.0, ev_fight=20.0), self._policy())
        assert d.route == Route.HUMAN_REVIEW

    def test_low_score_negative_ev_routes_auto_refund(self):
        d = route_dispute(self._inp(fight_score=30.0, ev_fight=-40.0), self._policy())
        assert d.route == Route.AUTO_REFUND

    def test_low_amount_routes_auto_refund(self):
        d = route_dispute(self._inp(dispute_amount=15.0, fight_score=85.0, ev_fight=10.0), self._policy())
        assert d.route == Route.AUTO_REFUND
        assert d.primary_reason == RoutingReason.LOW_AMOUNT_NOT_WORTH_FIGHTING

    def test_high_value_routes_human_review(self):
        d = route_dispute(self._inp(dispute_amount=1500.0), self._policy())
        assert d.route == Route.HUMAN_REVIEW
        assert d.primary_reason == RoutingReason.HIGH_VALUE_HUMAN_REQUIRED

    def test_repeat_fraudster_routes_escalate(self):
        d = route_dispute(self._inp(buyer_dispute_count_90d=5), self._policy())
        assert d.route == Route.ESCALATE
        assert d.primary_reason == RoutingReason.REPEAT_FRAUDSTER_ESCALATE

    def test_duplicate_always_routes_auto_fight(self):
        d = route_dispute(self._inp(claim_type="DUPLICATE", fight_score=40.0, ev_fight=-10.0), self._policy())
        assert d.route == Route.AUTO_FIGHT

    def test_snad_routes_human_review_by_policy(self):
        d = route_dispute(self._inp(claim_type="SNAD"), self._policy())
        assert d.route == Route.HUMAN_REVIEW

    def test_snad_low_score_can_route_settle(self):
        d = route_dispute(self._inp(
            claim_type="SNAD",
            fight_score=50.0,
            ev_fight=5.0,
        ), self._policy(always_human_review_snad=False))
        assert d.route == Route.SETTLE

    def test_evidence_not_ready_delays_auto_fight(self):
        d = route_dispute(self._inp(fight_score=85.0, evidence_ready=False), self._policy())
        assert d.route == Route.HUMAN_REVIEW
        assert d.primary_reason == RoutingReason.MISSING_EVIDENCE

    def test_capacity_cap_routes_to_human(self):
        d = route_dispute(
            self._inp(fight_score=85.0, ev_fight=60.0),
            self._policy(max_auto_fight_per_day=5),
            auto_fight_used_today=5,
        )
        assert d.route == Route.HUMAN_REVIEW
        assert d.primary_reason == RoutingReason.CAPACITY_LIMIT_REVIEW

    def test_hdr_pressure_mode_routes_negative_ev_to_refund(self):
        d = route_dispute(
            self._inp(fight_score=70.0, ev_fight=-5.0, is_hdr_contributing=True),
            self._policy(hdr_pressure_mode=True),
        )
        assert d.route == Route.AUTO_REFUND
        assert d.primary_reason == RoutingReason.HDR_PRESSURE_REFUND

    def test_hdr_pressure_mode_raises_fight_threshold(self):
        # Score 75 qualifies under normal (threshold=72) but not under HDR pressure (threshold=82)
        d_normal = route_dispute(
            self._inp(fight_score=75.0, ev_fight=30.0, is_hdr_contributing=True),
            self._policy(hdr_pressure_mode=False),
        )
        d_hdr = route_dispute(
            self._inp(fight_score=75.0, ev_fight=30.0, is_hdr_contributing=False),
            self._policy(hdr_pressure_mode=True, hdr_pressure_fight_threshold=82.0),
        )
        assert d_normal.route == Route.AUTO_FIGHT
        assert d_hdr.route != Route.AUTO_FIGHT  # Doesn't meet elevated threshold

    def test_weak_evidence_no_delivery_confirmation_routes_partial_refund(self):
        d = route_dispute(self._inp(
            fight_score=55.0,
            ev_fight=15.0,
            evidence_strength="WEAK",
            has_tracking=True,
            has_delivery_confirmation=False,
            claim_type="ITEM_NOT_RECEIVED",
        ), self._policy(always_human_review_snad=False))
        assert d.route == Route.PARTIAL_REFUND

    def test_deeply_negative_ev_routes_auto_refund_regardless(self):
        d = route_dispute(self._inp(fight_score=70.0, ev_fight=-100.0), self._policy())
        assert d.route == Route.AUTO_REFUND

    def test_summary_line_contains_dispute_id(self):
        d = route_dispute(self._inp(), self._policy())
        assert "D-001" in d.summary_line

    def test_confidence_in_range(self):
        d = route_dispute(self._inp(), self._policy())
        assert 0.0 <= d.confidence <= 1.0

    def test_action_summary_non_empty(self):
        d = route_dispute(self._inp(), self._policy())
        assert len(d.action_summary) > 10


class TestPortfolioRouting:

    def _make_disputes(self, n: int) -> list[DisputeRoutingInput]:
        disputes = []
        for i in range(n):
            disputes.append(DisputeRoutingInput(
                dispute_id=f"D-{i:03d}",
                claim_type="ITEM_NOT_RECEIVED",
                fight_score=float(40 + i * 5),  # Scores 40 to 40+5n
                ev_fight=float(-20 + i * 10),
                dispute_amount=100.0 + i * 50,
                evidence_ready=True,
                evidence_strength="STRONG" if i % 2 == 0 else "MODERATE",
                has_tracking=True,
                has_delivery_confirmation=i % 2 == 0,
                buyer_dispute_count_90d=0,
                is_hdr_contributing=True,
                days_remaining=10,
            ))
        return disputes

    def test_portfolio_returns_all_decisions(self):
        disputes = self._make_disputes(5)
        plan = route_portfolio(disputes, RoutingPolicy())
        assert plan.total_disputes == 5
        assert len(plan.decisions) == 5

    def test_portfolio_route_counts_add_up(self):
        disputes = self._make_disputes(8)
        plan = route_portfolio(disputes, RoutingPolicy())
        total = (
            plan.auto_fight_count + plan.human_review_count +
            plan.auto_refund_count + plan.partial_refund_count +
            plan.settle_count + plan.escalate_count
        )
        assert total == plan.total_disputes

    def test_portfolio_respects_capacity_cap(self):
        disputes = self._make_disputes(10)  # Many disputes with high scores
        plan = route_portfolio(disputes, RoutingPolicy(max_auto_fight_per_day=3))
        assert plan.auto_fight_count <= 3

    def test_portfolio_at_risk_amount_is_human_review_plus_escalate(self):
        disputes = self._make_disputes(5)
        plan = route_portfolio(disputes, RoutingPolicy())
        at_risk_manual = sum(
            d.dispute_amount
            for d, dec in zip(disputes, plan.decisions)
            if dec.route in (Route.HUMAN_REVIEW, Route.ESCALATE)
        )
        assert plan.total_at_risk_amount == pytest.approx(at_risk_manual, abs=0.01)

    def test_portfolio_summary_non_empty(self):
        plan = route_portfolio(self._make_disputes(3), RoutingPolicy())
        assert len(plan.summary) > 20

    def test_capacity_remaining_computed(self):
        disputes = self._make_disputes(3)
        plan = route_portfolio(disputes, RoutingPolicy(max_auto_fight_per_day=10))
        assert plan.capacity_remaining == 10 - plan.auto_fight_count
