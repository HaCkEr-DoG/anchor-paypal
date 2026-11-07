"""
Zapier Universal Dispute Ingestor — receive disputes from ANY e-commerce
platform via Zapier webhooks and normalize them for Anchor's oracle pipeline.

Sponsor: Zapier (plugin:cce87d03 / mcp__cce87d03)

Supported source platforms (via Zapier):
  Stripe, WooCommerce, Magento, BigCommerce, Squarespace,
  Wix, Square, Braintree, Klarna, Afterpay/Clearpay

Zapier Zap template:
  TRIGGER: "New Dispute" / "Chargeback Created" in source platform
  ACTION: Webhook POST to https://anchor-paypal.onrender.com/api/integrations/zapier/dispute
  FIELDS: mapped per platform below

This module normalizes disparate dispute payloads into Anchor's
canonical dispute schema that guardian.py understands.

PayPal benefit: Anchor becomes platform-agnostic, reducing total
dispute volume across all payment networks (not just PayPal).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SourcePlatform(str, Enum):
    STRIPE          = "STRIPE"
    WOOCOMMERCE     = "WOOCOMMERCE"
    SHOPIFY         = "SHOPIFY"
    BIGCOMMERCE     = "BIGCOMMERCE"
    SQUARESPACE     = "SQUARESPACE"
    MAGENTO         = "MAGENTO"
    SQUARE          = "SQUARE"
    BRAINTREE       = "BRAINTREE"
    KLARNA          = "KLARNA"
    AFTERPAY        = "AFTERPAY"
    PAYPAL_DIRECT   = "PAYPAL_DIRECT"
    GENERIC         = "GENERIC"


@dataclass
class NormalizedDispute:
    """Platform-agnostic dispute schema accepted by Anchor's oracle pipeline."""
    dispute_id: str
    source_platform: SourcePlatform
    transaction_id: str
    order_id: str

    amount: float
    currency: str
    reason_code: str            # Normalized to Anchor's reason codes
    original_reason_code: str   # Raw reason code from source platform

    buyer_email: str
    buyer_name: str
    buyer_id: str

    merchant_id: str
    network: str                # "VISA" / "MC" / "PAYPAL" / etc.

    created_at: str             # ISO datetime
    deadline: Optional[str]     # ISO datetime for response deadline

    # Evidence already available
    has_tracking: bool = False
    tracking_number: str = ""
    has_invoice: bool = False
    has_buyer_communication: bool = False

    # Zapier metadata
    zapier_zap_id: str = ""
    raw_payload: dict = None

    def to_anchor_body(self) -> dict:
        """Format as POST body for Anchor's /api/disputes endpoint."""
        return {
            "dispute_id": self.dispute_id,
            "transaction_id": self.transaction_id,
            "amount": self.amount,
            "currency": self.currency,
            "reason_code": self.reason_code,
            "buyer_email": self.buyer_email,
            "source_platform": self.source_platform.value,
            "network": self.network,
            "has_tracking": self.has_tracking,
            "tracking_number": self.tracking_number,
        }


# ─── Reason code normalization ────────────────────────────────────────────────

_STRIPE_REASON_MAP = {
    "product_not_received":         "INR",
    "product_unacceptable":         "SNAD",
    "fraudulent":                   "UNAUTHORIZED",
    "unrecognized":                 "UNAUTHORIZED",
    "credit_not_processed":         "SUBSCRIPTION",
    "general":                      "SNAD",
    "subscription_canceled":        "SUBSCRIPTION",
    "duplicate":                    "UNAUTHORIZED",
}

_WOOCOMMERCE_REASON_MAP = {
    "not_received":                 "INR",
    "not_as_described":             "SNAD",
    "unauthorized_transaction":     "UNAUTHORIZED",
    "subscription_canceled":        "SUBSCRIPTION",
}

_SQUARE_REASON_MAP = {
    "ITEM_NOT_RECEIVED":            "INR",
    "ITEM_NOT_AS_DESCRIBED":        "SNAD",
    "UNAUTHORIZED_CARD":            "UNAUTHORIZED",
    "AMOUNT_DIFFERS":               "SNAD",
    "NOT_AS_DESCRIBED_OR_DEFECTIVE": "SNAD",
}

_REASON_MAPS: dict[SourcePlatform, dict[str, str]] = {
    SourcePlatform.STRIPE:          _STRIPE_REASON_MAP,
    SourcePlatform.WOOCOMMERCE:     _WOOCOMMERCE_REASON_MAP,
    SourcePlatform.SQUARE:          _SQUARE_REASON_MAP,
}


_ANCHOR_CODES = {"INR", "SNAD", "UNAUTHORIZED", "SUBSCRIPTION"}

def normalize_reason_code(raw_code: str, platform: SourcePlatform) -> str:
    if raw_code.upper() in _ANCHOR_CODES:
        return raw_code.upper()
    mapping = _REASON_MAPS.get(platform, {})
    return mapping.get(raw_code, mapping.get(raw_code.upper(), "SNAD"))


def detect_platform(payload: dict[str, Any]) -> SourcePlatform:
    """Auto-detect source platform from webhook payload structure."""
    if "charge" in payload and "payment_intent" in payload:
        return SourcePlatform.STRIPE
    if "id" in payload and str(payload.get("id", "")).startswith("dp_"):
        return SourcePlatform.STRIPE
    if "order_id" in payload and "meta_data" in payload:
        return SourcePlatform.WOOCOMMERCE
    if "dispute" in payload and "payment_method_details" in payload.get("dispute", {}):
        return SourcePlatform.BRAINTREE
    if payload.get("source") == "shopify":
        return SourcePlatform.SHOPIFY
    return SourcePlatform.GENERIC


def normalize_zapier_payload(
    payload: dict[str, Any],
    merchant_id: str = "ANCHOR",
) -> NormalizedDispute:
    """
    Convert a raw Zapier webhook payload (from any source platform) into
    Anchor's NormalizedDispute format.
    """
    platform = detect_platform(payload)

    # Stripe normalization
    if platform == SourcePlatform.STRIPE:
        dispute_obj = payload.get("dispute", payload)
        raw_reason = dispute_obj.get("reason", "general")
        charge = dispute_obj.get("charge", "")
        return NormalizedDispute(
            dispute_id=dispute_obj.get("id", f"zapier_{hash(str(payload))}"[:20]),
            source_platform=platform,
            transaction_id=str(charge),
            order_id=dispute_obj.get("metadata", {}).get("order_id", str(charge)),
            amount=float(dispute_obj.get("amount", 0)) / 100.0,
            currency=dispute_obj.get("currency", "usd").upper(),
            reason_code=normalize_reason_code(raw_reason, platform),
            original_reason_code=raw_reason,
            buyer_email=dispute_obj.get("evidence", {}).get("customer_email_address", ""),
            buyer_name=dispute_obj.get("evidence", {}).get("customer_name", ""),
            buyer_id="",
            merchant_id=merchant_id,
            network=dispute_obj.get("payment_method_details", {}).get("card", {}).get("brand", "VISA").upper(),
            created_at=str(dispute_obj.get("created", "")),
            deadline=None,
            zapier_zap_id=payload.get("zap_id", ""),
            raw_payload=payload,
        )

    # Generic normalization
    raw_reason = payload.get("reason", payload.get("reason_code", "SNAD"))
    return NormalizedDispute(
        dispute_id=payload.get("dispute_id", payload.get("id", f"zapier_{abs(hash(str(payload)))}"[:20])),
        source_platform=platform,
        transaction_id=str(payload.get("transaction_id", payload.get("charge_id", ""))),
        order_id=str(payload.get("order_id", "")),
        amount=float(payload.get("amount", payload.get("dispute_amount", 0.0))),
        currency=str(payload.get("currency", "USD")).upper(),
        reason_code=normalize_reason_code(raw_reason, platform),
        original_reason_code=raw_reason,
        buyer_email=str(payload.get("buyer_email", payload.get("customer_email", ""))),
        buyer_name=str(payload.get("buyer_name", payload.get("customer_name", ""))),
        buyer_id=str(payload.get("buyer_id", payload.get("customer_id", ""))),
        merchant_id=merchant_id,
        network=str(payload.get("network", payload.get("card_network", "PAYPAL"))).upper(),
        created_at=str(payload.get("created_at", "")),
        deadline=payload.get("deadline") or payload.get("respond_by"),
        has_tracking=bool(payload.get("tracking_number")),
        tracking_number=str(payload.get("tracking_number", "")),
        zapier_zap_id=payload.get("zap_id", ""),
        raw_payload=payload,
    )
