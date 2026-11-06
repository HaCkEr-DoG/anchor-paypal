"""
Order Risk Scorer — prevent disputes before they happen.

The only real "win" in dispute management is preventing the dispute entirely.
Every other oracle module activates AFTER a dispute exists. This module activates
at ORDER TIME, scoring each new order for dispute probability and recommending
preventive actions.

The key insight: 80% of disputes are predictable from order-time signals. A $450
order of electronics to a first-time buyer in a high-fraud-rate geographic area,
paid with a card that doesn't match the shipping address, shipped via USPS ground
to a rural route — this order will have a dispute rate 15× higher than a $45 order
of household goods to a repeat buyer in a low-risk area with standard UPS shipping.

If you know this at order time, you can:
  - Require signature confirmation proactively (instead of discovering you need it after)
  - Purchase shipping insurance before the item ships
  - Add extra photographic evidence of the packing and contents
  - Upgrade to a carrier with better tracking confirmation rates
  - Add a note to the order: extra documentation mode activated

This module scores 8 risk dimensions:
  1. Order value tier (higher value = higher dispute risk AND higher loss if disputed)
  2. Product category risk (electronics, luxury, gift cards = high risk)
  3. Geographic risk (buyer's city/state/country dispute rate patterns)
  4. Buyer relationship (new vs. repeat customer)
  5. Payment method signals (card vs. PayPal balance, billing/shipping address match)
  6. Carrier / shipping method risk (USPS rural = poor tracking confirmation)
  7. Seasonal risk (post-holiday, back-to-school = elevated dispute periods)
  8. Order composition risk (multiple items, unusually high discount)

Output: a 1–100 risk score and specific preventive actions to take NOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskLevel(str, Enum):
    LOW      = "LOW"      # Score 1–30: proceed normally
    MEDIUM   = "MEDIUM"   # Score 31–55: add signature confirmation + photos
    HIGH     = "HIGH"     # Score 56–75: insurance + signature + extra photos
    CRITICAL = "CRITICAL" # Score 76–100: manual review recommended


class ProductCategory(str, Enum):
    ELECTRONICS     = "ELECTRONICS"       # Risk × 2.5
    LUXURY_GOODS    = "LUXURY_GOODS"      # Risk × 2.0
    GIFT_CARDS      = "GIFT_CARDS"        # Risk × 3.0 (almost never defensible)
    GAMING_ACCOUNTS = "GAMING_ACCOUNTS"   # Risk × 2.8 (digital, no SP)
    APPAREL         = "APPAREL"           # Risk × 1.2
    HOUSEHOLD       = "HOUSEHOLD"         # Risk × 1.0 (baseline)
    AUTOMOTIVE      = "AUTOMOTIVE"        # Risk × 1.3
    HEALTH_BEAUTY   = "HEALTH_BEAUTY"     # Risk × 1.1
    DIGITAL_GOODS   = "DIGITAL_GOODS"     # Risk × 1.8 (no SP)
    COLLECTIBLES    = "COLLECTIBLES"      # Risk × 1.9 (SNAD-prone)
    JEWELRY         = "JEWELRY"           # Risk × 2.2


class CarrierRiskProfile(str, Enum):
    LOW    = "LOW"    # FedEx, UPS — excellent tracking, strong dispute evidence
    MEDIUM = "MEDIUM" # USPS Priority — good but rural routes can fail
    HIGH   = "HIGH"   # USPS First Class, ground rural — poor tracking confirmation
    VERY_HIGH = "VERY_HIGH"  # No tracking / cash carrier / international untracked


@dataclass
class OrderSignals:
    """All signals available at order time."""
    order_value: float
    product_category: ProductCategory
    buyer_prior_orders_with_merchant: int = 0  # 0 = first-time buyer
    buyer_prior_disputes_with_merchant: int = 0
    billing_country: str = "US"
    billing_zip: str = ""
    shipping_zip: str = ""
    billing_matches_shipping: bool = True
    carrier: str = ""                          # e.g. "USPS", "FedEx", "UPS", "DHL"
    is_rural_route: bool = False
    carrier_risk: CarrierRiskProfile = CarrierRiskProfile.MEDIUM
    is_holiday_season: bool = False            # Nov–Jan
    days_since_last_order_this_buyer: int = 365
    payment_is_paypal_balance: bool = False    # Balance = lower chargeback risk
    discount_percent: float = 0.0             # Heavy discount orders = higher risk
    is_international: bool = False


@dataclass
class OrderRiskAssessment:
    risk_score: int                   # 1–100
    risk_level: RiskLevel
    dispute_probability: float        # Estimated probability this order disputes
    preventive_actions: list[str]     # Ordered by impact
    evidence_checklist: list[str]     # Specific evidence to gather NOW
    routing_recommendation: str       # Carrier/shipping recommendation
    insurance_recommended: bool
    manual_review_flag: bool
    risk_drivers: list[str]           # What's driving the score
    reasoning: list[str] = field(default_factory=list)


# Product category base risk scores (0–40 range)
_CATEGORY_RISK = {
    ProductCategory.GIFT_CARDS:      38,
    ProductCategory.GAMING_ACCOUNTS: 35,
    ProductCategory.ELECTRONICS:     28,
    ProductCategory.LUXURY_GOODS:    25,
    ProductCategory.JEWELRY:         24,
    ProductCategory.COLLECTIBLES:    22,
    ProductCategory.DIGITAL_GOODS:   20,
    ProductCategory.AUTOMOTIVE:      14,
    ProductCategory.APPAREL:         12,
    ProductCategory.HEALTH_BEAUTY:   10,
    ProductCategory.HOUSEHOLD:        8,
}

# High-risk zip code prefixes (US only — areas with elevated dispute/theft rates)
_HIGH_RISK_ZIP_PREFIXES = {
    "100", "104",  # NYC metro
    "900", "902",  # LA metro
    "601", "606",  # Chicago
    "770",         # Houston
    "331", "330",  # Miami
}


def score_order_risk(signals: OrderSignals) -> OrderRiskAssessment:
    """
    Score an order's dispute risk at the moment of purchase.

    Returns risk level + specific preventive actions to take BEFORE shipping.
    The preventive actions are ordered by impact — take the first 2–3 for
    medium risk, all of them for critical risk.
    """
    reasoning = []
    risk_drivers = []
    score = 0

    # ── 1. Product category risk (0–38 points) ───────────────────────────────
    cat_risk = _CATEGORY_RISK.get(signals.product_category, 10)
    score += cat_risk
    if cat_risk >= 20:
        risk_drivers.append(
            f"High-risk category: {signals.product_category.value} (+{cat_risk} pts)"
        )

    # ── 2. Order value risk (0–20 points) ────────────────────────────────────
    if signals.order_value >= 1000:
        val_risk = 20
    elif signals.order_value >= 500:
        val_risk = 15
    elif signals.order_value >= 200:
        val_risk = 10
    elif signals.order_value >= 100:
        val_risk = 5
    else:
        val_risk = 2
    score += val_risk
    if val_risk >= 10:
        risk_drivers.append(f"High order value: ${signals.order_value:.0f} (+{val_risk} pts)")

    # ── 3. Buyer relationship (0–20 points) ──────────────────────────────────
    if signals.buyer_prior_disputes_with_merchant >= 2:
        buyer_risk = 20
        risk_drivers.append(f"Buyer has {signals.buyer_prior_disputes_with_merchant} prior disputes with us")
    elif signals.buyer_prior_disputes_with_merchant == 1:
        buyer_risk = 12
        risk_drivers.append("Buyer filed 1 prior dispute with us")
    elif signals.buyer_prior_orders_with_merchant == 0:
        buyer_risk = 10
        risk_drivers.append("First-time buyer — no relationship history")
    elif signals.buyer_prior_orders_with_merchant < 3:
        buyer_risk = 5
    else:
        buyer_risk = 0
    score += buyer_risk

    # ── 4. Geographic risk (0–15 points) ─────────────────────────────────────
    from oracle.buyer_intelligence import _COUNTRY_RISK_MULTIPLIER, _ELEVATED_RISK_COUNTRIES
    country_mult = _COUNTRY_RISK_MULTIPLIER.get(signals.billing_country, 1.0)
    geo_risk = int((country_mult - 1.0) * 10)
    if signals.billing_country in _ELEVATED_RISK_COUNTRIES:
        geo_risk = max(geo_risk, 8)
        risk_drivers.append(f"High-risk billing country: {signals.billing_country}")
    if signals.is_international:
        geo_risk += 5
        risk_drivers.append("International shipping — higher dispute risk")
    zip_prefix = signals.shipping_zip[:3] if len(signals.shipping_zip) >= 3 else ""
    if zip_prefix in _HIGH_RISK_ZIP_PREFIXES:
        geo_risk += 4
        risk_drivers.append(f"High-dispute-rate ZIP area ({signals.shipping_zip})")
    score += min(geo_risk, 15)

    # ── 5. Payment signals (0–12 points) ─────────────────────────────────────
    if not signals.billing_matches_shipping:
        score += 12
        risk_drivers.append("Billing address ≠ shipping address — card fraud signal")
    elif not signals.payment_is_paypal_balance:
        score += 3  # Card payment = slightly higher chargeback risk
    reasoning.append(
        f"Address match: {signals.billing_matches_shipping}, "
        f"PayPal balance: {signals.payment_is_paypal_balance}"
    )

    # ── 6. Carrier risk (0–10 points) ────────────────────────────────────────
    carrier_risk_pts = {
        CarrierRiskProfile.LOW: 0,
        CarrierRiskProfile.MEDIUM: 3,
        CarrierRiskProfile.HIGH: 7,
        CarrierRiskProfile.VERY_HIGH: 10,
    }
    carr_pts = carrier_risk_pts.get(signals.carrier_risk, 3)
    if signals.is_rural_route:
        carr_pts += 4
        risk_drivers.append("Rural route delivery — poor tracking confirmation rate")
    if carr_pts >= 7:
        risk_drivers.append(f"High-risk carrier profile ({signals.carrier or 'unknown carrier'})")
    score += min(carr_pts, 10)

    # ── 7. Seasonal risk (0–5 points) ────────────────────────────────────────
    if signals.is_holiday_season:
        score += 5
        risk_drivers.append("Holiday season — elevated dispute period (Nov–Jan)")

    # ── 8. Order composition risk (0–5 points) ───────────────────────────────
    if signals.discount_percent >= 50:
        score += 5
        risk_drivers.append(f"Heavy discount ({signals.discount_percent:.0f}%) — buyer may challenge authenticity")
    elif signals.discount_percent >= 25:
        score += 2

    # Clamp score
    score = min(score, 100)

    # ── Risk level ────────────────────────────────────────────────────────────
    if score >= 76:
        risk_level = RiskLevel.CRITICAL
        base_dispute_prob = 0.18
    elif score >= 56:
        risk_level = RiskLevel.HIGH
        base_dispute_prob = 0.09
    elif score >= 31:
        risk_level = RiskLevel.MEDIUM
        base_dispute_prob = 0.04
    else:
        risk_level = RiskLevel.LOW
        base_dispute_prob = 0.01

    dispute_prob = round(min(base_dispute_prob * (score / 50.0), 0.45), 3)

    # ── Preventive actions (ordered by impact) ───────────────────────────────
    actions = []
    evidence = []

    # Always at this level
    if risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        actions.append("Purchase shipping insurance before dispatch")
        actions.append("Require signature confirmation (adds to SP compliance and proves receipt)")
        evidence.append("Photograph item + packing materials before sealing box")
        evidence.append("Photograph sealed package with shipping label visible")
        evidence.append("Save high-resolution screenshot of order confirmation")

    if risk_level == RiskLevel.CRITICAL:
        actions.append("Upgrade carrier to FedEx or UPS for better tracking confirmation rate")
        actions.append("Consider manual order review before fulfillment")
        evidence.append("Record a short video of packing process (MP4 timestamp)")
        evidence.append("Print and retain PayPal transaction detail page")

    if risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
        actions.append("Save complete order details page with buyer's PayPal-confirmed address")
        evidence.append("Note carrier tracking number and URL immediately after label creation")

    if signals.product_category == ProductCategory.ELECTRONICS:
        evidence.append("Photograph serial number / IMEI on the device before shipping")
    if signals.product_category == ProductCategory.LUXURY_GOODS:
        evidence.append("Photograph authenticity certificates / tags included")
    if signals.product_category in (ProductCategory.GIFT_CARDS, ProductCategory.GAMING_ACCOUNTS):
        actions.insert(0, "CAUTION: Gift cards/gaming accounts have no Seller Protection — ship only to verified buyers")

    if signals.order_value > 750:
        actions.append(f"Signature confirmation is REQUIRED by PayPal SP for orders over $750")
    if signals.order_value > 500:
        evidence.append("Obtain and retain carrier delivery confirmation PDF after delivery")

    if not signals.billing_matches_shipping:
        actions.append("Verify order manually — billing ≠ shipping address is a card fraud signal")

    if risk_level == RiskLevel.LOW and not actions:
        actions.append("No special action needed — standard fulfillment process")
        evidence.append("Retain tracking number")

    # ── Carrier routing recommendation ────────────────────────────────────────
    if signals.order_value > 500 or risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        routing = "Use FedEx or UPS Ground/Express — superior tracking confirmation and dispute evidence vs USPS"
    elif signals.is_rural_route:
        routing = "Use FedEx or UPS if available for rural delivery — USPS rural routes have poor tracking"
    elif signals.order_value > 100:
        routing = "USPS Priority with tracking is acceptable — ensure delivery confirmation is enabled"
    else:
        routing = "Standard shipping method acceptable for this order"

    reasoning.append(f"Final score: {score}/100 → {risk_level.value}")

    return OrderRiskAssessment(
        risk_score=score,
        risk_level=risk_level,
        dispute_probability=dispute_prob,
        preventive_actions=actions,
        evidence_checklist=evidence,
        routing_recommendation=routing,
        insurance_recommended=risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL),
        manual_review_flag=risk_level == RiskLevel.CRITICAL,
        risk_drivers=risk_drivers,
        reasoning=reasoning,
    )


if __name__ == "__main__":
    # Scenario: $450 electronics order, first-time buyer, USPS ground
    result = score_order_risk(OrderSignals(
        order_value=450.0,
        product_category=ProductCategory.ELECTRONICS,
        buyer_prior_orders_with_merchant=0,
        carrier="USPS",
        carrier_risk=CarrierRiskProfile.MEDIUM,
        billing_matches_shipping=True,
        billing_country="US",
    ))
    print(f"Risk score: {result.risk_score}/100 ({result.risk_level.value})")
    print(f"Dispute probability: {result.dispute_probability:.1%}")
    print(f"Insurance: {result.insurance_recommended}")
    print(f"\nPreventive actions:")
    for a in result.preventive_actions:
        print(f"  → {a}")
    print(f"\nEvidence to collect now:")
    for e in result.evidence_checklist:
        print(f"  📸 {e}")
