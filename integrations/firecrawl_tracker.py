"""
Firecrawl Carrier Tracker — scrape real-time delivery status from carrier
websites to verify tracking claims in dispute evidence.

Sponsor: Firecrawl (mcp__1d0d361d / plugin:brightdata)

Used by:
  - guardian.py enrichment layer: verify delivery status before making FIGHT decision
  - INR dispute evidence builder: confirm carrier status before submitting
  - Chargeback prevention advisor: check if package is actually delayed/lost

Supported carriers (via Firecrawl scrape):
  USPS, UPS, FedEx, DHL, OnTrac, LaserShip, Amazon Logistics, Canada Post

Usage at runtime (when Firecrawl MCP is connected):
    result = await scrape_tracking_status(tracking_number, carrier)
    # result.is_delivered, result.delivery_date, result.last_event

This module provides:
  1. URL builders for each carrier's public tracking page
  2. Response parsers (expected page text patterns)
  3. Pure dataclasses for passing tracking context to oracle modules
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import re


class CarrierCode(str, Enum):
    USPS     = "USPS"
    UPS      = "UPS"
    FEDEX    = "FEDEX"
    DHL      = "DHL"
    ONTRAC   = "ONTRAC"
    AMAZON   = "AMAZON"
    CANADA   = "CANADA_POST"
    UNKNOWN  = "UNKNOWN"


class DeliveryStatus(str, Enum):
    DELIVERED       = "DELIVERED"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    IN_TRANSIT      = "IN_TRANSIT"
    PENDING_PICKUP  = "PENDING_PICKUP"
    EXCEPTION       = "EXCEPTION"       # delivery attempted, failed
    LOST            = "LOST"
    RETURNED        = "RETURNED"
    UNKNOWN         = "UNKNOWN"


@dataclass
class TrackingResult:
    tracking_number: str
    carrier: CarrierCode
    status: DeliveryStatus
    is_delivered: bool
    delivery_date: Optional[str]    # ISO date string if delivered
    last_event: str                 # human-readable last event
    last_event_location: str
    days_in_transit: int
    scrape_url: str
    raw_status_text: str            # for evidence screenshots
    # For dispute evidence
    can_use_as_proof_of_delivery: bool
    dispute_defensibility: str      # "STRONG" / "MODERATE" / "WEAK"


# ─── Carrier detection ────────────────────────────────────────────────────────

_TRACKING_PATTERNS: list[tuple[re.Pattern, CarrierCode]] = [
    (re.compile(r"^1Z[A-Z0-9]{16}$", re.I), CarrierCode.UPS),
    (re.compile(r"^\d{22}$"), CarrierCode.USPS),          # USPS full barcode
    (re.compile(r"^9[24]\d{18,22}$"), CarrierCode.USPS),  # USPS priority/express
    (re.compile(r"^\d{12}$"), CarrierCode.FEDEX),
    (re.compile(r"^\d{15}$"), CarrierCode.FEDEX),
    (re.compile(r"^\d{20}$"), CarrierCode.FEDEX),
    (re.compile(r"^[0-9]{10}$"), CarrierCode.DHL),
    (re.compile(r"^C\d{14}$", re.I), CarrierCode.CANADA),
    (re.compile(r"^TBA\d{12}$", re.I), CarrierCode.AMAZON),
]


def detect_carrier(tracking_number: str, carrier_hint: str = "") -> CarrierCode:
    """Auto-detect carrier from tracking number format, with optional hint."""
    if carrier_hint:
        hint = carrier_hint.lower()
        for code in CarrierCode:
            if code.value.lower() in hint:
                return code

    tn = tracking_number.strip().upper()
    for pattern, carrier in _TRACKING_PATTERNS:
        if pattern.match(tn):
            return carrier
    return CarrierCode.UNKNOWN


# ─── Tracking URL builders ────────────────────────────────────────────────────

_TRACKING_URLS = {
    CarrierCode.USPS:   "https://tools.usps.com/go/TrackConfirmAction?tLabels={tn}",
    CarrierCode.UPS:    "https://www.ups.com/track?tracknum={tn}",
    CarrierCode.FEDEX:  "https://www.fedex.com/fedextrack/?tracknumbers={tn}",
    CarrierCode.DHL:    "https://www.dhl.com/en/express/tracking.html?AWB={tn}",
    CarrierCode.ONTRAC: "https://www.ontrac.com/tracking/?number={tn}",
    CarrierCode.AMAZON: "https://track.amazon.com/tracking/{tn}",
    CarrierCode.CANADA: "https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={tn}",
}


def build_tracking_url(tracking_number: str, carrier: CarrierCode) -> str:
    template = _TRACKING_URLS.get(carrier, "https://www.google.com/search?q={tn}+tracking")
    return template.format(tn=tracking_number)


# ─── Response parsers (applied to Firecrawl page text) ───────────────────────

_DELIVERED_KEYWORDS = [
    "delivered", "package was delivered", "shipment delivered",
    "your package has been delivered", "left at", "signed for",
    "picked up",
]
_IN_TRANSIT_KEYWORDS = [
    "in transit", "on the way", "out for delivery", "arriving",
    "departed", "arrived at", "in transit to",
]
_EXCEPTION_KEYWORDS = [
    "delivery attempted", "exception", "unable to deliver",
    "delivery exception", "delivery failed", "notice left",
]
_LOST_KEYWORDS = [
    "lost", "missing", "not found", "no record",
]


def parse_tracking_page(
    page_text: str,
    tracking_number: str,
    carrier: CarrierCode,
    scrape_url: str,
) -> TrackingResult:
    """
    Parse Firecrawl page text for a carrier tracking page into a TrackingResult.
    Called at runtime after Firecrawl scrapes the tracking URL.
    """
    text = page_text.lower()

    is_delivered = any(kw in text for kw in _DELIVERED_KEYWORDS)
    is_exception = any(kw in text for kw in _EXCEPTION_KEYWORDS)
    is_lost = any(kw in text for kw in _LOST_KEYWORDS)
    is_in_transit = any(kw in text for kw in _IN_TRANSIT_KEYWORDS)

    if is_delivered:
        status = DeliveryStatus.DELIVERED
    elif is_lost:
        status = DeliveryStatus.LOST
    elif is_exception:
        status = DeliveryStatus.EXCEPTION
    elif is_in_transit:
        status = DeliveryStatus.IN_TRANSIT
    else:
        status = DeliveryStatus.UNKNOWN

    # Extract delivery date (heuristic: look for date patterns near "delivered")
    delivery_date = None
    if is_delivered:
        date_pattern = re.compile(
            r"(january|february|march|april|may|june|july|august|september|october|november|december)"
            r"\s+\d{1,2},?\s+\d{4}|"
            r"\d{1,2}/\d{1,2}/\d{2,4}",
            re.I,
        )
        dates = date_pattern.findall(page_text)
        if dates:
            delivery_date = dates[-1]

    # Defensibility for dispute evidence
    if is_delivered and delivery_date:
        defensibility = "STRONG"
    elif is_delivered:
        defensibility = "MODERATE"
    elif is_exception or is_lost:
        defensibility = "WEAK"
    else:
        defensibility = "MODERATE"

    return TrackingResult(
        tracking_number=tracking_number,
        carrier=carrier,
        status=status,
        is_delivered=is_delivered,
        delivery_date=delivery_date,
        last_event=status.value.replace("_", " ").title(),
        last_event_location="",
        days_in_transit=0,
        scrape_url=scrape_url,
        raw_status_text=page_text[:500],
        can_use_as_proof_of_delivery=is_delivered and defensibility in ("STRONG", "MODERATE"),
        dispute_defensibility=defensibility,
    )
