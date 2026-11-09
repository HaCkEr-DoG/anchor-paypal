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
    CHARGEBACK      = "CHARGEBACK"
    PRE_ARB         = "PRE_ARB"
    PRE_ARBITRATION = "PRE_ARBITRATION"
    ARBITRATION     = "ARBITRATION"
    INQUIRY         = "INQUIRY"
    CLOSED          = "CLOSED"


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
    network: str = "PAYPAL"
    stage: str = "CHARGEBACK"
    stage_started_epoch: int = 0
    now_epoch: int = 0
    # wave35 extra fields (optional, ignored by old track_appeal_windows if stage_started_epoch set)
    disputed_amount: float = 100.0
    ruling_outcome: object = None
    days_since_ruling: int = 3
    appeal_cost: float = 25.0
    chargeback_fee: float = 20.0
    has_new_evidence: bool = False
    base_win_probability: float = 0.35


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
# Wave48 / Wave35 test-API
# ---------------------------------------------------------------------------
from enum import Enum as _Enum8
from dataclasses import dataclass as _dc8, field as _f8

# Save original before override so wave57 path can still call it
_track_appeal_windows_original = track_appeal_windows


class UrgencyBand(str, _Enum8):  # noqa: F811 — replaces WindowStatus aliases
    CRITICAL = "CRITICAL"   # ≤ 2 days
    HIGH     = "HIGH"       # 3–7 days
    MEDIUM   = "MEDIUM"     # 8–14 days
    LOW      = "LOW"        # > 14 days
    EXPIRED  = "EXPIRED"    # past deadline
    NA       = "NA"


# Network window tables for wave48 (days per stage)
_W48_WINDOWS = {
    "PAYPAL": {
        DisputeStage.INQUIRY: 10,
        DisputeStage.CHARGEBACK: 10,
        DisputeStage.PRE_ARB: 0,
        DisputeStage.PRE_ARBITRATION: 0,
        DisputeStage.ARBITRATION: 0,
    },
    "VISA": {
        DisputeStage.INQUIRY: 20,
        DisputeStage.CHARGEBACK: 20,
        DisputeStage.PRE_ARB: 30,
        DisputeStage.PRE_ARBITRATION: 30,
        DisputeStage.ARBITRATION: 45,
    },
    "MC": {
        DisputeStage.INQUIRY: 20,
        DisputeStage.CHARGEBACK: 20,
        DisputeStage.PRE_ARB: 30,
        DisputeStage.PRE_ARBITRATION: 30,
        DisputeStage.ARBITRATION: 45,
    },
    "AMEX": {
        DisputeStage.INQUIRY: 20,
        DisputeStage.CHARGEBACK: 20,
        DisputeStage.PRE_ARB: 0,
        DisputeStage.PRE_ARBITRATION: 0,
        DisputeStage.ARBITRATION: 45,
    },
    "DISCOVER": {
        DisputeStage.INQUIRY: 20,
        DisputeStage.CHARGEBACK: 20,
        DisputeStage.PRE_ARB: 30,
        DisputeStage.PRE_ARBITRATION: 30,
        DisputeStage.ARBITRATION: 45,
    },
}

_DAY = 86400


@_dc8
class DeadlineEntry:
    label: str
    days_remaining: float
    urgency: UrgencyBand


@_dc8
class DisputeWindowInput:
    dispute_id: str
    network: object = None   # Network enum or str
    stage: object = None     # DisputeStage enum or str
    stage_opened_epoch: int = 0
    now_epoch: int = 0


@_dc8
class _WindowReport48:
    dispute_id: str
    deadlines: list
    can_still_appeal: bool
    any_window_expired: bool
    primary_deadline: object    # DeadlineEntry or None
    summary: str


def _urgency_for_days(days: float) -> UrgencyBand:
    if days < 0:
        return UrgencyBand.EXPIRED
    if days <= 2:
        return UrgencyBand.CRITICAL
    if days <= 7:
        return UrgencyBand.HIGH
    if days <= 14:
        return UrgencyBand.MEDIUM
    return UrgencyBand.LOW


def track_appeal_windows(inp) -> object:  # noqa: F811
    """Compute appeal window deadlines for a dispute. Handles both old and new input types."""
    # wave57 path: AppealWindowInput with stage_started_epoch set → original AppealWindowReport
    if isinstance(inp, AppealWindowInput) and inp.stage_started_epoch != 0:
        return _track_appeal_windows_original(inp)

    # Legacy path: has stage_started_epoch field but no stage_opened_epoch (old code)
    if hasattr(inp, "stage_started_epoch") and not hasattr(inp, "stage_opened_epoch"):
        # Legacy path — reproduce original logic
        net = inp.network.upper() if isinstance(inp.network, str) else inp.network.value
        stage_val = inp.stage.upper() if isinstance(inp.stage, str) else inp.stage
        try:
            stage_enum = DisputeStage(stage_val)
        except ValueError:
            stage_enum = DisputeStage.CHARGEBACK

        win_map = _W48_WINDOWS.get(net, _W48_WINDOWS["VISA"])
        days_open = int((inp.now_epoch - inp.stage_started_epoch) / _DAY)
        deadlines = []
        for stage_e, total_days in win_map.items():
            if total_days <= 0:
                continue
            remaining = max(-1.0, total_days - days_open)
            urgency = _urgency_for_days(remaining)
            label_map = {
                DisputeStage.INQUIRY: "inquiry response",
                DisputeStage.CHARGEBACK: "dispute response",
                DisputeStage.PRE_ARB: "pre-arbitration",
                DisputeStage.PRE_ARBITRATION: "pre-arbitration",
                DisputeStage.ARBITRATION: "arbitration",
            }
            label = f"File {label_map.get(stage_e, str(stage_e))} ({total_days}d)"
            deadlines.append(DeadlineEntry(label=label, days_remaining=max(0.0, remaining), urgency=urgency))
        deadlines.sort(key=lambda d: d.days_remaining)
        primary = deadlines[0] if deadlines else None
        can_appeal = any(d.urgency != UrgencyBand.EXPIRED for d in deadlines)
        any_expired = any(d.urgency == UrgencyBand.EXPIRED for d in deadlines)
        summary = f"Dispute {inp.dispute_id} [{net}]: {len(deadlines)} deadlines"
        return _WindowReport48(
            dispute_id=inp.dispute_id,
            deadlines=deadlines,
            can_still_appeal=can_appeal,
            any_window_expired=any_expired,
            primary_deadline=primary,
            summary=summary,
        )

    # New wave48 path with stage_opened_epoch
    if not isinstance(inp, DisputeWindowInput):
        # Fallback for unknown types
        return _WindowReport48(
            dispute_id=getattr(inp, "dispute_id", "?"),
            deadlines=[], can_still_appeal=False,
            any_window_expired=False, primary_deadline=None,
            summary="Unknown input type",
        )

    net = inp.network.value if hasattr(inp.network, "value") else str(inp.network).upper()
    stage_val = inp.stage.value if hasattr(inp.stage, "value") else str(inp.stage).upper()
    try:
        stage_enum = DisputeStage(stage_val)
    except ValueError:
        stage_enum = DisputeStage.CHARGEBACK

    if stage_enum == DisputeStage.CLOSED:
        return _WindowReport48(
            dispute_id=inp.dispute_id,
            deadlines=[], can_still_appeal=False,
            any_window_expired=False, primary_deadline=None,
            summary=f"Dispute {inp.dispute_id}: CLOSED — no active deadlines",
        )

    days_elapsed = (inp.now_epoch - inp.stage_opened_epoch) / _DAY
    win_map = _W48_WINDOWS.get(net, _W48_WINDOWS["VISA"])

    # Build deadlines based on current stage
    stage_sequence = [
        DisputeStage.INQUIRY,
        DisputeStage.CHARGEBACK,
        DisputeStage.PRE_ARBITRATION,
        DisputeStage.ARBITRATION,
    ]
    label_map = {
        DisputeStage.INQUIRY: "inquiry response",
        DisputeStage.CHARGEBACK: "dispute response",
        DisputeStage.PRE_ARB: "pre-arbitration",
        DisputeStage.PRE_ARBITRATION: "pre-arbitration",
        DisputeStage.ARBITRATION: "arbitration",
    }

    # For the current stage, compute response deadline
    # For subsequent stages, compute cumulative deadlines
    deadlines = []
    # Response deadline for current stage
    curr_days = win_map.get(stage_enum, win_map.get(DisputeStage.CHARGEBACK, 20))
    if curr_days and curr_days > 0:
        remaining = curr_days - days_elapsed
        urgency = _urgency_for_days(remaining)
        label = f"File {label_map.get(stage_enum, 'response')} ({curr_days}d)"
        deadlines.append(DeadlineEntry(label=label, days_remaining=max(0.0, remaining), urgency=urgency))

    # Appeal deadline (if applicable — different from response)
    appeal_days = win_map.get(DisputeStage.ARBITRATION, 0)
    if appeal_days and appeal_days > 0 and stage_enum not in (DisputeStage.ARBITRATION,):
        remaining = appeal_days - days_elapsed
        urgency = _urgency_for_days(remaining)
        deadlines.append(DeadlineEntry(
            label=f"File appeal ({appeal_days}d)",
            days_remaining=max(0.0, remaining),
            urgency=urgency,
        ))

    # Pre-arb deadline if in CHARGEBACK/INQUIRY stage and network supports it
    if stage_enum in (DisputeStage.CHARGEBACK, DisputeStage.INQUIRY):
        pre_arb_days = win_map.get(DisputeStage.PRE_ARBITRATION, 0)
        if pre_arb_days and pre_arb_days > 0:
            remaining = pre_arb_days - days_elapsed
            urgency = _urgency_for_days(remaining)
            deadlines.append(DeadlineEntry(
                label=f"File pre-arbitration rebuttal ({pre_arb_days}d)",
                days_remaining=max(0.0, remaining),
                urgency=urgency,
            ))

    # Remove duplicate deadlines and sort by days remaining
    seen_labels = set()
    unique_deadlines = []
    for d in deadlines:
        if d.label not in seen_labels:
            seen_labels.add(d.label)
            unique_deadlines.append(d)
    unique_deadlines.sort(key=lambda d: d.days_remaining)

    can_appeal = any(d.urgency != UrgencyBand.EXPIRED for d in unique_deadlines)
    any_expired = any(d.urgency == UrgencyBand.EXPIRED for d in unique_deadlines)
    primary = unique_deadlines[0] if unique_deadlines else None

    summary = (
        f"Dispute {inp.dispute_id} [{net}|{stage_val}]: "
        f"{len(unique_deadlines)} deadlines | "
        f"{'expired' if any_expired else 'active'}"
    )

    return _WindowReport48(
        dispute_id=inp.dispute_id,
        deadlines=unique_deadlines,
        can_still_appeal=can_appeal,
        any_window_expired=any_expired,
        primary_deadline=primary,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Wave35 test-API — AppealOutcome/AppealRecommendation/AppealWindowInput/track_appeal_window
# ---------------------------------------------------------------------------

class AppealOutcome(str, _Enum8):
    WON_ORIGINAL  = "WON_ORIGINAL"
    LOST_ORIGINAL = "LOST_ORIGINAL"
    PARTIAL       = "PARTIAL"
    WON           = "WON"
    LOST          = "LOST"
    SETTLED       = "SETTLED"
    PENDING       = "PENDING"


class AppealRecommendation(str, _Enum8):
    APPEAL_NOW     = "APPEAL_NOW"
    APPEAL_OPTIONAL = "APPEAL_OPTIONAL"
    DO_NOT_APPEAL  = "DO_NOT_APPEAL"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    PREPARE_APPEAL = "PREPARE_APPEAL"
    WAIT           = "WAIT"
    NO_ACTION      = "NO_ACTION"


_APPEAL_WINDOWS_DAYS = {
    "PAYPAL": 10, "VISA": 45, "MC": 45, "AMEX": 60, "DISCOVER": 45,
}


@_dc8
class _AppealWindowReport35:
    dispute_id: str
    appeal_window_days: int
    days_remaining: int
    window_expired: bool
    recommendation: AppealRecommendation
    appeal_win_probability: float
    ev_of_appealing: float
    is_ev_positive: bool
    deadline_urgency: str
    required_steps: list
    summary: str


def track_appeal_window(inp: AppealWindowInput) -> _AppealWindowReport35:
    """Compute appeal window and recommendation (wave35 test-API)."""
    net = str(inp.network).upper() if inp.network else "PAYPAL"
    window_days = _APPEAL_WINDOWS_DAYS.get(net, 10)
    days_remaining = max(0, window_days - inp.days_since_ruling)
    window_expired = inp.days_since_ruling >= window_days

    # Determine recommendation based on outcome & EV
    outcome = inp.ruling_outcome
    if outcome in (AppealOutcome.WON_ORIGINAL, AppealOutcome.WON):
        rec = AppealRecommendation.DO_NOT_APPEAL
    elif window_expired:
        rec = AppealRecommendation.WINDOW_EXPIRED
    else:
        # Compute EV
        base_p = inp.base_win_probability
        if inp.has_new_evidence:
            base_p = min(0.90, base_p + 0.20)
        ev = base_p * inp.disputed_amount - inp.appeal_cost - (1 - base_p) * inp.chargeback_fee
        if ev > 10 and inp.has_new_evidence:
            rec = AppealRecommendation.APPEAL_NOW
        elif ev > 0:
            rec = AppealRecommendation.APPEAL_OPTIONAL
        else:
            rec = AppealRecommendation.DO_NOT_APPEAL

    # Win probability
    p_win = inp.base_win_probability
    if inp.has_new_evidence:
        p_win = min(0.90, p_win + 0.20)

    ev = p_win * inp.disputed_amount - inp.appeal_cost - (1 - p_win) * inp.chargeback_fee
    is_ev_positive = ev > 0

    # Deadline urgency
    if days_remaining <= 2:
        urgency = "CRITICAL"
    elif days_remaining <= 7:
        urgency = "HIGH"
    elif days_remaining >= window_days * 0.75:
        urgency = "LOW"
    else:
        urgency = "MEDIUM"

    required_steps = []
    if rec in (AppealRecommendation.APPEAL_NOW, AppealRecommendation.APPEAL_OPTIONAL):
        required_steps = [
            "Compile new evidence package",
            f"File appeal with {net} through dispute portal",
            f"Deadline: {days_remaining} days remaining",
        ]

    summary = (
        f"Dispute {inp.dispute_id} [{net}]: "
        f"window={window_days}d remaining={days_remaining}d | "
        f"p_win={p_win:.0%} ev=${ev:.2f} → {rec.value}"
    )

    return _AppealWindowReport35(
        dispute_id=inp.dispute_id,
        appeal_window_days=window_days,
        days_remaining=days_remaining,
        window_expired=window_expired,
        recommendation=rec,
        appeal_win_probability=round(p_win, 4),
        ev_of_appealing=round(ev, 4),
        is_ev_positive=is_ev_positive,
        deadline_urgency=urgency,
        required_steps=required_steps,
        summary=summary,
    )
