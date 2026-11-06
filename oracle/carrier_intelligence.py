"""
Carrier Intelligence — carrier-specific dispute defense strategy.

The carrier a merchant uses determines how strong their INR (Item Not Received)
dispute defense is. PayPal's dispute team is more familiar with some carriers
than others, and certain carriers provide better delivery confirmation formats.

Key insight: PayPal's dispute reviewers trust USPS + UPS + FedEx tracking
pages much more than regional/economy carriers. Upgrading from an economy
carrier (DHL eCommerce, OnTrac, etc.) can flip a borderline INR dispute from
LOSS to WIN.

This module provides:
  1. Carrier-specific win rate modifiers for INR disputes
  2. Upgrade ROI calculator: is it worth paying more for a better carrier?
  3. Carrier evidence quality score (1-10)
  4. Shipping method recommendations per order value

Signature confirmation thresholds:
  - PayPal requires signature for ≥$750 to honor Seller Protection
  - Most carriers charge $3-6 for signature confirmation add-on

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CarrierTier(str, Enum):
    TIER_1 = "TIER_1"   # Best PayPal dispute support: USPS, UPS, FedEx
    TIER_2 = "TIER_2"   # Good: DHL Express (not DHL eCommerce), OnTrac, LaserShip
    TIER_3 = "TIER_3"   # Poor: Regional/economy, no real-time tracking
    UNKNOWN = "UNKNOWN"


class CarrierName(str, Enum):
    USPS         = "USPS"
    UPS          = "UPS"
    FEDEX        = "FEDEX"
    DHL_EXPRESS  = "DHL_EXPRESS"
    DHL_ECOMMERCE = "DHL_ECOMMERCE"
    ONTRAC       = "ONTRAC"
    LASERSHIP    = "LASERSHIP"
    PITNEY_BOWES = "PITNEY_BOWES"
    OTHER        = "OTHER"


_CARRIER_DATA: dict[CarrierName, dict] = {
    CarrierName.USPS: {
        "tier": CarrierTier.TIER_1,
        "inr_win_rate_modifier": +0.10,    # Additive to merchant's base win rate
        "evidence_quality": 9,              # 1-10
        "signature_available": True,
        "signature_cost": 3.45,
        "avg_transit_days": 3.5,
        "tracking_reliability": 0.97,
        "paypal_trust_note": "USPS tracking is fully trusted by PayPal reviewers",
    },
    CarrierName.UPS: {
        "tier": CarrierTier.TIER_1,
        "inr_win_rate_modifier": +0.12,
        "evidence_quality": 10,
        "signature_available": True,
        "signature_cost": 5.75,
        "avg_transit_days": 3.0,
        "tracking_reliability": 0.99,
        "paypal_trust_note": "UPS Proof of Delivery is PayPal's gold standard",
    },
    CarrierName.FEDEX: {
        "tier": CarrierTier.TIER_1,
        "inr_win_rate_modifier": +0.11,
        "evidence_quality": 10,
        "signature_available": True,
        "signature_cost": 5.50,
        "avg_transit_days": 2.5,
        "tracking_reliability": 0.99,
        "paypal_trust_note": "FedEx Delivery Confirmation photo is accepted by PayPal",
    },
    CarrierName.DHL_EXPRESS: {
        "tier": CarrierTier.TIER_2,
        "inr_win_rate_modifier": +0.05,
        "evidence_quality": 7,
        "signature_available": True,
        "signature_cost": 4.00,
        "avg_transit_days": 3.0,
        "tracking_reliability": 0.95,
        "paypal_trust_note": "DHL Express (not eCommerce) has real-time tracking trusted by PayPal",
    },
    CarrierName.DHL_ECOMMERCE: {
        "tier": CarrierTier.TIER_3,
        "inr_win_rate_modifier": -0.08,
        "evidence_quality": 4,
        "signature_available": False,
        "signature_cost": 0.0,
        "avg_transit_days": 7.0,
        "tracking_reliability": 0.80,
        "paypal_trust_note": "DHL eCommerce tracking is often incomplete — PayPal reviewers flag this",
    },
    CarrierName.ONTRAC: {
        "tier": CarrierTier.TIER_2,
        "inr_win_rate_modifier": +0.02,
        "evidence_quality": 6,
        "signature_available": True,
        "signature_cost": 4.50,
        "avg_transit_days": 3.5,
        "tracking_reliability": 0.90,
        "paypal_trust_note": "OnTrac acceptable but less familiar to PayPal than USPS/UPS/FedEx",
    },
    CarrierName.LASERSHIP: {
        "tier": CarrierTier.TIER_3,
        "inr_win_rate_modifier": -0.05,
        "evidence_quality": 4,
        "signature_available": False,
        "signature_cost": 0.0,
        "avg_transit_days": 3.0,
        "tracking_reliability": 0.82,
        "paypal_trust_note": "LaserShip has poor delivery confirmation — weak INR defense",
    },
    CarrierName.PITNEY_BOWES: {
        "tier": CarrierTier.TIER_3,
        "inr_win_rate_modifier": -0.07,
        "evidence_quality": 3,
        "signature_available": False,
        "signature_cost": 0.0,
        "avg_transit_days": 8.0,
        "tracking_reliability": 0.78,
        "paypal_trust_note": "Pitney Bowes (Newgistics) tracking is often delayed — high INR risk",
    },
    CarrierName.OTHER: {
        "tier": CarrierTier.UNKNOWN,
        "inr_win_rate_modifier": -0.03,
        "evidence_quality": 5,
        "signature_available": False,
        "signature_cost": 0.0,
        "avg_transit_days": 5.0,
        "tracking_reliability": 0.85,
        "paypal_trust_note": "Unknown carrier — PayPal may not accept tracking as evidence",
    },
}


@dataclass
class CarrierInput:
    """Input for carrier intelligence analysis."""
    order_id: str
    carrier: CarrierName
    order_amount: float
    merchant_base_inr_win_rate: float = 0.55   # Merchant's base INR win rate
    fight_cost: float = 25.0
    current_signature: bool = False


@dataclass
class CarrierUpgradeOption:
    """One upgrade option from current carrier to a better one."""
    target_carrier: CarrierName
    target_tier: CarrierTier
    win_rate_improvement: float               # Additive
    shipping_cost_premium: float              # $ extra per shipment
    roi_breakeven_dispute_rate: float         # Minimum dispute rate to justify upgrade
    is_worth_upgrading: bool
    rationale: str


@dataclass
class CarrierIntelReport:
    """
    Complete carrier intelligence for one order/shipment.
    """
    order_id: str
    carrier: CarrierName
    carrier_tier: CarrierTier
    evidence_quality: int               # 1-10
    inr_win_rate_with_carrier: float    # Base + modifier
    tracking_reliability: float
    signature_available: bool
    signature_recommended: bool         # True if order ≥ $750 or win rate benefit
    signature_cost: float
    paypal_trust_note: str

    # Upgrade analysis
    upgrade_options: list[CarrierUpgradeOption]
    best_upgrade: Optional[CarrierUpgradeOption]
    should_upgrade: bool

    # EV comparison
    ev_fight_current: float
    ev_fight_best_carrier: float
    ev_upgrade_improvement: float

    summary: str


def _win_rate(carrier: CarrierName, base_rate: float, has_signature: bool) -> float:
    data = _CARRIER_DATA.get(carrier, _CARRIER_DATA[CarrierName.OTHER])
    rate = base_rate + data["inr_win_rate_modifier"]
    if has_signature:
        rate += 0.08   # Signature confirmation adds ~8pp win rate
    return round(max(0.05, min(0.95, rate)), 3)


def _upgrade_roi(
    current_win_rate: float,
    target_carrier: CarrierName,
    base_rate: float,
    order_amount: float,
    fight_cost: float,
    signature: bool,
    shipping_premium: float,
) -> CarrierUpgradeOption:
    """Compute ROI for upgrading to target carrier."""
    target_win_rate = _win_rate(target_carrier, base_rate, signature)
    win_rate_delta = target_win_rate - current_win_rate
    ev_improvement_per_dispute = win_rate_delta * order_amount

    # Break-even: at what dispute rate does the EV improvement justify the shipping premium?
    # shipping_premium per shipment = ev_improvement × dispute_rate
    # dispute_rate = shipping_premium / ev_improvement
    if ev_improvement_per_dispute > 0:
        breakeven_rate = round(shipping_premium / ev_improvement_per_dispute, 4)
    else:
        breakeven_rate = 1.0  # Never worth it

    is_worth = win_rate_delta > 0.05 and breakeven_rate < 0.03  # <3% dispute rate breaks even

    target_data = _CARRIER_DATA.get(target_carrier, _CARRIER_DATA[CarrierName.OTHER])
    if is_worth:
        rationale = (
            f"Upgrade to {target_carrier.value} adds {win_rate_delta:.0%} INR win rate "
            f"(EV+${ev_improvement_per_dispute:+.0f}/dispute). "
            f"Break-even at {breakeven_rate:.1%} dispute rate."
        )
    else:
        rationale = (
            f"{target_carrier.value} only adds {win_rate_delta:.0%} win rate — "
            f"not worth ${shipping_premium:.0f} shipping premium at typical dispute rates."
        )

    return CarrierUpgradeOption(
        target_carrier=target_carrier,
        target_tier=target_data["tier"],
        win_rate_improvement=round(win_rate_delta, 3),
        shipping_cost_premium=shipping_premium,
        roi_breakeven_dispute_rate=breakeven_rate,
        is_worth_upgrading=is_worth,
        rationale=rationale,
    )


def analyze_carrier(inp: CarrierInput) -> CarrierIntelReport:
    """
    Analyze carrier choice for one order and recommend upgrades if beneficial.
    """
    data = _CARRIER_DATA.get(inp.carrier, _CARRIER_DATA[CarrierName.OTHER])
    current_win_rate = _win_rate(inp.carrier, inp.merchant_base_inr_win_rate, inp.current_signature)
    sig_recommended = (
        inp.order_amount >= 750.0
        or (data["signature_available"] and inp.order_amount >= 300.0 and current_win_rate < 0.60)
    )

    ev_current = round(current_win_rate * inp.order_amount - inp.fight_cost, 2)

    # Generate upgrade options (only for Tier 2/3 carriers)
    upgrade_options: list[CarrierUpgradeOption] = []
    if data["tier"] in (CarrierTier.TIER_2, CarrierTier.TIER_3, CarrierTier.UNKNOWN):
        # Approximate shipping premiums vs current carrier
        tier3_premium = {
            CarrierName.USPS: 1.50,
            CarrierName.UPS: 3.00,
            CarrierName.FEDEX: 2.50,
        }
        tier2_premium = {
            CarrierName.USPS: 0.50,
            CarrierName.UPS: 1.50,
            CarrierName.FEDEX: 1.25,
        }
        premiums = tier3_premium if data["tier"] == CarrierTier.TIER_3 else tier2_premium
        for target, premium in premiums.items():
            if CarrierName(target) != inp.carrier:
                opt = _upgrade_roi(
                    current_win_rate,
                    CarrierName(target),
                    inp.merchant_base_inr_win_rate,
                    inp.order_amount,
                    inp.fight_cost,
                    inp.current_signature or sig_recommended,
                    premium,
                )
                upgrade_options.append(opt)

    best_upgrade = None
    if upgrade_options:
        worthwhile = [o for o in upgrade_options if o.is_worth_upgrading]
        if worthwhile:
            best_upgrade = max(worthwhile, key=lambda o: o.win_rate_improvement)

    best_carrier_win_rate = _win_rate(
        best_upgrade.target_carrier if best_upgrade else inp.carrier,
        inp.merchant_base_inr_win_rate,
        inp.current_signature or sig_recommended,
    )
    ev_best = round(best_carrier_win_rate * inp.order_amount - inp.fight_cost, 2)
    ev_improvement = round(ev_best - ev_current, 2)

    summary = (
        f"Carrier {inp.order_id}: {inp.carrier.value} tier={data['tier'].value} "
        f"quality={data['evidence_quality']}/10 | "
        f"INR WR={current_win_rate:.0%} | "
        f"EV=${ev_current:+.0f} | "
        f"upgrade={'YES' if best_upgrade else 'NO'}"
    )

    return CarrierIntelReport(
        order_id=inp.order_id,
        carrier=inp.carrier,
        carrier_tier=data["tier"],
        evidence_quality=data["evidence_quality"],
        inr_win_rate_with_carrier=current_win_rate,
        tracking_reliability=data["tracking_reliability"],
        signature_available=data["signature_available"],
        signature_recommended=sig_recommended,
        signature_cost=data["signature_cost"],
        paypal_trust_note=data["paypal_trust_note"],
        upgrade_options=upgrade_options,
        best_upgrade=best_upgrade,
        should_upgrade=best_upgrade is not None,
        ev_fight_current=ev_current,
        ev_fight_best_carrier=ev_best,
        ev_upgrade_improvement=ev_improvement,
        summary=summary,
    )
