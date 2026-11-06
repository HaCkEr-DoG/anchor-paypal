"""
Merchant Health Monitor — real-time PayPal account risk dashboard.

Every dispute module in Anchor makes individual dispute decisions. This module
makes ACCOUNT-LEVEL decisions: given our current dispute rate trajectory, how
close are we to the thresholds that restrict or destroy the account?

PayPal's threshold cascade (2024):
  > 0.50%: Elevated Dispute Rate  — warning emails, monitoring
  > 1.00%: High Dispute Rate      — limited processing, possible holds
  > 1.50%: Excessive Dispute Rate — $30 per-dispute HDR fee on ALL disputes
  > 3.00%: Suspension threshold   — account review, potential suspension
  > 5.00%: Termination risk       — permanent restriction likely

The key insight competitors miss: thresholds are ROLLING WINDOWS (last 90 days
for most programs, monthly for HDR). One bad month doesn't just hurt that month —
it stays in the window for 90 days. A merchant who had 50 disputes in January is
still carrying that toward their March rate.

This module computes:
  1. Exact current rate (not just "are we over?")
  2. Trajectory: at current dispute velocity, when do we cross each threshold?
  3. Disputes-allowed budget: how many more disputes can we absorb before crossing?
  4. Emergency protocol: if crossing is imminent (<30 days), what must change NOW?
  5. Recovery projection: if we implement specific actions, when do we recover?

This turns dispute management from reactive (respond to individual disputes) to
strategic (manage the account's long-term rate trajectory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class AccountStatus(str, Enum):
    HEALTHY       = "HEALTHY"        # < 0.50%  — green
    ELEVATED      = "ELEVATED"       # 0.50–1.00% — yellow
    HIGH          = "HIGH"           # 1.00–1.50% — orange
    EXCESSIVE     = "EXCESSIVE"      # 1.50–3.00% — red (HDR fee active)
    CRITICAL      = "CRITICAL"       # 3.00–5.00% — suspend risk
    TERMINAL      = "TERMINAL"       # > 5.00%   — termination risk


@dataclass
class DisputeWindow:
    """Rolling 90-day dispute window data."""
    window_days: int = 90
    total_transactions: int = 0      # Transactions in window
    total_disputes: int = 0          # Disputes filed in window
    # Per-week breakdown for trend analysis
    weekly_disputes: list[int] = field(default_factory=list)   # Last 13 weeks
    weekly_transactions: list[int] = field(default_factory=list)  # Last 13 weeks
    # Current open disputes (may escalate)
    open_disputes: int = 0
    # HDR charges already incurred this month
    hdr_fees_ytd: float = 0.0


@dataclass
class MerchantHealthReport:
    """Complete account health assessment."""
    # Current state
    current_dispute_rate: float          # Precise rate (0.0–1.0)
    account_status: AccountStatus
    hdr_fee_active: bool                 # True if >1.50% and paying $30/dispute
    hdr_fee_per_dispute: float           # 0 or 30
    # Threshold proximity
    disputes_until_next_threshold: int   # How many more disputes before crossing
    days_until_threshold_cross: int      # At current velocity, when do we cross?
    next_threshold_name: str             # e.g., "EXCESSIVE (1.50%)"
    next_threshold_rate: float
    # Trajectory
    weekly_dispute_velocity: float       # Avg disputes/week (last 4 weeks)
    weekly_transaction_velocity: float   # Avg transactions/week
    trend_direction: str                 # "IMPROVING" / "STABLE" / "WORSENING"
    rate_30d_ago: float                  # Rate 30 days ago (trend comparison)
    # Budget
    dispute_budget_30d: int              # Disputes allowed in next 30 days
    dispute_budget_90d: int              # Disputes allowed in next 90 days
    # Financial exposure
    monthly_hdr_exposure: float          # Projected HDR fees if rate unchanged
    annual_hdr_exposure: float
    # Recommendations
    emergency_actions: list[str]         # If critical/terminal: do these NOW
    preventive_actions: list[str]        # For all statuses
    health_score: int                    # 0–100 (100 = best)
    reasoning: list[str] = field(default_factory=list)


# PayPal threshold definitions: (rate, label, fee_per_dispute)
_THRESHOLDS = [
    (0.0050, "ELEVATED (0.50%)",    0.0),
    (0.0100, "HIGH (1.00%)",        0.0),
    (0.0150, "EXCESSIVE (1.50%)",  30.0),
    (0.0300, "CRITICAL (3.00%)",    30.0),
    (0.0500, "TERMINAL (5.00%)",    30.0),
]


def assess_merchant_health(window: DisputeWindow) -> MerchantHealthReport:
    """
    Compute complete account health, trajectory, and recommended actions.

    The core value: merchant knows not just "am I over?" but "in 23 days I'll
    cross 1.50% and start paying $30/dispute on ALL disputes unless I add
    signature confirmation to the next 150 orders."
    """
    reasoning: list[str] = []

    if window.total_transactions == 0:
        # No transaction history — return safe defaults
        return _empty_report()

    # ── 1. Current rate ───────────────────────────────────────────────────────
    current_rate = window.total_disputes / window.total_transactions
    # Worst case: include open disputes (they'll likely become formal)
    worst_rate = (window.total_disputes + window.open_disputes) / window.total_transactions
    reasoning.append(
        f"Current dispute rate: {current_rate:.3%} "
        f"(worst-case with {window.open_disputes} open: {worst_rate:.3%})"
    )

    # ── 2. Account status ─────────────────────────────────────────────────────
    if current_rate >= 0.0500:
        status = AccountStatus.TERMINAL
    elif current_rate >= 0.0300:
        status = AccountStatus.CRITICAL
    elif current_rate >= 0.0150:
        status = AccountStatus.EXCESSIVE
    elif current_rate >= 0.0100:
        status = AccountStatus.HIGH
    elif current_rate >= 0.0050:
        status = AccountStatus.ELEVATED
    else:
        status = AccountStatus.HEALTHY

    hdr_active = current_rate >= 0.0150
    hdr_fee = 30.0 if hdr_active else 0.0

    # ── 3. Next threshold and budget ─────────────────────────────────────────
    next_threshold_rate = 1.0
    next_threshold_name = "No threshold"
    for thresh_rate, thresh_name, _ in _THRESHOLDS:
        if current_rate < thresh_rate:
            next_threshold_rate = thresh_rate
            next_threshold_name = thresh_name
            break

    # Disputes allowed until next threshold:
    # threshold_rate = (current_disputes + N) / (current_transactions + weekly_tx * weeks)
    # Solve for N (new disputes) over 30d / 90d horizon
    weekly_tx = _weekly_velocity(window.weekly_transactions)
    weekly_disputes = _weekly_velocity(window.weekly_disputes)
    tx_next_30d = weekly_tx * 4.3
    tx_next_90d = weekly_tx * 13

    budget_30 = max(0, int(
        next_threshold_rate * (window.total_transactions + tx_next_30d)
        - window.total_disputes
    ))
    budget_90 = max(0, int(
        next_threshold_rate * (window.total_transactions + tx_next_90d)
        - window.total_disputes
    ))

    # Days until crossing: at current dispute velocity
    if weekly_disputes > 0 and next_threshold_rate < 1.0:
        # Solving: (disputes + weekly_disputes * weeks) / (tx + weekly_tx * weeks) = threshold
        # Rearranged: weeks = (threshold * tx - disputes) / (weekly_disputes - threshold * weekly_tx)
        numerator = next_threshold_rate * window.total_transactions - window.total_disputes
        denominator = weekly_disputes - next_threshold_rate * weekly_tx
        if denominator > 0 and numerator > 0:
            weeks_until = numerator / denominator
            days_until = int(weeks_until * 7)
        elif denominator <= 0:
            days_until = 999  # Rate improving faster than disputes arriving
        else:
            days_until = 0  # Already crossed
    elif next_threshold_rate >= 1.0:
        days_until = 999
    else:
        days_until = 999

    reasoning.append(
        f"At current velocity ({weekly_disputes:.1f} disputes/week, "
        f"{weekly_tx:.0f} tx/week): "
        f"crosses {next_threshold_name} in ~{days_until} days"
    )

    # ── 4. Trend ──────────────────────────────────────────────────────────────
    rate_30d_ago = _rate_30d_ago(window)
    if current_rate < rate_30d_ago * 0.90:
        trend = "IMPROVING"
    elif current_rate > rate_30d_ago * 1.10:
        trend = "WORSENING"
    else:
        trend = "STABLE"

    # ── 5. Financial exposure ─────────────────────────────────────────────────
    monthly_disputes_projected = weekly_disputes * 4.3
    monthly_hdr = monthly_disputes_projected * hdr_fee if hdr_active else 0.0
    annual_hdr = monthly_hdr * 12

    # ── 6. Health score (0–100, 100 = best) ──────────────────────────────────
    # Score based on distance from nearest threshold
    if next_threshold_rate <= current_rate:
        health_score = 0
    else:
        clearance = (next_threshold_rate - current_rate) / next_threshold_rate
        health_score = min(100, int(clearance * 150))  # 100 when well below threshold
    if trend == "IMPROVING":
        health_score = min(100, health_score + 10)
    elif trend == "WORSENING":
        health_score = max(0, health_score - 15)

    # ── 7. Actions ────────────────────────────────────────────────────────────
    emergency_actions, preventive_actions = _build_actions(
        status, days_until, budget_30, current_rate, hdr_active
    )

    reasoning.append(f"Health score: {health_score}/100 | Trend: {trend}")
    reasoning.append(
        f"Dispute budget: {budget_30} in next 30d, {budget_90} in next 90d"
    )

    disputes_until_next = budget_30 if days_until <= 30 else budget_90

    return MerchantHealthReport(
        current_dispute_rate=round(current_rate, 5),
        account_status=status,
        hdr_fee_active=hdr_active,
        hdr_fee_per_dispute=hdr_fee,
        disputes_until_next_threshold=disputes_until_next,
        days_until_threshold_cross=days_until,
        next_threshold_name=next_threshold_name,
        next_threshold_rate=next_threshold_rate,
        weekly_dispute_velocity=round(weekly_disputes, 2),
        weekly_transaction_velocity=round(weekly_tx, 2),
        trend_direction=trend,
        rate_30d_ago=round(rate_30d_ago, 5),
        dispute_budget_30d=budget_30,
        dispute_budget_90d=budget_90,
        monthly_hdr_exposure=round(monthly_hdr, 2),
        annual_hdr_exposure=round(annual_hdr, 2),
        emergency_actions=emergency_actions,
        preventive_actions=preventive_actions,
        health_score=health_score,
        reasoning=reasoning,
    )


def _weekly_velocity(weekly_data: list[int]) -> float:
    """Average over last 4 weeks, with recency weighting."""
    if not weekly_data:
        return 0.0
    recent = weekly_data[-4:] if len(weekly_data) >= 4 else weekly_data
    if len(recent) == 4:
        # Weighted: most recent week counts more
        return (recent[0] * 1 + recent[1] * 2 + recent[2] * 3 + recent[3] * 4) / 10.0
    return sum(recent) / len(recent)


def _rate_30d_ago(window: DisputeWindow) -> float:
    """Estimate dispute rate as of 30 days ago using last 4 weeks of data."""
    if len(window.weekly_disputes) < 4 or len(window.weekly_transactions) < 4:
        return window.total_disputes / max(window.total_transactions, 1)
    # Remove last 4 weeks of data to approximate 30-day-old state
    old_disputes = window.total_disputes - sum(window.weekly_disputes[-4:])
    old_tx = window.total_transactions - sum(window.weekly_transactions[-4:])
    if old_tx <= 0:
        return 0.0
    return old_disputes / old_tx


def _build_actions(
    status: AccountStatus,
    days_until: int,
    budget_30: int,
    rate: float,
    hdr_active: bool,
) -> tuple[list[str], list[str]]:
    emergency: list[str] = []
    preventive: list[str] = []

    if status in (AccountStatus.CRITICAL, AccountStatus.TERMINAL):
        emergency.extend([
            "IMMEDIATE: Contact PayPal merchant support — explain proactive steps being taken",
            "IMMEDIATE: Pause or reduce order volume to give rate time to recover",
            "IMMEDIATE: Fight ALL disputes aggressively — do not accept any claim",
            "IMMEDIATE: Activate signature confirmation on 100% of orders",
            "IMMEDIATE: Review last 30 days for systematic issue (carrier, product, region)",
        ])
    elif status == AccountStatus.EXCESSIVE:
        emergency.extend([
            f"URGENT: HDR fee active — each dispute costs $30 extra. ${rate*30*4:.0f}/month extra",
            "Activate signature confirmation on all orders >$100",
            "Fight all disputes — accepting costs $30 more than fighting now",
        ])
    elif days_until <= 14:
        emergency.extend([
            f"WARNING: {days_until} days until threshold crossing — act now",
            f"Dispute budget is {budget_30} disputes in next 30 days — prioritize prevention",
            "Pause high-risk product categories temporarily",
        ])

    # Always recommend
    if rate >= 0.005:
        preventive.extend([
            "Review order_risk.py outputs — increase threshold for CRITICAL orders",
            "Activate pre-shipment photo documentation for all orders",
        ])
    preventive.extend([
        "Run dispute_dna.py weekly to identify and fix systemic dispute sources",
        "Activate alert_interceptor.py for Ethoca/Verifi alerts — prevents disputes hitting count",
        f"Current dispute budget: {budget_30} disputes in next 30 days before next threshold",
    ])
    if hdr_active:
        preventive.insert(0, "HDR fee active — fight EVERY dispute, never accept unless sub-$15")

    return emergency, preventive


def _empty_report() -> MerchantHealthReport:
    return MerchantHealthReport(
        current_dispute_rate=0.0,
        account_status=AccountStatus.HEALTHY,
        hdr_fee_active=False,
        hdr_fee_per_dispute=0.0,
        disputes_until_next_threshold=999,
        days_until_threshold_cross=999,
        next_threshold_name="ELEVATED (0.50%)",
        next_threshold_rate=0.005,
        weekly_dispute_velocity=0.0,
        weekly_transaction_velocity=0.0,
        trend_direction="STABLE",
        rate_30d_ago=0.0,
        dispute_budget_30d=999,
        dispute_budget_90d=999,
        monthly_hdr_exposure=0.0,
        annual_hdr_exposure=0.0,
        emergency_actions=[],
        preventive_actions=["No transaction history available"],
        health_score=100,
    )


if __name__ == "__main__":
    window = DisputeWindow(
        total_transactions=2000,
        total_disputes=28,  # 1.40% — just under EXCESSIVE threshold
        open_disputes=4,
        weekly_disputes=[2, 3, 3, 4],       # Worsening trend
        weekly_transactions=[150, 155, 148, 160],
        hdr_fees_ytd=0.0,
    )
    report = assess_merchant_health(window)
    print(f"Dispute rate: {report.current_dispute_rate:.3%}")
    print(f"Status: {report.account_status.value}")
    print(f"Health score: {report.health_score}/100")
    print(f"HDR active: {report.hdr_fee_active}")
    print(f"Days until EXCESSIVE threshold: {report.days_until_threshold_cross}")
    print(f"Dispute budget (30d): {report.dispute_budget_30d}")
    print(f"Trend: {report.trend_direction}")
    if report.emergency_actions:
        print(f"\nEMERGENCY ACTIONS:")
        for a in report.emergency_actions:
            print(f"  🚨 {a}")
