"""
Appeal Window Tracker — compute remaining time in dispute appeal windows
and emit urgency-ranked action deadlines for all open disputes.

Covers:
  - PayPal internal response deadlines (10 days)
  - Visa/MC/Amex/Discover network appeal windows (45 days)
  - Pre-arbitration windows (Visa/MC/Discover: 30 days after CB)
  - Arbitration filing deadlines (45 days after pre-arb rejection)
  - Stage-specific windows: response, appeal, pre-arb, arbitration

Output: per-dispute deadline summary with urgency bands
  CRITICAL (≤2 days), HIGH (3–5 days), MODERATE (6–14 days), LOW (>14 days)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

DAY_SECS = 86400


class Network(str, Enum):
    PAYPAL   = "PAYPAL"
    VISA     = "VISA"
    MC       = "MC"
    AMEX     = "AMEX"
    DISCOVER = "DISCOVER"


class DisputeStage(str, Enum):
    INQUIRY            = "INQUIRY"
    CHARGEBACK         = "CHARGEBACK"
    PRE_ARBITRATION    = "PRE_ARBITRATION"
    ARBITRATION        = "ARBITRATION"
    CLOSED             = "CLOSED"


class UrgencyBand(str, Enum):
    CRITICAL = "CRITICAL"   # ≤2 days
    HIGH     = "HIGH"       # 3–5 days
    MODERATE = "MODERATE"   # 6–14 days
    LOW      = "LOW"        # >14 days
    EXPIRED  = "EXPIRED"    # already past


# ── Network-specific window sizes (days) ─────────────────────────────
_RESPONSE_WINDOWS: dict[Network, int] = {
    Network.PAYPAL:   10,
    Network.VISA:     20,
    Network.MC:       20,
    Network.AMEX:     20,
    Network.DISCOVER: 20,
}

_APPEAL_WINDOWS: dict[Network, int] = {
    Network.PAYPAL:   10,
    Network.VISA:     45,
    Network.MC:       45,
    Network.AMEX:     45,
    Network.DISCOVER: 45,
}

_PRE_ARB_WINDOWS: dict[Network, int] = {
    Network.PAYPAL:   0,    # No pre-arb for PayPal
    Network.VISA:     30,
    Network.MC:       30,
    Network.AMEX:     0,    # No pre-arb for Amex
    Network.DISCOVER: 30,
}

_ARB_WINDOWS: dict[Network, int] = {
    Network.PAYPAL:   0,    # No arbitration for PayPal
    Network.VISA:     45,
    Network.MC:       45,
    Network.AMEX:     45,
    Network.DISCOVER: 45,
}


@dataclass
class DisputeWindowInput:
    """Input for a single dispute's appeal window calculation."""
    dispute_id: str
    network: Network
    stage: DisputeStage

    # Epoch timestamps for stage-entry events
    stage_opened_epoch: int               # When current stage started
    now_epoch: int                        # Current timestamp (for testing)

    # Optional timestamps for other stages (used to compute remaining windows)
    chargeback_opened_epoch: Optional[int] = None
    pre_arb_opened_epoch: Optional[int] = None

    transaction_amount: float = 0.0
    reason_code: str = ""


@dataclass
class DeadlineItem:
    """A single deadline with remaining time."""
    label: str
    deadline_epoch: int
    days_remaining: float
    urgency: UrgencyBand
    action_required: str


@dataclass
class AppealWindowReport:
    """Full appeal window report for one dispute."""
    dispute_id: str
    network: Network
    stage: DisputeStage
    now_epoch: int

    deadlines: list[DeadlineItem]           # All relevant deadlines, sorted soonest first
    primary_deadline: Optional[DeadlineItem]  # Most urgent actionable deadline
    any_window_expired: bool
    can_still_appeal: bool

    summary: str


def _urgency(days: float) -> UrgencyBand:
    if days < 0:
        return UrgencyBand.EXPIRED
    if days <= 2:
        return UrgencyBand.CRITICAL
    if days <= 5:
        return UrgencyBand.HIGH
    if days <= 14:
        return UrgencyBand.MODERATE
    return UrgencyBand.LOW


def _make_deadline(label: str, opened_epoch: int, window_days: int,
                   now_epoch: int, action: str) -> Optional[DeadlineItem]:
    if window_days <= 0:
        return None
    deadline = opened_epoch + window_days * DAY_SECS
    days_rem = (deadline - now_epoch) / DAY_SECS
    return DeadlineItem(
        label=label,
        deadline_epoch=deadline,
        days_remaining=round(days_rem, 2),
        urgency=_urgency(days_rem),
        action_required=action,
    )


def track_appeal_windows(inp: DisputeWindowInput) -> AppealWindowReport:
    """Compute all appeal deadlines for a dispute and rank by urgency."""
    deadlines: list[DeadlineItem] = []

    if inp.stage == DisputeStage.INQUIRY:
        d = _make_deadline(
            "Inquiry response",
            inp.stage_opened_epoch,
            _RESPONSE_WINDOWS[inp.network],
            inp.now_epoch,
            "Respond to inquiry or escalates to chargeback",
        )
        if d:
            deadlines.append(d)

    elif inp.stage == DisputeStage.CHARGEBACK:
        d = _make_deadline(
            "Chargeback response",
            inp.stage_opened_epoch,
            _RESPONSE_WINDOWS[inp.network],
            inp.now_epoch,
            "Submit evidence to contest chargeback",
        )
        if d:
            deadlines.append(d)

        # Appeal window (starts at same time for most networks)
        d2 = _make_deadline(
            "Chargeback appeal",
            inp.stage_opened_epoch,
            _APPEAL_WINDOWS[inp.network],
            inp.now_epoch,
            "File appeal to escalate to pre-arbitration",
        )
        if d2:
            deadlines.append(d2)

    elif inp.stage == DisputeStage.PRE_ARBITRATION:
        base = inp.pre_arb_opened_epoch or inp.stage_opened_epoch
        d = _make_deadline(
            "Pre-arbitration response",
            base,
            _PRE_ARB_WINDOWS[inp.network],
            inp.now_epoch,
            "Respond to pre-arb or accept liability",
        )
        if d:
            deadlines.append(d)

        # Arbitration filing window (after pre-arb rejection)
        d2 = _make_deadline(
            "Arbitration filing",
            base,
            _ARB_WINDOWS[inp.network],
            inp.now_epoch,
            "File for arbitration to escalate dispute",
        )
        if d2:
            deadlines.append(d2)

    elif inp.stage == DisputeStage.ARBITRATION:
        d = _make_deadline(
            "Arbitration response",
            inp.stage_opened_epoch,
            45,   # Standard 45-day arb window for all networks
            inp.now_epoch,
            "Submit final evidence to arbitration panel",
        )
        if d:
            deadlines.append(d)

    # Sort by days_remaining ascending (soonest first, expired last)
    deadlines.sort(key=lambda x: x.days_remaining if x.days_remaining >= 0 else 999_999)

    any_expired = any(d.urgency == UrgencyBand.EXPIRED for d in deadlines)
    can_appeal  = any(d.urgency != UrgencyBand.EXPIRED for d in deadlines)
    primary     = deadlines[0] if deadlines else None

    if primary:
        summary = (
            f"Dispute {inp.dispute_id} [{inp.network.value}/{inp.stage.value}]: "
            f"primary deadline '{primary.label}' in {primary.days_remaining:.1f}d "
            f"({primary.urgency.value})"
        )
    else:
        summary = (
            f"Dispute {inp.dispute_id} [{inp.network.value}/{inp.stage.value}]: "
            f"no active deadlines (stage={inp.stage.value})"
        )

    return AppealWindowReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        stage=inp.stage,
        now_epoch=inp.now_epoch,
        deadlines=deadlines,
        primary_deadline=primary,
        any_window_expired=any_expired,
        can_still_appeal=can_appeal,
        summary=summary,
    )
