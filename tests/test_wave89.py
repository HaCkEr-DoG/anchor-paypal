"""Wave 89 — Settlement Offer Evaluator tests."""
import pytest
from oracle.settlement_evaluator import (
    SettlementDecision,
    SettlementOfferInput, SettlementEvaluationReport,
    evaluate_settlement,
    _ev_fight, _ev_settle, _counter_offer,
)


def _inp(dispute_id="D-001", claim_type="INR", amount=100.0, offer=30.0,
         win_prob=0.60, cb_fee=20.0, resp_cost=10.0, days=7,
         evidence=0.60):
    return SettlementOfferInput(
        dispute_id=dispute_id,
        claim_type=claim_type,
        original_amount_usd=amount,
        offer_amount_usd=offer,
        win_probability=win_prob,
        chargeback_fee_usd=cb_fee,
        response_cost_usd=resp_cost,
        days_until_deadline=days,
        evidence_strength=evidence,
    )


# ─── EV calculations ──────────────────────────────────────────────────────────

class TestEvFight:
    def test_certain_win_no_fee(self):
        # p=1, amount=100, cb=0, cost=0 → EV = 100
        assert abs(_ev_fight(100, 1.0, 0, 0) - 100) < 1e-9

    def test_certain_loss_full_fee(self):
        # p=0, amount=100, cb=20, cost=0 → EV = -(100+20) = -120
        assert abs(_ev_fight(100, 0.0, 20, 0) - (-120)) < 1e-9

    def test_fifty_fifty(self):
        # p=0.5, amount=100, cb=20, cost=0 → EV = 50 - 60 = -10
        assert abs(_ev_fight(100, 0.5, 20, 0) - (-10)) < 1e-9

    def test_response_cost_reduces_ev(self):
        ev_no_cost = _ev_fight(100, 0.7, 20, 0)
        ev_with_cost = _ev_fight(100, 0.7, 20, 10)
        assert ev_with_cost < ev_no_cost


class TestEvSettle:
    def test_zero_offer_full_recovery(self):
        # Offer=0 means buyer waives entirely → merchant keeps full amount
        assert abs(_ev_settle(100, 0, 0) - 100) < 1e-9

    def test_full_offer_zero_recovery(self):
        # Offer=100 → merchant loses full amount minus cost
        assert abs(_ev_settle(100, 100, 0) - 0) < 1e-9

    def test_partial_offer(self):
        assert abs(_ev_settle(100, 40, 10) - 50) < 1e-9


class TestCounterOffer:
    def test_zero_win_prob_full_amount(self):
        # Breakeven at p=0: buyer should waive everything
        # counter = amount * (1-0) * (1-0.10) = amount * 0.9
        # but breakeven = amount*(1-0) = amount → counter = amount*(1-0.10)=90
        c = _counter_offer(100, 0.0)
        assert abs(c - 90.0) < 0.01

    def test_full_win_prob_zero(self):
        # Breakeven at p=1: merchant should pay nothing → counter=0
        c = _counter_offer(100, 1.0)
        assert c == 0.0

    def test_mid_prob_between_zero_and_full(self):
        c = _counter_offer(100, 0.5)
        assert 0.0 < c < 100.0


# ─── Decision logic ───────────────────────────────────────────────────────────

class TestDecisionAccept:
    def test_negative_ev_fight_leads_to_accept(self):
        # p=0.3, amount=100, cb=20 → EV(fight) = 30 - 70×1.2 - 10 = 30-84-10 = -64
        r = evaluate_settlement(_inp(amount=100, offer=30, win_prob=0.3, evidence=0.5))
        assert r.decision == SettlementDecision.ACCEPT

    def test_offer_above_threshold_leads_to_accept(self):
        # Very generous offer: offer = 1 (almost full refund waived)
        # EV(settle) = 100-1-10 = 89; EV(fight) with p=0.5, cb=20, cost=10 = 50-60-10 = -20
        r = evaluate_settlement(_inp(amount=100, offer=1, win_prob=0.5))
        assert r.decision == SettlementDecision.ACCEPT


class TestDecisionCounter:
    def test_stingy_offer_counter(self):
        # p=0.60, amount=100, offer=92
        # EV(fight)=2.0, EV(settle)=100-92-10=-2 → fight beats settle → COUNTER or REJECT
        r = evaluate_settlement(_inp(amount=100, offer=92, win_prob=0.60, evidence=0.50))
        assert r.decision in (SettlementDecision.COUNTER, SettlementDecision.REJECT)

    def test_counter_offer_non_zero(self):
        r = evaluate_settlement(_inp(amount=100, offer=80, win_prob=0.70, evidence=0.50))
        if r.decision == SettlementDecision.COUNTER:
            assert r.counter_offer_usd > 0.0


class TestDecisionReject:
    def test_strong_evidence_high_win_prob_reject(self):
        # p=0.90, evidence=0.95, big offer gap → should reject and fight
        r = evaluate_settlement(_inp(amount=200, offer=150, win_prob=0.90, evidence=0.95))
        assert r.decision == SettlementDecision.REJECT

    def test_reject_counter_offer_is_zero(self):
        r = evaluate_settlement(_inp(amount=200, offer=150, win_prob=0.90, evidence=0.95))
        if r.decision == SettlementDecision.REJECT:
            assert r.counter_offer_usd == 0.0


class TestDeadlineOverride:
    def test_last_day_weak_evidence_forces_accept(self):
        # Even if fight EV looks better, deadline=1 + weak evidence → ACCEPT
        r = evaluate_settlement(_inp(amount=100, offer=80, win_prob=0.70,
                                     days=1, evidence=0.30))
        assert r.decision == SettlementDecision.ACCEPT

    def test_deadline_override_in_reasoning(self):
        r = evaluate_settlement(_inp(amount=100, offer=80, win_prob=0.70,
                                     days=1, evidence=0.30))
        assert "override" in r.reasoning.lower() or "deadline" in r.reasoning.lower()


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_preserved(self):
        r = evaluate_settlement(_inp(dispute_id="DISP-XYZ"))
        assert r.dispute_id == "DISP-XYZ"

    def test_ev_fight_matches_formula(self):
        inp = _inp(amount=100, win_prob=0.7, cb_fee=20, resp_cost=10)
        r = evaluate_settlement(inp)
        expected = round(0.7*100 - 0.3*120 - 10, 4)
        assert abs(r.ev_fight - expected) < 1e-3

    def test_accept_threshold_positive_for_good_win_prob(self):
        r = evaluate_settlement(_inp(win_prob=0.8))
        assert r.accept_threshold_usd > 0.0

    def test_reasoning_non_empty(self):
        r = evaluate_settlement(_inp())
        assert len(r.reasoning) > 10

    def test_factors_list_exists(self):
        r = evaluate_settlement(_inp())
        assert isinstance(r.factors, list)
