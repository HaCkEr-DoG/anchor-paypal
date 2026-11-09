"""
HDR War Room — emergency action protocol engine for High Dispute Rate intervention.

PayPal's High Dispute Rate (HDR) program punishes merchants with elevated dispute rates:
  < 0.50%:  Normal (safe)
  0.50-1.0%: ELEVATED — warning, monitor closely
  1.0-1.5%: LIMITED — processing restrictions may activate
  1.5-3.0%: FEE TIER — $30 surcharge per dispute above 1.5% threshold
  3.0-5.0%: SEVERE — additional restrictions + account review
  5.0%+:    TERMINAL — account suspension risk

The war room module answers:
  "Given current HDR rate, trajectory, and time window — what are the highest-leverage
  interventions to reduce rate before PayPal's next monthly calculation?"

Key insight: HDR is computed on a ROLLING 90-DAY window.
  - New disputes enter at the front
  - Old disputes fall off at the back
  - You can lower HDR by: (a) reducing new disputes, (b) increasing transaction volume,
    or (c) waiting for old dispute clusters to age out

This module provides:
  - Current zone assessment
  - Projected HDR in 30/60/90 days under different intervention scenarios
  - Prioritized action plan sorted by expected rate reduction
  - Specific product/channel pause recommendations
  - Preemptive refund ROI calculation

Output:
  - WarRoomReport with current zone, projections, and action plan
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HDRZone(str, Enum):
    SAFE     = "SAFE"      # < 0.50%
    ELEVATED = "ELEVATED"  # 0.50-1.0%
    LIMITED  = "LIMITED"   # 1.0-1.5%
    FEE_TIER = "FEE_TIER"  # 1.5-3.0%
    SEVERE   = "SEVERE"    # 3.0-5.0%
    TERMINAL = "TERMINAL"  # 5.0%+


class InterventionType(str, Enum):
    PAUSE_HIGH_RISK_PRODUCTS  = "PAUSE_HIGH_RISK_PRODUCTS"
    PAUSE_HIGH_RISK_CHANNELS  = "PAUSE_HIGH_RISK_CHANNELS"
    PREEMPTIVE_REFUNDS        = "PREEMPTIVE_REFUNDS"
    UPGRADE_CARRIER           = "UPGRADE_CARRIER"
    REQUIRE_SIGNATURE         = "REQUIRE_SIGNATURE"
    INCREASE_VOLUME           = "INCREASE_VOLUME"
    BLOCK_RISKY_BUYERS        = "BLOCK_RISKY_BUYERS"
    CONTACT_PAYPAL            = "CONTACT_PAYPAL"
    EVIDENCE_QUALITY_BOOST    = "EVIDENCE_QUALITY_BOOST"
    ADDRESS_VERIFICATION      = "ADDRESS_VERIFICATION"


# Zone boundaries (inclusive lower)
_ZONE_THRESHOLDS = [
    (0.050, HDRZone.TERMINAL),
    (0.030, HDRZone.SEVERE),
    (0.015, HDRZone.FEE_TIER),
    (0.010, HDRZone.LIMITED),
    (0.005, HDRZone.ELEVATED),
    (0.000, HDRZone.SAFE),
]


def _classify_zone(rate: float) -> HDRZone:
    for threshold, zone in _ZONE_THRESHOLDS:
        if rate >= threshold:
            return zone
    return HDRZone.SAFE


@dataclass
class HDRWarRoomInput:
    """Current merchant state for war room analysis."""
    # Current HDR metrics
    current_hdr_rate: float          # E.g. 0.012 = 1.2%
    disputes_last_90d: int           # Total disputes in rolling window
    transactions_last_90d: int       # Total transactions in rolling window

    # Trend signals
    disputes_last_30d: int = 0       # Recent dispute acceleration
    transactions_last_30d: int = 0   # Recent transaction volume

    # Monthly revenue context
    monthly_gmv: float = 100000.0
    avg_dispute_amount: float = 150.0

    # Existing interventions
    has_carrier_upgrade: bool = False
    has_buyer_vetting: bool = False
    has_address_verification: bool = False

    # Product mix (% of disputes from high-risk categories)
    high_risk_product_pct: float = 0.30   # % of sales that are high-risk products
    high_risk_channel_pct: float = 0.20   # % of sales from high-risk channels

    # Preemptive refund context
    open_at_risk_disputes: int = 0        # Disputes where you could proactively refund
    avg_at_risk_dispute_amount: float = 100.0

    # PayPal relationship context
    months_in_hdr_program: int = 0        # How long at current zone


@dataclass
class HDRIntervention:
    """One specific recommended action with projected impact."""
    intervention_type: InterventionType
    title: str
    description: str
    expected_rate_reduction: float    # Absolute reduction (e.g. 0.003 = 0.3pp)
    implementation_effort: str        # LOW / MEDIUM / HIGH
    time_to_impact_days: int         # Days until rate starts improving
    revenue_impact_estimate: float    # Negative = revenue loss, positive = revenue protected
    priority: int                     # 1 = highest


@dataclass
class HDRProjection:
    """Projected HDR rate under a specific scenario."""
    scenario: str
    days_out: int
    projected_rate: float
    projected_zone: HDRZone
    assumptions: list[str] = field(default_factory=list)


@dataclass
class WarRoomReport:
    """
    Complete HDR War Room assessment.
    Entry point for emergency dispute rate intervention.
    """
    # Current state
    current_hdr_rate: float
    current_zone: HDRZone
    is_emergency: bool      # True if LIMITED or worse

    # Rate to next zone
    bps_to_next_threshold: int      # Basis points of headroom (positive = moving away)
    bps_from_prev_threshold: int    # Basis points above last threshold

    # Monthly fee impact (if in FEE_TIER)
    monthly_hdr_fee: float
    fee_tier_annual_cost: float

    # Projections
    projections: list[HDRProjection]

    # Interventions (sorted by priority)
    interventions: list[HDRIntervention]

    # Narrative
    zone_assessment: str
    immediate_actions: list[str]     # Do today
    this_week_actions: list[str]     # Do this week
    avoid_actions: list[str]         # What NOT to do (common mistakes)

    summary: str


def _bps_to_next_threshold(rate: float, zone: HDRZone) -> int:
    """Basis points of room before entering next (worse) zone. Negative = already over."""
    zone_upper = {
        HDRZone.SAFE: 0.005,
        HDRZone.ELEVATED: 0.010,
        HDRZone.LIMITED: 0.015,
        HDRZone.FEE_TIER: 0.030,
        HDRZone.SEVERE: 0.050,
        HDRZone.TERMINAL: 1.0,
    }
    upper = zone_upper.get(zone, 1.0)
    return int((upper - rate) * 10000)


def _bps_from_prev_threshold(rate: float, zone: HDRZone) -> int:
    """Basis points above entering this zone (how deep in)."""
    zone_lower = {
        HDRZone.SAFE: 0.0,
        HDRZone.ELEVATED: 0.005,
        HDRZone.LIMITED: 0.010,
        HDRZone.FEE_TIER: 0.015,
        HDRZone.SEVERE: 0.030,
        HDRZone.TERMINAL: 0.050,
    }
    lower = zone_lower.get(zone, 0.0)
    return int((rate - lower) * 10000)


def _monthly_hdr_fee(rate: float, transactions_per_month: int) -> float:
    """$30 per dispute above 1.5% threshold."""
    if rate <= 0.015:
        return 0.0
    excess_disputes = (rate - 0.015) * transactions_per_month
    return excess_disputes * 30.0


def _build_projections(inp: HDRWarRoomInput) -> list[HDRProjection]:
    """
    Project HDR rate under three scenarios: do-nothing, moderate, aggressive.
    Uses simplified rolling window model.
    """
    projections: list[HDRProjection] = []
    t90 = inp.transactions_last_90d
    d90 = inp.disputes_last_90d
    # Monthly run rate
    d30 = inp.disputes_last_30d or round(d90 / 3)
    t30 = inp.transactions_last_30d or round(t90 / 3)

    for days_out in [30, 60, 90]:
        # Periods rolling off (old) and new additions
        months = days_out / 30
        d_aging_off = round(d30 * months * 0.9)   # 90% of old disputes age off

        # Do-nothing: same rate continues
        new_d = round(d30 * months)
        new_t = round(t30 * months)
        proj_d = max(0, d90 - d_aging_off + new_d)
        proj_t = max(1, t90 - round(t30 * months * 0.9) + new_t)
        rate_dn = proj_d / proj_t
        projections.append(HDRProjection(
            "do_nothing", days_out, round(rate_dn, 4), _classify_zone(rate_dn),
            [f"Assumes {d30} disputes/month and {t30} transactions/month continue"],
        ))

        # Moderate: 20% dispute reduction, 10% volume increase
        new_d_mod = round(d30 * months * 0.80)
        new_t_mod = round(t30 * months * 1.10)
        proj_d_mod = max(0, d90 - d_aging_off + new_d_mod)
        proj_t_mod = max(1, t90 - round(t30 * months * 0.9) + new_t_mod)
        rate_mod = proj_d_mod / proj_t_mod
        projections.append(HDRProjection(
            "moderate_intervention", days_out, round(rate_mod, 4), _classify_zone(rate_mod),
            ["20% dispute reduction via carrier upgrade + buyer vetting", "10% transaction volume increase"],
        ))

        # Aggressive: 40% dispute reduction, 20% volume increase + preemptive refunds
        new_d_agg = round(d30 * months * 0.60)
        new_t_agg = round(t30 * months * 1.20)
        proj_d_agg = max(0, d90 - d_aging_off + new_d_agg)
        proj_t_agg = max(1, t90 - round(t30 * months * 0.9) + new_t_agg)
        rate_agg = proj_d_agg / proj_t_agg
        projections.append(HDRProjection(
            "aggressive_intervention", days_out, round(rate_agg, 4), _classify_zone(rate_agg),
            ["40% dispute reduction: pause high-risk products + preemptive refunds + carrier upgrade", "20% volume increase from safe channels"],
        ))

    return projections


def _build_interventions(inp: HDRWarRoomInput, zone: HDRZone) -> list[HDRIntervention]:
    interventions: list[HDRIntervention] = []
    priority = 1

    # 1. Preemptive refunds — fastest impact on open disputes
    if inp.open_at_risk_disputes > 0:
        cost = inp.open_at_risk_disputes * inp.avg_at_risk_dispute_amount
        saved_rate = inp.open_at_risk_disputes / max(1, inp.transactions_last_90d)
        interventions.append(HDRIntervention(
            InterventionType.PREEMPTIVE_REFUNDS,
            f"Issue preemptive refunds on {inp.open_at_risk_disputes} at-risk open disputes",
            f"Proactively refund disputes where you are likely to lose. Removes them from HDR count immediately.",
            expected_rate_reduction=round(saved_rate, 4),
            implementation_effort="LOW",
            time_to_impact_days=1,
            revenue_impact_estimate=-cost,
            priority=priority,
        ))
        priority += 1

    # 2. Pause high-risk products
    if inp.high_risk_product_pct > 0.15:
        reduction = inp.current_hdr_rate * inp.high_risk_product_pct * 0.70
        interventions.append(HDRIntervention(
            InterventionType.PAUSE_HIGH_RISK_PRODUCTS,
            "Pause or restrict high-risk product categories",
            f"Temporarily pause sales of gift cards, digital goods, event tickets, or collectibles "
            f"(currently {inp.high_risk_product_pct:.0%} of sales). These categories have 3-8x average dispute rates.",
            expected_rate_reduction=round(reduction, 4),
            implementation_effort="LOW",
            time_to_impact_days=3,
            revenue_impact_estimate=-inp.monthly_gmv * inp.high_risk_product_pct * 0.20,
            priority=priority,
        ))
        priority += 1

    # 3. Carrier upgrade
    if not inp.has_carrier_upgrade:
        interventions.append(HDRIntervention(
            InterventionType.UPGRADE_CARRIER,
            "Upgrade to PLATINUM carrier (FedEx Express or USPS Priority)",
            "Switch to carriers with <1% INR dispute rate. Provides tracking + delivery confirmation "
            "that PayPal reviewers accept as delivery proof. Reduces INR disputes by 40-60%.",
            expected_rate_reduction=round(inp.current_hdr_rate * 0.20, 4),
            implementation_effort="LOW",
            time_to_impact_days=7,
            revenue_impact_estimate=-inp.monthly_gmv * 0.002,  # ~0.2% shipping cost increase
            priority=priority,
        ))
        priority += 1

    # 4. Increase volume (dilutes rate)
    interventions.append(HDRIntervention(
        InterventionType.INCREASE_VOLUME,
        "Increase transaction volume in safe product categories",
        "More transactions at safe dispute rates dilutes HDR rate without reducing dispute count. "
        "Even 10% volume increase in low-risk products can move rate down 1-2 basis points.",
        expected_rate_reduction=round(inp.current_hdr_rate * 0.10, 4),
        implementation_effort="MEDIUM",
        time_to_impact_days=14,
        revenue_impact_estimate=inp.monthly_gmv * 0.10,
        priority=priority,
    ))
    priority += 1

    # 5. Block risky buyers
    if not inp.has_buyer_vetting:
        interventions.append(HDRIntervention(
            InterventionType.BLOCK_RISKY_BUYERS,
            "Activate buyer dispute prediction and block repeat disputers",
            "Block buyers with >10% lifetime dispute rate. Enable pre-order dispute prediction. "
            "Buyer vetting reduces dispute rate by 15-25% with minimal revenue impact.",
            expected_rate_reduction=round(inp.current_hdr_rate * 0.15, 4),
            implementation_effort="LOW",
            time_to_impact_days=3,
            revenue_impact_estimate=-inp.monthly_gmv * 0.01,
            priority=priority,
        ))
        priority += 1

    # 6. Require signature on all orders
    if zone in (HDRZone.FEE_TIER, HDRZone.SEVERE, HDRZone.TERMINAL):
        interventions.append(HDRIntervention(
            InterventionType.REQUIRE_SIGNATURE,
            "Require signature confirmation on ALL orders (emergency mode)",
            "Signature confirmation provides definitive delivery proof. In fee tier or severe zone, "
            "the carrier cost (~$3-5/shipment) is vastly cheaper than $30/dispute HDR fees.",
            expected_rate_reduction=round(inp.current_hdr_rate * 0.12, 4),
            implementation_effort="LOW",
            time_to_impact_days=7,
            revenue_impact_estimate=-inp.transactions_last_30d * 4.0,
            priority=priority,
        ))
        priority += 1

    # 7. Contact PayPal (only for severe/terminal)
    if zone in (HDRZone.SEVERE, HDRZone.TERMINAL):
        interventions.append(HDRIntervention(
            InterventionType.CONTACT_PAYPAL,
            "Contact PayPal Merchant Relations immediately",
            "At SEVERE or TERMINAL rates, proactive outreach to PayPal demonstrates good faith. "
            "Explain remediation plan. Request temporary processing allowance while rate recovers.",
            expected_rate_reduction=0.0,
            implementation_effort="MEDIUM",
            time_to_impact_days=0,
            revenue_impact_estimate=0.0,
            priority=priority,
        ))
        priority += 1

    interventions.sort(key=lambda i: i.priority)
    return interventions


def _build_zone_assessment(rate: float, zone: HDRZone, bps_to_next: int) -> str:
    assessments = {
        HDRZone.SAFE: f"HDR at {rate:.2%} — SAFE zone. {bps_to_next}bps buffer to ELEVATED. No action required; monitor monthly.",
        HDRZone.ELEVATED: f"HDR at {rate:.2%} — ELEVATED zone. {bps_to_next}bps to LIMITED restrictions. Implement preventive measures now.",
        HDRZone.LIMITED: f"HDR at {rate:.2%} — LIMITED zone. Processing restrictions may activate. URGENT: initiate war room protocol.",
        HDRZone.FEE_TIER: f"HDR at {rate:.2%} — FEE TIER. $30/dispute surcharge active. Immediate action required to stop bleeding.",
        HDRZone.SEVERE: f"HDR at {rate:.2%} — SEVERE zone. Account review active. Executive escalation + PayPal contact required NOW.",
        HDRZone.TERMINAL: f"HDR at {rate:.2%} — TERMINAL zone. Account suspension imminent. All non-essential operations should halt.",
    }
    return assessments.get(zone, f"HDR at {rate:.2%}")


def assess_hdr_war_room(inp: HDRWarRoomInput) -> WarRoomReport:
    """
    Generate complete HDR war room assessment with prioritized intervention plan.
    """
    zone = _classify_zone(inp.current_hdr_rate)
    is_emergency = zone not in (HDRZone.SAFE, HDRZone.ELEVATED)

    bps_to_next = _bps_to_next_threshold(inp.current_hdr_rate, zone)
    bps_from_prev = _bps_from_prev_threshold(inp.current_hdr_rate, zone)

    t_per_month = inp.transactions_last_30d or round(inp.transactions_last_90d / 3)
    monthly_fee = _monthly_hdr_fee(inp.current_hdr_rate, t_per_month)
    annual_fee = monthly_fee * 12

    projections = _build_projections(inp)
    interventions = _build_interventions(inp, zone)

    zone_assessment = _build_zone_assessment(inp.current_hdr_rate, zone, bps_to_next)

    # Immediate vs. this-week actions
    immediate = [i.title for i in interventions if i.time_to_impact_days <= 1]
    this_week = [i.title for i in interventions if 1 < i.time_to_impact_days <= 7]

    avoid = [
        "Do NOT pause ALL sales — diluting volume by reducing transactions makes rate WORSE",
        "Do NOT dispute every case to 'win' HDR — fighting doesn't remove disputes from count",
        "Do NOT ignore the trajectory — waiting a month to act compounds the fee exposure",
        "Do NOT refund only the top 10 disputes — partial effort rarely moves rate enough",
    ]

    summary = (
        f"HDR {inp.current_hdr_rate:.2%} — {zone.value} | "
        f"Headroom to next threshold: {bps_to_next}bps | "
        f"Monthly fee: ${monthly_fee:,.0f} | "
        f"Top action: {interventions[0].title if interventions else 'Monitor'}"
    )

    return WarRoomReport(
        current_hdr_rate=inp.current_hdr_rate,
        current_zone=zone,
        is_emergency=is_emergency,
        bps_to_next_threshold=bps_to_next,
        bps_from_prev_threshold=bps_from_prev,
        monthly_hdr_fee=round(monthly_fee, 2),
        fee_tier_annual_cost=round(annual_fee, 2),
        projections=projections,
        interventions=interventions,
        zone_assessment=zone_assessment,
        immediate_actions=immediate,
        this_week_actions=this_week,
        avoid_actions=avoid,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Wave5 / test-API  — HDRSnapshot, HDRForecast, analyze_hdr_crisis
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dc5w, field as _f5w


@_dc5w
class HDRSnapshot:
    """Wave5 merchant snapshot for war-room analysis."""
    snapshot_date: object = None
    total_transactions_90d: int = 1000
    total_disputes_90d: int = 10
    dispute_rate: float = 0.010
    zone: HDRZone = HDRZone.LIMITED
    weekly_new_disputes: list = _f5w(default_factory=lambda: [2, 2, 3, 3])
    weekly_new_transactions: list = _f5w(default_factory=lambda: [70, 70, 70, 70])
    open_disputes: int = 5


@_dc5w
class _HDRAction:
    action_id: str
    description: str
    urgency: str = "HIGH"


@_dc5w
class HDRForecast:
    trajectory: str           # "WORSENING" | "IMPROVING" | "STABLE"
    zone_30d: HDRZone
    days_30_projected: float  # projected dispute rate in 30 days


@_dc5w
class _HDRCrisisReport:
    current_zone: HDRZone
    executive_escalation_required: bool
    paypal_proactive_contact_recommended: bool
    basis_points_from_limited: float
    immediate_actions: list
    forecast: HDRForecast
    maximum_new_disputes_this_month: int
    transactions_needed_to_normalize: int
    summary: str
    alarm_message: str


def analyze_hdr_crisis(snap: HDRSnapshot) -> _HDRCrisisReport:
    """Wave5 crisis analysis from HDRSnapshot."""
    rate = snap.dispute_rate
    zone = snap.zone
    tx = snap.total_transactions_90d
    disp = snap.total_disputes_90d

    # Basis points from LIMITED threshold (1.00%)
    bps_from_limited = (0.010 - rate) * 10000.0

    # Escalation / contact flags
    exec_escalate = zone in (HDRZone.SEVERE, HDRZone.TERMINAL)
    pp_contact = zone in (HDRZone.LIMITED, HDRZone.FEE_TIER, HDRZone.SEVERE, HDRZone.TERMINAL)

    # Immediate actions
    actions = [_HDRAction("REVIEW_DISPUTE_ROOT_CAUSE", "Audit last 30 disputes for root cause")]
    if zone in (HDRZone.LIMITED, HDRZone.FEE_TIER, HDRZone.SEVERE, HDRZone.TERMINAL):
        actions.insert(0, _HDRAction("PREEMPTIVE_REFUNDS", "Issue preemptive refunds on at-risk orders"))
        actions.append(_HDRAction("PAUSE_HIGH_RISK_PRODUCTS", "Pause listings with elevated dispute rates"))
    if zone in (HDRZone.SEVERE, HDRZone.TERMINAL):
        actions.insert(0, _HDRAction("EXEC_ESCALATION", "Escalate to executive team and PayPal account manager"))

    # Forecast
    wd = snap.weekly_new_disputes
    if len(wd) >= 2:
        trend = wd[-1] - wd[0]
        if trend > 0.5:
            traj = "WORSENING"
        elif trend < -0.5:
            traj = "IMPROVING"
        else:
            traj = "STABLE"
    else:
        traj = "STABLE"

    tx_per_week = sum(snap.weekly_new_transactions) / max(1, len(snap.weekly_new_transactions))
    disp_per_week = sum(wd) / max(1, len(wd))
    # Simple 30d projection
    tx_30d = tx_per_week * 4.3 + max(0, tx - tx_per_week * 13)
    disp_delta = (wd[-1] - wd[0]) * 2 if len(wd) >= 2 else 0
    proj_disp = disp + disp_delta
    proj_rate = proj_disp / max(1, tx) if tx > 0 else rate
    proj_zone = _classify_zone(proj_rate)
    forecast = HDRForecast(trajectory=traj, zone_30d=proj_zone, days_30_projected=round(proj_rate, 5))

    # Max new disputes before next threshold
    next_thresh = {HDRZone.SAFE: 0.005, HDRZone.ELEVATED: 0.010, HDRZone.LIMITED: 0.015,
                   HDRZone.FEE_TIER: 0.030, HDRZone.SEVERE: 0.050, HDRZone.TERMINAL: 1.0}
    upper = next_thresh.get(zone, 1.0)
    max_new = max(0, int(upper * tx) - disp)

    # Transactions needed to normalize (bring rate to safe 0.005)
    target_rate = 0.005
    tx_needed = max(0, int(disp / target_rate) - tx) if target_rate > 0 else 0

    summary = (
        f"HDR {zone.value}: rate={rate*100:.2f}% ({bps_from_limited:+.0f}bps from LIMITED) | "
        f"disputes={disp}/{tx} | forecast={traj}"
    )

    if zone == HDRZone.TERMINAL:
        alarm = f"TERMINAL HDR zone: {rate*100:.2f}% — suspension risk. Immediate intervention required."
    elif zone == HDRZone.SEVERE:
        alarm = f"SEVERE HDR zone: {rate*100:.2f}% — HDR program consequences escalating. Exec escalation needed."
    elif zone in (HDRZone.LIMITED, HDRZone.FEE_TIER):
        alarm = f"{zone.value} HDR zone: {rate*100:.2f}% — contact PayPal and reduce dispute volume immediately."
    else:
        alarm = f"HDR zone {zone.value}: {rate*100:.2f}% — monitor closely, {bps_from_limited:+.0f}bps from LIMITED."

    return _HDRCrisisReport(
        current_zone=zone,
        executive_escalation_required=exec_escalate,
        paypal_proactive_contact_recommended=pp_contact,
        basis_points_from_limited=bps_from_limited,
        immediate_actions=actions,
        forecast=forecast,
        maximum_new_disputes_this_month=max_new,
        transactions_needed_to_normalize=tx_needed,
        summary=summary,
        alarm_message=alarm,
    )
