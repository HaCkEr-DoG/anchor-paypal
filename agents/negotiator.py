"""
Settlement Negotiation Engine — the third path nobody uses.

Most dispute tools offer two choices: fight or accept. PayPal has a third
action — `make-offer` — that virtually no tool uses. This module uses it.

Why partial settlement is often the best play:
  - Win probability 35-70%: too risky to fight confidently
  - Fighting costs: dispute fee ($15-30) + merchant's time (1-2h) + rate impact
  - Full refund costs: 100% of dispute amount
  - Settlement at 40%: costs less than full refund AND avoids dispute fee AND
    the dispute is marked RESOLVED (not counted the same way in rate calc
    during the offer-acceptance period)
  - Buyer psychology: a prompt offer often settles disputes before they
    escalate to formal chargebacks

The EV model:
  EV(fight)      = P(win) × amount − dispute_fee − time_cost
  EV(accept)     = −amount
  EV(negotiate)  = −offer_amount − small_overhead
  Best path      = max(EV(fight), EV(accept), EV(negotiate))

When EV(negotiate) > EV(fight): settle.
When EV(negotiate) > EV(accept): settle rather than giving full refund.

Offer amount calibration:
  - win_prob 65-70%:  30% offer  (lean toward merchant — buyer likely exaggerating)
  - win_prob 50-65%:  50% offer  (true uncertainty — split)
  - win_prob 35-50%:  65% offer  (buyer likely right, but keep some)
  - Below 35%:        full refund via accept (don't waste the offer overhead)
  - Above 70%:        fight (evidence strong, winning likely)
"""

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from oracle.decision import ClaimType, PAYPAL_DISPUTE_FEE_STANDARD, PAYPAL_DISPUTE_FEE_HIGH_VOLUME

# Below this dollar amount negotiation overhead isn't worth it
# ($30 minimum offer to clear the dispute fee and leave meaningful savings)
MINIMUM_NEGOTIATION_AMOUNT = 30.0


@dataclass
class NegotiationResult:
    should_negotiate: bool
    offer_fraction: float          # 0.0–1.0 fraction of dispute amount to offer
    offer_amount: float            # dollar amount to offer
    ev_negotiate: float            # expected value of negotiation path
    ev_fight: float
    ev_accept: float
    reasoning: str
    # PayPal API context
    make_offer_available: bool     # whether HATEOAS link is present


def _age_adjusted_fraction(base_fraction: float, dispute_age_days: int) -> float:
    """
    Older disputes need a slightly higher offer — the buyer is more frustrated
    and PayPal scrutinises delayed responses more heavily.

    Age adjustments (additive, capped to keep offers below 1.0):
      < 3 days  : no change
      3–7 days  : +3%
      8–14 days : +6%
      > 14 days : +10%
    """
    if dispute_age_days < 3:
        return base_fraction
    if dispute_age_days < 8:
        return min(base_fraction + 0.03, 0.90)
    if dispute_age_days < 15:
        return min(base_fraction + 0.06, 0.90)
    return min(base_fraction + 0.10, 0.90)


def evaluate_negotiation(
    dispute_amount: float,
    win_probability: float,
    claim_type: ClaimType,
    ev_fight: float,
    high_volume_tier: bool = False,
    dispute_age_days: int = 0,
    *,
    merchant_time_value_per_hour: float = 50.0,
    hours_to_fight: float = 1.5,
) -> NegotiationResult:
    """
    Evaluate whether a partial settlement offer is EV-positive.

    Parameters
    ----------
    dispute_amount          : full dispute amount in USD
    win_probability         : Oracle's win probability (0.0–1.0)
    claim_type              : INR, SNAD, UNAUTHORIZED, BILLING
    ev_fight                : Oracle's computed EV(fight)
    high_volume_tier        : whether the merchant is in HDR ($30 fee)
    merchant_time_value     : merchant's hourly rate for opportunity cost
    hours_to_fight          : expected hours to fight this dispute
    """
    dispute_fee = PAYPAL_DISPUTE_FEE_HIGH_VOLUME if high_volume_tier else PAYPAL_DISPUTE_FEE_STANDARD
    time_cost = merchant_time_value_per_hour * hours_to_fight

    ev_accept = -dispute_amount

    # Hard floor: tiny disputes don't benefit from negotiation overhead
    if dispute_amount < MINIMUM_NEGOTIATION_AMOUNT:
        return NegotiationResult(
            should_negotiate=False,
            offer_fraction=0.0,
            offer_amount=0.0,
            ev_negotiate=ev_accept,
            ev_fight=ev_fight,
            ev_accept=ev_accept,
            reasoning=f"Dispute amount ${dispute_amount:.2f} < ${MINIMUM_NEGOTIATION_AMOUNT:.0f} minimum — accept directly.",
            make_offer_available=False,
        )

    # Determine base offer fraction then age-adjust
    base_fraction = _offer_fraction(win_probability, claim_type)
    offer_fraction = _age_adjusted_fraction(base_fraction, dispute_age_days) if base_fraction > 0 else 0.0

    if offer_fraction <= 0.0:
        # No offer — either fight (high win prob) or accept (very low win prob)
        return NegotiationResult(
            should_negotiate=False,
            offer_fraction=0.0,
            offer_amount=0.0,
            ev_negotiate=ev_accept,  # same as accept when no offer
            ev_fight=ev_fight,
            ev_accept=ev_accept,
            reasoning=_no_offer_reason(win_probability),
            make_offer_available=False,
        )

    offer_amount = round(dispute_amount * offer_fraction, 2)
    # EV(negotiate): we pay offer_amount with ~70% chance buyer accepts
    # If buyer rejects, we're back to fight/accept choice
    acceptance_prob = _estimate_acceptance_prob(offer_fraction, claim_type)
    ev_negotiate = -(offer_amount * acceptance_prob) + (1.0 - acceptance_prob) * ev_fight

    should_negotiate = (
        ev_negotiate > ev_fight
        and ev_negotiate > ev_accept
        and offer_amount > dispute_fee  # only worth it if above fee threshold
    )

    if not should_negotiate:
        # Negotiation not better than other paths — but still report calculation
        return NegotiationResult(
            should_negotiate=False,
            offer_fraction=offer_fraction,
            offer_amount=offer_amount,
            ev_negotiate=round(ev_negotiate, 2),
            ev_fight=round(ev_fight, 2),
            ev_accept=round(ev_accept, 2),
            reasoning=(
                f"EV(negotiate)=${ev_negotiate:.2f} not better than "
                f"EV(fight)=${ev_fight:.2f} — stick with Oracle recommendation."
            ),
            make_offer_available=False,
        )

    return NegotiationResult(
        should_negotiate=True,
        offer_fraction=offer_fraction,
        offer_amount=offer_amount,
        ev_negotiate=round(ev_negotiate, 2),
        ev_fight=round(ev_fight, 2),
        ev_accept=round(ev_accept, 2),
        reasoning=(
            f"EV(negotiate)=${ev_negotiate:.2f} > EV(fight)=${ev_fight:.2f} > EV(accept)=${ev_accept:.2f}. "
            f"Offer {offer_fraction:.0%} refund (${offer_amount:.2f}) — "
            f"estimated {acceptance_prob:.0%} chance buyer accepts. "
            f"Saves ${(dispute_amount - offer_amount):.2f} vs full refund."
        ),
        make_offer_available=False,  # checked by Guardian from HATEOAS links
    )


async def submit_offer(
    dispute_id: str,
    offer_amount: float,
    paypal_base: str,
    token: str,
    note: str = "",
) -> bool:
    """
    Submit a partial refund offer via PayPal's make-offer API.

    Returns True on success (200/204), False otherwise.
    """
    if not note:
        note = (
            f"We'd like to offer a partial refund of ${offer_amount:.2f} to resolve this dispute "
            f"quickly. We value your business and want to reach a fair resolution."
        )
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{paypal_base}/v1/customer/disputes/{dispute_id}/make-offer",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "note": note,
                    "offer_type": "REFUND",
                    "offer_amount": {
                        "currency_code": "USD",
                        "value": str(offer_amount),
                    },
                },
            )
        success = r.status_code in (200, 204)
        if not success:
            print(f"[Negotiator] make-offer failed for {dispute_id}: {r.status_code} {r.text[:200]}")
        return success
    except Exception as exc:
        print(f"[Negotiator] make-offer error for {dispute_id}: {exc}")
        return False


def _offer_fraction(win_prob: float, claim_type: ClaimType) -> float:
    """
    Determine what fraction of the dispute amount to offer.
    Returns 0.0 when negotiation is not appropriate.
    """
    # Unauthorized claims: never negotiate (bank-level fraud, negotiation looks bad)
    if claim_type == ClaimType.UNAUTHORIZED:
        return 0.0

    # Very high win probability: fight
    if win_prob >= 0.70:
        return 0.0

    # Very low win probability: just accept fully
    if win_prob < 0.35:
        return 0.0

    # Negotiation zone: 35-70%
    if win_prob >= 0.65:
        return 0.30   # strong position — only offer 30% back
    if win_prob >= 0.50:
        return 0.50   # true uncertainty — 50/50
    return 0.65       # buyer likely right — offer 65% but not 100%


def _estimate_acceptance_prob(offer_fraction: float, claim_type: ClaimType) -> float:
    """
    Estimate probability buyer accepts the offer.
    Higher offer = higher acceptance. SNAD buyers more likely to accept than INR.
    """
    base = 0.50 + (offer_fraction - 0.30) * 0.60   # 0.30 offer → ~50%, 0.65 offer → ~71%
    if claim_type == ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED:
        base += 0.10   # SNAD buyers often want acknowledgment, not full refund
    return min(0.85, max(0.30, base))


def _no_offer_reason(win_prob: float) -> str:
    if win_prob >= 0.70:
        return f"Win probability {win_prob:.0%} ≥ 70% — fight directly, negotiation not needed."
    if win_prob < 0.35:
        return f"Win probability {win_prob:.0%} < 35% — accept directly, negotiation overhead not worth it."
    return "Claim type (UNAUTHORIZED) not suitable for partial settlement."
