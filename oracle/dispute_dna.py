"""
Dispute DNA — pattern analytics to prevent future disputes.

Every other oracle module is REACTIVE — it handles disputes that already exist.
This module is STRATEGIC — it analyzes historical dispute patterns to identify
root causes so they can be fixed at the source.

The core insight: 80% of disputes come from 20% of SKUs / carriers / geographic
areas. Find those 20% and fix them, and 80% of your dispute volume disappears.

This module analyzes:
  1. SKU clustering — which products generate the most disputes (and why)
  2. Carrier failure rates — which carriers cause "item not received" at high rates
  3. Geographic heat maps — which ZIP codes / regions have elevated dispute rates
  4. Dispute timing patterns — do disputes spike after a carrier delay? After holidays?
  5. Root cause identification — is the problem a product defect, misleading listing,
     specific carrier, or fraud pattern?
  6. Resolution success rates — which claim types are you winning vs losing?

Armed with this, a merchant can:
  - Remove a high-dispute SKU from the catalog
  - Switch from USPS to FedEx for a specific region
  - Fix a misleading product listing that generates SNAD disputes
  - Add signature confirmation for a specific product category

Data model: DisputeEvent (one per dispute), analyzed in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeOutcome(str, Enum):
    WON          = "WON"          # Merchant won
    LOST         = "LOST"         # Merchant lost / full refund
    SETTLED      = "SETTLED"      # Partial refund
    WITHDRAWN    = "WITHDRAWN"    # Buyer withdrew
    PENDING      = "PENDING"      # Not yet resolved


class RootCause(str, Enum):
    CARRIER_DELIVERY_FAILURE = "CARRIER_DELIVERY_FAILURE"  # Carrier failed, item not received
    CARRIER_DAMAGE           = "CARRIER_DAMAGE"            # Item arrived damaged
    LISTING_MISMATCH         = "LISTING_MISMATCH"          # Item didn't match description
    PRODUCT_DEFECT           = "PRODUCT_DEFECT"            # Item was defective
    FRAUD_SERIAL_DISPUTER    = "FRAUD_SERIAL_DISPUTER"      # Buyer is a serial fraudster
    FRAUD_FRIENDLY           = "FRAUD_FRIENDLY"            # Buyer received item, still disputed
    ADDRESS_ISSUE            = "ADDRESS_ISSUE"             # Wrong address / undeliverable
    FULFILLMENT_ERROR        = "FULFILLMENT_ERROR"         # Wrong item shipped
    DELAYED_DELIVERY         = "DELAYED_DELIVERY"          # Item arrived very late
    UNCLEAR                  = "UNCLEAR"                   # Cannot determine from data


@dataclass
class DisputeEvent:
    """One historical dispute record."""
    dispute_id: str
    sku: str                              # Product identifier
    claim_type: str                       # INR, SNAD, UNAUTHORIZED, etc.
    carrier: str                          # USPS, FedEx, UPS, DHL
    shipping_zip: str                     # Destination ZIP code
    billing_country: str                  # Buyer's country
    dispute_amount: float
    outcome: DisputeOutcome
    settlement_cost: float = 0.0          # Actual cost (0 if won)
    filed_date: str = ""                  # ISO date
    days_after_ship: int = 0              # How many days after shipment
    buyer_prior_disputes: int = 0
    had_signature_confirmation: bool = False
    had_tracking_delivered: bool = False
    root_cause: RootCause = RootCause.UNCLEAR


@dataclass
class SKUCluster:
    """Dispute pattern for one SKU."""
    sku: str
    total_disputes: int
    dispute_rate: float               # disputes / total orders (if known)
    total_loss: float                 # Sum of all settlement costs
    win_rate: float
    top_claim_types: list[str]        # Most common reasons
    top_root_causes: list[RootCause]
    recommendation: str


@dataclass
class CarrierProfile:
    """Dispute pattern by carrier."""
    carrier: str
    total_disputes: int
    inr_rate: float                   # INR disputes / all disputes for this carrier
    win_rate: float
    avg_days_to_dispute: float
    high_risk_regions: list[str]      # ZIP prefixes with elevated disputes


@dataclass
class GeographicCluster:
    """Dispute patterns by ZIP prefix or country."""
    region: str                       # ZIP prefix or country code
    total_disputes: int
    dispute_rate_index: float         # 1.0 = baseline, 2.0 = double the baseline
    dominant_claim_type: str
    recommendation: str


@dataclass
class DisputeDNA:
    """Full pattern analysis of historical disputes."""
    total_disputes_analyzed: int
    total_loss: float
    overall_win_rate: float

    # Sorted by total_loss descending
    top_sku_clusters: list[SKUCluster]

    # Sorted by inr_rate descending
    carrier_profiles: list[CarrierProfile]

    # Sorted by dispute_rate_index descending
    geographic_clusters: list[GeographicCluster]

    # Root cause distribution
    root_cause_distribution: dict[str, float]   # RootCause.value → fraction

    # Win rate by claim type
    win_rate_by_claim_type: dict[str, float]

    # Key insights + action recommendations
    top_insights: list[str]
    action_items: list[str]

    reasoning: list[str] = field(default_factory=list)


def analyze_dispute_dna(
    disputes: list[DisputeEvent],
    total_orders_30d: int = 0,         # Total orders (for rate calculation)
    focus_top_n: int = 5,              # How many top SKUs/carriers/regions to surface
) -> DisputeDNA:
    """
    Analyze a list of historical disputes and extract pattern insights.

    Returns:
      - Which SKUs are driving losses
      - Which carriers are causing INR disputes
      - Which regions have elevated rates
      - Root cause distribution
      - Win rate by claim type
      - Specific action items

    The action items are ordered by impact — biggest loss reduction first.
    """
    reasoning = []

    if not disputes:
        return DisputeDNA(
            total_disputes_analyzed=0,
            total_loss=0.0,
            overall_win_rate=0.0,
            top_sku_clusters=[],
            carrier_profiles=[],
            geographic_clusters=[],
            root_cause_distribution={},
            win_rate_by_claim_type={},
            top_insights=["No dispute history provided — cannot analyze patterns"],
            action_items=[],
        )

    n = len(disputes)
    total_loss = sum(d.settlement_cost for d in disputes)
    won = sum(1 for d in disputes if d.outcome == DisputeOutcome.WON)
    overall_win_rate = won / n if n else 0.0

    reasoning.append(
        f"Analyzed {n} disputes: {won} won ({overall_win_rate:.0%}), "
        f"total loss ${total_loss:.0f}"
    )

    # ── SKU clustering ────────────────────────────────────────────────────────
    sku_data: dict[str, list[DisputeEvent]] = {}
    for d in disputes:
        sku_data.setdefault(d.sku, []).append(d)

    sku_clusters = []
    for sku, sku_disputes in sku_data.items():
        sku_loss = sum(d.settlement_cost for d in sku_disputes)
        sku_won = sum(1 for d in sku_disputes if d.outcome == DisputeOutcome.WON)
        sku_win_rate = sku_won / len(sku_disputes)

        # Count claim types
        claim_counts: dict[str, int] = {}
        for d in sku_disputes:
            claim_counts[d.claim_type] = claim_counts.get(d.claim_type, 0) + 1
        top_claims = sorted(claim_counts.keys(), key=lambda k: -claim_counts[k])

        # Count root causes
        cause_counts: dict[str, int] = {}
        for d in sku_disputes:
            cause_counts[d.root_cause.value] = cause_counts.get(d.root_cause.value, 0) + 1
        top_causes_raw = sorted(cause_counts.keys(), key=lambda k: -cause_counts[k])
        top_causes = [RootCause(c) for c in top_causes_raw]

        # Generate recommendation
        rec = _sku_recommendation(sku, sku_disputes, top_claims, top_causes)

        sku_clusters.append(SKUCluster(
            sku=sku,
            total_disputes=len(sku_disputes),
            dispute_rate=len(sku_disputes) / max(total_orders_30d, 1),
            total_loss=sku_loss,
            win_rate=sku_win_rate,
            top_claim_types=top_claims[:3],
            top_root_causes=top_causes[:3],
            recommendation=rec,
        ))

    sku_clusters.sort(key=lambda s: -s.total_loss)
    top_skus = sku_clusters[:focus_top_n]

    # ── Carrier profiles ──────────────────────────────────────────────────────
    carrier_data: dict[str, list[DisputeEvent]] = {}
    for d in disputes:
        if d.carrier:
            carrier_data.setdefault(d.carrier.upper(), []).append(d)

    carrier_profiles = []
    for carrier, carrier_disputes in carrier_data.items():
        inr_disputes = [d for d in carrier_disputes if "NOT_RECEIVED" in d.claim_type or "INR" in d.claim_type]
        inr_rate = len(inr_disputes) / len(carrier_disputes)
        c_won = sum(1 for d in carrier_disputes if d.outcome == DisputeOutcome.WON)
        c_win_rate = c_won / len(carrier_disputes)
        avg_days = (
            sum(d.days_after_ship for d in carrier_disputes) / len(carrier_disputes)
            if carrier_disputes else 0
        )

        # Find high-risk regions for this carrier
        zip_counts: dict[str, int] = {}
        for d in carrier_disputes:
            prefix = d.shipping_zip[:3] if len(d.shipping_zip) >= 3 else d.shipping_zip
            zip_counts[prefix] = zip_counts.get(prefix, 0) + 1
        high_risk_zips = [z for z, cnt in sorted(zip_counts.items(), key=lambda x: -x[1]) if cnt >= 2][:3]

        carrier_profiles.append(CarrierProfile(
            carrier=carrier,
            total_disputes=len(carrier_disputes),
            inr_rate=round(inr_rate, 3),
            win_rate=round(c_win_rate, 3),
            avg_days_to_dispute=round(avg_days, 1),
            high_risk_regions=high_risk_zips,
        ))

    carrier_profiles.sort(key=lambda c: -c.inr_rate)

    # ── Geographic clustering ─────────────────────────────────────────────────
    # Cluster by country for international, ZIP prefix for domestic
    geo_data: dict[str, list[DisputeEvent]] = {}
    for d in disputes:
        if d.billing_country != "US":
            key = d.billing_country
        else:
            key = f"ZIP:{d.shipping_zip[:3]}" if len(d.shipping_zip) >= 3 else "ZIP:???"
        geo_data.setdefault(key, []).append(d)

    geo_clusters = []
    baseline_rate = n / max(total_orders_30d, n)  # fallback if no order total
    for region, region_disputes in geo_data.items():
        region_rate = len(region_disputes) / max(total_orders_30d, 1)
        rate_index = region_rate / max(baseline_rate, 0.001)

        # Dominant claim type
        rc_counts: dict[str, int] = {}
        for d in region_disputes:
            rc_counts[d.claim_type] = rc_counts.get(d.claim_type, 0) + 1
        dominant = max(rc_counts, key=lambda k: rc_counts[k]) if rc_counts else "UNKNOWN"

        rec = ""
        if rate_index >= 3.0:
            rec = f"Consider requiring signature confirmation for all {region} orders"
        elif rate_index >= 2.0:
            rec = f"Use premium carrier (FedEx/UPS) for {region} to improve tracking"

        geo_clusters.append(GeographicCluster(
            region=region,
            total_disputes=len(region_disputes),
            dispute_rate_index=round(rate_index, 2),
            dominant_claim_type=dominant,
            recommendation=rec,
        ))

    geo_clusters.sort(key=lambda g: -g.dispute_rate_index)

    # ── Root cause distribution ───────────────────────────────────────────────
    cause_total: dict[str, int] = {}
    for d in disputes:
        cause_total[d.root_cause.value] = cause_total.get(d.root_cause.value, 0) + 1
    cause_dist = {k: round(v / n, 3) for k, v in cause_total.items()}

    # ── Win rate by claim type ────────────────────────────────────────────────
    claim_wins: dict[str, list[int]] = {}
    for d in disputes:
        claim_wins.setdefault(d.claim_type, []).append(1 if d.outcome == DisputeOutcome.WON else 0)
    win_rate_by_claim = {
        ct: round(sum(wins) / len(wins), 3)
        for ct, wins in claim_wins.items()
    }

    # ── Insights and action items ─────────────────────────────────────────────
    insights = []
    actions = []

    # Top SKU insight
    if top_skus:
        worst_sku = top_skus[0]
        insights.append(
            f"SKU '{worst_sku.sku}' accounts for the most dispute losses "
            f"(${worst_sku.total_loss:.0f}, {worst_sku.total_disputes} disputes). "
            f"Top claim: {worst_sku.top_claim_types[0] if worst_sku.top_claim_types else 'N/A'}"
        )
        if worst_sku.total_loss > 200:
            actions.append(worst_sku.recommendation)

    # Carrier insight
    if carrier_profiles:
        worst_carrier = carrier_profiles[0]
        if worst_carrier.inr_rate > 0.5:
            insights.append(
                f"{worst_carrier.carrier} has {worst_carrier.inr_rate:.0%} INR rate — "
                f"consider switching to FedEx/UPS for high-value orders"
            )
            actions.append(
                f"Switch high-value orders (>$100) from {worst_carrier.carrier} to FedEx or UPS "
                f"Ground to reduce INR disputes ({worst_carrier.total_disputes} disputes at risk)"
            )

    # Win rate insight
    worst_claim = min(win_rate_by_claim.items(), key=lambda x: x[1]) if win_rate_by_claim else None
    if worst_claim and worst_claim[1] < 0.40:
        insights.append(
            f"Winning only {worst_claim[1]:.0%} of {worst_claim[0]} disputes — "
            f"evidence package needs improvement for this claim type"
        )
        actions.append(
            f"Build a stronger evidence template for {worst_claim[0]} disputes — "
            f"current win rate {worst_claim[1]:.0%} is below the 50% threshold"
        )

    # Fraud pattern
    fraud_disputes = [d for d in disputes if d.root_cause in (RootCause.FRAUD_SERIAL_DISPUTER, RootCause.FRAUD_FRIENDLY)]
    if len(fraud_disputes) > n * 0.15:
        insights.append(
            f"{len(fraud_disputes)/n:.0%} of disputes appear to be fraud "
            f"({len(fraud_disputes)}/{n}) — implement buyer intelligence screening at checkout"
        )
        actions.append(
            "Implement buyer risk scoring (oracle.buyer_intelligence) at checkout to block "
            "TIER_4/TIER_5 risk buyers before the transaction"
        )

    # Listing mismatch
    snad_mismatch = [d for d in disputes if d.root_cause == RootCause.LISTING_MISMATCH]
    if len(snad_mismatch) > n * 0.10:
        insights.append(
            f"{len(snad_mismatch)} disputes ({len(snad_mismatch)/n:.0%}) caused by listing "
            f"mismatch — product descriptions need audit"
        )
        actions.append(
            "Audit product listings for top SNAD SKUs — update descriptions/photos "
            f"to reduce 'significantly not as described' claims"
        )

    return DisputeDNA(
        total_disputes_analyzed=n,
        total_loss=round(total_loss, 2),
        overall_win_rate=round(overall_win_rate, 3),
        top_sku_clusters=top_skus,
        carrier_profiles=carrier_profiles,
        geographic_clusters=geo_clusters[:focus_top_n],
        root_cause_distribution=cause_dist,
        win_rate_by_claim_type=win_rate_by_claim,
        top_insights=insights,
        action_items=actions,
        reasoning=reasoning,
    )


def _sku_recommendation(
    sku: str,
    sku_disputes: list[DisputeEvent],
    top_claims: list[str],
    top_causes: list[RootCause],
) -> str:
    win_rate = sum(1 for d in sku_disputes if d.outcome == DisputeOutcome.WON) / len(sku_disputes)
    total_loss = sum(d.settlement_cost for d in sku_disputes)
    n = len(sku_disputes)

    if top_causes and top_causes[0] == RootCause.LISTING_MISMATCH:
        return f"SKU {sku}: Update product listing/photos — {n} SNAD disputes suggest description mismatch"
    elif top_causes and top_causes[0] == RootCause.PRODUCT_DEFECT:
        return f"SKU {sku}: QC review needed — {n} defect-related disputes; consider supplier change"
    elif top_causes and top_causes[0] == RootCause.FRAUD_SERIAL_DISPUTER:
        return f"SKU {sku}: High fraud target — add buyer verification requirement at checkout"
    elif top_claims and "NOT_RECEIVED" in top_claims[0]:
        return f"SKU {sku}: Add signature confirmation for all orders of this product"
    elif win_rate < 0.30 and total_loss > 100:
        return f"SKU {sku}: Win rate {win_rate:.0%} — strengthen evidence package template for this product"
    elif n >= 5 and total_loss > 300:
        return f"SKU {sku}: Consider removing from catalog — ${total_loss:.0f} in losses from {n} disputes"
    else:
        return f"SKU {sku}: Monitor — {n} disputes, ${total_loss:.0f} loss, {win_rate:.0%} win rate"


if __name__ == "__main__":
    sample = [
        DisputeEvent("D001", "SKU-HEADPHONES", "ITEM_NOT_RECEIVED", "USPS", "10001", "US", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
        DisputeEvent("D002", "SKU-HEADPHONES", "ITEM_NOT_RECEIVED", "USPS", "10002", "US", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
        DisputeEvent("D003", "SKU-HEADPHONES", "SIGNIFICANTLY_NOT_AS_DESCRIBED", "FedEx", "90210", "US", 89.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.LISTING_MISMATCH),
        DisputeEvent("D004", "SKU-CHARGER", "ITEM_NOT_RECEIVED", "USPS", "60601", "US", 25.0, DisputeOutcome.SETTLED, settlement_cost=12.5, root_cause=RootCause.CARRIER_DELIVERY_FAILURE),
        DisputeEvent("D005", "SKU-CHARGER", "ITEM_NOT_RECEIVED", "USPS", "60602", "US", 25.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.FRAUD_FRIENDLY),
        DisputeEvent("D006", "SKU-LAPTOP", "UNAUTHORIZED", "FedEx", "77001", "US", 850.0, DisputeOutcome.WON, settlement_cost=0.0, root_cause=RootCause.FRAUD_SERIAL_DISPUTER),
        DisputeEvent("D007", "SKU-HEADPHONES", "ITEM_NOT_RECEIVED", "USPS", "10003", "BR", 89.0, DisputeOutcome.LOST, settlement_cost=89.0, root_cause=RootCause.FRAUD_SERIAL_DISPUTER),
    ]
    dna = analyze_dispute_dna(sample, total_orders_30d=200)
    print(f"Analyzed {dna.total_disputes_analyzed} disputes, total loss ${dna.total_loss:.0f}")
    print(f"Overall win rate: {dna.overall_win_rate:.0%}")
    print(f"\nTop SKU clusters:")
    for sku in dna.top_sku_clusters:
        print(f"  {sku.sku}: {sku.total_disputes} disputes, ${sku.total_loss:.0f} loss, {sku.win_rate:.0%} win")
        print(f"    → {sku.recommendation}")
    print(f"\nInsights:")
    for insight in dna.top_insights:
        print(f"  💡 {insight}")
    print(f"\nAction items:")
    for action in dna.action_items:
        print(f"  → {action}")
