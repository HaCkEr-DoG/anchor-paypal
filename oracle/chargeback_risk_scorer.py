"""
Chargeback Risk Scorer — pre-transaction risk scoring to predict
whether a given transaction is likely to result in a chargeback.

Used BEFORE a dispute is filed, to flag high-risk transactions at time
of fulfillment or authorization, allowing the merchant to take
preventive action (require additional verification, delay fulfillment,
flag for manual review).

Risk factors:
  1. Order velocity — multiple orders in short window
  2. High-value first order — large first purchase from new account
  3. Shipping/billing address mismatch
  4. Email domain risk — free webmail vs. corporate email
  5. Device / IP reputation signals
  6. Category-specific risk — digital goods, travel, electronics
  7. Card BIN risk — prepaid, virtual, or international cards
  8. Time-of-day risk — orders placed at unusual hours

Output:
  - RiskScore 0–100
  - RiskTier: VERY_HIGH / HIGH / MODERATE / LOW / MINIMAL
  - Triggered risk factors with weights
  - Recommended action: APPROVE / REVIEW / VERIFY / DECLINE

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskTier(str, Enum):
    VERY_HIGH = "VERY_HIGH"   # Score ≥ 75
    HIGH      = "HIGH"        # 55–74
    MODERATE  = "MODERATE"    # 35–54
    LOW       = "LOW"         # 15–34
    MINIMAL   = "MINIMAL"     # < 15


class RiskAction(str, Enum):
    DECLINE  = "DECLINE"   # Score ≥ 85 — do not fulfill
    VERIFY   = "VERIFY"    # 65–84 — require additional ID verification
    REVIEW   = "REVIEW"    # 40–64 — manual review before fulfillment
    APPROVE  = "APPROVE"   # < 40 — proceed normally


class ProductCategory(str, Enum):
    DIGITAL_GOODS = "DIGITAL_GOODS"   # Highest risk — instant delivery
    ELECTRONICS   = "ELECTRONICS"
    TRAVEL        = "TRAVEL"
    APPAREL       = "APPAREL"
    FOOD          = "FOOD"
    SERVICES      = "SERVICES"
    PHYSICAL_GENERAL = "PHYSICAL_GENERAL"


# Category base risk adjustments
_CATEGORY_RISK: dict[str, int] = {
    "DIGITAL_GOODS":   20,
    "ELECTRONICS":     12,
    "TRAVEL":          10,
    "APPAREL":          5,
    "FOOD":             2,
    "SERVICES":         5,
    "PHYSICAL_GENERAL": 0,
}

# Free webmail providers (high-risk — hard to verify identity)
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "protonmail.com", "mail.com", "ymail.com", "live.com",
    "msn.com", "inbox.com", "zoho.com", "gmx.com", "guerrillamail.com",
    "mailinator.com", "tempmail.com", "10minutemail.com",
}

# BIN prefixes associated with prepaid/virtual cards (simplified)
_HIGH_RISK_BIN_PREFIXES = {"4903", "4917", "4508", "4844", "4111"}


@dataclass
class ChargebackRiskInput:
    """Input for pre-transaction chargeback risk scoring."""
    transaction_id: str
    amount: float
    product_category: ProductCategory

    # Account signals
    account_age_days: int = 365
    account_prior_orders: int = 5
    account_prior_disputes: int = 0

    # Address signals
    billing_address_hash: str = ""
    shipping_address_hash: str = ""
    billing_shipping_match: bool = True

    # Email
    email_domain: str = ""       # e.g., "gmail.com"

    # Device / network
    ip_country_matches_billing: bool = True
    is_vpn_or_proxy: bool = False
    device_fingerprint_new: bool = False    # True = first time seeing this device

    # Card signals
    card_bin_prefix: str = ""   # First 4 digits
    is_international_card: bool = False
    is_prepaid_card: bool = False

    # Time signals
    order_hour_utc: int = 12   # 0–23

    # Velocity (orders in last 24h from this account)
    orders_last_24h: int = 1


def _category_score(cat: ProductCategory) -> int:
    return _CATEGORY_RISK.get(cat.value, 0)


def _account_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    if inp.account_age_days < 7:
        score += 20
        reasons.append(f"Very new account ({inp.account_age_days}d)")
    elif inp.account_age_days < 30:
        score += 12
        reasons.append(f"New account ({inp.account_age_days}d)")
    elif inp.account_age_days < 90:
        score += 5

    if inp.account_prior_disputes >= 3:
        score += 25
        reasons.append(f"Account has {inp.account_prior_disputes} prior disputes")
    elif inp.account_prior_disputes >= 1:
        score += 12
        reasons.append(f"Account has prior disputes")

    if inp.account_prior_orders == 0:
        score += 10
        reasons.append("First order from this account")

    return score, reasons


def _address_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    if not inp.billing_shipping_match and inp.billing_address_hash and inp.shipping_address_hash:
        if inp.amount >= 100.0:
            score += 15
            reasons.append("Billing/shipping address mismatch on high-value order")
        else:
            score += 8
            reasons.append("Billing/shipping address mismatch")
    return score, reasons


def _email_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    domain = inp.email_domain.lower().strip()
    if domain in _FREE_EMAIL_DOMAINS:
        score += 5    # Slight signal — most fraud uses free email, but so does most legit
    # Disposable/temp email is very high risk
    if any(x in domain for x in ("mailinator", "guerrillamail", "tempmail", "10minute")):
        score += 20
        reasons.append(f"Disposable email domain: {domain}")
    return score, reasons


def _device_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    if inp.is_vpn_or_proxy:
        score += 15
        reasons.append("VPN/proxy detected")
    if not inp.ip_country_matches_billing:
        score += 10
        reasons.append("IP country does not match billing country")
    if inp.device_fingerprint_new and inp.amount >= 200.0:
        score += 8
        reasons.append("New device fingerprint on high-value order")
    return score, reasons


def _card_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    if inp.is_prepaid_card:
        score += 15
        reasons.append("Prepaid card — cannot reliably link to identity")
    elif inp.card_bin_prefix in _HIGH_RISK_BIN_PREFIXES:
        score += 10
        reasons.append(f"High-risk BIN prefix {inp.card_bin_prefix}")
    if inp.is_international_card and not inp.ip_country_matches_billing:
        score += 10
        reasons.append("International card with IP country mismatch")
    return score, reasons


def _velocity_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    if inp.orders_last_24h >= 5:
        score += 20
        reasons.append(f"{inp.orders_last_24h} orders in last 24h — high velocity")
    elif inp.orders_last_24h >= 3:
        score += 10
        reasons.append(f"{inp.orders_last_24h} orders in last 24h")
    return score, reasons


def _time_score(inp: ChargebackRiskInput) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    # Overnight orders (2–5 AM UTC) slightly elevated
    if 2 <= inp.order_hour_utc <= 5:
        score += 5
        reasons.append(f"Order at unusual hour (UTC {inp.order_hour_utc}:00)")
    return score, reasons


def _tier_from_score(score: int) -> RiskTier:
    if score >= 75:
        return RiskTier.VERY_HIGH
    if score >= 55:
        return RiskTier.HIGH
    if score >= 35:
        return RiskTier.MODERATE
    if score >= 15:
        return RiskTier.LOW
    return RiskTier.MINIMAL


def _action_from_score(score: int) -> RiskAction:
    if score >= 85:
        return RiskAction.DECLINE
    if score >= 65:
        return RiskAction.VERIFY
    if score >= 40:
        return RiskAction.REVIEW
    return RiskAction.APPROVE


@dataclass
class RiskFactor:
    name: str
    score_contribution: int
    description: str


@dataclass
class ChargebackRiskReport:
    """Pre-transaction chargeback risk report."""
    transaction_id: str
    risk_score: int               # 0–100
    risk_tier: RiskTier
    recommended_action: RiskAction
    risk_factors: list[RiskFactor]
    top_reason: str
    summary: str


def score_chargeback_risk(inp: ChargebackRiskInput) -> ChargebackRiskReport:
    """Score a transaction's chargeback risk before fulfillment."""
    factors: list[RiskFactor] = []

    # Collect all sub-scores
    sub_checks = [
        ("Category",  _category_score(inp.product_category), []),
    ]

    # Build tuples (name, score, reasons) for account/address/etc.
    acct_s,   acct_r   = _account_score(inp)
    addr_s,   addr_r   = _address_score(inp)
    email_s,  email_r  = _email_score(inp)
    dev_s,    dev_r    = _device_score(inp)
    card_s,   card_r   = _card_score(inp)
    vel_s,    vel_r    = _velocity_score(inp)
    time_s,   time_r   = _time_score(inp)
    cat_s               = _category_score(inp.product_category)

    total = cat_s + acct_s + addr_s + email_s + dev_s + card_s + vel_s + time_s
    total = min(total, 100)

    # Build factor list (only non-zero)
    if cat_s:
        factors.append(RiskFactor("Category", cat_s,
                                   f"{inp.product_category.value} has elevated base risk"))
    for s, reasons in [
            (acct_s, acct_r), (addr_s, addr_r), (email_s, email_r),
            (dev_s, dev_r), (card_s, card_r), (vel_s, vel_r), (time_s, time_r)]:
        for reason in reasons:
            factors.append(RiskFactor(reason.split()[0], s, reason))

    top = max(factors, key=lambda f: f.score_contribution).description if factors else "No elevated risk factors"

    tier   = _tier_from_score(total)
    action = _action_from_score(total)

    summary = (
        f"Risk {inp.transaction_id}: "
        f"score={total} | tier={tier.value} | action={action.value}"
    )

    return ChargebackRiskReport(
        transaction_id=inp.transaction_id,
        risk_score=total,
        risk_tier=tier,
        recommended_action=action,
        risk_factors=factors,
        top_reason=top,
        summary=summary,
    )
