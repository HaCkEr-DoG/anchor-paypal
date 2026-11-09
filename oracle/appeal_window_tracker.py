"""
Appeal Window Tracker — compute all appeal deadlines across PayPal
dispute lifecycle stages (chargeback → pre-arb → arbitration) and
flag expiring or expired windows.

Networks:
  PAYPAL   — only chargeback stage, 10d window, no appeals
  VISA     — 20d chargeback, 30d pre-arb, 45d arb
  MC       — 20d chargeback, 30d pre-arb, 45d arb
  AMEX     — 20d chargeback, no pre-arb, 45d arb
  DISCOVER — 20d chargeback, 30d pre-arb, 45d arb (Discover never does arb in practice)

All windows are calendar days from phase start.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL   = "PAYPAL"
    VISA     = "VISA"
    MC       = "MC"
    AMEX     = "AMEX"
    DISCOVER = "DISCOVER"


class DisputeStage(str, Enum):
    CHARGEBACK  = "CHARGEBACK"
    PRE_ARB     = "PRE_ARB"
    ARBITRATION = "ARBITRATION"


class WindowStatus(str, Enum):
    OPEN     = "OPEN"       # Window is open, time remains
    EXPIRING = "EXPIRING"   # ≤ 3 days left — urgent
    EXPIRED  = "EXPIRED"    # Past deadline
    NA       = "NA"         # This stage not available for the network


@dataclass
class StageWindow:
    stage: DisputeStage
    total_days: int
    epoch_start: int
    epoch_deadline: int
    days_elapsed: int
    days_remaining: int
    pct_elapsed: float      # 0.0–1.0
    status: WindowStatus
    action_required: str


@dataclass
class AppealWindowInput:
    dispute_id: str
    network: str
    stage: str              # Current DisputeStage value
    stage_started_epoch: int
    now_epoch: int


@dataclass
class AppealWindowReport:
    dispute_id: str
    network: str
    current_stage: DisputeStage

    current_window: StageWindow
    next_window: Optional[StageWindow]       # Pre-arb (if applicable)
    final_window: Optional[StageWindow]      # Arbitration (if applicable)

    overall_status: WindowStatus             # Most urgent across windows
    days_to_act: int                         # Days left in current window
    critical_actions: list[str]

    has_pre_arb: bool
    has_arb: bool

    summary: str


# Network window tables: stage → days
_WINDOWS: dict[str, dict[DisputeStage, Optional[int]]] = {
    "PAYPAL":   {DisputeStage.CHARGEBACK: 10, DisputeStage.PRE_ARB: None, DisputeStage.ARBITRATION: None},
    "VISA":     {DisputeStage.CHARGEBACK: 20, DisputeStage.PRE_ARB: 30,   DisputeStage.ARBITRATION: 45},
    "MC":       {DisputeStage.CHARGEBACK: 20, DisputeStage.PRE_ARB: 30,   DisputeStage.ARBITRATION: 45},
    "AMEX":     {DisputeStage.CHARGEBACK: 20, DisputeStage.PRE_ARB: None, DisputeStage.ARBITRATION: 45},
    "DISCOVER": {DisputeStage.CHARGEBACK: 20, DisputeStage.PRE_ARB: 30,   DisputeStage.ARBITRATION: 45},
}

_ACTION_BY_STAGE: dict[DisputeStage, str] = {
    DisputeStage.CHARGEBACK:  "File dispute response with supporting evidence",
    DisputeStage.PRE_ARB:     "File pre-arbitration rebuttal package",
    DisputeStage.ARBITRATION: "Submit formal arbitration filing through network portal",
}

_STAGE_ORDER = [DisputeStage.CHARGEBACK, DisputeStage.PRE_ARB, DisputeStage.ARBITRATION]

DAY_SECS = 86400


def _make_window(
    stage: DisputeStage,
    total_days: Optional[int],
    epoch_start: int,
    now: int,
) -> StageWindow:
    if total_days is None:
        return StageWindow(
            stage=stage, total_days=0,
            epoch_start=epoch_start, epoch_deadline=epoch_start,
            days_elapsed=0, days_remaining=0, pct_elapsed=1.0,
            status=WindowStatus.NA, action_required="Not applicable for this network",
        )

    epoch_dl  = epoch_start + total_days * DAY_SECS
    elapsed_s = now - epoch_start
    elapsed_d = max(0, int(elapsed_s / DAY_SECS))
    remain_d  = max(-1, int((epoch_dl - now) / DAY_SECS))
    pct       = round(min(1.0, max(0.0, elapsed_d / total_days)), 4)

    if remain_d < 0:
        status = WindowStatus.EXPIRED
    elif remain_d <= 3:
        status = WindowStatus.EXPIRING
    else:
        status = WindowStatus.OPEN

    action = _ACTION_BY_STAGE.get(stage, "Take action before deadline")
    return StageWindow(
        stage=stage, total_days=total_days,
        epoch_start=epoch_start, epoch_deadline=epoch_dl,
        days_elapsed=elapsed_d, days_remaining=max(0, remain_d),
        pct_elapsed=pct, status=status, action_required=action,
    )


def track_appeal_windows(inp: AppealWindowInput) -> AppealWindowReport:
    """Compute all appeal windows for a dispute at its current stage."""
    net   = inp.network.upper()
    stage = DisputeStage(inp.stage.upper())
    table = _WINDOWS.get(net, _WINDOWS["PAYPAL"])

    has_pre_arb = table.get(DisputeStage.PRE_ARB) is not None
    has_arb     = table.get(DisputeStage.ARBITRATION) is not None

    # Build all three stage windows from the same phase_start
    # (network deadlines are independent; each counts from the phase start
    # of that stage, but here we approximate from the initial stage start
    # for a merged view)
    current_w = _make_window(stage, table.get(stage), inp.stage_started_epoch, inp.now_epoch)

    # Pre-arb and arb windows are anchored to dispute start (approximate)
    next_stage_idx = _STAGE_ORDER.index(stage) + 1
    next_w: Optional[StageWindow] = None
    final_w: Optional[StageWindow] = None

    if next_stage_idx < len(_STAGE_ORDER):
        next_stage = _STAGE_ORDER[next_stage_idx]
        next_w = _make_window(next_stage, table.get(next_stage), inp.stage_started_epoch, inp.now_epoch)

    if has_arb and stage != DisputeStage.ARBITRATION:
        final_w = _make_window(DisputeStage.ARBITRATION, table.get(DisputeStage.ARBITRATION),
                                inp.stage_started_epoch, inp.now_epoch)

    # Overall status = most urgent
    all_w = [w for w in [current_w, next_w, final_w] if w and w.status != WindowStatus.NA]
    status_priority = {WindowStatus.EXPIRED: 3, WindowStatus.EXPIRING: 2,
                       WindowStatus.OPEN: 1, WindowStatus.NA: 0}
    overall = max(all_w, key=lambda w: status_priority[w.status]).status if all_w else WindowStatus.NA

    days_to_act = current_w.days_remaining

    # Critical actions
    crits: list[str] = []
    if current_w.status == WindowStatus.EXPIRED:
        crits.append(f"EXPIRED: {stage.value} window closed — no further action possible at this stage")
    elif current_w.status == WindowStatus.EXPIRING:
        crits.append(f"EXPIRING: {days_to_act} day(s) left — {current_w.action_required}")
    else:
        crits.append(f"{days_to_act} days remaining in {stage.value} window — {current_w.action_required}")

    if next_w and next_w.status not in (WindowStatus.NA, WindowStatus.EXPIRED):
        if has_pre_arb and stage == DisputeStage.CHARGEBACK:
            crits.append(f"Pre-arb escalation available if chargeback is lost")
    if has_arb:
        crits.append(f"Arbitration available as final option ({table[DisputeStage.ARBITRATION]}d window from stage start)")

    summary = (
        f"Appeal {inp.dispute_id} [{net}|{stage.value}]: "
        f"days_remaining={days_to_act} | status={current_w.status.value} | "
        f"pre_arb={has_pre_arb} arb={has_arb}"
    )

    return AppealWindowReport(
        dispute_id=inp.dispute_id,
        network=net,
        current_stage=stage,
        current_window=current_w,
        next_window=next_w,
        final_window=final_w,
        overall_status=overall,
        days_to_act=days_to_act,
        critical_actions=crits,
        has_pre_arb=has_pre_arb,
        has_arb=has_arb,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
track_appeal_window = track_appeal_windows
DisputeWindowInput = AppealWindowInput
UrgencyBand = WindowStatus


class AppealOutcome(str, Enum):
    WON = "WON"
    LOST = "LOST"
    SETTLED = "SETTLED"
    PENDING = "PENDING"


class AppealRecommendation(str, Enum):
    APPEAL_NOW = "APPEAL_NOW"
    PREPARE_APPEAL = "PREPARE_APPEAL"
    WAIT = "WAIT"
    NO_ACTION = "NO_ACTION"
