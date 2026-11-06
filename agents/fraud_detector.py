"""
Buyer Risk Profiler — identifies serial disputors before they file a dispute.

The problem: "friendly fraud" accounts for ~40% of chargebacks. A serial disputer
files 3–5 disputes against different merchants before PayPal acts. Each merchant
absorbs the loss individually, never knowing the buyer has done this before.

With Anchor tracking all disputes and captures, we can:
  1. At capture time (when buyer pays): score the buyer's dispute history
  2. If high risk: flag for extra confirmation (require phone verification, etc.)
  3. At dispute time: automatically understand if this is a known disputer and
     factor that into the Oracle's win probability (sellers can submit prior-dispute
     pattern to PayPal as evidence)

PayPal's buyer risk signal:
  - >2 disputes in 90 days = elevated risk
  - >5 disputes in 180 days = high risk / serial disputer
  - Disputes that were reversed (seller won) = strong fraud signal when buyer disputes again

This module queries the Elasticsearch event store for buyer dispute history.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


@dataclass
class BuyerRiskProfile:
    buyer_email: str
    merchant_id: str
    total_disputes_90d: int
    total_disputes_180d: int
    disputes_reversed_90d: int     # buyer lost (seller won) — strong fraud signal
    total_transactions_90d: int
    dispute_rate: float            # disputes / transactions in 90d window
    risk_level: str                # "low" | "medium" | "high" | "critical"
    risk_score: float              # 0.0 – 1.0
    is_serial_disputer: bool
    recommendation: str
    flags: list[str] = field(default_factory=list)
    checked_at: str = ""

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()


def _score_buyer(
    disputes_90d: int,
    disputes_180d: int,
    reversed_90d: int,
    dispute_rate: float,
) -> tuple[float, str, bool, list[str]]:
    score = 0.0
    flags = []

    # Volume signals
    if disputes_90d >= 5:
        score += 0.50
        flags.append(f"{disputes_90d} disputes in last 90 days")
    elif disputes_90d >= 3:
        score += 0.30
        flags.append(f"{disputes_90d} disputes in last 90 days")
    elif disputes_90d >= 2:
        score += 0.15
        flags.append(f"{disputes_90d} disputes in last 90 days")

    if disputes_180d >= 8:
        score += 0.20
        flags.append(f"{disputes_180d} disputes in last 180 days")
    elif disputes_180d >= 5:
        score += 0.10

    # Reversal signals — when seller won previously and buyer disputes again
    if reversed_90d >= 2:
        score += 0.30
        flags.append(f"{reversed_90d} prior disputes reversed (seller won) — repeat fraud pattern")
    elif reversed_90d >= 1:
        score += 0.15
        flags.append(f"{reversed_90d} prior dispute reversed (seller won)")

    # Rate signal
    if dispute_rate >= 0.5:
        score += 0.20
        flags.append(f"{dispute_rate:.0%} dispute rate on transactions in 90d window")

    score = min(score, 1.0)
    is_serial = disputes_90d >= 3 or (disputes_180d >= 5 and reversed_90d >= 1)

    if score >= 0.70 or is_serial:
        level = "critical"
    elif score >= 0.45:
        level = "high"
    elif score >= 0.20:
        level = "medium"
    else:
        level = "low"

    return score, level, is_serial, flags


def _make_recommendation(level: str, is_serial: bool, risk_score: float) -> str:
    if level == "critical" or is_serial:
        return (
            "HIGH RISK — serial disputer profile. Require phone/ID verification before "
            "fulfillment. Document everything. If INR dispute filed, submit buyer dispute "
            "history as pattern-of-fraud evidence."
        )
    if level == "high":
        return (
            "Elevated risk. Add delivery signature requirement and photograph package at dispatch. "
            "Require address confirmation before shipping."
        )
    if level == "medium":
        return (
            "Moderate risk. Use tracking with delivery confirmation. Keep all order records."
        )
    return "Normal risk — no special action required."


async def score_buyer(buyer_email: str, merchant_id: str) -> BuyerRiskProfile:
    """
    Score a buyer based on their dispute history in Elasticsearch.
    Call this at capture time (when buyer pays) to get a pre-dispute risk signal.
    """
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")

    if not es_url or not es_key:
        return BuyerRiskProfile(
            buyer_email=buyer_email,
            merchant_id=merchant_id,
            total_disputes_90d=0,
            total_disputes_180d=0,
            disputes_reversed_90d=0,
            total_transactions_90d=0,
            dispute_rate=0.0,
            risk_level="low",
            risk_score=0.0,
            is_serial_disputer=False,
            recommendation="No ES configured — buyer history unavailable.",
        )

    now = datetime.now(timezone.utc)
    since_90d = (now - timedelta(days=90)).isoformat()
    since_180d = (now - timedelta(days=180)).isoformat()

    disputes_90d = await _count_buyer_disputes(buyer_email, since_90d, es_url, es_key)
    disputes_180d = await _count_buyer_disputes(buyer_email, since_180d, es_url, es_key)
    reversed_90d = await _count_buyer_reversals(buyer_email, since_90d, es_url, es_key)
    transactions_90d = await _count_buyer_transactions(buyer_email, since_90d, es_url, es_key)

    dispute_rate = disputes_90d / transactions_90d if transactions_90d > 0 else 0.0
    risk_score, risk_level, is_serial, flags = _score_buyer(
        disputes_90d, disputes_180d, reversed_90d, dispute_rate
    )
    recommendation = _make_recommendation(risk_level, is_serial, risk_score)

    return BuyerRiskProfile(
        buyer_email=buyer_email,
        merchant_id=merchant_id,
        total_disputes_90d=disputes_90d,
        total_disputes_180d=disputes_180d,
        disputes_reversed_90d=reversed_90d,
        total_transactions_90d=transactions_90d,
        dispute_rate=round(dispute_rate, 4),
        risk_level=risk_level,
        risk_score=round(risk_score, 3),
        is_serial_disputer=is_serial,
        recommendation=recommendation,
        flags=flags,
    )


async def _es_count(query: dict, es_url: str, es_key: str) -> int:
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(
                f"{es_url}/anchor-events/_count",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query,
            )
        return int(r.json().get("count", 0) or 0)
    except Exception as exc:
        print(f"[FraudDetector] ES count failed: {exc}")
        return 0


async def _count_buyer_disputes(buyer_email: str, since: str, es_url: str, es_key: str) -> int:
    return await _es_count({
        "query": {
            "bool": {
                "filter": [
                    {"term": {"buyer_email": buyer_email}},
                    {"term": {"event_type": "dispute_opened"}},
                    {"range": {"timestamp": {"gte": since}}},
                ]
            }
        }
    }, es_url, es_key)


async def _count_buyer_reversals(buyer_email: str, since: str, es_url: str, es_key: str) -> int:
    return await _es_count({
        "query": {
            "bool": {
                "filter": [
                    {"term": {"buyer_email": buyer_email}},
                    {"term": {"event_type": "dispute_resolved_seller_won"}},
                    {"range": {"timestamp": {"gte": since}}},
                ]
            }
        }
    }, es_url, es_key)


async def _count_buyer_transactions(buyer_email: str, since: str, es_url: str, es_key: str) -> int:
    return await _es_count({
        "query": {
            "bool": {
                "filter": [
                    {"term": {"buyer_email": buyer_email}},
                    {"term": {"event_type": "capture"}},
                    {"range": {"timestamp": {"gte": since}}},
                ]
            }
        }
    }, es_url, es_key)
