"""
Dispute Rate Monitor — real-time account survival system.

PayPal enforcement thresholds (US, 2026):
  < 0.5%  — Normal operation
  0.5–1%  — Elevated monitoring (soft warning)
  1–1.5%  — Account limitations: payment holds, sending limits, enhanced review
  1.5–3%  — High Dispute Program: $30 non-refundable fee per dispute
  > 3%    — Account suspension / termination

This module solves the problem no other tool addresses:
  "Should I fight THIS dispute, given where my dispute rate sits today?"

Classic example: you're at 0.97% rate with 1,000 eligible transactions.
The Oracle says FIGHT (EV = +$45). But if you fight and lose, your rate becomes 1.0%.
PayPal sees you at 1.0% next review → account limitations → payment holds.
Every $ of held funds costs you more than the $45 you would have recovered.
The correct decision is ACCEPT STRATEGICALLY and protect the account.

DisputeRateMonitor answers:
  - What is my exact dispute rate across 30/60/90-day windows?
  - Which threshold am I approaching and when will I cross it?
  - How many more disputes can I fight before the next threshold?
  - Should I accept this dispute to protect the account?
  - What is the projected rate if I fight all / accept all current open disputes?
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


# PayPal enforcement thresholds — standard seller program, US
THRESHOLDS = [
    (0.030, "critical",   "Account suspension / termination risk"),
    (0.015, "hdr",        "High Dispute Program: $30 non-refundable fee"),
    (0.010, "limited",    "Account limitations: payment holds, sending limits"),
    (0.005, "elevated",   "Elevated monitoring — approaching 1% limit"),
    (0.000, "safe",       "Normal operation"),
]

# Estimated business cost of threshold crossing (as a multiple of monthly revenue)
# Used to calculate the "threshold-crossing premium" in the Oracle override.
THRESHOLD_COST_FRACTION = {
    "elevated": 0.00,   # Soft monitoring — no immediate financial impact
    "limited":  0.05,   # ~5% monthly revenue in held funds / friction cost
    "hdr":      0.08,   # $30/dispute fee + operational overhead
    "critical": 0.25,   # Suspension = all revenue blocked
}


@dataclass
class DisputeRateWindow:
    window_days: int
    dispute_count: int
    dispute_amount: float
    capture_count: int
    capture_amount: float

    @property
    def rate_by_count(self) -> float:
        return self.dispute_count / self.capture_count if self.capture_count > 0 else 0.0

    @property
    def rate_by_amount(self) -> float:
        return self.dispute_amount / self.capture_amount if self.capture_amount > 0 else 0.0


@dataclass
class AccountHealth:
    merchant_id: str
    window_30d: DisputeRateWindow
    window_90d: DisputeRateWindow
    threshold_status: str          # "safe" | "elevated" | "limited" | "hdr" | "critical"
    threshold_description: str
    next_threshold: float          # rate value of next-higher threshold
    next_threshold_name: str
    safe_fight_quota: int          # disputes we can fight before crossing next threshold
    threshold_breach_at: int       # dispute count that would cross next threshold
    # Projections
    projected_rate_all_accepted: float   # floor: if all open disputes accepted
    projected_rate_all_fought: float     # ceiling: if all open disputes fought and lost
    # Recommendation
    fight_posture: str             # "fight_freely" | "fight_selectively" | "accept_strategically" | "accept_all"
    posture_reason: str
    # Raw context
    open_disputes: int             # count of unresolved disputes right now
    checked_at: str
    # Trajectory forecasting (new)
    rate_velocity_per_day: float = 0.0        # positive = rising, negative = falling
    days_until_next_threshold: Optional[int] = None  # None if rate stable/falling


def classify_rate(rate: float) -> tuple[str, str]:
    for threshold, name, description in THRESHOLDS:
        if rate >= threshold:
            return name, description
    return "safe", "Normal operation"


def next_threshold_above(current_rate: float) -> tuple[float, str]:
    thresholds_asc = list(reversed(THRESHOLDS))
    for threshold, name, description in thresholds_asc:
        if threshold > current_rate:
            return threshold, name
    return THRESHOLDS[0][0], THRESHOLDS[0][1]


async def get_account_health(merchant_id: str, open_disputes: int = 0) -> AccountHealth:
    """
    Pull dispute + capture metrics from Elasticsearch and calculate account health.

    Parameters
    ----------
    merchant_id   : merchant to query
    open_disputes : count of disputes currently awaiting decision (from caller context)
    """
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")

    w30 = await _query_window(merchant_id, 30, es_url, es_key)
    w90 = await _query_window(merchant_id, 90, es_url, es_key)

    rate_30d = w30.rate_by_amount   # PayPal uses amount-based rate
    status, description = classify_rate(rate_30d)
    next_t, next_name = next_threshold_above(rate_30d)

    # Safe fight quota: how many more disputes until next threshold
    # next_threshold = (disputes + extra) / captures  →  extra = next_t * captures - disputes
    if w30.capture_amount > 0:
        quota_as_amount = max(0.0, next_t * w30.capture_amount - w30.dispute_amount)
        # Approximate as count using average dispute size to give an integer
        avg_dispute_size = (w30.dispute_amount / w30.dispute_count) if w30.dispute_count > 0 else 50.0
        safe_quota_count = int(quota_as_amount / avg_dispute_size) if avg_dispute_size > 0 else 0
    else:
        safe_quota_count = 999
        quota_as_amount = 0.0

    breach_at = w30.dispute_count + safe_quota_count

    # Projections: what rate looks like if all open disputes are accepted vs fought+lost
    if w30.capture_amount > 0:
        avg_disp_size = (w30.dispute_amount / w30.dispute_count) if w30.dispute_count > 0 else 50.0
        open_amount = open_disputes * avg_disp_size
        proj_accepted = w30.rate_by_amount                           # accept = rate stays same
        proj_fought   = (w30.dispute_amount + open_amount) / w30.capture_amount  # fought+lost
    else:
        proj_accepted = 0.0
        proj_fought = 0.0

    # Fight posture
    posture, posture_reason = _calculate_posture(status, safe_quota_count, open_disputes)

    # Trajectory: compare 7-day rate to 30-day rate to get velocity
    w7 = await _query_window(merchant_id, 7, es_url, es_key)
    velocity, days_to_breach = _compute_trajectory(rate_30d, w7.rate_by_amount, next_t)

    return AccountHealth(
        merchant_id=merchant_id,
        window_30d=w30,
        window_90d=w90,
        threshold_status=status,
        threshold_description=description,
        next_threshold=next_t,
        next_threshold_name=next_name,
        safe_fight_quota=safe_quota_count,
        threshold_breach_at=breach_at,
        projected_rate_all_accepted=round(proj_accepted, 5),
        projected_rate_all_fought=round(proj_fought, 5),
        fight_posture=posture,
        posture_reason=posture_reason,
        open_disputes=open_disputes,
        checked_at=datetime.now(timezone.utc).isoformat(),
        rate_velocity_per_day=round(velocity, 6),
        days_until_next_threshold=days_to_breach,
    )


def _calculate_posture(status: str, quota: int, open_disputes: int) -> tuple[str, str]:
    if status in ("critical", "hdr"):
        return "accept_all", (
            "Rate is in High Dispute Program or critical zone — accept every dispute to "
            "reduce rate and protect account from suspension."
        )
    if status == "limited":
        return "accept_strategically", (
            "Rate is above 1%: account limitations active. Accept borderline disputes; "
            "only fight with very strong evidence (win prob ≥ 85%)."
        )
    if quota == 0:
        return "accept_strategically", (
            "No remaining fight quota before next threshold — accepting all disputes until "
            "rate drops below threshold."
        )
    if quota <= 3 or (open_disputes > 0 and quota < open_disputes):
        return "fight_selectively", (
            f"Only {quota} dispute(s) remain before threshold crossing. "
            "Fight only disputes with strong evidence (win prob ≥ 75%)."
        )
    if status == "elevated":
        return "fight_selectively", (
            "Rate is approaching 1% threshold — be selective, prefer disputes with tracking/delivery proof."
        )
    return "fight_freely", "Dispute rate is healthy — fight based on Oracle EV recommendation."


async def should_protect_rate(
    merchant_id: str,
    oracle_ev: float,
    oracle_win_prob: float,
    open_disputes: int = 0,
) -> tuple[bool, str, AccountHealth]:
    """
    Returns (should_override, override_reason, health).
    When True, the caller should change Oracle's FIGHT → ACCEPT to protect the account.
    """
    health = await get_account_health(merchant_id, open_disputes=open_disputes)

    if health.fight_posture == "accept_all":
        return True, health.posture_reason, health

    if health.fight_posture == "accept_strategically":
        if oracle_win_prob < 0.85:
            return True, (
                f"{health.posture_reason} Win probability {oracle_win_prob:.0%} < 85% threshold."
            ), health
        return False, "", health

    if health.fight_posture == "fight_selectively":
        if oracle_win_prob < 0.75:
            return True, (
                f"{health.posture_reason} Win probability {oracle_win_prob:.0%} < 75% threshold."
            ), health
        return False, "", health

    # fight_freely — no override
    return False, "", health


def _compute_trajectory(
    rate_30d: float,
    rate_7d: float,
    next_threshold: float,
) -> tuple[float, Optional[int]]:
    """
    Estimate rate velocity (per day) and days until next threshold breach.

    Uses 7-day rate as the recent trend vs 30-day baseline.
    If rate is falling (velocity ≤ 0), days_until = None.
    """
    # Daily velocity: difference between recent 7d rate and 30d average, divided by 7
    velocity = (rate_7d - rate_30d) / 7.0

    if velocity <= 0 or rate_30d >= next_threshold:
        return velocity, None

    gap = next_threshold - rate_30d
    days = int(gap / velocity) if velocity > 1e-9 else None
    return velocity, days


async def _query_window(
    merchant_id: str, days: int, es_url: str, es_key: str
) -> DisputeRateWindow:
    if not es_url or not es_key:
        return DisputeRateWindow(
            window_days=days, dispute_count=0, dispute_amount=0.0,
            capture_count=0, capture_amount=0.0,
        )

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"merchant_id": merchant_id}},
                    {"range": {"timestamp": {"gte": since}}},
                ]
            }
        },
        "aggs": {
            "disputes": {
                "filter": {"term": {"event_type": "eligible_dispute"}},
                "aggs": {
                    "total_amount": {"sum": {"field": "amount"}},
                    "count": {"value_count": {"field": "event_id"}},
                },
            },
            "captures": {
                "filter": {"term": {"event_type": "capture"}},
                "aggs": {
                    "total_amount": {"sum": {"field": "amount"}},
                    "count": {"value_count": {"field": "event_id"}},
                },
            },
        },
        "size": 0,
    }

    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(
                f"{es_url}/anchor-events/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query,
            )
        aggs = r.json().get("aggregations", {})
        d = aggs.get("disputes", {})
        cap = aggs.get("captures", {})
        return DisputeRateWindow(
            window_days=days,
            dispute_count=int(d.get("count", {}).get("value", 0) or 0),
            dispute_amount=float(d.get("total_amount", {}).get("value", 0) or 0),
            capture_count=int(cap.get("count", {}).get("value", 0) or 0),
            capture_amount=float(cap.get("total_amount", {}).get("value", 0) or 0),
        )
    except Exception as exc:
        print(f"[RiskMonitor] ES query failed ({exc})")
        return DisputeRateWindow(
            window_days=days, dispute_count=0, dispute_amount=0.0,
            capture_count=0, capture_amount=0.0,
        )
