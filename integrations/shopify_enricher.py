"""
Shopify Order Enricher — bridge between Shopify's order/fulfillment data
and Anchor's oracle modules.

Sponsor: Shopify (plugin:small-business:shopify)

Shopify knows things oracle modules currently default to "UNKNOWN":
  - Delivery status (fulfillment_status, tracking_number, carrier)
  - Buyer LTV (total_spent, orders_count across all merchants)
  - Item category (product_type, tags)
  - Transaction amount vs. dispute amount match
  - Whether signature confirmation was purchased
  - Hours since purchase (created_at → now)
  - Express vs. standard shipping
  - Digital vs. physical product

This module converts a raw Shopify order dict (from webhook or API call)
into enriched inputs for every oracle module that needs real data.

Usage:
    order = fetch_shopify_order(order_id)   # from Shopify API
    ctx = enrich_from_shopify_order(order)
    # ctx.prevention_input, ctx.ltv_input, ctx.risk_input are all populated
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ─── Item category mapping ────────────────────────────────────────────────────

_CATEGORY_MAP = {
    "electronics": "ELECTRONICS",
    "computers": "ELECTRONICS",
    "phones": "ELECTRONICS",
    "jewelry": "JEWELRY",
    "watches": "JEWELRY",
    "gems": "JEWELRY",
    "gift_cards": "GIFT_CARDS",
    "giftcard": "GIFT_CARDS",
    "digital": "DIGITAL_GOODS",
    "software": "DIGITAL_GOODS",
    "download": "DIGITAL_GOODS",
    "subscription": "SUBSCRIPTION",
    "clothing": "CLOTHING",
    "apparel": "CLOTHING",
    "fashion": "CLOTHING",
    "services": "SERVICES",
}


def _map_item_category(product_type: str, tags: str) -> str:
    text = (product_type + " " + tags).lower()
    for keyword, category in _CATEGORY_MAP.items():
        if keyword in text:
            return category
    return "PHYSICAL_GOODS"


def _hours_since_purchase(created_at_iso: str) -> float:
    """Return hours elapsed since Shopify order created_at timestamp."""
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return round((now - created).total_seconds() / 3600, 1)
    except Exception:
        return 72.0  # safe default


def _delivery_status(fulfillment_status: Optional[str], tracking_number: Optional[str]) -> str:
    if not fulfillment_status:
        return "PENDING"
    fs = (fulfillment_status or "").lower()
    if fs in ("fulfilled", "shipped"):
        return "IN_TRANSIT"
    if fs == "partial":
        return "IN_TRANSIT"
    if fs in ("unfulfilled", ""):
        return "PENDING"
    return "UNKNOWN"


def _ltv_tier(total_spent: float) -> str:
    if total_spent >= 500:
        return "VIP"
    if total_spent >= 200:
        return "HIGH"
    if total_spent >= 50:
        return "MEDIUM"
    if total_spent >= 10:
        return "LOW"
    return "NEGLIGIBLE"


@dataclass
class ShopifyEnrichmentContext:
    """All oracle-ready inputs derived from one Shopify order."""
    order_id: str
    merchant_id: str

    # Raw extracted fields
    item_category: str
    delivery_status: str
    carrier_has_tracking: bool
    tracking_number: Optional[str]
    hours_since_purchase: float
    is_digital_product: bool
    is_express_shipping: bool
    ship_to_billing_match: bool
    has_3ds_authentication: bool

    # Buyer fields
    buyer_email: str
    buyer_account_age_days: int
    buyer_prior_orders: int
    buyer_total_spent: float
    buyer_ltv_tier: str

    # Financial
    order_amount: float
    currency: str

    # For prevention advisor
    days_past_expected_delivery: int

    # Errors during enrichment (non-fatal)
    warnings: list[str] = field(default_factory=list)

    def to_prevention_kwargs(self) -> dict:
        """Kwargs to pass to PreventionInput (overriding defaults with real data)."""
        return {
            "delivery_status": self.delivery_status,
            "days_past_expected_delivery": self.days_past_expected_delivery,
            "carrier_has_tracking": self.carrier_has_tracking,
            "buyer_tier": self.buyer_ltv_tier,
            "hours_since_purchase": self.hours_since_purchase,
            "is_digital_product": self.is_digital_product,
        }

    def to_ltv_kwargs(self) -> dict:
        """Kwargs to pass to LTVDecisionInput."""
        return {
            "buyer_ltv_estimate": self.buyer_total_spent * 1.5,
            "buyer_ltv_tier": self.buyer_ltv_tier,
            "buyer_total_spend": self.buyer_total_spent,
            "buyer_last_purchase_days_ago": max(0, int(self.hours_since_purchase / 24)),
        }

    def to_risk_kwargs(self) -> dict:
        """Kwargs to pass to TransactionRiskInput."""
        return {
            "item_category": self.item_category,
            "buyer_account_age_days": self.buyer_account_age_days,
            "buyer_prior_transactions": self.buyer_prior_orders,
            "buyer_email_domain": self.buyer_email.split("@")[-1] if "@" in self.buyer_email else "",
            "is_express_shipping": self.is_express_shipping,
            "is_digital_delivery": self.is_digital_product,
            "has_3ds_authentication": self.has_3ds_authentication,
            "ship_to_billing_match": self.ship_to_billing_match,
        }

    def to_dna_kwargs(self) -> dict:
        """Kwargs to pass to FraudDNAInput."""
        return {
            "buyer_transactions_total": max(self.buyer_prior_orders, 1),
            "tracking_confirmed_delivered": self.delivery_status == "DELIVERED",
            "hours_since_purchase": self.hours_since_purchase,
        }


def enrich_from_shopify_order(order: dict[str, Any], merchant_id: str = "ANCHOR") -> ShopifyEnrichmentContext:
    """
    Convert a raw Shopify order dict (from webhook payload or REST API response)
    into a ShopifyEnrichmentContext with all oracle-ready fields populated.

    Handles both Shopify REST API format and webhook payload format.
    """
    warnings = []

    order_id = str(order.get("id", order.get("order_id", "UNKNOWN")))

    # Amount
    try:
        amount = float(order.get("total_price", order.get("total_price_usd", 0.0)))
    except (ValueError, TypeError):
        amount = 0.0
        warnings.append("Could not parse total_price")

    currency = order.get("currency", "USD")

    # Fulfillment / delivery
    fulfillments = order.get("fulfillments", [])
    tracking_number = None
    has_tracking = False
    carrier = ""

    if fulfillments:
        f = fulfillments[0]  # primary fulfillment
        tracking_number = f.get("tracking_number")
        has_tracking = bool(tracking_number)
        carrier = (f.get("tracking_company") or "").lower()

    fs = order.get("fulfillment_status") or ""
    delivery_status = _delivery_status(fs, tracking_number)

    # Express shipping
    shipping_lines = order.get("shipping_lines", [])
    is_express = any(
        any(kw in (sl.get("title") or "").lower() for kw in ("express", "overnight", "priority", "2-day", "next"))
        for sl in shipping_lines
    )

    # Digital product check
    items = order.get("line_items", [])
    product_types = " ".join(str(li.get("product_type", "") or "") for li in items)
    tags = " ".join(str(li.get("tags", "") or "") for li in items) + " " + str(order.get("tags", "") or "")
    item_category = _map_item_category(product_types, tags)
    is_digital = item_category in ("DIGITAL_GOODS", "SUBSCRIPTION")

    # Days past expected delivery
    # Shopify doesn't always have this; estimate from shipping line or default to 0
    days_past = 0
    if delivery_status in ("IN_TRANSIT", "PENDING"):
        created_at = order.get("created_at", "")
        if created_at:
            hours_elapsed = _hours_since_purchase(created_at)
            # Standard shipping SLA ≈ 7 days; express ≈ 2 days
            sla_hours = 48 if is_express else 168
            if hours_elapsed > sla_hours:
                days_past = int((hours_elapsed - sla_hours) / 24)

    # Hours since purchase
    created_at_str = order.get("created_at", "")
    hours_since = _hours_since_purchase(created_at_str) if created_at_str else 72.0

    # Billing vs shipping address match
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or {}
    ship_to_billing = (
        billing.get("zip", "").strip().lower() == shipping.get("zip", "").strip().lower()
        and billing.get("country_code", "").lower() == shipping.get("country_code", "").lower()
    )

    # 3DS — Shopify doesn't expose this directly; check payment gateway note
    gateway = (order.get("payment_gateway") or "").lower()
    has_3ds = "3ds" in gateway or "three" in gateway or bool(
        order.get("payment_details", {}) and
        order.get("payment_details", {}).get("credit_card_bin")
    )

    # Buyer
    customer = order.get("customer") or {}
    buyer_email = customer.get("email") or order.get("email") or ""
    buyer_prior_orders = int(customer.get("orders_count", 1))
    try:
        buyer_total_spent = float(customer.get("total_spent", 0.0))
    except (ValueError, TypeError):
        buyer_total_spent = 0.0

    # Account age
    created_customer = customer.get("created_at", "")
    if created_customer:
        try:
            c_dt = datetime.fromisoformat(created_customer.replace("Z", "+00:00"))
            buyer_account_age_days = (datetime.now(timezone.utc) - c_dt).days
        except Exception:
            buyer_account_age_days = 365
    else:
        buyer_account_age_days = max(1, buyer_prior_orders * 30)

    ltv_tier = _ltv_tier(buyer_total_spent)

    return ShopifyEnrichmentContext(
        order_id=order_id,
        merchant_id=merchant_id,
        item_category=item_category,
        delivery_status=delivery_status,
        carrier_has_tracking=has_tracking,
        tracking_number=tracking_number,
        hours_since_purchase=hours_since,
        is_digital_product=is_digital,
        is_express_shipping=is_express,
        ship_to_billing_match=ship_to_billing,
        has_3ds_authentication=has_3ds,
        buyer_email=buyer_email,
        buyer_account_age_days=buyer_account_age_days,
        buyer_prior_orders=buyer_prior_orders,
        buyer_total_spent=buyer_total_spent,
        buyer_ltv_tier=ltv_tier,
        order_amount=amount,
        currency=currency,
        days_past_expected_delivery=days_past,
        warnings=warnings,
    )
