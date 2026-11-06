"""
Dispute Clustering Engine — group open disputes into clusters by shared
characteristics so the merchant can apply the same response template
across a batch, reducing per-dispute handling time.

Clustering dimensions:
  1. Claim type (INR / UNAUTHORIZED / SNAD / SUBSCRIPTION / DUPLICATE)
  2. Amount band (micro <$25, small $25-99, medium $100-499, large ≥$500)
  3. Network (PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER)
  4. Priority band (P1 / P2 / P3 / P4)
  5. Evidence readiness (evidence_complete / evidence_partial / no_evidence)

Output: a list of clusters, each with a template recommendation,
shared actions that apply to every dispute in the cluster, and an
estimated batch processing time.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AmountBand(str, Enum):
    MICRO   = "MICRO"    # <$25
    SMALL   = "SMALL"    # $25–$99
    MEDIUM  = "MEDIUM"   # $100–$499
    LARGE   = "LARGE"    # ≥$500


class EvidenceReadiness(str, Enum):
    COMPLETE = "COMPLETE"   # All required evidence is present
    PARTIAL  = "PARTIAL"    # Some evidence present
    NONE     = "NONE"       # No evidence available


@dataclass
class DisputeForClustering:
    """One dispute to be clustered."""
    dispute_id: str
    claim_type: str              # INR / UNAUTHORIZED / SNAD / SUBSCRIPTION / DUPLICATE
    amount: float
    network: str                 # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    priority_band: str           # P1 / P2 / P3 / P4
    evidence_readiness: EvidenceReadiness
    days_until_deadline: int
    sp_eligible: bool = False
    ce30_applicable: bool = False


@dataclass
class ClusterKey:
    claim_type: str
    amount_band: AmountBand
    network: str
    evidence_readiness: EvidenceReadiness

    def label(self) -> str:
        return f"{self.claim_type}/{self.amount_band.value}/{self.network}/{self.evidence_readiness.value}"


@dataclass
class DisputeCluster:
    """A group of disputes sharing a cluster key."""
    cluster_id: str
    key: ClusterKey
    dispute_ids: list[str]
    dispute_count: int
    total_amount: float
    min_days_until_deadline: int

    # Batch guidance
    template_name: str           # Suggested response template
    shared_actions: list[str]    # Actions that apply to every dispute in the batch
    estimated_minutes: float     # Estimated batch handling time (minutes)
    priority_order: int          # Lower = handle first


@dataclass
class ClusteringInput:
    """Input: a list of disputes to cluster."""
    merchant_id: str
    disputes: list[DisputeForClustering]
    minutes_per_dispute_manual: float = 15.0  # Baseline manual time per dispute


def _amount_band(amount: float) -> AmountBand:
    if amount < 25:
        return AmountBand.MICRO
    if amount < 100:
        return AmountBand.SMALL
    if amount < 500:
        return AmountBand.MEDIUM
    return AmountBand.LARGE


def _cluster_key(d: DisputeForClustering) -> ClusterKey:
    return ClusterKey(
        claim_type=d.claim_type.upper(),
        amount_band=_amount_band(d.amount),
        network=d.network.upper(),
        evidence_readiness=d.evidence_readiness,
    )


def _template(key: ClusterKey) -> str:
    ct = key.claim_type
    er = key.evidence_readiness
    if ct == "INR":
        if er == EvidenceReadiness.COMPLETE:
            return "INR_FULL_DEFENSE"
        if er == EvidenceReadiness.PARTIAL:
            return "INR_PARTIAL_DEFENSE"
        return "INR_CONCEDE"
    if ct == "UNAUTHORIZED":
        if er == EvidenceReadiness.COMPLETE:
            return "UNAUTH_FULL_DEFENSE"
        return "UNAUTH_BASIC_DEFENSE"
    if ct == "SNAD":
        if er == EvidenceReadiness.COMPLETE:
            return "SNAD_FULL_DEFENSE"
        return "SNAD_SETTLE_OFFER"
    if ct == "SUBSCRIPTION":
        return "SUBSCRIPTION_DEFENSE"
    if ct == "DUPLICATE":
        if er == EvidenceReadiness.COMPLETE:
            return "DUPLICATE_FIGHT"
        return "DUPLICATE_REFUND"
    return "GENERIC_DEFENSE"


def _shared_actions(key: ClusterKey, cluster_disputes: list[DisputeForClustering]) -> list[str]:
    actions: list[str] = []
    ct = key.claim_type

    if ct == "INR":
        actions.append("Pull carrier tracking for all disputes in this batch")
        if key.evidence_readiness == EvidenceReadiness.COMPLETE:
            actions.append("Submit INR defense template with tracking numbers")
        else:
            actions.append("Identify disputes missing tracking — consider individual review")
    elif ct == "UNAUTHORIZED":
        actions.append("Verify IP address logs for all disputes in this batch")
        if any(d.ce30_applicable for d in cluster_disputes):
            actions.append("Apply CE3.0 (Compelling Evidence 3.0) for eligible Visa disputes")
    elif ct == "SNAD":
        actions.append("Confirm product description accuracy for batch")
        actions.append("Gather return policy documentation for batch response")
    elif ct == "SUBSCRIPTION":
        actions.append("Pull billing agreement records for all subscription disputes")
    elif ct == "DUPLICATE":
        actions.append("Cross-check payment processor logs for all disputes")

    if key.amount_band == AmountBand.LARGE:
        actions.append("Review large-amount disputes individually before batch response")

    sp_disputes = [d for d in cluster_disputes if d.sp_eligible]
    if sp_disputes:
        actions.append(f"Flag {len(sp_disputes)} SP-eligible dispute(s) for priority handling")

    return actions


def _priority_order(key: ClusterKey, min_days: int) -> int:
    """Lower = handle first."""
    score = min_days * 10
    if key.amount_band == AmountBand.LARGE:
        score -= 20
    if key.evidence_readiness == EvidenceReadiness.COMPLETE:
        score -= 5
    if key.claim_type == "UNAUTHORIZED":
        score -= 10
    return max(0, score)


@dataclass
class ClusteringReport:
    """Full clustering result."""
    merchant_id: str
    total_disputes: int
    total_clusters: int
    clusters: list[DisputeCluster]  # Sorted by priority_order

    total_amount: float
    estimated_manual_minutes: float
    estimated_clustered_minutes: float
    time_savings_minutes: float

    summary: str


def cluster_disputes(inp: ClusteringInput) -> ClusteringReport:
    """Group disputes into clusters and generate batch guidance."""
    # Group by cluster key
    groups: dict[str, list[DisputeForClustering]] = {}
    for d in inp.disputes:
        key = _cluster_key(d)
        label = key.label()
        if label not in groups:
            groups[label] = []
        groups[label].append(d)

    clusters: list[DisputeCluster] = []
    for idx, (label, disputes) in enumerate(groups.items()):
        key = _cluster_key(disputes[0])
        min_days = min(d.days_until_deadline for d in disputes)
        total_amt = sum(d.amount for d in disputes)
        actions = _shared_actions(key, disputes)
        template = _template(key)
        porder = _priority_order(key, min_days)

        # Time estimate: batch is ~40% faster than individual
        est_minutes = len(disputes) * inp.minutes_per_dispute_manual * 0.60

        clusters.append(DisputeCluster(
            cluster_id=f"C{idx+1:03d}",
            key=key,
            dispute_ids=[d.dispute_id for d in disputes],
            dispute_count=len(disputes),
            total_amount=round(total_amt, 2),
            min_days_until_deadline=min_days,
            template_name=template,
            shared_actions=actions,
            estimated_minutes=round(est_minutes, 1),
            priority_order=porder,
        ))

    clusters.sort(key=lambda c: c.priority_order)

    total_disputes = len(inp.disputes)
    total_amount = sum(d.amount for d in inp.disputes)
    manual_time = total_disputes * inp.minutes_per_dispute_manual
    clustered_time = sum(c.estimated_minutes for c in clusters)
    savings = round(manual_time - clustered_time, 1)

    summary = (
        f"Clustering {inp.merchant_id}: "
        f"{total_disputes} disputes → {len(clusters)} clusters | "
        f"time_savings={savings:.0f}min | total=${total_amount:.2f}"
    )

    return ClusteringReport(
        merchant_id=inp.merchant_id,
        total_disputes=total_disputes,
        total_clusters=len(clusters),
        clusters=clusters,
        total_amount=round(total_amount, 2),
        estimated_manual_minutes=round(manual_time, 1),
        estimated_clustered_minutes=round(clustered_time, 1),
        time_savings_minutes=savings,
        summary=summary,
    )
