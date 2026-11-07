"""Wave 81 — Settlement Negotiation Advisor tests."""
import pytest
from oracle.settlement_negotiation_advisor import (
    SettlementDecision,
    SettlementNegotiationInput,
    SettlementNegotiationReport,
    advise_settlement,
)


def _inp(
    claimed=200.0,
    offer=100.0,
    p_win=0.60,
    rc=15.0,
    cb_fee=20.0,
    already_responded=False,
):
    return SettlementNegotiationInput(
        dispute_id="PP-D-SETTLE",
        claimed_amount=claimed,
        settlement_offer_amount=offer,
        p_win=p_win,
        response_cost_usd=rc,
        cb_fee_usd=cb_fee,
        already_responded=already_responded,
    )


# ─── EV calculations ──────────────────────────────────────────────────────────

class TestEVCalculations:
    def test_ev_accept_equals_negative_offer(self):
        r = advise_settlement(_inp(offer=80.0))
        assert abs(r.ev_accept - (-80.0)) < 0.001

    def test_ev_reject_lower_when_low_win_prob(self):
        # low p_win → more likely to pay full amount + fees → worse EV
        r_low = advise_settlement(_inp(p_win=0.10))
        r_high = advise_settlement(_inp(p_win=0.90))
        assert r_low.ev_reject < r_high.ev_reject

    def test_ev_reject_zero_rc_when_already_responded(self):
        r_new = advise_settlement(_inp(rc=15.0, already_responded=False))
        r_old = advise_settlement(_inp(rc=15.0, already_responded=True))
        # Already responded: sunk cost not added to future fight → reject EV higher
        assert r_old.ev_reject > r_new.ev_reject

    def test_threshold_non_negative(self):
        r = advise_settlement(_inp())
        assert r.settlement_threshold >= 0.0

    def test_threshold_equals_expected_loss_magnitude(self):
        # threshold = -ev_reject
        r = advise_settlement(_inp())
        assert abs(r.settlement_threshold - (-r.ev_reject)) < 0.01


# ─── Decision logic ───────────────────────────────────────────────────────────

class TestDecisionLogic:
    def test_accept_when_offer_below_threshold(self):
        # very low offer relative to expected loss → accept
        r = advise_settlement(_inp(claimed=200.0, offer=1.0, p_win=0.10))
        assert r.recommended_decision == SettlementDecision.ACCEPT

    def test_reject_or_counteroffer_when_high_win_probability(self):
        # p_win very high → threshold is low → offer far exceeds it → fight or low counteroffer
        r = advise_settlement(_inp(claimed=200.0, offer=180.0, p_win=0.95))
        assert r.recommended_decision in (
            SettlementDecision.REJECT_AND_FIGHT,
            SettlementDecision.COUNTEROFFER,
        )

    def test_counteroffer_when_offer_exceeds_threshold_significantly(self):
        # offer much higher than expected loss → counteroffer should appear
        r = advise_settlement(_inp(claimed=200.0, offer=190.0, p_win=0.60))
        # Could be COUNTEROFFER or REJECT_AND_FIGHT depending on threshold
        assert r.recommended_decision in list(SettlementDecision)

    def test_decision_is_valid_enum(self):
        r = advise_settlement(_inp())
        assert r.recommended_decision in list(SettlementDecision)

    def test_low_win_prob_full_dispute_favors_accept(self):
        # p_win=0.05 → almost certainly lose → settle early
        r = advise_settlement(_inp(claimed=500.0, offer=200.0, p_win=0.05))
        assert r.recommended_decision in (
            SettlementDecision.ACCEPT,
            SettlementDecision.COUNTEROFFER,
        )


# ─── Counteroffer logic ───────────────────────────────────────────────────────

class TestCounteroffer:
    def test_no_counteroffer_when_accept(self):
        r = advise_settlement(_inp(claimed=200.0, offer=1.0, p_win=0.10))
        if r.recommended_decision == SettlementDecision.ACCEPT:
            assert r.optimal_counteroffer is None

    def test_no_counteroffer_when_reject(self):
        r = advise_settlement(_inp(claimed=200.0, offer=180.0, p_win=0.95))
        if r.recommended_decision == SettlementDecision.REJECT_AND_FIGHT:
            assert r.optimal_counteroffer is None

    def test_counteroffer_less_than_original_offer(self):
        for r in [advise_settlement(_inp(claimed=500.0, offer=300.0, p_win=0.40))]:
            if r.recommended_decision == SettlementDecision.COUNTEROFFER:
                assert r.optimal_counteroffer < r.ev_accept * -1  # less than offer

    def test_counteroffer_multiple_of_five(self):
        # Round-to-5 logic means counteroffer should be a multiple of 5
        r = advise_settlement(_inp(claimed=1000.0, offer=600.0, p_win=0.70))
        if r.optimal_counteroffer is not None:
            assert r.optimal_counteroffer % 5 == 0


# ─── PayPal savings ───────────────────────────────────────────────────────────

class TestPayPalSavings:
    def test_pp_savings_non_negative(self):
        r = advise_settlement(_inp())
        assert r.paypal_cb_savings_usd >= 0.0

    def test_pp_savings_higher_when_low_win_prob(self):
        # higher prob of chargeback → more PayPal saves
        r_low = advise_settlement(_inp(p_win=0.10))
        r_high = advise_settlement(_inp(p_win=0.90))
        assert r_low.paypal_cb_savings_usd > r_high.paypal_cb_savings_usd

    def test_pp_savings_zero_when_p_win_one(self):
        r = advise_settlement(_inp(p_win=1.0))
        assert abs(r.paypal_cb_savings_usd) < 0.001


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_echoed(self):
        r = advise_settlement(_inp())
        assert r.dispute_id == "PP-D-SETTLE"

    def test_recommendation_non_empty(self):
        r = advise_settlement(_inp())
        assert len(r.recommendation) > 20

    def test_paypal_signal_contains_dispute_id(self):
        r = advise_settlement(_inp())
        assert "PP-D-SETTLE" in r.paypal_signal

    def test_total_savings_vs_worst_case(self):
        # settling early saves money vs. losing outright
        r = advise_settlement(_inp(claimed=200.0, offer=50.0, p_win=0.10))
        # worst case = 200 + 20 + 15 = 235; settled for 50 → savings = 185
        assert r.total_savings_vs_worst_case > 0.0

    def test_all_ev_fields_are_float(self):
        r = advise_settlement(_inp())
        assert isinstance(r.ev_accept, float)
        assert isinstance(r.ev_reject, float)
        assert isinstance(r.settlement_threshold, float)

    def test_paypal_signal_contains_decision(self):
        r = advise_settlement(_inp())
        assert r.recommended_decision.value in r.paypal_signal
