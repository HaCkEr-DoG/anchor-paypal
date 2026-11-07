"""
Dispute Cluster Analyzer — detect coordinated fraud patterns across disputes.

When multiple disputes share suspicious attributes (same buyer, same product,
same IP subnet, same day, similar amounts), it may indicate:
  - Friendly fraud rings: a buyer purchasing from multiple merchants
  - Coordinated chargeback abuse: multiple buyers targeting the same seller
  - Return fraud: serial SNAD claims on the same product listing

This oracle clusters a batch of disputes by shared attributes and scores each
cluster by its fraud signal strength.

Cluster dimensions (each contributes to the cluster fraud score):
  same_buyer_id         +0.40  — strongest signal (direct repeat)
  same_ip_subnet_24     +0.25  — same /24 IP block (possible same household/VPN)
  same_product_id       +0.20  — same SKU targeted repeatedly
  same_dispute_day      +0.10  — all disputed on same calendar day
  similar_amount        +0.05  — amounts within 10% of each other

A cluster needs ≥2 disputes to form. Clusters with fraud_score ≥ 0.60 are
flagged as HIGH_RISK; 0.30–0.59 as MODERATE; below as LOW.

PayPal benefit:
  - Cross-merchant cluster data (buyer_id patterns) only PayPal can see
  - HIGH_RISK clusters feed PayPal's buyer risk team for account review
  - Merchants see clusters within their own dispute history (product targeting)
  - Cluster data reduces duplicate investigation work across cases
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import List, Set, Optional


class ClusterRisk(str, Enum):
    HIGH_RISK = "HIGH_RISK"
    MODERATE  = "MODERATE"
    LOW       = "LOW"


@dataclass
class DisputeRecord:
    dispute_id: str
    buyer_id: str
    product_id: str
    ip_address: str             # IPv4 string; "" if unknown
    dispute_date: str           # ISO date "2026-10-15"
    transaction_amount: float
    reason_code: str


@dataclass
class DisputeCluster:
    cluster_id: str
    dispute_ids: List[str]
    shared_attributes: List[str]    # e.g. ["same_buyer_id", "same_product_id"]
    fraud_score: float
    risk_level: ClusterRisk
    recommendation: str


@dataclass
class ClusterAnalysisInput:
    merchant_id: str
    disputes: List[DisputeRecord]
    amount_similarity_threshold: float = 0.10   # 10% for "similar amount"


@dataclass
class ClusterAnalysisReport:
    merchant_id: str
    total_disputes_analyzed: int
    total_clusters_found: int
    high_risk_clusters: List[DisputeCluster]
    all_clusters: List[DisputeCluster]
    unclustered_dispute_ids: List[str]
    overall_risk: str               # HIGH / MODERATE / LOW / NONE
    paypal_signal: str


def _ip_subnet_24(ip: str) -> str:
    """Return /24 prefix, e.g. '192.168.1' from '192.168.1.100'."""
    if not ip:
        return ""
    parts = ip.split(".")
    if len(parts) != 4:
        return ip
    return ".".join(parts[:3])


def _amounts_similar(a: float, b: float, threshold: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= threshold


def _shared_attrs(d1: DisputeRecord, d2: DisputeRecord, threshold: float) -> List[str]:
    attrs = []
    if d1.buyer_id and d1.buyer_id == d2.buyer_id:
        attrs.append("same_buyer_id")
    sub1 = _ip_subnet_24(d1.ip_address)
    sub2 = _ip_subnet_24(d2.ip_address)
    if sub1 and sub1 == sub2:
        attrs.append("same_ip_subnet_24")
    if d1.product_id and d1.product_id == d2.product_id:
        attrs.append("same_product_id")
    if d1.dispute_date and d1.dispute_date == d2.dispute_date:
        attrs.append("same_dispute_day")
    if _amounts_similar(d1.transaction_amount, d2.transaction_amount, threshold):
        attrs.append("similar_amount")
    return attrs


_ATTR_WEIGHTS = {
    "same_buyer_id":    0.40,
    "same_ip_subnet_24":0.25,
    "same_product_id":  0.20,
    "same_dispute_day": 0.10,
    "similar_amount":   0.05,
}


def _fraud_score(shared: List[str]) -> float:
    return round(min(1.0, sum(_ATTR_WEIGHTS.get(a, 0.0) for a in shared)), 4)


def _risk_level(score: float) -> ClusterRisk:
    if score >= 0.60:
        return ClusterRisk.HIGH_RISK
    if score >= 0.30:
        return ClusterRisk.MODERATE
    return ClusterRisk.LOW


def analyze_dispute_clusters(inp: ClusterAnalysisInput) -> ClusterAnalysisReport:
    disputes = inp.disputes
    n = len(disputes)
    if n < 2:
        return ClusterAnalysisReport(
            merchant_id=inp.merchant_id,
            total_disputes_analyzed=n,
            total_clusters_found=0,
            high_risk_clusters=[],
            all_clusters=[],
            unclustered_dispute_ids=[d.dispute_id for d in disputes],
            overall_risk="NONE",
            paypal_signal=f"Merchant {inp.merchant_id}: {n} disputes — no clusters possible.",
        )

    # Build adjacency: pairs that share ≥1 attribute
    adj: dict = {d.dispute_id: set() for d in disputes}
    pair_attrs: dict = {}
    for d1, d2 in combinations(disputes, 2):
        shared = _shared_attrs(d1, d2, inp.amount_similarity_threshold)
        if shared:
            adj[d1.dispute_id].add(d2.dispute_id)
            adj[d2.dispute_id].add(d1.dispute_id)
            key = tuple(sorted([d1.dispute_id, d2.dispute_id]))
            pair_attrs[key] = shared

    # Union-find to group connected components
    parent = {d.dispute_id: d.dispute_id for d in disputes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for did, neighbors in adj.items():
        for nb in neighbors:
            union(did, nb)

    # Group by root
    groups: dict = {}
    for d in disputes:
        root = find(d.dispute_id)
        groups.setdefault(root, []).append(d.dispute_id)

    clusters: List[DisputeCluster] = []
    clustered: Set[str] = set()
    cluster_seq = 0

    for root, dids in groups.items():
        if len(dids) < 2:
            continue
        clustered.update(dids)
        # Collect all shared attributes across all pairs in this cluster
        all_attrs: Set[str] = set()
        for d1_id, d2_id in combinations(dids, 2):
            key = tuple(sorted([d1_id, d2_id]))
            if key in pair_attrs:
                all_attrs.update(pair_attrs[key])

        score = _fraud_score(list(all_attrs))
        risk = _risk_level(score)
        cluster_seq += 1

        if risk == ClusterRisk.HIGH_RISK:
            rec = (
                f"HIGH_RISK cluster of {len(dids)} disputes sharing: "
                f"{', '.join(sorted(all_attrs))}. "
                "Escalate to fraud review immediately."
            )
        elif risk == ClusterRisk.MODERATE:
            rec = (
                f"Moderate pattern across {len(dids)} disputes. "
                "Review for coordinated abuse; consider requiring extra verification."
            )
        else:
            rec = f"Low-signal cluster of {len(dids)} disputes. Monitor for escalation."

        clusters.append(DisputeCluster(
            cluster_id=f"CLU-{cluster_seq:03d}",
            dispute_ids=sorted(dids),
            shared_attributes=sorted(all_attrs),
            fraud_score=score,
            risk_level=risk,
            recommendation=rec,
        ))

    clusters.sort(key=lambda c: c.fraud_score, reverse=True)
    high_risk = [c for c in clusters if c.risk_level == ClusterRisk.HIGH_RISK]
    unclustered = [d.dispute_id for d in disputes if d.dispute_id not in clustered]

    if high_risk:
        overall = "HIGH"
    elif any(c.risk_level == ClusterRisk.MODERATE for c in clusters):
        overall = "MODERATE"
    elif clusters:
        overall = "LOW"
    else:
        overall = "NONE"

    paypal_signal = (
        f"Merchant {inp.merchant_id}: {n} disputes → "
        f"{len(clusters)} clusters, {len(high_risk)} high-risk. "
        f"Overall={overall}."
    )

    return ClusterAnalysisReport(
        merchant_id=inp.merchant_id,
        total_disputes_analyzed=n,
        total_clusters_found=len(clusters),
        high_risk_clusters=high_risk,
        all_clusters=clusters,
        unclustered_dispute_ids=unclustered,
        overall_risk=overall,
        paypal_signal=paypal_signal,
    )
