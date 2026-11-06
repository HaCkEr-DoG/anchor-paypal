"""
Digital Goods Defense Engine — win disputes on items PayPal Seller Protection excludes.

PayPal Seller Protection does NOT cover digital goods (software, downloads, subscriptions,
NFTs, digital gift cards, online services). This means:
  - Every digital goods dispute must be won on evidence merits alone
  - No SP shield to invoke — the merchant is on their own
  - Different evidence types are required (access logs, IP records, download confirmations)

However, digital goods disputes ARE still winnable — PayPal's dispute resolution
process applies equally. The evidence requirements are just different.

This module encodes:
  1. The 4 digital goods dispute categories with their specific evidence patterns
  2. Access log analysis (key proof: buyer used the product after purchase)
  3. IP/geolocation matching (purchase IP vs. delivery IP vs. buyer's PayPal IP)
  4. Win rate by evidence completeness
  5. The "friendly fraud" detection pattern specific to digital goods

Digital goods fraud pattern: buyer purchases, downloads/uses immediately,
then files INR or SNAD claiming they never received it. Access logs showing
the product was downloaded or accessed are the decisive counter-evidence.

Key PayPal ruling criteria for digital goods (from known outcomes):
  - For INR: proof of delivery = download confirmation email + server access log
  - For SNAD: description match = screenshot of description at purchase time
  - For UNAUTHORIZED: proof of use = access log showing login from buyer's IP/device
  - For billing disputes: proof of active subscription + ToS agreement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DigitalClaimType(str, Enum):
    INR_DOWNLOAD       = "INR_DOWNLOAD"       # "I never received the download"
    INR_ACCESS         = "INR_ACCESS"          # "I never got access to the account/service"
    SNAD_DIGITAL       = "SNAD_DIGITAL"        # "The software/file doesn't match the description"
    UNAUTHORIZED       = "UNAUTHORIZED"        # "I didn't authorize this subscription/purchase"
    SUBSCRIPTION_STOP  = "SUBSCRIPTION_STOP"   # "I cancelled but was still charged"


@dataclass
class AccessLogSignal:
    has_download_log: bool = False          # Server log showing file was downloaded
    has_login_log: bool = False             # Login event after purchase
    download_timestamp_vs_dispute: str = ""  # "before" | "after" | "unknown"
    purchase_ip: Optional[str] = None      # IP at purchase
    access_ip: Optional[str] = None        # IP at download/login
    ips_match: Optional[bool] = None       # True if same IP or same geographic cluster
    device_fingerprint_match: bool = False  # Same device at purchase and access
    access_count: int = 0                  # How many times buyer accessed
    delivery_email_sent: bool = False      # Delivery confirmation email sent


@dataclass
class DigitalGoodsRubric:
    """Evidence rubric specific to digital goods disputes."""
    score: float                        # 0.0–1.0 rubric alignment
    win_probability: float              # Estimated win probability (no SP)
    critical_evidence: list[str]        # Must-have evidence for this claim
    supporting_evidence: list[str]      # Helpful additional evidence
    missing_critical: list[str]         # Critical evidence not yet gathered
    evidence_note: str                  # Optimized submission note
    friendly_fraud_detected: bool = False  # True if usage log contradicts INR claim
    reasoning: list[str] = field(default_factory=list)


# Base win rates for digital goods by claim type (no SP shield)
_DIGITAL_BASE_WIN_RATES = {
    DigitalClaimType.INR_DOWNLOAD:      0.55,  # Winnable with download logs
    DigitalClaimType.INR_ACCESS:        0.50,  # Winnable with access logs
    DigitalClaimType.SNAD_DIGITAL:      0.35,  # Harder — subjective
    DigitalClaimType.UNAUTHORIZED:      0.25,  # Hardest — no SP, card-issuer side
    DigitalClaimType.SUBSCRIPTION_STOP: 0.45,  # Winnable with ToS + no cancellation
}


def score_digital_rubric(
    claim_type: DigitalClaimType,
    *,
    access_log: Optional[AccessLogSignal] = None,
    has_delivery_email: bool = False,
    has_item_description_proof: bool = False,
    has_terms_of_service: bool = False,
    has_cancellation_policy: bool = False,
    has_no_cancellation_received: bool = False,
    has_buyer_communication: bool = False,
    has_active_usage_proof: bool = False,  # Buyer clearly used the product
    dispute_amount: float = 0.0,
) -> DigitalGoodsRubric:
    """
    Score digital goods dispute evidence against PayPal's grading criteria.

    Digital goods have no Seller Protection — every win is on evidence merits.
    The decisive evidence for digital goods is access logs showing usage, which
    directly contradicts "I never received it" claims.
    """
    reasoning = []
    al = access_log or AccessLogSignal()

    base_win_rate = _DIGITAL_BASE_WIN_RATES[claim_type]
    score = 0.0
    missing_critical = []

    if claim_type in (DigitalClaimType.INR_DOWNLOAD, DigitalClaimType.INR_ACCESS):
        critical = ["download_or_access_log", "delivery_email"]
        supporting = ["ip_match_proof", "buyer_communication", "item_description_proof"]

        # Download/access log is the decisive evidence
        if al.has_download_log or al.has_login_log:
            score += 0.45
            reasoning.append("Access log present: decisive counter to INR claim")
            if al.download_timestamp_vs_dispute == "before":
                score += 0.15
                reasoning.append("Download was BEFORE dispute filed: strong fraud signal")
        else:
            missing_critical.append("download_or_access_log")
            reasoning.append("No access log: weakest possible position for digital INR")

        if has_delivery_email:
            score += 0.20
            reasoning.append("Delivery email: confirms access link was sent")
        else:
            missing_critical.append("delivery_email")

        if al.ips_match is True:
            score += 0.10
            reasoning.append("IP match: purchase IP matches access IP")
        elif al.ips_match is False:
            reasoning.append("IP mismatch: different IP at purchase vs. access (note: VPN common)")

        if al.access_count > 1:
            score += 0.10
            reasoning.append(f"Multiple access events ({al.access_count}x): buyer used product repeatedly")

        if has_buyer_communication:
            score += 0.10
            reasoning.append("Buyer communication: confirms product receipt/awareness")

    elif claim_type == DigitalClaimType.SNAD_DIGITAL:
        critical = ["item_description_proof"]
        supporting = ["delivery_email", "buyer_communication", "access_log"]

        if has_item_description_proof:
            score += 0.45
            reasoning.append("Description proof: original listing vs. delivered content")
        else:
            missing_critical.append("item_description_proof")

        if has_delivery_email:
            score += 0.15

        if al.has_download_log:
            score += 0.15
            reasoning.append("Access log: buyer used product (weakens SNAD claim)")

        if has_buyer_communication:
            score += 0.15

        if has_active_usage_proof:
            score += 0.10
            reasoning.append("Active usage: buyer engaged with product (contradicts SNAD)")

    elif claim_type == DigitalClaimType.UNAUTHORIZED:
        critical = ["access_log_with_ip", "delivery_email"]
        supporting = ["device_fingerprint_match", "buyer_communication"]

        if al.has_login_log and al.purchase_ip:
            score += 0.40
            reasoning.append("Login log + purchase IP: buyer's device accessed product")
        elif al.has_login_log:
            score += 0.25
        else:
            missing_critical.append("access_log_with_ip")

        if al.ips_match is True or al.device_fingerprint_match:
            score += 0.20
            reasoning.append("IP/device match: same device at purchase and access")

        if has_delivery_email:
            score += 0.15
        else:
            missing_critical.append("delivery_email")

        if has_buyer_communication:
            score += 0.15

    elif claim_type == DigitalClaimType.SUBSCRIPTION_STOP:
        critical = ["terms_of_service", "no_cancellation_proof"]
        supporting = ["delivery_email", "active_usage_proof", "buyer_communication"]

        if has_terms_of_service:
            score += 0.35
            reasoning.append("ToS: buyer agreed to recurring billing terms")
        else:
            missing_critical.append("terms_of_service")

        if has_no_cancellation_received:
            score += 0.30
            reasoning.append("No cancellation: no cancellation request on record before charge")
        else:
            missing_critical.append("no_cancellation_proof")

        if has_active_usage_proof:
            score += 0.20
            reasoning.append("Active usage during billing period: buyer used service they're disputing")

        if has_buyer_communication:
            score += 0.10

        if has_cancellation_policy:
            score += 0.05

    # Cap score at 0.95
    score = min(score, 0.95)

    # Adjust win probability based on rubric score
    if score >= 0.75:
        win_prob = min(base_win_rate + 0.25, 0.85)
    elif score >= 0.50:
        win_prob = base_win_rate + 0.10
    elif score >= 0.25:
        win_prob = base_win_rate
    else:
        win_prob = max(base_win_rate - 0.20, 0.05)

    # Friendly fraud detection: buyer accessed product then filed INR
    friendly_fraud = (
        claim_type in (DigitalClaimType.INR_DOWNLOAD, DigitalClaimType.INR_ACCESS)
        and (al.has_download_log or al.has_login_log)
        and al.download_timestamp_vs_dispute == "before"
        and al.access_count >= 1
    )
    if friendly_fraud:
        win_prob = min(win_prob + 0.20, 0.90)
        reasoning.append(
            "FRIENDLY FRAUD DETECTED: buyer accessed product before filing INR claim"
        )

    # Build evidence note
    note = _build_digital_evidence_note(
        claim_type, al, has_delivery_email, has_item_description_proof,
        has_terms_of_service, has_no_cancellation_received, friendly_fraud
    )

    return DigitalGoodsRubric(
        score=round(score, 3),
        win_probability=round(win_prob, 3),
        critical_evidence=critical if 'critical' in dir() else [],
        supporting_evidence=supporting if 'supporting' in dir() else [],
        missing_critical=missing_critical,
        evidence_note=note,
        friendly_fraud_detected=friendly_fraud,
        reasoning=reasoning,
    )


def _build_digital_evidence_note(
    claim_type: DigitalClaimType,
    al: AccessLogSignal,
    has_delivery_email: bool,
    has_item_description: bool,
    has_tos: bool,
    has_no_cancellation: bool,
    friendly_fraud: bool,
) -> str:
    lines = []
    lines.append(
        "Note: This is a digital goods transaction. "
        "PayPal Seller Protection does not apply to digital items; "
        "however, the following evidence demonstrates fulfillment."
    )

    if friendly_fraud:
        lines.append(
            "SERVER LOGS SHOW THE BUYER ACCESSED THE PRODUCT BEFORE FILING THIS DISPUTE. "
            "Attached: access log with timestamp showing download/login occurred prior to "
            "the dispute filing date. This directly contradicts the claim."
        )
    elif al.has_download_log or al.has_login_log:
        lines.append(
            "Server access logs confirm the digital product was accessed by the buyer. "
            "Access logs are attached."
        )

    if has_delivery_email:
        lines.append(
            "A delivery confirmation email was sent to the buyer at the time of purchase. "
            "Copy attached."
        )

    if claim_type == DigitalClaimType.SNAD_DIGITAL and has_item_description:
        lines.append(
            "The original product listing is attached. "
            "The digital item delivered matches the description exactly."
        )

    if claim_type == DigitalClaimType.SUBSCRIPTION_STOP:
        if has_tos:
            lines.append(
                "The buyer agreed to our Terms of Service at signup, "
                "which include the recurring billing schedule. Terms attached."
            )
        if has_no_cancellation:
            lines.append(
                "No cancellation request was received prior to the billing date. "
                "Our cancellation log is attached."
            )

    return " ".join(lines)


if __name__ == "__main__":
    # Scenario: buyer downloads software, then files INR — classic friendly fraud
    access = AccessLogSignal(
        has_download_log=True,
        has_login_log=False,
        download_timestamp_vs_dispute="before",
        purchase_ip="203.0.113.10",
        access_ip="203.0.113.10",
        ips_match=True,
        access_count=3,
        delivery_email_sent=True,
    )
    result = score_digital_rubric(
        DigitalClaimType.INR_DOWNLOAD,
        access_log=access,
        has_delivery_email=True,
        dispute_amount=49.0,
    )
    print(f"Rubric score: {result.score:.0%}")
    print(f"Win probability: {result.win_probability:.0%}")
    print(f"Friendly fraud detected: {result.friendly_fraud_detected}")
    for r in result.reasoning:
        print(f"  → {r}")
    print(f"\nEvidence note:\n{result.evidence_note}")
