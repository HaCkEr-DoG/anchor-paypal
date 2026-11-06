"""
Reason Code Strategy Advisor — produce a tailored dispute strategy
based solely on the reason code and network combination.

This oracle encodes the network-specific dispute rules for the most
common reason codes and translates them into concrete action checklists.

Strategies are independent of merchant-specific evidence — they describe
the ideal playbook for each code/network pair.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StrategyType(str, Enum):
    FIGHT_SP         = "FIGHT_SP"           # Use Seller Protection path
    FIGHT_EVIDENCE   = "FIGHT_EVIDENCE"     # Build evidence package and fight
    FIGHT_CE3        = "FIGHT_CE3"          # Use CE3.0 Compelling Evidence path
    ACCEPT_EARLY     = "ACCEPT_EARLY"       # Accept — no viable defense
    PARTIAL_ACCEPT   = "PARTIAL_ACCEPT"     # Partial refund and document
    ESCALATE_MANAGER = "ESCALATE_MANAGER"   # Needs senior review


class RiskFactor(str, Enum):
    SP_NEVER_APPLIES   = "SP_NEVER_APPLIES"
    SP_MAY_APPLY       = "SP_MAY_APPLY"
    CE3_AVAILABLE      = "CE3_AVAILABLE"
    SIGNATURE_REQUIRED = "SIGNATURE_REQUIRED"
    HARD_TO_WIN        = "HARD_TO_WIN"
    EASY_TO_WIN        = "EASY_TO_WIN"


@dataclass
class ReasonCodeStrategyInput:
    dispute_id: str
    reason_code: str
    network: str
    transaction_amount: float = 0.0


@dataclass
class EvidenceRequirement:
    evidence_type: str
    required: bool          # True = must have; False = nice to have
    description: str


@dataclass
class ReasonCodeStrategyReport:
    dispute_id: str
    reason_code: str
    network: str

    strategy_type: StrategyType
    headline: str               # One-sentence strategy summary

    evidence_requirements: list[EvidenceRequirement]
    action_checklist: list[str]  # Ordered steps for the agent to follow
    risk_factors: list[RiskFactor]

    sp_eligible: bool
    ce3_eligible: bool
    base_win_rate: float

    escalation_tip: str
    summary: str


# ─── Evidence requirement helpers ─────────────────────────────────────────────

def _tracking_req(required: bool) -> EvidenceRequirement:
    return EvidenceRequirement("TRACKING_NUMBER", required, "Carrier tracking with delivery scan to buyer's address")

def _invoice_req(required: bool) -> EvidenceRequirement:
    return EvidenceRequirement("INVOICE", required, "Order confirmation and invoice matching disputed transaction")

def _ip_log_req(required: bool) -> EvidenceRequirement:
    return EvidenceRequirement("IP_LOGIN_LOG", required, "IP address and login session log at time of transaction")

def _3ds_req(required: bool) -> EvidenceRequirement:
    return EvidenceRequirement("3DS_AUTHENTICATION", required, "3DS authentication result showing PASSED or ATTEMPT")

def _avs_req() -> EvidenceRequirement:
    return EvidenceRequirement("AVS_CVV_RESULT", False, "AVS full match and CVV2 match from authorization record")

def _photos_req() -> EvidenceRequirement:
    return EvidenceRequirement("ITEM_PHOTOS", True, "Photos of item matching listing description exactly")

def _policy_req() -> EvidenceRequirement:
    return EvidenceRequirement("POLICY_SCREENSHOT", True, "Screenshot of policy displayed at checkout and links to policy page")

def _comms_req() -> EvidenceRequirement:
    return EvidenceRequirement("BUYER_COMMUNICATIONS", False, "All email/chat/ticket records with buyer about this transaction")

def _sub_terms_req() -> EvidenceRequirement:
    return EvidenceRequirement("SUBSCRIPTION_TERMS", True, "Subscription terms accepted, renewal notice sent, usage log")

def _sig_req() -> EvidenceRequirement:
    return EvidenceRequirement("SIGNATURE_CONFIRMATION", True, "Adult signature confirmation from carrier — required for ≥ $750")


# ─── Strategy database ─────────────────────────────────────────────────────────

def _strategy_inr(network: str, amount: float) -> dict:
    reqs = [_tracking_req(True), _invoice_req(True), _comms_req()]
    if amount >= 750:
        reqs.append(_sig_req())

    checklist = [
        "Pull carrier tracking showing delivery scan to buyer's address",
        "Confirm delivery address matches buyer's shipping address in order",
        "Attach signed proof of delivery if amount ≥ $750",
        "Include original order confirmation and invoice",
        "If buyer contacted support, attach the ticket or email thread",
        "Submit via Seller Protection claim if enrolled",
    ]
    risks = [RiskFactor.SP_MAY_APPLY]
    if amount >= 750:
        risks.append(RiskFactor.SIGNATURE_REQUIRED)

    return dict(
        strategy_type=StrategyType.FIGHT_SP,
        headline="Fight with delivery evidence; use Seller Protection if enrolled",
        evidence_requirements=reqs,
        action_checklist=checklist,
        risk_factors=risks,
        sp_eligible=True, ce3_eligible=False, base_win_rate=0.52,
        escalation_tip="If tracking shows delivery but buyer insists not received, escalate to network dispute team",
    )


def _strategy_snad(network: str, amount: float) -> dict:
    reqs = [_photos_req(), _invoice_req(True), _policy_req(), _comms_req()]
    checklist = [
        "Compare listing description against item photos — document all matches",
        "Gather all buyer communications — especially any that pre-date the dispute",
        "Verify return/refund policy was visible at checkout",
        "If buyer never attempted return, note that in response (contradicts 'not as described')",
        "Gather any third-party reviews or specifications confirming item accuracy",
        "Consider partial refund if listing was ambiguous — avoids chargeback fee",
    ]
    return dict(
        strategy_type=StrategyType.FIGHT_EVIDENCE,
        headline="Gather listing evidence and document policy display; consider partial if ambiguous",
        evidence_requirements=reqs,
        action_checklist=checklist,
        risk_factors=[RiskFactor.SP_NEVER_APPLIES, RiskFactor.HARD_TO_WIN],
        sp_eligible=False, ce3_eligible=False, base_win_rate=0.38,
        escalation_tip="SNAD disputes are hard to win — if evidence is thin, accept early to avoid fee",
    )


def _strategy_unauthorized(network: str, amount: float) -> dict:
    reqs = [_3ds_req(True), _ip_log_req(True), _invoice_req(True), _avs_req()]
    is_visa  = network.upper() == "VISA"
    is_ce3   = is_visa  # CE3 only applies to Visa

    checklist = [
        "Pull 3DS authentication result from payment gateway — PASSED is strongest defense",
        "Pull IP address and device fingerprint at transaction time",
        "Compare IP geolocation against buyer's registered address",
        "Pull AVS and CVV2 response from authorization",
        "Check if buyer has prior non-disputed transactions at same IP/device",
    ]
    if is_ce3:
        checklist.append("If 2+ prior non-disputed transactions exist with this buyer → prepare CE3.0 package (Visa form + prior txn evidence)")

    strat = StrategyType.FIGHT_CE3 if is_ce3 else StrategyType.FIGHT_EVIDENCE
    risks = [RiskFactor.SP_MAY_APPLY]
    if is_ce3:
        risks.append(RiskFactor.CE3_AVAILABLE)

    return dict(
        strategy_type=strat,
        headline=f"{'CE3.0 package + authentication evidence' if is_ce3 else 'Authentication evidence'} — fight assertively",
        evidence_requirements=reqs,
        action_checklist=checklist,
        risk_factors=risks,
        sp_eligible=True, ce3_eligible=is_ce3, base_win_rate=0.45,
        escalation_tip="If 3DS was not used, win probability drops — escalate if amount > $200",
    )


def _strategy_subscription(network: str, amount: float) -> dict:
    reqs = [_sub_terms_req(), _invoice_req(True), _comms_req()]
    checklist = [
        "Pull subscription agreement showing buyer accepted recurring billing terms",
        "Attach renewal notification email sent before the billing date",
        "Attach usage log showing service was accessed/consumed",
        "Show cancellation policy was disclosed — link and timestamp",
        "If buyer never contacted support to cancel, document that absence",
        "If buyer used the service after billing, document usage (login logs, page views)",
    ]
    return dict(
        strategy_type=StrategyType.FIGHT_EVIDENCE,
        headline="Document subscription terms acceptance and service usage",
        evidence_requirements=reqs,
        action_checklist=checklist,
        risk_factors=[RiskFactor.SP_MAY_APPLY],
        sp_eligible=True, ce3_eligible=False, base_win_rate=0.55,
        escalation_tip="Strong case if usage log exists; accept if buyer never consumed the service",
    )


def _strategy_4807(network: str, amount: float) -> dict:
    # 4807 = cardholder did not authorize — Visa specific, highest base win rate
    reqs = [_3ds_req(True), _ip_log_req(True), _invoice_req(True)]
    checklist = [
        "Pull 3DS authentication record — 4807 disputes almost always lost without 3DS",
        "Document IP/device match to known buyer session",
        "If 3DS passed: submit with authentication record as primary evidence",
        "Reference Visa chargeback guide rule 10.7 in cover letter",
    ]
    return dict(
        strategy_type=StrategyType.FIGHT_EVIDENCE,
        headline="Authentication-first fight — 3DS virtually guarantees win on 4807",
        evidence_requirements=reqs,
        action_checklist=checklist,
        risk_factors=[RiskFactor.SP_MAY_APPLY, RiskFactor.EASY_TO_WIN],
        sp_eligible=True, ce3_eligible=False, base_win_rate=0.70,
        escalation_tip="If 3DS is missing, win rate drops dramatically — escalate immediately",
    )


_STRATEGY_DISPATCH = {
    "INR":           _strategy_inr,
    "ITEM_NOT_RECEIVED": _strategy_inr,
    "SNAD":          _strategy_snad,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": _strategy_snad,
    "13.1":          _strategy_snad,
    "13.3":          _strategy_snad,
    "4853":          _strategy_snad,
    "UNAUTHORIZED":  _strategy_unauthorized,
    "10.4":          _strategy_unauthorized,
    "SUBSCRIPTION":  _strategy_subscription,
    "12.6":          _strategy_subscription,
    "4807":          _strategy_4807,
}


def advise_reason_code_strategy(inp: ReasonCodeStrategyInput) -> ReasonCodeStrategyReport:
    """Return a tailored dispute strategy for the given reason code and network."""
    code = inp.reason_code.upper()
    net  = inp.network.upper()

    dispatch_fn = _STRATEGY_DISPATCH.get(code)

    if dispatch_fn is None:
        # Default: fight with generic evidence package
        strategy_data = dict(
            strategy_type=StrategyType.FIGHT_EVIDENCE,
            headline=f"Generic fight strategy for {code} — assemble all available evidence",
            evidence_requirements=[_invoice_req(True), _comms_req()],
            action_checklist=[
                "Identify what the buyer claims went wrong",
                "Gather all transaction records and communication logs",
                "Submit response with full evidence package",
            ],
            risk_factors=[],
            sp_eligible=False, ce3_eligible=False, base_win_rate=0.45,
            escalation_tip="Unknown reason code — consult network guidelines directly",
        )
    else:
        strategy_data = dispatch_fn(net, inp.transaction_amount)

    summary = (
        f"Strategy {inp.dispute_id} [{net}|{code}]: "
        f"{strategy_data['strategy_type'].value} | "
        f"sp={strategy_data['sp_eligible']} ce3={strategy_data['ce3_eligible']} | "
        f"base_win={strategy_data['base_win_rate']:.0%}"
    )

    return ReasonCodeStrategyReport(
        dispute_id=inp.dispute_id,
        reason_code=code,
        network=net,
        strategy_type=strategy_data["strategy_type"],
        headline=strategy_data["headline"],
        evidence_requirements=strategy_data["evidence_requirements"],
        action_checklist=strategy_data["action_checklist"],
        risk_factors=strategy_data["risk_factors"],
        sp_eligible=strategy_data["sp_eligible"],
        ce3_eligible=strategy_data["ce3_eligible"],
        base_win_rate=strategy_data["base_win_rate"],
        escalation_tip=strategy_data["escalation_tip"],
        summary=summary,
    )
