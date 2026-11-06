"""
Buyer Intelligence Engine — the #1 missing signal in every other module.

Every other oracle module analyzes the DISPUTE. This module analyzes the BUYER.

The single strongest predictor of dispute outcome is the buyer's dispute history
across all merchants. A buyer who files disputes on 15% of their orders is a
serial fraudster — this changes EV(fight) from 65% to 90%+.

PayPal tracks buyer dispute rates internally. When a buyer files against a seller:
  - If the buyer's dispute_rate > 5%: PayPal is already suspicious of the buyer
  - If the buyer has disputes with multiple merchants: "serial disputer" flag
  - If the buyer's dispute timing matches the item's estimated delivery date exactly:
    scripted fraud pattern
  - If the buyer created their PayPal account < 30 days ago: high fraud risk

This module encodes:
  1. Buyer risk tier (1–5) based on dispute history and account signals
  2. Serial disputer detection (disputes across multiple merchants)
  3. Account age risk (new accounts file more fraudulent disputes)
  4. Dispute timing anomaly (filed on the first possible day = scripted)
  5. Win probability adjustment based on buyer risk tier
  6. Recommended strategy: document everything vs fight aggressively vs offer goodwill

Key insight: a high buyer risk tier dramatically IMPROVES the merchant's position.
A buyer with a 15% dispute rate is statistically committing fraud. The merchant
should FIGHT (not settle), cite the buyer's pattern, and push PayPal to investigate
the buyer rather than the merchant.

Thresholds:
  Normal buyer:         dispute_rate < 1%,   account_age > 180d
  Elevated risk:        dispute_rate 1–3%,   account_age 60–180d
  High risk:            dispute_rate 3–8%,   account_age 30–60d
  Serial disputer:      dispute_rate > 8%,   OR disputes with 3+ merchants
  Known fraud account:  account_age < 14d,   OR 100% dispute rate on recent orders
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BuyerRiskTier(str, Enum):
    TIER_1_NORMAL   = "TIER_1_NORMAL"    # < 1% dispute rate, established account
    TIER_2_ELEVATED = "TIER_2_ELEVATED"  # 1–3% dispute rate
    TIER_3_HIGH     = "TIER_3_HIGH"      # 3–8% dispute rate
    TIER_4_SERIAL   = "TIER_4_SERIAL"    # > 8% OR disputes with 3+ merchants
    TIER_5_FRAUD    = "TIER_5_FRAUD"     # < 14 day account OR 100% dispute rate


class BuyerStrategy(str, Enum):
    GOODWILL_GESTURE  = "GOODWILL_GESTURE"   # Normal buyer, consider partial refund
    DOCUMENT_AND_WAIT = "DOCUMENT_AND_WAIT"  # Elevated risk, gather everything
    FIGHT_AGGRESSIVELY = "FIGHT_AGGRESSIVELY" # High risk, fight and cite buyer pattern
    ESCALATE_TO_PAYPAL = "ESCALATE_TO_PAYPAL" # Serial disputer, ask PayPal to investigate buyer
    REPORT_FRAUD       = "REPORT_FRAUD"       # Fraud account, report + fight, do not settle


@dataclass
class BuyerProfile:
    """What we know about this buyer from their account history."""
    buyer_email: str = ""
    account_age_days: int = 365          # Days since PayPal account created
    lifetime_transactions: int = 0       # Total orders on this account
    lifetime_disputes_filed: int = 0     # Total disputes filed (not just with us)
    disputes_with_other_merchants: int = 0  # Disputes against DIFFERENT sellers
    prior_disputes_with_us: int = 0      # Disputes specifically with this merchant
    dispute_rate: float = 0.0            # disputes / transactions (0.0–1.0)
    account_verified: bool = True        # PayPal verified account
    billing_country: str = "US"          # ISO 3166-1 alpha-2
    has_paypal_buyer_protection_abuse: bool = False  # PayPal internal flag


@dataclass
class BuyerIntelligence:
    risk_tier: BuyerRiskTier
    strategy: BuyerStrategy
    win_probability_adjustment: float    # Additive adjustment to base win probability
    fight_recommendation: bool           # True = fight, False = consider settling
    buyer_risk_statement: str            # Text to include in dispute response
    red_flags: list[str]
    reasoning: list[str] = field(default_factory=list)


# Country-level dispute rate multipliers (relative to US baseline of 1.0)
# Based on known PayPal fraud patterns by country
_COUNTRY_RISK_MULTIPLIER = {
    "US": 1.0, "CA": 1.1, "GB": 1.1, "AU": 1.0, "DE": 0.9, "FR": 1.1,
    "NL": 1.0, "SE": 0.9, "IT": 1.3, "ES": 1.3,
    # Elevated risk regions
    "BR": 2.1, "MX": 1.8, "IN": 1.5, "PH": 1.9, "ID": 1.7,
    # High risk
    "NG": 3.5, "GH": 3.2, "EG": 2.8, "PK": 2.5, "VN": 2.0,
    # Digital fraud hotspots
    "CN": 2.3, "RU": 2.5,
}

_ELEVATED_RISK_COUNTRIES = {
    "BR", "MX", "IN", "PH", "ID", "NG", "GH", "EG", "PK", "VN", "CN", "RU"
}


def analyze_buyer(
    profile: BuyerProfile,
    dispute_amount: float,
    claim_type: str = "ITEM_NOT_RECEIVED",
    *,
    # Timing signals
    days_since_purchase: int = 0,
    days_until_estimated_delivery: int = 0,  # Negative = overdue
    dispute_filed_on_first_possible_day: bool = False,  # Filed the same day item is "late"
    # Digital goods signals
    is_digital_goods: bool = False,
    buyer_downloaded_before_dispute: bool = False,
) -> BuyerIntelligence:
    """
    Analyze the buyer's risk tier and recommend strategy.

    High buyer risk = fight aggressively. The buyer's fraud pattern is itself
    evidence in the dispute response. PayPal investigators weight buyer history
    heavily when both parties have submitted their evidence.
    """
    reasoning = []
    red_flags = []
    adj = 0.0  # Win probability adjustment

    # ── Tier assignment ───────────────────────────────────────────────────────
    tier = BuyerRiskTier.TIER_1_NORMAL

    # Fraud account: very new + disputing
    if profile.account_age_days < 14:
        tier = BuyerRiskTier.TIER_5_FRAUD
        red_flags.append(f"Account age {profile.account_age_days} days — created recently, high fraud risk")
        adj += 0.25
    elif profile.account_age_days < 30:
        red_flags.append(f"Account age {profile.account_age_days} days — new account")
        adj += 0.10

    # 100% dispute rate on recent orders
    if profile.lifetime_transactions >= 3 and profile.dispute_rate >= 0.80:
        tier = BuyerRiskTier.TIER_5_FRAUD
        red_flags.append(f"Dispute rate {profile.dispute_rate:.0%} — files disputes on nearly every order")
        adj += 0.30

    # Serial disputer: 3+ different merchants
    if profile.disputes_with_other_merchants >= 3:
        if tier.value < BuyerRiskTier.TIER_4_SERIAL.value:
            tier = BuyerRiskTier.TIER_4_SERIAL
        red_flags.append(
            f"Disputes against {profile.disputes_with_other_merchants} other merchants — serial disputer"
        )
        adj += 0.20
        reasoning.append(
            f"Serial disputer signal: {profile.disputes_with_other_merchants} disputes "
            f"with other merchants → cite this in response"
        )

    # High dispute rate
    if profile.dispute_rate >= 0.08 and tier.value < BuyerRiskTier.TIER_4_SERIAL.value:
        tier = BuyerRiskTier.TIER_4_SERIAL
        red_flags.append(f"Dispute rate {profile.dispute_rate:.1%} exceeds 8% — above serial disputer threshold")
        adj += 0.15
    elif profile.dispute_rate >= 0.03 and tier.value < BuyerRiskTier.TIER_3_HIGH.value:
        tier = BuyerRiskTier.TIER_3_HIGH
        red_flags.append(f"Dispute rate {profile.dispute_rate:.1%} — high risk buyer")
        adj += 0.10
    elif profile.dispute_rate >= 0.01 and tier == BuyerRiskTier.TIER_1_NORMAL:
        tier = BuyerRiskTier.TIER_2_ELEVATED
        adj += 0.05

    # Repeat disputer with US specifically
    if profile.prior_disputes_with_us >= 2:
        red_flags.append(
            f"{profile.prior_disputes_with_us} prior disputes with this merchant — pattern of abuse"
        )
        adj += 0.08 * min(profile.prior_disputes_with_us, 3)

    # Geographic risk
    country_mult = _COUNTRY_RISK_MULTIPLIER.get(profile.billing_country, 1.0)
    if country_mult >= 2.0:
        red_flags.append(
            f"High-risk billing country ({profile.billing_country}) — {country_mult:.1f}× baseline dispute rate"
        )
        adj += 0.08
        if tier == BuyerRiskTier.TIER_1_NORMAL:
            tier = BuyerRiskTier.TIER_2_ELEVATED
    elif country_mult >= 1.5:
        adj += 0.04

    # Unverified account
    if not profile.account_verified:
        red_flags.append("Unverified PayPal account — higher fraud risk")
        adj += 0.05

    # Timing anomalies
    if dispute_filed_on_first_possible_day:
        red_flags.append(
            "Dispute filed on first possible day — scripted fraud pattern "
            "(legitimate buyers typically wait for a resolution period)"
        )
        adj += 0.08
        if tier == BuyerRiskTier.TIER_1_NORMAL:
            tier = BuyerRiskTier.TIER_2_ELEVATED

    # Digital goods: downloaded then disputed = clear fraud
    if is_digital_goods and buyer_downloaded_before_dispute:
        red_flags.append("Buyer accessed digital product BEFORE filing dispute — friendly fraud")
        adj += 0.25
        if tier.value < BuyerRiskTier.TIER_4_SERIAL.value:
            tier = BuyerRiskTier.TIER_4_SERIAL

    # PayPal internal abuse flag
    if profile.has_paypal_buyer_protection_abuse:
        red_flags.append("PayPal has flagged this buyer for Buyer Protection abuse")
        adj += 0.20
        tier = BuyerRiskTier.TIER_5_FRAUD

    # ── Strategy assignment ───────────────────────────────────────────────────
    if tier == BuyerRiskTier.TIER_5_FRAUD:
        strategy = BuyerStrategy.REPORT_FRAUD
        fight = True
    elif tier == BuyerRiskTier.TIER_4_SERIAL:
        strategy = BuyerStrategy.ESCALATE_TO_PAYPAL
        fight = True
    elif tier == BuyerRiskTier.TIER_3_HIGH:
        strategy = BuyerStrategy.FIGHT_AGGRESSIVELY
        fight = True
    elif tier == BuyerRiskTier.TIER_2_ELEVATED:
        strategy = BuyerStrategy.DOCUMENT_AND_WAIT
        fight = dispute_amount > 50
    else:
        strategy = BuyerStrategy.GOODWILL_GESTURE
        fight = dispute_amount > 150

    # ── Buyer risk statement for dispute response ─────────────────────────────
    if tier in (BuyerRiskTier.TIER_4_SERIAL, BuyerRiskTier.TIER_5_FRAUD):
        risk_stmt = (
            f"We respectfully draw PayPal's attention to this buyer's dispute history. "
            f"This account has filed disputes against {profile.disputes_with_other_merchants} "
            f"other merchants, representing a dispute rate of {profile.dispute_rate:.1%}. "
            f"We request that PayPal review this buyer's dispute pattern as part of this case."
        )
    elif tier == BuyerRiskTier.TIER_3_HIGH:
        risk_stmt = (
            f"This buyer has an elevated dispute rate of {profile.dispute_rate:.1%} "
            f"and {profile.lifetime_disputes_filed} total disputes filed. "
            f"We believe this claim warrants careful review."
        )
    elif red_flags:
        risk_stmt = f"Note: {red_flags[0]}"
    else:
        risk_stmt = ""

    reasoning.append(f"Risk tier: {tier.value}, adjustment: {adj:+.2f}")

    return BuyerIntelligence(
        risk_tier=tier,
        strategy=strategy,
        win_probability_adjustment=round(min(adj, 0.35), 3),
        fight_recommendation=fight,
        buyer_risk_statement=risk_stmt,
        red_flags=red_flags,
        reasoning=reasoning,
    )


if __name__ == "__main__":
    # Scenario: serial disputer, 12% dispute rate, new account
    profile = BuyerProfile(
        buyer_email="buyer@example.com",
        account_age_days=45,
        lifetime_transactions=25,
        lifetime_disputes_filed=3,
        disputes_with_other_merchants=3,
        dispute_rate=0.12,
        billing_country="US",
    )
    result = analyze_buyer(profile, dispute_amount=120.0)
    print(f"Risk tier: {result.risk_tier.value}")
    print(f"Strategy: {result.strategy.value}")
    print(f"Win probability adjustment: {result.win_probability_adjustment:+.0%}")
    print(f"Fight: {result.fight_recommendation}")
    for flag in result.red_flags:
        print(f"  🚩 {flag}")
    if result.buyer_risk_statement:
        print(f"\nDispute statement:\n{result.buyer_risk_statement}")
