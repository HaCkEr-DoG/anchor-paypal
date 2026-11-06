"""
Autopilot Execution Engine — Chargebacks911 parity as code.

The gap this closes: Chargebacks911 is a managed service — humans review disputes,
gather evidence, and submit responses. This costs $30–$150/dispute + revenue share.
More importantly, humans are slow: they batch disputes, miss deadlines, and can't
run at 3 AM when a deadline expires.

Anchor's autopilot does this autonomously:
  1. Receives a dispute event (from watcher.py or webhook)
  2. Runs the full oracle stack: decision → buyer_intelligence → ensemble → rubric
  3. Builds the evidence package (evidence_builder → paypal_rubric → response_optimizer)
  4. Dispatches the correct PayPal API action (respond, accept, appeal, escalate)
  5. Schedules follow-up deadlines (deadline_tracker)
  6. Logs outcome for feedback loop (feedback.py)

This module is the orchestration layer — it sequences oracle calls and produces
an `AutopilotPlan` that the execution layer (agents/guardian.py) can dispatch.
Pure computation: no I/O, no API calls. The plan is inspectable and reversible.

Execution modes:
  FULLY_AUTONOMOUS   — execute immediately without human review
  HUMAN_IN_LOOP      — generate plan, wait for operator approval, then execute
  ADVISORY_ONLY      — generate plan, surface to operator, do not execute

The mode is set per merchant configuration. High-value or ambiguous disputes
default to HUMAN_IN_LOOP. Clear fraud + high win probability → FULLY_AUTONOMOUS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExecutionMode(str, Enum):
    FULLY_AUTONOMOUS = "FULLY_AUTONOMOUS"  # Execute without human review
    HUMAN_IN_LOOP    = "HUMAN_IN_LOOP"     # Generate plan, wait for approval
    ADVISORY_ONLY    = "ADVISORY_ONLY"     # Plan only, no execution


class DisputeAction(str, Enum):
    RESPOND_AND_FIGHT  = "RESPOND_AND_FIGHT"   # Submit evidence response to PayPal
    ACCEPT_CLAIM       = "ACCEPT_CLAIM"        # Accept and refund (EV-optimal)
    APPEAL             = "APPEAL"              # Appeal a lost dispute
    ESCALATE_TO_PAYPAL = "ESCALATE_TO_PAYPAL"  # Request PayPal fraud investigation
    SEND_MESSAGE       = "SEND_MESSAGE"        # Send negotiation message to buyer
    WAIT_FOR_EVIDENCE  = "WAIT_FOR_EVIDENCE"   # Gather more evidence before acting
    ACCEPT_PARTIAL     = "ACCEPT_PARTIAL"      # Negotiate partial settlement


class ConfidenceLevel(str, Enum):
    HIGH   = "HIGH"    # Act autonomously
    MEDIUM = "MEDIUM"  # Flag for review
    LOW    = "LOW"     # Require human decision


@dataclass
class DisputeContext:
    """Everything known about a dispute at orchestration time."""
    dispute_id: str
    claim_type: str
    dispute_amount: float
    days_until_deadline: int
    # Oracle decision inputs
    oracle_recommendation: str           # "FIGHT" / "ACCEPT" (from decision.py)
    oracle_ev_fight: float
    oracle_win_probability: float
    oracle_confidence: float
    # Buyer intelligence
    buyer_risk_tier: str = "TIER_1_NORMAL"
    buyer_strategy: str = "GOODWILL_GESTURE"
    buyer_win_probability_adjustment: float = 0.0
    # Evidence availability
    has_tracking: bool = False
    has_delivery_confirmation: bool = False
    has_buyer_communication: bool = False
    has_item_description_proof: bool = False
    is_digital_goods: bool = False
    digital_goods_accessed: bool = False
    # Negotiation context
    settlement_opening_offer: Optional[str] = None  # From settlement_negotiator
    settlement_opening_cost: float = 0.0
    # Rubric signals
    rubric_score: Optional[float] = None  # PayPal rubric score (0–100 from paypal_rubric)
    # Ensemble
    ensemble_recommendation: Optional[str] = None  # From ensemble.py
    # State
    already_responded: bool = False
    appeal_window_open: bool = False
    dispute_outcome: Optional[str] = None  # Set if dispute already resolved


@dataclass
class ExecutionStep:
    """One step in the autopilot execution plan."""
    step_num: int
    action: str                    # API method name (e.g. "provide_evidence")
    params: dict                   # Parameters for the API call
    description: str               # Human-readable explanation
    is_critical: bool = True       # If False, skip on failure and continue
    deadline_hours: Optional[int] = None  # Urgency override for this step


@dataclass
class AutopilotPlan:
    """Complete execution plan for one dispute."""
    dispute_id: str
    primary_action: DisputeAction
    execution_mode: ExecutionMode
    confidence: ConfidenceLevel
    steps: list[ExecutionStep]
    # EV summary
    ev_fight: float
    ev_accept: float
    win_probability: float
    # Negotiation
    negotiation_message: Optional[str]  # None if not negotiating
    # Risk flags
    requires_human_review: bool
    review_reason: Optional[str]
    # Outcome tracking
    expected_resolution_days: int
    deadline_hours_remaining: int
    reasoning: list[str] = field(default_factory=list)


def plan_autopilot(context: DisputeContext) -> AutopilotPlan:
    """
    Orchestrate the full response plan for a dispute.

    Sequences oracle decisions → evidence strategy → PayPal API actions.
    Returns an inspectable plan the execution layer can dispatch or surface for review.
    """
    reasoning: list[str] = []

    # ── 1. Determine primary action ───────────────────────────────────────────
    effective_win_prob = min(
        context.oracle_win_probability + context.buyer_win_probability_adjustment, 0.95
    )
    reasoning.append(
        f"Win probability: {context.oracle_win_probability:.0%} base "
        f"+ {context.buyer_win_probability_adjustment:+.0%} buyer adj "
        f"= {effective_win_prob:.0%} effective"
    )

    # Use ensemble recommendation if available, else oracle
    fight_signal = context.ensemble_recommendation or context.oracle_recommendation

    if context.dispute_outcome and context.dispute_outcome == "LOST" and context.appeal_window_open:
        primary = DisputeAction.APPEAL
        reasoning.append("Dispute lost — appeal window open, planning appeal response")
    elif context.buyer_risk_tier in ("TIER_4_SERIAL", "TIER_5_FRAUD"):
        primary = DisputeAction.ESCALATE_TO_PAYPAL
        reasoning.append(
            f"Buyer risk tier {context.buyer_risk_tier} — escalate to PayPal, "
            f"request fraud investigation"
        )
    elif fight_signal == "FIGHT" and not context.already_responded:
        primary = DisputeAction.RESPOND_AND_FIGHT
        reasoning.append(
            f"Oracle says FIGHT with {effective_win_prob:.0%} win probability"
        )
    elif fight_signal == "ACCEPT":
        if context.settlement_opening_offer and context.buyer_risk_tier not in (
            "TIER_4_SERIAL", "TIER_5_FRAUD"
        ):
            primary = DisputeAction.SEND_MESSAGE
            reasoning.append(
                f"Accept signal but negotiation path exists — open with "
                f"{context.settlement_opening_offer} (${context.settlement_opening_cost:.2f})"
            )
        else:
            primary = DisputeAction.ACCEPT_CLAIM
            reasoning.append("Oracle says ACCEPT — refunding is EV-optimal")
    else:
        primary = DisputeAction.WAIT_FOR_EVIDENCE
        reasoning.append("Insufficient evidence for confident decision — gather more first")

    # ── 2. Confidence and execution mode ─────────────────────────────────────
    confidence, mode = _assess_confidence(context, primary, effective_win_prob)
    reasoning.append(f"Confidence: {confidence.value} → mode: {mode.value}")

    # ── 3. Build execution steps ──────────────────────────────────────────────
    steps = _build_steps(context, primary, effective_win_prob)

    # ── 4. Human review gate ─────────────────────────────────────────────────
    requires_review = mode == ExecutionMode.HUMAN_IN_LOOP or confidence == ConfidenceLevel.LOW
    review_reason = None
    if confidence == ConfidenceLevel.LOW:
        review_reason = f"Low confidence ({context.oracle_confidence:.0%}) — human review required"
    elif context.dispute_amount > 500 and mode != ExecutionMode.FULLY_AUTONOMOUS:
        requires_review = True
        review_reason = f"High-value dispute (${context.dispute_amount:.2f}) — verify plan before dispatch"

    # ── 5. Negotiation message ────────────────────────────────────────────────
    negotiation_msg = None
    if primary == DisputeAction.SEND_MESSAGE and context.settlement_opening_offer:
        negotiation_msg = _default_negotiation_message(
            context.settlement_opening_offer,
            context.settlement_opening_cost,
            context.dispute_amount,
        )

    expected_days = {
        DisputeAction.RESPOND_AND_FIGHT: 20,
        DisputeAction.ACCEPT_CLAIM: 3,
        DisputeAction.APPEAL: 30,
        DisputeAction.ESCALATE_TO_PAYPAL: 25,
        DisputeAction.SEND_MESSAGE: 7,
        DisputeAction.WAIT_FOR_EVIDENCE: 5,
        DisputeAction.ACCEPT_PARTIAL: 5,
    }.get(primary, 14)

    return AutopilotPlan(
        dispute_id=context.dispute_id,
        primary_action=primary,
        execution_mode=mode,
        confidence=confidence,
        steps=steps,
        ev_fight=context.oracle_ev_fight,
        ev_accept=0.0,
        win_probability=effective_win_prob,
        negotiation_message=negotiation_msg,
        requires_human_review=requires_review,
        review_reason=review_reason,
        expected_resolution_days=expected_days,
        deadline_hours_remaining=context.days_until_deadline * 24,
        reasoning=reasoning,
    )


def _assess_confidence(
    ctx: DisputeContext,
    primary: DisputeAction,
    win_prob: float,
) -> tuple[ConfidenceLevel, ExecutionMode]:
    """Map dispute signals to confidence and execution mode."""
    # High confidence: clear fraud, high win prob, strong evidence
    if ctx.buyer_risk_tier in ("TIER_4_SERIAL", "TIER_5_FRAUD"):
        return ConfidenceLevel.HIGH, ExecutionMode.FULLY_AUTONOMOUS
    if ctx.is_digital_goods and ctx.digital_goods_accessed:
        return ConfidenceLevel.HIGH, ExecutionMode.FULLY_AUTONOMOUS
    if win_prob >= 0.80 and ctx.has_delivery_confirmation:
        return ConfidenceLevel.HIGH, ExecutionMode.FULLY_AUTONOMOUS
    if primary == DisputeAction.ACCEPT_CLAIM and ctx.dispute_amount < 30:
        return ConfidenceLevel.HIGH, ExecutionMode.FULLY_AUTONOMOUS

    # Medium confidence: standard disputes with reasonable evidence
    if win_prob >= 0.55 and (ctx.has_tracking or ctx.has_buyer_communication):
        if ctx.dispute_amount > 500:
            return ConfidenceLevel.MEDIUM, ExecutionMode.HUMAN_IN_LOOP
        return ConfidenceLevel.MEDIUM, ExecutionMode.FULLY_AUTONOMOUS

    # Medium confidence for accept
    if primary == DisputeAction.ACCEPT_CLAIM:
        return ConfidenceLevel.MEDIUM, ExecutionMode.FULLY_AUTONOMOUS

    # Low confidence: insufficient evidence, low win prob, high value
    return ConfidenceLevel.LOW, ExecutionMode.HUMAN_IN_LOOP


def _build_steps(
    ctx: DisputeContext,
    primary: DisputeAction,
    win_prob: float,
) -> list[ExecutionStep]:
    """Build the ordered list of API calls to execute."""
    steps: list[ExecutionStep] = []
    n = 1

    if primary == DisputeAction.RESPOND_AND_FIGHT:
        # Step 1: Build evidence package
        steps.append(ExecutionStep(
            step_num=n, action="build_evidence_package",
            params={
                "dispute_id": ctx.dispute_id,
                "claim_type": ctx.claim_type,
                "include_tracking": ctx.has_tracking,
                "include_delivery_confirmation": ctx.has_delivery_confirmation,
                "include_buyer_communication": ctx.has_buyer_communication,
                "include_description_proof": ctx.has_item_description_proof,
                "include_digital_access_log": ctx.is_digital_goods and ctx.digital_goods_accessed,
                "buyer_risk_statement": ctx.buyer_risk_tier not in ("TIER_1_NORMAL", "TIER_2_ELEVATED"),
            },
            description="Build evidence package from available signals",
        ))
        n += 1

        # Step 2: Score with PayPal rubric
        if ctx.rubric_score is None:
            steps.append(ExecutionStep(
                step_num=n, action="score_paypal_rubric",
                params={"dispute_id": ctx.dispute_id, "claim_type": ctx.claim_type},
                description="Score response against PayPal rubric to maximize acceptance",
                is_critical=False,
            ))
            n += 1

        # Step 3: Optimize response
        steps.append(ExecutionStep(
            step_num=n, action="optimize_response",
            params={"dispute_id": ctx.dispute_id, "win_probability": win_prob},
            description="Optimize response text for PayPal AI scoring patterns",
            is_critical=False,
        ))
        n += 1

        # Step 4: Submit to PayPal
        steps.append(ExecutionStep(
            step_num=n, action="provide_evidence",
            params={"dispute_id": ctx.dispute_id},
            description="Submit evidence response to PayPal",
            deadline_hours=ctx.days_until_deadline * 24 - 2,
        ))
        n += 1

        # Step 5: Schedule deadline reminder
        steps.append(ExecutionStep(
            step_num=n, action="schedule_deadline_reminder",
            params={
                "dispute_id": ctx.dispute_id,
                "remind_hours_before": 48,
            },
            description="Schedule reminder 48h before deadline",
            is_critical=False,
        ))

    elif primary == DisputeAction.ACCEPT_CLAIM:
        steps.append(ExecutionStep(
            step_num=n, action="accept_claim",
            params={"dispute_id": ctx.dispute_id, "note": "Accepting claim per EV analysis"},
            description=f"Accept dispute claim — refund ${ctx.dispute_amount:.2f}",
        ))
        n += 1
        steps.append(ExecutionStep(
            step_num=n, action="log_outcome",
            params={"dispute_id": ctx.dispute_id, "outcome": "ACCEPTED", "action_taken": "accept_claim"},
            description="Log accepted outcome for feedback and rate tracking",
            is_critical=False,
        ))

    elif primary == DisputeAction.APPEAL:
        steps.append(ExecutionStep(
            step_num=n, action="build_appeal_package",
            params={"dispute_id": ctx.dispute_id, "claim_type": ctx.claim_type},
            description="Build appeal package with new evidence",
        ))
        n += 1
        steps.append(ExecutionStep(
            step_num=n, action="submit_appeal",
            params={"dispute_id": ctx.dispute_id},
            description="Submit appeal to PayPal",
            deadline_hours=72,
        ))

    elif primary == DisputeAction.ESCALATE_TO_PAYPAL:
        steps.append(ExecutionStep(
            step_num=n, action="send_message",
            params={
                "dispute_id": ctx.dispute_id,
                "message_type": "BUYER_FRAUD_REPORT",
                "include_buyer_history": True,
            },
            description="Send buyer fraud report to PayPal investigator",
        ))
        n += 1
        steps.append(ExecutionStep(
            step_num=n, action="provide_evidence",
            params={"dispute_id": ctx.dispute_id, "include_fraud_signals": True},
            description="Submit evidence with buyer risk statement",
        ))

    elif primary == DisputeAction.SEND_MESSAGE:
        steps.append(ExecutionStep(
            step_num=n, action="send_message",
            params={
                "dispute_id": ctx.dispute_id,
                "message_type": "NEGOTIATION_OPENING",
                "offer_type": ctx.settlement_opening_offer,
                "offer_amount": ctx.settlement_opening_cost,
            },
            description=f"Send negotiation opening: {ctx.settlement_opening_offer} "
                        f"(${ctx.settlement_opening_cost:.2f})",
        ))
        n += 1
        steps.append(ExecutionStep(
            step_num=n, action="schedule_followup",
            params={"dispute_id": ctx.dispute_id, "followup_hours": 72},
            description="Schedule follow-up if buyer doesn't respond in 72h",
            is_critical=False,
        ))

    elif primary == DisputeAction.WAIT_FOR_EVIDENCE:
        steps.append(ExecutionStep(
            step_num=n, action="flag_for_review",
            params={"dispute_id": ctx.dispute_id, "reason": "insufficient_evidence"},
            description="Flag dispute for operator evidence gathering",
        ))

    return steps


def _default_negotiation_message(offer_type: str, offer_cost: float, amount: float) -> str:
    if offer_type == "STORE_CREDIT_120":
        return (
            f"Dear Customer,\n\n"
            f"We're sorry to hear about your experience. As a valued customer, "
            f"we'd like to offer you store credit of ${amount * 1.20:.2f} (20% above your "
            f"purchase price) to resolve this immediately — no return required.\n\n"
            f"Please reply to confirm and we'll apply it to your account right away.\n\nBest regards"
        )
    elif offer_type in ("PARTIAL_30", "PARTIAL_50"):
        pct = 0.30 if offer_type == "PARTIAL_30" else 0.50
        return (
            f"Dear Customer,\n\n"
            f"We want to resolve this fairly. We'd like to offer a partial refund "
            f"of ${offer_cost:.2f} ({int(pct*100)}% of your purchase) while you keep the item.\n\n"
            f"Please reply to confirm and we'll process this immediately.\n\nBest regards"
        )
    return (
        f"Dear Customer,\n\n"
        f"We'd like to offer a resolution valued at ${offer_cost:.2f} to settle this matter. "
        f"Please reply to confirm.\n\nBest regards"
    )


def autopilot_summary(plan: AutopilotPlan) -> str:
    """Human-readable one-line summary for dashboards and logs."""
    emoji = {
        DisputeAction.RESPOND_AND_FIGHT:  "⚔️",
        DisputeAction.ACCEPT_CLAIM:       "✅",
        DisputeAction.APPEAL:             "📣",
        DisputeAction.ESCALATE_TO_PAYPAL: "🚨",
        DisputeAction.SEND_MESSAGE:       "💬",
        DisputeAction.WAIT_FOR_EVIDENCE:  "⏳",
        DisputeAction.ACCEPT_PARTIAL:     "🤝",
    }
    icon = emoji.get(plan.primary_action, "?")
    review = " [REVIEW NEEDED]" if plan.requires_human_review else ""
    return (
        f"{icon} {plan.dispute_id}: {plan.primary_action.value} "
        f"| {plan.confidence.value} confidence "
        f"| {plan.win_probability:.0%} win prob "
        f"| {plan.deadline_hours_remaining}h remaining"
        f"{review}"
    )


if __name__ == "__main__":
    ctx = DisputeContext(
        dispute_id="PP-D-12345678",
        claim_type="ITEM_NOT_RECEIVED",
        dispute_amount=120.0,
        days_until_deadline=5,
        oracle_recommendation="FIGHT",
        oracle_ev_fight=28.0,
        oracle_win_probability=0.72,
        oracle_confidence=0.80,
        buyer_risk_tier="TIER_1_NORMAL",
        buyer_win_probability_adjustment=0.0,
        has_tracking=True,
        has_delivery_confirmation=True,
    )
    plan = plan_autopilot(ctx)
    print(autopilot_summary(plan))
    print(f"\nSteps ({len(plan.steps)} total):")
    for step in plan.steps:
        critical = "CRITICAL" if step.is_critical else "optional"
        print(f"  {step.step_num}. [{critical}] {step.description}")
    print(f"\nReasoning:")
    for r in plan.reasoning:
        print(f"  → {r}")
