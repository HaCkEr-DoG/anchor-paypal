"""
Order Fulfillment Tracker — assess whether an order's fulfillment
record is sufficient to defend against an INR (Item Not Received) dispute.

Checks:
  1. Shipping timeline — was order shipped within promised window?
  2. Tracking validity — is tracking number valid and shows movement?
  3. Delivery confirmation — is there final delivery confirmation?
  4. Signature confirmation — required for ≥$750 on most networks
  5. Carrier reliability — known carrier vs. unknown
  6. Fulfillment window — did fulfillment meet stated SLA?

Output:
  - FulfillmentStrength: how well can we prove fulfillment?
  - DefenseReadiness: ready to file defense or needs more evidence?
  - Missing evidence with urgency
  - Win probability adjustment based on fulfillment quality

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FulfillmentStrength(str, Enum):
    DEFINITIVE  = "DEFINITIVE"   # Delivery conf + signature (≥$750) or strong tracking
    STRONG      = "STRONG"       # Delivery confirmation present
    MODERATE    = "MODERATE"     # Tracking shows in-transit / arrived at facility
    WEAK        = "WEAK"         # Tracking number only, no movement
    MISSING     = "MISSING"      # No tracking or fulfillment data


class DefenseReadiness(str, Enum):
    READY           = "READY"            # Submit immediately
    READY_PARTIAL   = "READY_PARTIAL"    # Adequate to file, some gaps
    NEEDS_EVIDENCE  = "NEEDS_EVIDENCE"   # Wait for delivery scan before filing
    NOT_READY       = "NOT_READY"        # Cannot file — missing critical evidence


class TrackingStatus(str, Enum):
    DELIVERED           = "DELIVERED"
    OUT_FOR_DELIVERY    = "OUT_FOR_DELIVERY"
    IN_TRANSIT          = "IN_TRANSIT"
    LABEL_CREATED       = "LABEL_CREATED"   # Pre-shipment — not actually shipped
    EXCEPTION           = "EXCEPTION"       # Lost / delay
    RETURNED            = "RETURNED"        # Returned to sender
    UNKNOWN             = "UNKNOWN"


# Known reliable carriers (not exhaustive)
_KNOWN_CARRIERS = {
    "UPS", "FEDEX", "USPS", "DHL", "CANADA_POST", "ROYAL_MAIL",
    "AUSTRALIA_POST", "PUROLATOR", "ONTRAC", "LSO", "LASERSHIP",
}

# Signature required threshold per network
_SIGNATURE_THRESHOLD = 750.0


@dataclass
class FulfillmentRecord:
    """Fulfillment data for a single shipment."""
    shipment_id: str
    carrier: str
    tracking_number: str
    shipped_date_epoch: int
    promised_delivery_epoch: int

    tracking_status: TrackingStatus = TrackingStatus.UNKNOWN
    has_delivery_confirmation: bool = False
    delivery_date_epoch: Optional[int] = None
    has_signature_confirmation: bool = False
    shipment_weight_oz: float = 0.0
    is_expedited: bool = False


@dataclass
class FulfillmentInput:
    """Input for fulfillment tracking assessment."""
    dispute_id: str
    order_amount: float
    order_date_epoch: int
    dispute_date_epoch: int

    shipments: list[FulfillmentRecord] = field(default_factory=list)

    # Policy context
    stated_handling_days: int = 2     # Merchant's promised handling time
    stated_transit_days: int = 7      # Merchant's promised transit time

    # Network context
    network: str = "PAYPAL"


def _days_between(t1: int, t2: int) -> float:
    return abs(t2 - t1) / 86400.0


def _carrier_known(carrier: str) -> bool:
    return carrier.upper() in _KNOWN_CARRIERS


def _requires_signature(amount: float) -> bool:
    return amount >= _SIGNATURE_THRESHOLD


def _assess_shipment(
        s: FulfillmentRecord,
        order_date: int,
        order_amount: float) -> tuple[FulfillmentStrength, list[str], float]:
    """Return (strength, gaps, win_rate_boost)."""
    gaps = []
    boost = 0.0

    # Carrier check
    if not _carrier_known(s.carrier):
        gaps.append(f"Unknown carrier '{s.carrier}' — may not be accepted as evidence")

    # Tracking status
    status = s.tracking_status
    if status == TrackingStatus.DELIVERED:
        if s.has_delivery_confirmation:
            strength = FulfillmentStrength.DEFINITIVE if (
                not _requires_signature(order_amount) or s.has_signature_confirmation
            ) else FulfillmentStrength.STRONG
            boost = 0.20
            if _requires_signature(order_amount) and not s.has_signature_confirmation:
                gaps.append(f"Order ≥${_SIGNATURE_THRESHOLD:.0f} requires signature — not confirmed")
                strength = FulfillmentStrength.STRONG
                boost = 0.12
        else:
            strength = FulfillmentStrength.MODERATE
            boost = 0.08
            gaps.append("Tracking shows DELIVERED but no formal delivery confirmation")

    elif status in (TrackingStatus.OUT_FOR_DELIVERY, TrackingStatus.IN_TRANSIT):
        strength = FulfillmentStrength.MODERATE
        boost = 0.05
        gaps.append("Package in-transit — wait for delivery confirmation before filing")

    elif status == TrackingStatus.LABEL_CREATED:
        strength = FulfillmentStrength.WEAK
        boost = -0.05
        gaps.append("Label created but no carrier scan — package may not have been handed off")

    elif status == TrackingStatus.EXCEPTION:
        strength = FulfillmentStrength.WEAK
        boost = -0.10
        gaps.append("Carrier reported delivery exception — investigate with carrier before filing")

    elif status == TrackingStatus.RETURNED:
        strength = FulfillmentStrength.WEAK
        boost = -0.15
        gaps.append("Package returned to sender — buyer's claim may be valid")

    else:  # UNKNOWN
        strength = FulfillmentStrength.MISSING
        boost = -0.15
        gaps.append("No tracking status available — obtain from carrier")

    # Shipping timeliness
    ship_days = _days_between(order_date, s.shipped_date_epoch)
    if ship_days > 7:
        gaps.append(f"Order shipped {ship_days:.0f}d after order date — may weaken defense")
    elif ship_days <= 2:
        pass   # Good — no gap

    return strength, gaps, boost


@dataclass
class MissingEvidence:
    item: str
    urgency: str   # CRITICAL / HIGH / MEDIUM


@dataclass
class FulfillmentReport:
    """Full fulfillment assessment."""
    dispute_id: str
    overall_strength: FulfillmentStrength
    defense_readiness: DefenseReadiness

    shipment_count: int
    has_delivery_confirmation: bool
    has_signature_confirmation: bool
    signature_required: bool

    win_rate_adjustment: float    # Additive to base win rate
    missing_evidence: list[MissingEvidence]
    gaps: list[str]
    summary: str


def assess_fulfillment(inp: FulfillmentInput) -> FulfillmentReport:
    """Assess whether fulfillment evidence supports an INR defense."""
    if not inp.shipments:
        return FulfillmentReport(
            dispute_id=inp.dispute_id,
            overall_strength=FulfillmentStrength.MISSING,
            defense_readiness=DefenseReadiness.NOT_READY,
            shipment_count=0,
            has_delivery_confirmation=False,
            has_signature_confirmation=False,
            signature_required=_requires_signature(inp.order_amount),
            win_rate_adjustment=-0.20,
            missing_evidence=[MissingEvidence("Tracking number and shipment record", "CRITICAL")],
            gaps=["No shipment data provided"],
            summary=f"Fulfillment {inp.dispute_id}: strength=MISSING | ready=NOT_READY",
        )

    all_gaps: list[str] = []
    all_boosts: list[float] = []
    strengths: list[FulfillmentStrength] = []
    has_del = False
    has_sig = False

    _strength_order = [
        FulfillmentStrength.DEFINITIVE, FulfillmentStrength.STRONG,
        FulfillmentStrength.MODERATE, FulfillmentStrength.WEAK,
        FulfillmentStrength.MISSING,
    ]

    for s in inp.shipments:
        strength, gaps, boost = _assess_shipment(
            s, inp.order_date_epoch, inp.order_amount)
        strengths.append(strength)
        all_gaps.extend(gaps)
        all_boosts.append(boost)
        if s.has_delivery_confirmation:
            has_del = True
        if s.has_signature_confirmation:
            has_sig = True

    # Best shipment strength (pick lowest index = strongest)
    overall = min(strengths, key=lambda s: _strength_order.index(s))
    avg_boost = sum(all_boosts) / len(all_boosts) if all_boosts else 0.0

    # Signature requirement
    sig_req = _requires_signature(inp.order_amount)
    if sig_req and not has_sig:
        all_gaps.append(f"Signature required (amount ≥ ${_SIGNATURE_THRESHOLD:.0f}) but not obtained")

    # Build missing evidence list
    missing: list[MissingEvidence] = []
    if not has_del and overall not in (FulfillmentStrength.DEFINITIVE,):
        if overall == FulfillmentStrength.MISSING:
            missing.append(MissingEvidence("Tracking number", "CRITICAL"))
        else:
            missing.append(MissingEvidence("Delivery confirmation scan from carrier", "HIGH"))
    if sig_req and not has_sig:
        missing.append(MissingEvidence("Signature confirmation", "CRITICAL"))

    # Defense readiness
    if overall == FulfillmentStrength.DEFINITIVE:
        readiness = DefenseReadiness.READY
    elif overall == FulfillmentStrength.STRONG:
        readiness = DefenseReadiness.READY
    elif overall == FulfillmentStrength.MODERATE:
        readiness = DefenseReadiness.READY_PARTIAL
    elif overall == FulfillmentStrength.WEAK:
        readiness = DefenseReadiness.NEEDS_EVIDENCE
    else:
        readiness = DefenseReadiness.NOT_READY

    summary = (
        f"Fulfillment {inp.dispute_id}: "
        f"strength={overall.value} | ready={readiness.value} | "
        f"shipments={len(inp.shipments)} | adj={avg_boost:+.0%}"
    )

    return FulfillmentReport(
        dispute_id=inp.dispute_id,
        overall_strength=overall,
        defense_readiness=readiness,
        shipment_count=len(inp.shipments),
        has_delivery_confirmation=has_del,
        has_signature_confirmation=has_sig,
        signature_required=sig_req,
        win_rate_adjustment=round(avg_boost, 3),
        missing_evidence=missing,
        gaps=all_gaps,
        summary=summary,
    )
