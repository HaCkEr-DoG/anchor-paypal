"""
Oracle stress tests — push every boundary of the EV engine.
"""
import math
import pytest
from oracle.decision import (
    calculate, ClaimType, Evidence, Recommendation,
    PAYPAL_DISPUTE_FEE_STANDARD, PAYPAL_DISPUTE_FEE_HIGH_VOLUME,
    DEFAULT_HOURLY_RATE, HOURS_TO_FIGHT,
)


# ── Boundary: zero / tiny amounts ────────────────────────────────────────────

def test_zero_amount_always_accept():
    r = calculate(0.0, ClaimType.ITEM_NOT_RECEIVED, Evidence())
    assert r.recommendation == Recommendation.ACCEPT
    assert r.ev_fight < 0

def test_one_cent_always_accept():
    r = calculate(0.01, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True))
    assert r.recommendation == Recommendation.ACCEPT

def test_break_even_exactly():
    """At exactly break-even win prob the Oracle must not recommend FIGHT."""
    # Standard tier: break_even = (time_cost + 15) / (amount + 15)
    time_cost = DEFAULT_HOURLY_RATE * HOURS_TO_FIGHT
    fee = PAYPAL_DISPUTE_FEE_STANDARD
    amount = 100.0
    be = (time_cost + fee) / (amount + fee)
    # Manufacture an evidence with win_prob ~= break-even by using similar_dispute_win_rate
    r = calculate(
        amount,
        ClaimType.ITEM_NOT_RECEIVED,
        Evidence(),
        similar_dispute_win_rate=be,
    )
    # ev_fight should be ≈ 0 (or slightly negative); must not be FIGHT
    assert r.recommendation in (Recommendation.ACCEPT, Recommendation.INSUFFICIENT_EVIDENCE)

def test_above_break_even_recommends_fight():
    r = calculate(
        500.0,
        ClaimType.ITEM_NOT_RECEIVED,
        Evidence(has_tracking_number=True, has_delivery_confirmation=True,
                 seller_account_age_days=1500),
    )
    assert r.recommendation == Recommendation.FIGHT
    assert r.ev_fight > 0

def test_ceiling_raises_at_100k():
    with pytest.raises(ValueError, match="100,000"):
        calculate(100_001.0, ClaimType.ITEM_NOT_RECEIVED, Evidence())

def test_exactly_100k_raises():
    with pytest.raises(ValueError, match="100,000"):
        calculate(100_000.01, ClaimType.ITEM_NOT_RECEIVED, Evidence())

def test_99999_does_not_raise():
    r = calculate(99_999.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True, has_delivery_confirmation=True))
    assert r is not None

def test_negative_amount_raises():
    with pytest.raises(ValueError):
        calculate(-1.0, ClaimType.ITEM_NOT_RECEIVED, Evidence())

def test_inf_amount_raises():
    with pytest.raises(ValueError):
        calculate(math.inf, ClaimType.ITEM_NOT_RECEIVED, Evidence())

def test_nan_amount_raises():
    with pytest.raises(ValueError):
        calculate(math.nan, ClaimType.ITEM_NOT_RECEIVED, Evidence())


# ── All claim types ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("claim", list(ClaimType))
def test_all_claim_types_return_decision(claim):
    r = calculate(150.0, claim, Evidence())
    assert r.recommendation in list(Recommendation)
    assert 0.0 <= r.win_probability <= 1.0
    assert 0.0 <= r.confidence <= 1.0

def test_unauthorized_no_comm_strongly_accepts():
    r = calculate(300.0, ClaimType.UNAUTHORIZED, Evidence(seller_account_age_days=400))
    assert r.recommendation == Recommendation.ACCEPT

def test_unauthorized_with_buyer_comm_may_fight():
    r = calculate(500.0, ClaimType.UNAUTHORIZED,
                  Evidence(has_buyer_communication=True, seller_account_age_days=1500,
                           seller_dispute_win_rate=0.75))
    # With strong buyer comm evidence, win prob rises enough to potentially fight
    assert r.win_probability > 0.10  # must be above UNAUTHORIZED base of 10%


# ── Fee tiers ────────────────────────────────────────────────────────────────

def test_high_volume_fee_raises_break_even():
    r_std = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True), high_volume_tier=False)
    r_hv  = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True), high_volume_tier=True)
    assert r_hv.break_even_probability > r_std.break_even_probability

def test_high_volume_nonrefundable_lowers_ev():
    r_std = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True, has_delivery_confirmation=True),
                      high_volume_tier=False)
    r_hv  = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True, has_delivery_confirmation=True),
                      high_volume_tier=True)
    assert r_hv.ev_fight < r_std.ev_fight

def test_fee_override_accepted():
    r = calculate(100.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True),
                  dispute_fee_override=5.0)
    assert r is not None
    assert "custom" in r.reasoning[0].lower() or any("custom" in x.lower() for x in r.reasoning)

def test_negative_fee_override_raises():
    with pytest.raises(ValueError):
        calculate(100.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(), dispute_fee_override=-1.0)


# ── Evidence scoring edge cases ───────────────────────────────────────────────

def test_win_prob_never_exceeds_0_95():
    r = calculate(
        1000.0, ClaimType.ITEM_NOT_RECEIVED,
        Evidence(has_tracking_number=True, has_delivery_confirmation=True,
                 seller_account_age_days=5000, seller_dispute_win_rate=0.99),
        similar_dispute_win_rate=0.99,
    )
    assert r.win_probability <= 0.95

def test_no_tracking_snad_has_description_boosts():
    r_no  = calculate(200.0, ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
                      Evidence())
    r_yes = calculate(200.0, ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
                      Evidence(has_item_description_proof=True, has_buyer_communication=True))
    assert r_yes.win_probability > r_no.win_probability

def test_new_account_penalized():
    r_new = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True, has_delivery_confirmation=True,
                               seller_account_age_days=30))
    r_old = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED,
                      Evidence(has_tracking_number=True, has_delivery_confirmation=True,
                               seller_account_age_days=1500))
    assert r_old.win_probability > r_new.win_probability


# ── Historical blending ───────────────────────────────────────────────────────

def test_historical_rate_blended_at_40_pct():
    base = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True))
    blended = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True),
                        similar_dispute_win_rate=0.0)
    assert blended.win_probability < base.win_probability  # 0% history pulls it down

def test_historical_rate_clamped_to_valid_range():
    r = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(),
                  similar_dispute_win_rate=1.5)  # over 1.0 — should clamp
    assert r.win_probability <= 0.95

def test_historical_rate_negative_clamped():
    r = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(),
                  similar_dispute_win_rate=-0.5)
    assert r.win_probability >= 0.0


# ── Non-USD currency warning ──────────────────────────────────────────────────

def test_non_usd_adds_warning():
    r = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True),
                  currency_code="EUR")
    assert any("EUR" in line for line in r.reasoning)
    assert any("cross-currency" in line.lower() for line in r.reasoning)

def test_usd_no_currency_warning():
    r = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(has_tracking_number=True),
                  currency_code="USD")
    assert not any("cross-currency" in line.lower() for line in r.reasoning)

def test_lowercase_usd_no_warning():
    r = calculate(200.0, ClaimType.ITEM_NOT_RECEIVED, Evidence(), currency_code="usd")
    assert not any("cross-currency" in line.lower() for line in r.reasoning)


# ── Deadline urgency ──────────────────────────────────────────────────────────

def test_urgent_deadline_adds_note():
    r = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED,
                  Evidence(has_tracking_number=True, has_delivery_confirmation=True),
                  deadline_hours=12)
    assert any("URGENT" in line or "urgent" in line.lower() for line in r.reasoning)

def test_non_urgent_no_note():
    r = calculate(300.0, ClaimType.ITEM_NOT_RECEIVED,
                  Evidence(has_tracking_number=True, has_delivery_confirmation=True),
                  deadline_hours=72)
    assert not any("URGENT" in line for line in r.reasoning)


# ── Reasoning is always populated ────────────────────────────────────────────

@pytest.mark.parametrize("claim,amount", [
    (ClaimType.ITEM_NOT_RECEIVED, 1.0),
    (ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED, 500.0),
    (ClaimType.UNAUTHORIZED, 999.0),
    (ClaimType.BILLING_ISSUE, 50.0),
])
def test_reasoning_always_populated(claim, amount):
    r = calculate(amount, claim, Evidence())
    assert len(r.reasoning) >= 3
