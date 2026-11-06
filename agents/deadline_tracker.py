"""
Deadline Guardian — never lose a dispute by default again.

PayPal's most brutal rule: miss the response deadline → automatic loss.
No appeal, no second chance, no refund of the dispute fee.
Default deadline: 10 days from dispute open (some claim types: 7 days).

Without this module:
  - Merchant goes on vacation
  - 3 disputes open while they're away
  - 10 days pass, no response
  - PayPal auto-closes: merchant loses all 3
  - $500 in chargebacks + $45 in dispute fees + 3 disputes added to rate
  - Rate crosses 1%, account gets limited

With this module:
  - Deadline stored in ES when dispute arrives
  - APScheduler checks every 30 minutes
  - Escalating Slack alerts: 7 days / 3 days / 24 hours / 6 hours / 2 hours
  - If merchant still hasn't responded at 2h before deadline:
    Guardian auto-accepts rather than allowing auto-loss
    (auto-accept = refund without dispute fee)
  - Deadline breach = never

Alert escalation schedule:
  7 days  → informational
  3 days  → attention needed
  24 hours → urgent
  6 hours  → CRITICAL — respond now
  2 hours  → auto-action triggered

Data stored per dispute:
  {
    "dispute_id": "PP-D-123",
    "merchant_id": "merch_1",
    "amount": 95.00,
    "reason_code": "ITEM_NOT_RECEIVED",
    "opened_at": "2026-11-01T12:00:00Z",
    "response_deadline": "2026-11-11T12:00:00Z",
    "last_alert_level": "7_days",
    "auto_acted": false
  }
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


ALERT_THRESHOLDS_HOURS = [
    (168, "7_days",   "🗓️ Dispute deadline in 7 days"),
    (72,  "3_days",   "⚠️ Dispute deadline in 3 days — please review"),
    (24,  "24_hours", "🔴 URGENT: Dispute deadline in 24 hours"),
    (6,   "6_hours",  "🚨 CRITICAL: Dispute deadline in 6 hours — respond NOW"),
    (2,   "2_hours",  "⛔ Auto-action in 2 hours unless you respond"),
]

# When less than this many hours remain and no human action, auto-accept
AUTO_ACTION_HOURS = 2


@dataclass
class OpenDispute:
    dispute_id: str
    merchant_id: str
    amount: float
    reason_code: str
    opened_at: str
    response_deadline: str
    last_alert_level: str = ""
    auto_acted: bool = False
    win_probability: float = 0.0


def compute_deadline(opened_at_iso: str, reason_code: str) -> str:
    """
    Compute the PayPal response deadline from dispute open time.
    Most disputes: 10 days. INQUIRY stage disputes: 20 days.
    """
    try:
        opened = datetime.fromisoformat(opened_at_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        opened = datetime.now(timezone.utc)

    # PayPal standard: 10 days for most dispute types
    # INQUIRY stage can extend to 20 days, but we're conservative
    days = 10
    if reason_code.upper() in {"INQUIRY", "BILLING_ISSUES", "BILLING_ISSUE"}:
        days = 20

    deadline = opened + timedelta(days=days)
    return deadline.isoformat()


def hours_until_deadline(response_deadline_iso: str) -> float:
    """Return fractional hours until the deadline (negative if past)."""
    try:
        deadline = datetime.fromisoformat(response_deadline_iso.replace("Z", "+00:00"))
        delta = deadline - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0
    except Exception:
        return -1.0


async def store_open_dispute(dispute: OpenDispute) -> bool:
    """
    Persist an open dispute to the ES deadline-tracking index.
    Uses op_type=create to avoid overwriting existing entries.
    """
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return False

    doc = {
        "dispute_id": dispute.dispute_id,
        "merchant_id": dispute.merchant_id,
        "amount": dispute.amount,
        "reason_code": dispute.reason_code,
        "opened_at": dispute.opened_at,
        "response_deadline": dispute.response_deadline,
        "last_alert_level": dispute.last_alert_level,
        "auto_acted": dispute.auto_acted,
        "win_probability": dispute.win_probability,
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.put(
                f"{es_url}/anchor-deadlines/_doc/{dispute.dispute_id}?op_type=create",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=doc,
            )
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"[DeadlineTracker] store failed for {dispute.dispute_id}: {exc}")
        return False


async def mark_dispute_resolved(dispute_id: str) -> bool:
    """Remove a dispute from the deadline-tracking index when resolved."""
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.delete(
                f"{es_url}/anchor-deadlines/_doc/{dispute_id}",
                headers={"Authorization": f"ApiKey {es_key}"},
            )
        return r.status_code in (200, 204)
    except Exception as exc:
        print(f"[DeadlineTracker] mark_resolved failed for {dispute_id}: {exc}")
        return False


async def update_alert_level(dispute_id: str, level: str, auto_acted: bool = False) -> bool:
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(
                f"{es_url}/anchor-deadlines/_update/{dispute_id}",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json={"doc": {"last_alert_level": level, "auto_acted": auto_acted}},
            )
        return r.status_code == 200
    except Exception as exc:
        print(f"[DeadlineTracker] update_alert_level failed for {dispute_id}: {exc}")
        return False


async def get_open_disputes(merchant_id: str = "") -> list[OpenDispute]:
    """Fetch all open (non-auto-acted) disputes from the deadline-tracking index."""
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return []

    filters = [{"term": {"auto_acted": False}}]
    if merchant_id:
        filters.append({"term": {"merchant_id": merchant_id}})

    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(
                f"{es_url}/anchor-deadlines/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json={"query": {"bool": {"filter": filters}}, "size": 200},
            )
        hits = r.json().get("hits", {}).get("hits", [])
        return [
            OpenDispute(
                dispute_id=h["_source"]["dispute_id"],
                merchant_id=h["_source"]["merchant_id"],
                amount=h["_source"]["amount"],
                reason_code=h["_source"]["reason_code"],
                opened_at=h["_source"]["opened_at"],
                response_deadline=h["_source"]["response_deadline"],
                last_alert_level=h["_source"].get("last_alert_level", ""),
                auto_acted=h["_source"].get("auto_acted", False),
                win_probability=h["_source"].get("win_probability", 0.0),
            )
            for h in hits
        ]
    except Exception as exc:
        print(f"[DeadlineTracker] get_open_disputes failed: {exc}")
        return []


async def check_deadlines(
    merchant_id: str,
    notify_fn,
    auto_accept_fn,
) -> dict:
    """
    Main deadline sweep — called by APScheduler every 30 minutes.

    For each open dispute:
      1. Calculate hours remaining
      2. Send escalating alert if threshold crossed
      3. Auto-accept if < AUTO_ACTION_HOURS remain

    Parameters
    ----------
    merchant_id    : merchant to check
    notify_fn      : async callable(message: str)  — sends Slack alert
    auto_accept_fn : async callable(dispute_id: str, amount: float, reason: str)
                     — calls PayPal accept-claim API

    Returns summary dict for logging.
    """
    disputes = await get_open_disputes(merchant_id)
    if not disputes:
        return {"checked": 0, "alerts_sent": 0, "auto_acted": 0}

    alerts_sent = 0
    auto_acted = 0
    past_deadline = 0

    for dispute in disputes:
        hours_left = hours_until_deadline(dispute.response_deadline)

        if hours_left < 0:
            # Past deadline — attempt late response (PayPal sometimes accepts within
            # a short grace window; never hurts to try)
            past_deadline += 1
            overdue_h = abs(hours_left)
            print(
                f"[DeadlineTracker] PAST DEADLINE: {dispute.dispute_id} "
                f"({overdue_h:.1f}h overdue)"
            )
            if dispute.last_alert_level != "past_deadline":
                # First time we've seen this past-deadline — notify and attempt late accept
                recovery_msg = (
                    f"🚨 PAST DEADLINE: `{dispute.dispute_id}` is {overdue_h:.1f}h overdue.\n"
                    f"Attempting late acceptance (PayPal grace window).\n"
                    f"Amount: ${dispute.amount:.2f} | Reason: {dispute.reason_code}"
                )
                await notify_fn(recovery_msg)
                try:
                    await auto_accept_fn(
                        dispute.dispute_id,
                        dispute.amount,
                        reason=f"deadline_recovery:{overdue_h:.1f}h_overdue",
                    )
                except Exception as exc:
                    print(f"[DeadlineTracker] Recovery accept failed for {dispute.dispute_id}: {exc}")
                await update_alert_level(dispute.dispute_id, "past_deadline", auto_acted=True)
            continue

        if hours_left <= AUTO_ACTION_HOURS and not dispute.auto_acted:
            # Auto-action: accept rather than auto-lose
            print(
                f"[DeadlineTracker] Auto-accepting {dispute.dispute_id} "
                f"({hours_left:.1f}h left — protecting from auto-loss)"
            )
            try:
                await auto_accept_fn(
                    dispute.dispute_id,
                    dispute.amount,
                    reason=f"deadline_auto_accept:{hours_left:.1f}h_remaining",
                )
                await update_alert_level(dispute.dispute_id, "auto_accepted", auto_acted=True)
                await notify_fn(
                    f"⛔ Deadline auto-accept: `{dispute.dispute_id}` — only {hours_left:.1f}h "
                    f"left, accepted ${dispute.amount:.2f} to avoid auto-loss. "
                    f"Check your disputes dashboard."
                )
                auto_acted += 1
            except Exception as exc:
                print(f"[DeadlineTracker] Auto-accept failed for {dispute.dispute_id}: {exc}")
            continue

        # Check which alert tier we should send
        for threshold_hours, level, msg in sorted(ALERT_THRESHOLDS_HOURS, key=lambda x: x[0]):
            if hours_left <= threshold_hours and dispute.last_alert_level != level:
                # Only send if this is a new threshold crossing
                if _is_higher_urgency(level, dispute.last_alert_level):
                    alert = (
                        f"{msg}\n"
                        f"Dispute `{dispute.dispute_id}` — ${dispute.amount:.2f} "
                        f"({dispute.reason_code})\n"
                        f"Win probability: {dispute.win_probability:.0%}\n"
                        f"Deadline: {dispute.response_deadline}\n"
                        f"Hours remaining: *{hours_left:.1f}h*"
                    )
                    await notify_fn(alert)
                    await update_alert_level(dispute.dispute_id, level)
                    alerts_sent += 1
                    break  # only one alert per check cycle per dispute

    return {
        "checked": len(disputes),
        "alerts_sent": alerts_sent,
        "auto_acted": auto_acted,
        "past_deadline": past_deadline,
    }


_ALERT_ORDER = ["", "7_days", "3_days", "24_hours", "6_hours", "2_hours", "auto_accepted", "past_deadline"]


def _is_higher_urgency(new_level: str, current_level: str) -> bool:
    """Returns True if new_level is a higher urgency than current_level."""
    new_idx = _ALERT_ORDER.index(new_level) if new_level in _ALERT_ORDER else 0
    cur_idx = _ALERT_ORDER.index(current_level) if current_level in _ALERT_ORDER else 0
    return new_idx > cur_idx


def generate_response_template(
    dispute: "OpenDispute",
    tracking_number: str = "",
    delivered: bool = False,
    delivery_date: str = "",
    delivery_location: str = "",
) -> str:
    """
    Generate a pre-filled PayPal dispute response ready to paste.

    Called automatically when a dispute hits the 24h alert threshold.
    If carrier data is available, includes confirmed delivery details.
    Returns a plain-text template the merchant can submit as-is or edit.
    """
    lines = [
        f"RE: Dispute {dispute.dispute_id}",
        f"Claim type: {dispute.reason_code}",
        f"Order amount: ${dispute.amount:.2f}",
        "",
    ]

    reason = dispute.reason_code.upper()

    if reason in {"ITEM_NOT_RECEIVED", "INR"} and tracking_number:
        if delivered and delivery_date:
            lines += [
                "Dear PayPal Review Team,",
                "",
                f"This order was shipped and confirmed delivered by the carrier.",
                f"Tracking number: {tracking_number}",
                f"Delivery confirmed: {delivery_date}",
            ]
            if delivery_location:
                lines.append(f"Delivery location: {delivery_location}")
            lines += [
                "",
                "The carrier's tracking record confirms the item was delivered "
                "to the buyer's address. I am attaching the carrier confirmation "
                "as evidence. Please close this dispute in the seller's favor.",
            ]
        else:
            lines += [
                "Dear PayPal Review Team,",
                "",
                f"The order has been shipped with tracking number: {tracking_number}",
                "The package is in transit. Please allow additional delivery time.",
                "I am happy to provide any additional information needed.",
            ]
    elif reason in {"SIGNIFICANTLY_NOT_AS_DESCRIBED", "SNAD"}:
        lines += [
            "Dear PayPal Review Team,",
            "",
            "The item was accurately described in our listing. I am providing:",
            "1. Original product listing with photos and description",
            "2. Order confirmation showing buyer accepted item description",
            "3. Any buyer communications prior to dispute",
            "",
            "The item delivered matches the listing in all material respects.",
        ]
    elif reason == "UNAUTHORIZED":
        lines += [
            "Dear PayPal Review Team,",
            "",
            "This transaction was authorized by the account holder. Evidence:",
            "1. Order placed from buyer's verified PayPal account",
            "2. Shipping address matches buyer's confirmed address on file",
            "3. No prior contact from buyer claiming account compromise",
        ]
    else:
        lines += [
            "Dear PayPal Review Team,",
            "",
            "I am responding to dispute " + dispute.dispute_id + ".",
            "Please find attached relevant documentation for this transaction.",
            "I am committed to resolving this matter fairly.",
        ]

    lines += ["", "Thank you for your review."]
    return "\n".join(lines)
