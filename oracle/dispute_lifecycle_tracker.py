"""
Dispute Lifecycle Tracker — model and validate dispute state transitions.

A PayPal dispute goes through defined states. This module:
  1. Validates that a proposed state transition is legal
  2. Computes which actions are available in the current state
  3. Identifies the next required action and its deadline
  4. Detects lifecycle anomalies (stuck disputes, missed deadlines)
  5. Projects when a dispute will auto-close if no action is taken

PayPal Dispute States:
  OPEN        → Buyer opened dispute
  WAITING_MERCHANT  → PayPal waiting for merchant response
  WAITING_BUYER     → PayPal waiting for buyer response
  UNDER_REVIEW      → PayPal is reviewing
  RESOLVED_WON      → Merchant won
  RESOLVED_LOST     → Merchant lost
  APPEALING         → Merchant filed appeal
  APPEAL_REVIEW     → PayPal reviewing appeal
  APPEAL_WON        → Appeal granted
  APPEAL_LOST       → Appeal denied
  ESCALATED         → Escalated to card network
  AUTO_CLOSED       → Closed automatically (buyer inactivity)
  REFUNDED          → Merchant issued refund

Legal transitions:
  OPEN              → WAITING_MERCHANT, AUTO_CLOSED
  WAITING_MERCHANT  → UNDER_REVIEW, REFUNDED, AUTO_CLOSED
  WAITING_BUYER     → UNDER_REVIEW, AUTO_CLOSED
  UNDER_REVIEW      → RESOLVED_WON, RESOLVED_LOST
  RESOLVED_LOST     → APPEALING
  RESOLVED_WON      → (terminal)
  APPEALING         → APPEAL_REVIEW
  APPEAL_REVIEW     → APPEAL_WON, APPEAL_LOST
  APPEAL_WON        → (terminal)
  APPEAL_LOST       → ESCALATED
  ESCALATED         → RESOLVED_WON, RESOLVED_LOST
  AUTO_CLOSED       → (terminal)
  REFUNDED          → (terminal)

Standard deadlines:
  WAITING_MERCHANT response:  7 days (informal), 10 days (formal)
  WAITING_BUYER response:     10 days
  Appeal filing:              10 days after ruling
  Auto-close (buyer inactivity): 20 days

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeState(str, Enum):
    OPEN             = "OPEN"
    WAITING_MERCHANT = "WAITING_MERCHANT"
    WAITING_BUYER    = "WAITING_BUYER"
    UNDER_REVIEW     = "UNDER_REVIEW"
    RESOLVED_WON     = "RESOLVED_WON"
    RESOLVED_LOST    = "RESOLVED_LOST"
    APPEALING        = "APPEALING"
    APPEAL_REVIEW    = "APPEAL_REVIEW"
    APPEAL_WON       = "APPEAL_WON"
    APPEAL_LOST      = "APPEAL_LOST"
    ESCALATED        = "ESCALATED"
    AUTO_CLOSED      = "AUTO_CLOSED"
    REFUNDED         = "REFUNDED"


class DisputeAction(str, Enum):
    SUBMIT_RESPONSE  = "SUBMIT_RESPONSE"   # Merchant submits evidence
    ISSUE_REFUND     = "ISSUE_REFUND"       # Merchant issues refund
    FILE_APPEAL      = "FILE_APPEAL"        # Merchant files appeal
    ESCALATE         = "ESCALATE"           # Escalate to card network
    WAIT             = "WAIT"               # No action needed now
    MONITOR          = "MONITOR"            # Waiting for PayPal/buyer


class AnomalyType(str, Enum):
    MISSED_DEADLINE    = "MISSED_DEADLINE"
    STUCK_IN_STATE     = "STUCK_IN_STATE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    APPROACHING_AUTO_CLOSE = "APPROACHING_AUTO_CLOSE"


# Legal state transitions: from_state → set of to_states
_LEGAL_TRANSITIONS: dict[DisputeState, set[DisputeState]] = {
    DisputeState.OPEN:             {DisputeState.WAITING_MERCHANT, DisputeState.AUTO_CLOSED},
    DisputeState.WAITING_MERCHANT: {DisputeState.UNDER_REVIEW, DisputeState.REFUNDED, DisputeState.AUTO_CLOSED},
    DisputeState.WAITING_BUYER:    {DisputeState.UNDER_REVIEW, DisputeState.AUTO_CLOSED},
    DisputeState.UNDER_REVIEW:     {DisputeState.RESOLVED_WON, DisputeState.RESOLVED_LOST},
    DisputeState.RESOLVED_WON:     set(),
    DisputeState.RESOLVED_LOST:    {DisputeState.APPEALING},
    DisputeState.APPEALING:        {DisputeState.APPEAL_REVIEW},
    DisputeState.APPEAL_REVIEW:    {DisputeState.APPEAL_WON, DisputeState.APPEAL_LOST},
    DisputeState.APPEAL_WON:       set(),
    DisputeState.APPEAL_LOST:      {DisputeState.ESCALATED},
    DisputeState.ESCALATED:        {DisputeState.RESOLVED_WON, DisputeState.RESOLVED_LOST},
    DisputeState.AUTO_CLOSED:      set(),
    DisputeState.REFUNDED:         set(),
}

_TERMINAL_STATES = {
    DisputeState.RESOLVED_WON,
    DisputeState.APPEAL_WON,
    DisputeState.AUTO_CLOSED,
    DisputeState.REFUNDED,
    DisputeState.APPEAL_LOST,  # Terminal unless escalated
}

# Standard deadlines per state (days)
_STATE_DEADLINES: dict[DisputeState, int] = {
    DisputeState.WAITING_MERCHANT: 10,
    DisputeState.WAITING_BUYER:    10,
    DisputeState.RESOLVED_LOST:    10,   # 10 days to appeal
}

# State-available actions
_STATE_ACTIONS: dict[DisputeState, list[DisputeAction]] = {
    DisputeState.OPEN:             [DisputeAction.WAIT],
    DisputeState.WAITING_MERCHANT: [DisputeAction.SUBMIT_RESPONSE, DisputeAction.ISSUE_REFUND],
    DisputeState.WAITING_BUYER:    [DisputeAction.MONITOR],
    DisputeState.UNDER_REVIEW:     [DisputeAction.MONITOR],
    DisputeState.RESOLVED_WON:     [],
    DisputeState.RESOLVED_LOST:    [DisputeAction.FILE_APPEAL],
    DisputeState.APPEALING:        [DisputeAction.MONITOR],
    DisputeState.APPEAL_REVIEW:    [DisputeAction.MONITOR],
    DisputeState.APPEAL_WON:       [],
    DisputeState.APPEAL_LOST:      [DisputeAction.ESCALATE],
    DisputeState.ESCALATED:        [DisputeAction.MONITOR],
    DisputeState.AUTO_CLOSED:      [],
    DisputeState.REFUNDED:         [],
}


@dataclass
class StateHistoryEntry:
    """One state in the dispute's history."""
    state: DisputeState
    entered_on_day: int        # Day relative to dispute open (0 = opened)
    exited_on_day: Optional[int] = None
    notes: str = ""


@dataclass
class LifecycleInput:
    """Input for lifecycle tracking."""
    dispute_id: str
    claim_type: str
    dispute_amount: float

    current_state: DisputeState
    days_in_current_state: int    # How long in the current state

    state_history: list[StateHistoryEntry] = field(default_factory=list)
    total_days_open: int = 0

    # Context
    is_formal_dispute: bool = True
    merchant_responded: bool = False


@dataclass
class TransitionValidation:
    """Result of validating a state transition."""
    from_state: DisputeState
    to_state: DisputeState
    is_valid: bool
    reason: str


@dataclass
class LifecycleAnomaly:
    """A detected lifecycle problem."""
    anomaly_type: AnomalyType
    description: str
    severity: str         # HIGH / MEDIUM / LOW
    action_required: str


@dataclass
class LifecycleReport:
    """
    Complete lifecycle state analysis.
    """
    dispute_id: str
    current_state: DisputeState
    days_in_current_state: int

    # Deadline
    deadline_days: Optional[int]       # Deadline for current state action
    days_until_deadline: Optional[int]
    is_past_deadline: bool

    # Available actions
    available_actions: list[DisputeAction]
    recommended_action: DisputeAction

    # Next state
    possible_next_states: list[DisputeState]

    # Auto-close projection
    days_until_auto_close: Optional[int]  # None if not at risk

    # Anomalies
    anomalies: list[LifecycleAnomaly]

    # Terminal state check
    is_terminal: bool
    is_won: bool
    is_lost: bool

    summary: str


def validate_transition(from_state: DisputeState, to_state: DisputeState) -> TransitionValidation:
    """Check if a state transition is legal."""
    allowed = _LEGAL_TRANSITIONS.get(from_state, set())
    is_valid = to_state in allowed
    if is_valid:
        reason = f"{from_state.value} → {to_state.value} is a valid transition"
    elif from_state == to_state:
        reason = f"Already in state {from_state.value}"
    else:
        allowed_str = ", ".join(s.value for s in allowed) if allowed else "none"
        reason = f"Cannot transition from {from_state.value} to {to_state.value}. Allowed: {allowed_str}"
    return TransitionValidation(from_state=from_state, to_state=to_state, is_valid=is_valid, reason=reason)


def _detect_anomalies(inp: LifecycleInput) -> list[LifecycleAnomaly]:
    anomalies: list[LifecycleAnomaly] = []
    deadline = _STATE_DEADLINES.get(inp.current_state)

    if deadline and inp.days_in_current_state > deadline:
        anomalies.append(LifecycleAnomaly(
            anomaly_type=AnomalyType.MISSED_DEADLINE,
            description=f"Response deadline of {deadline}d exceeded (now {inp.days_in_current_state}d)",
            severity="HIGH",
            action_required=f"Submit response or refund immediately — {inp.days_in_current_state - deadline}d overdue",
        ))
    elif deadline and inp.days_in_current_state >= deadline - 2:
        anomalies.append(LifecycleAnomaly(
            anomaly_type=AnomalyType.APPROACHING_AUTO_CLOSE,
            description=f"Deadline approaching: {deadline - inp.days_in_current_state}d remaining",
            severity="MEDIUM",
            action_required="Take action within 24h to avoid auto-close",
        ))

    # Stuck in UNDER_REVIEW
    if inp.current_state == DisputeState.UNDER_REVIEW and inp.days_in_current_state > 30:
        anomalies.append(LifecycleAnomaly(
            anomaly_type=AnomalyType.STUCK_IN_STATE,
            description=f"Dispute under review for {inp.days_in_current_state} days",
            severity="MEDIUM",
            action_required="Contact PayPal Resolution Center to request status update",
        ))

    # WAITING_BUYER auto-close risk
    if inp.current_state == DisputeState.WAITING_BUYER and inp.days_in_current_state >= 15:
        anomalies.append(LifecycleAnomaly(
            anomaly_type=AnomalyType.APPROACHING_AUTO_CLOSE,
            description=f"Buyer inactive for {inp.days_in_current_state}d, auto-close in {20 - inp.days_in_current_state}d",
            severity="LOW",
            action_required="Monitor — may auto-close in merchant's favor",
        ))

    return anomalies


def analyze_lifecycle(inp: LifecycleInput) -> LifecycleReport:
    """
    Analyze the dispute's lifecycle state and produce guidance.
    """
    is_terminal = inp.current_state in _TERMINAL_STATES
    is_won = inp.current_state in (DisputeState.RESOLVED_WON, DisputeState.APPEAL_WON, DisputeState.AUTO_CLOSED)
    is_lost = inp.current_state in (DisputeState.RESOLVED_LOST, DisputeState.APPEAL_LOST)

    deadline = _STATE_DEADLINES.get(inp.current_state)
    days_until_deadline = (deadline - inp.days_in_current_state) if deadline else None
    is_past_deadline = (days_until_deadline is not None and days_until_deadline < 0)

    available_actions = _STATE_ACTIONS.get(inp.current_state, [])

    # Recommended action
    if is_terminal:
        recommended = DisputeAction.WAIT
    elif available_actions:
        recommended = available_actions[0]
    else:
        recommended = DisputeAction.MONITOR

    # Possible next states
    possible_next = list(_LEGAL_TRANSITIONS.get(inp.current_state, set()))

    # Auto-close risk
    auto_close_risk: Optional[int] = None
    if inp.current_state in (DisputeState.WAITING_BUYER, DisputeState.OPEN):
        days_left = 20 - inp.days_in_current_state
        if 0 < days_left <= 10:
            auto_close_risk = days_left

    anomalies = _detect_anomalies(inp)

    summary = (
        f"Lifecycle {inp.dispute_id}: state={inp.current_state.value} | "
        f"days_in_state={inp.days_in_current_state} | "
        f"deadline={'%dd' % days_until_deadline if days_until_deadline is not None else 'N/A'} | "
        f"anomalies={len(anomalies)} | "
        f"recommended={recommended.value}"
    )

    return LifecycleReport(
        dispute_id=inp.dispute_id,
        current_state=inp.current_state,
        days_in_current_state=inp.days_in_current_state,
        deadline_days=deadline,
        days_until_deadline=days_until_deadline,
        is_past_deadline=is_past_deadline,
        available_actions=available_actions,
        recommended_action=recommended,
        possible_next_states=possible_next,
        days_until_auto_close=auto_close_risk,
        anomalies=anomalies,
        is_terminal=is_terminal,
        is_won=is_won,
        is_lost=is_lost,
        summary=summary,
    )
