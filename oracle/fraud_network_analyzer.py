"""
Fraud Network Analyzer — cross-buyer fraud pattern detection.

Individual disputes look isolated. The fraud network analyzer looks at
signals across the merchant's entire dispute portfolio to identify:

  1. Address clustering: Multiple different buyers at same address
  2. Velocity patterns: Same buyer filing multiple disputes quickly
  3. Card-testing pattern: Small successful order then large disputed order
  4. Coordinated fraud: Multiple buyers with same dispute reason on same day
  5. Sequential ID fraud: Order IDs that increment suspiciously fast

PayPal's fraud team responds better to merchants who present network evidence
("these 5 buyers all used the same address / filed disputes the same day")
than merchants who fight each dispute individually.

Network risk levels:
  ISOLATED  — single dispute, no network signal
  ELEVATED  — 2-3 weak signals
  HIGH      — 3+ signals or one very strong signal (same address cluster ≥3)
  CRITICAL  — organized ring suspected (address cluster ≥5 or velocity ≥4 in 7d)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class FraudSignalType(str, Enum):
    ADDRESS_CLUSTER      = "ADDRESS_CLUSTER"       # Multiple buyers at same address
    VELOCITY_BURST       = "VELOCITY_BURST"        # Same buyer, multiple disputes fast
    CARD_TEST_SEQUENCE   = "CARD_TEST_SEQUENCE"    # Small probe → large dispute
    COORDINATED_FILING   = "COORDINATED_FILING"    # Multiple buyers same day, same reason
    SEQUENTIAL_ID        = "SEQUENTIAL_ID"         # Suspiciously sequential buyer IDs
    HIGH_DISPUTE_RATE    = "HIGH_DISPUTE_RATE"     # Buyer's personal dispute rate is extreme


class NetworkRiskLevel(str, Enum):
    ISOLATED = "ISOLATED"
    ELEVATED = "ELEVATED"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class BuyerSignal:
    """Signal data for one buyer in the network."""
    buyer_id: str
    email: str
    shipping_address: str         # Normalized (lowercase, stripped)
    dispute_count_90d: int
    dispute_amounts: list[float] = field(default_factory=list)
    dispute_dates: list[date] = field(default_factory=list)
    purchase_count_90d: int = 1
    first_order_amount: Optional[float] = None


@dataclass
class FraudSignal:
    """One detected fraud signal."""
    signal_type: FraudSignalType
    severity: float               # 0-1
    affected_buyer_ids: list[str]
    description: str
    evidence: str                 # What to include in PayPal fraud report


@dataclass
class FraudNetworkReport:
    """
    Complete fraud network analysis for one dispute in context of a portfolio.
    """
    dispute_id: str
    buyer_id: str

    # Risk assessment
    network_risk_level: NetworkRiskLevel
    risk_score: float             # 0-100
    is_organized_fraud: bool

    # Detected signals
    signals: list[FraudSignal]
    signal_count: int

    # Related buyers (potential co-conspirators)
    related_buyer_ids: list[str]  # Buyers who share signals with this buyer

    # Action guidance
    recommended_action: str
    paypal_report_summary: str    # Text block to paste into PayPal response
    flags_for_buyer_lifecycle: list[str]

    summary: str


def _normalize_address(addr: str) -> str:
    return " ".join(addr.lower().strip().split())


def _detect_address_cluster(
    target: BuyerSignal,
    all_buyers: list[BuyerSignal],
) -> Optional[FraudSignal]:
    """Detect buyers sharing a shipping address."""
    target_addr = _normalize_address(target.shipping_address)
    if not target_addr:
        return None
    cluster = [
        b for b in all_buyers
        if _normalize_address(b.shipping_address) == target_addr
        and b.buyer_id != target.buyer_id
        and b.dispute_count_90d > 0
    ]
    if not cluster:
        return None
    count = len(cluster) + 1  # Including target
    severity = min(1.0, 0.3 + (count - 2) * 0.2)
    return FraudSignal(
        signal_type=FraudSignalType.ADDRESS_CLUSTER,
        severity=round(severity, 2),
        affected_buyer_ids=[target.buyer_id] + [b.buyer_id for b in cluster],
        description=f"{count} buyers with disputes share shipping address: '{target_addr}'",
        evidence=(
            f"Address '{target_addr}' is associated with {count} different buyer accounts "
            f"that have all filed disputes within 90 days. Buyer IDs: "
            f"{', '.join([target.buyer_id] + [b.buyer_id for b in cluster])}."
        ),
    )


def _detect_velocity_burst(target: BuyerSignal) -> Optional[FraudSignal]:
    """Detect same buyer filing multiple disputes in a short window."""
    if target.dispute_count_90d < 2:
        return None
    if len(target.dispute_dates) < 2:
        # Use count as proxy
        if target.dispute_count_90d >= 4:
            severity = 0.80
        elif target.dispute_count_90d >= 3:
            severity = 0.65
        else:
            severity = 0.45
        return FraudSignal(
            signal_type=FraudSignalType.VELOCITY_BURST,
            severity=severity,
            affected_buyer_ids=[target.buyer_id],
            description=f"Buyer filed {target.dispute_count_90d} disputes in 90 days",
            evidence=(
                f"Buyer {target.buyer_id} has filed {target.dispute_count_90d} disputes "
                f"in the past 90 days. Industry average is <0.5 disputes per buyer."
            ),
        )
    # Check tightest window (sort dates, find min span over 2+ disputes)
    sorted_dates = sorted(target.dispute_dates)
    min_window = None
    for i in range(len(sorted_dates) - 1):
        span = (sorted_dates[i + 1] - sorted_dates[i]).days
        if min_window is None or span < min_window:
            min_window = span
    if min_window is not None and min_window <= 7:
        return FraudSignal(
            signal_type=FraudSignalType.VELOCITY_BURST,
            severity=0.85,
            affected_buyer_ids=[target.buyer_id],
            description=(
                f"Buyer filed {target.dispute_count_90d} disputes, "
                f"with 2 within {min_window} days"
            ),
            evidence=(
                f"Buyer {target.buyer_id} filed disputes on "
                f"{', '.join(str(d) for d in sorted_dates)}. "
                f"Two disputes within {min_window} days strongly suggests fraudulent intent."
            ),
        )
    return None


def _detect_card_test(target: BuyerSignal) -> Optional[FraudSignal]:
    """Detect card-testing: small successful order, then larger dispute."""
    if not target.first_order_amount or not target.dispute_amounts:
        return None
    large_disputes = [a for a in target.dispute_amounts if a > target.first_order_amount * 3]
    if large_disputes:
        largest = max(large_disputes)
        ratio = largest / target.first_order_amount
        return FraudSignal(
            signal_type=FraudSignalType.CARD_TEST_SEQUENCE,
            severity=min(1.0, round(0.5 + (ratio - 3) * 0.1, 2)),
            affected_buyer_ids=[target.buyer_id],
            description=(
                f"First order: ${target.first_order_amount:.0f}, "
                f"then dispute for ${largest:.0f} ({ratio:.1f}× larger)"
            ),
            evidence=(
                f"Buyer {target.buyer_id} made a first purchase of ${target.first_order_amount:.0f} "
                f"(likely card-test to verify payment) followed by a ${largest:.0f} order "
                f"that is now disputed. This is a known fraud pattern."
            ),
        )
    return None


def _detect_coordinated_filing(
    target: BuyerSignal,
    all_buyers: list[BuyerSignal],
) -> Optional[FraudSignal]:
    """Detect multiple buyers filing disputes on the same day with the same reason."""
    if not target.dispute_dates:
        return None
    target_dates = set(target.dispute_dates)
    coordinated = [
        b for b in all_buyers
        if b.buyer_id != target.buyer_id
        and b.dispute_dates
        and target_dates & set(b.dispute_dates)   # Overlapping dispute dates
        and b.dispute_count_90d >= 1
    ]
    if len(coordinated) >= 2:
        all_ids = [target.buyer_id] + [b.buyer_id for b in coordinated]
        return FraudSignal(
            signal_type=FraudSignalType.COORDINATED_FILING,
            severity=min(1.0, 0.4 + len(coordinated) * 0.15),
            affected_buyer_ids=all_ids,
            description=f"{len(all_ids)} buyers filed disputes on the same day",
            evidence=(
                f"Buyers {', '.join(all_ids)} all filed disputes on overlapping dates. "
                f"Coordinated filing within a 24-hour window suggests organized fraud."
            ),
        )
    return None


def _risk_level(score: float, signals: list[FraudSignal]) -> tuple[NetworkRiskLevel, bool]:
    critical_signals = [s for s in signals if s.signal_type == FraudSignalType.ADDRESS_CLUSTER
                        and s.severity >= 0.7]
    if score >= 70 or critical_signals:
        # Check for organized ring
        is_organized = (
            any(len(s.affected_buyer_ids) >= 5 for s in signals)
            or score >= 85
        )
        level = NetworkRiskLevel.CRITICAL if is_organized else NetworkRiskLevel.HIGH
        return level, is_organized
    if score >= 40:
        return NetworkRiskLevel.ELEVATED, False
    return NetworkRiskLevel.ISOLATED, False


def analyze_fraud_network(
    dispute_id: str,
    buyer: BuyerSignal,
    all_buyers: list[BuyerSignal],
) -> FraudNetworkReport:
    """
    Analyze fraud signals for one dispute in the context of the merchant's
    entire buyer portfolio.
    """
    signals: list[FraudSignal] = []

    addr_signal = _detect_address_cluster(buyer, all_buyers)
    if addr_signal:
        signals.append(addr_signal)

    vel_signal = _detect_velocity_burst(buyer)
    if vel_signal:
        signals.append(vel_signal)

    card_signal = _detect_card_test(buyer)
    if card_signal:
        signals.append(card_signal)

    coord_signal = _detect_coordinated_filing(buyer, all_buyers)
    if coord_signal:
        signals.append(coord_signal)

    # Score: sum of severity values, scaled to 0-100
    raw_score = sum(s.severity for s in signals) * 25  # 4 max signals × 1.0 × 25 = 100
    risk_score = round(min(100.0, raw_score), 1)

    risk_level, is_organized = _risk_level(risk_score, signals)

    # Related buyers (across all signals)
    related_ids: set[str] = set()
    for s in signals:
        for b in s.affected_buyer_ids:
            if b != buyer.buyer_id:
                related_ids.add(b)

    # Recommended action
    if risk_level == NetworkRiskLevel.CRITICAL:
        action = (
            "ESCALATE TO FRAUD TEAM immediately. Submit network fraud report to PayPal. "
            "Block all related buyer accounts and addresses."
        )
    elif risk_level == NetworkRiskLevel.HIGH:
        action = (
            "Fight dispute with network fraud evidence. Submit evidence of coordinated pattern. "
            "Flag buyer for SUSPICIOUS lifecycle stage."
        )
    elif risk_level == NetworkRiskLevel.ELEVATED:
        action = (
            "Include fraud network signals in dispute response. "
            "Monitor related buyer accounts closely."
        )
    else:
        action = "Treat as isolated dispute. No network fraud evidence detected."

    # PayPal report summary
    if signals:
        evidence_blocks = "\n".join(f"- {s.evidence}" for s in signals)
        paypal_report = (
            f"FRAUD NETWORK EVIDENCE for Dispute {dispute_id}:\n"
            f"Risk Level: {risk_level.value} (score={risk_score:.0f}/100)\n\n"
            f"Detected Patterns:\n{evidence_blocks}"
        )
    else:
        paypal_report = f"No fraud network evidence detected for dispute {dispute_id}."

    flags = []
    if risk_level in (NetworkRiskLevel.HIGH, NetworkRiskLevel.CRITICAL):
        flags.append("SUSPICIOUS")
        flags.append("BLOCK")
    elif risk_level == NetworkRiskLevel.ELEVATED:
        flags.append("ELEVATED_WATCH")

    summary = (
        f"Fraud network {dispute_id}: risk={risk_level.value} "
        f"(score={risk_score:.0f}) | signals={len(signals)} | "
        f"related_buyers={len(related_ids)}"
    )

    return FraudNetworkReport(
        dispute_id=dispute_id,
        buyer_id=buyer.buyer_id,
        network_risk_level=risk_level,
        risk_score=risk_score,
        is_organized_fraud=is_organized,
        signals=signals,
        signal_count=len(signals),
        related_buyer_ids=list(related_ids),
        recommended_action=action,
        paypal_report_summary=paypal_report,
        flags_for_buyer_lifecycle=flags,
        summary=summary,
    )
