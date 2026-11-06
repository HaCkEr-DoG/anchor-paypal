"""
Unauthorized Transaction Scorer — evaluate the strength of an UNAUTHORIZED
(fraud) dispute from the merchant's defense perspective.

Covers reason codes: 10.4 (Visa Unauthorized), 4807/4853/4855 (MC),
UNAUTHORIZED (PayPal), and similar codes indicating the buyer claims
they did not authorize the transaction.

Scores 5 dimensions:
  1. Delivery proof: IP/device match, shipping address match (0–25)
  2. Identity verification: 3DS, CVV, AVS results (0–25)
  3. Prior behavior: same device/account history, login evidence (0–20)
  4. CE 3.0 eligibility: 2+ prior non-disputed txns, Visa only (0–20)
  5. Chargeback pattern: first dispute vs. serial pattern (0–10)

Output: score 0–100, defensibility rating, win-probability adjustment

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AuthVerificationResult(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    NOT_RUN = "NOT_RUN"


class UnauthorizedDefensibility(str, Enum):
    STRONG       = "STRONG"       # Score ≥ 75
    ADEQUATE     = "ADEQUATE"     # Score 50–74
    WEAK         = "WEAK"         # Score 25–49
    INDEFENSIBLE = "INDEFENSIBLE" # Score < 25


@dataclass
class UnauthorizedTransactionInput:
    """Input for unauthorized transaction dispute scoring."""
    dispute_id: str
    transaction_amount: float
    network: str           # VISA, MC, PAYPAL, AMEX, DISCOVER
    reason_code: str

    # Authentication results
    three_d_secure_result: AuthVerificationResult = AuthVerificationResult.NOT_RUN
    cvv_result: AuthVerificationResult = AuthVerificationResult.NOT_RUN
    avs_result: AuthVerificationResult = AuthVerificationResult.NOT_RUN   # Address verification

    # Device / IP evidence
    ip_matches_prior_sessions: bool = False
    device_fingerprint_matches: bool = False
    shipped_to_account_address: bool = False
    billing_address_matches_avs: bool = False

    # Account behavior
    account_age_days: int = 0
    prior_purchases_on_account: int = 0
    login_before_purchase: bool = False
    login_from_known_device: bool = False

    # CE 3.0 eligibility (Visa only)
    ce3_eligible: bool = False   # 2+ prior non-disputed transactions on same card
    prior_non_disputed_txn_count: int = 0

    # Chargeback pattern
    prior_unauthorized_disputes: int = 0
    dispute_filed_within_30_days: bool = True


@dataclass
class ScoringDimensionUnauth:
    dimension: str
    score: int
    max_score: int
    rationale: str


@dataclass
class UnauthorizedTransactionReport:
    dispute_id: str
    transaction_amount: float
    network: str

    dimensions: list[ScoringDimensionUnauth]
    total_score: int
    defensibility: UnauthorizedDefensibility

    ce3_applicable: bool
    win_rate_adjustment: float

    strengths: list[str]
    weaknesses: list[str]
    required_evidence: list[str]

    recommendation: str
    summary: str


def score_unauthorized_transaction(inp: UnauthorizedTransactionInput) -> UnauthorizedTransactionReport:
    """Score unauthorized transaction dispute defensibility."""
    dimensions: list[ScoringDimensionUnauth] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    required_evidence: list[str] = []

    # ── Dimension 1: Delivery/address proof (0–25) ────────────────
    delivery_score = 0
    if inp.shipped_to_account_address:
        delivery_score += 12
        strengths.append("Shipped to account's registered address")
    if inp.ip_matches_prior_sessions:
        delivery_score += 7
        strengths.append("IP address matches prior sessions on account")
    if inp.device_fingerprint_matches:
        delivery_score += 6
        strengths.append("Device fingerprint matches prior account activity")
    if delivery_score == 0:
        weaknesses.append("No delivery or device match evidence")
        required_evidence.append("Provide IP address and shipping address match documentation")
    dimensions.append(ScoringDimensionUnauth(
        "Delivery & address proof", delivery_score, 25,
        f"shipped_to_addr={inp.shipped_to_account_address} ip_match={inp.ip_matches_prior_sessions}",
    ))

    # ── Dimension 2: Identity verification (0–25) ─────────────────
    auth_score = 0
    if inp.three_d_secure_result == AuthVerificationResult.PASS:
        auth_score += 15
        strengths.append("3D Secure authentication passed — liability shifts to issuer")
    elif inp.three_d_secure_result == AuthVerificationResult.NOT_RUN:
        weaknesses.append("3DS not run — no liability shift")
        required_evidence.append("Run 3D Secure on future high-value transactions")

    if inp.cvv_result == AuthVerificationResult.PASS:
        auth_score += 6
        strengths.append("CVV verification passed")
    elif inp.cvv_result == AuthVerificationResult.FAIL:
        weaknesses.append("CVV check failed — issuer may argue card data was stolen")

    if inp.avs_result == AuthVerificationResult.PASS:
        auth_score += 4
        strengths.append("AVS (address verification) passed")

    dimensions.append(ScoringDimensionUnauth(
        "Identity verification", auth_score, 25,
        f"3ds={inp.three_d_secure_result.value} cvv={inp.cvv_result.value} avs={inp.avs_result.value}",
    ))

    # ── Dimension 3: Prior behavior (0–20) ────────────────────────
    behavior_score = 0
    if inp.prior_purchases_on_account >= 3:
        behavior_score += 8
        strengths.append(f"Account has {inp.prior_purchases_on_account} prior purchases — established buyer")
    elif inp.prior_purchases_on_account >= 1:
        behavior_score += 4

    if inp.account_age_days >= 180:
        behavior_score += 5
        strengths.append(f"Account {inp.account_age_days} days old — not a fresh account")
    elif inp.account_age_days < 30:
        weaknesses.append("Account < 30 days old — higher fraud risk")

    if inp.login_before_purchase:
        behavior_score += 4
        strengths.append("Account was logged in before purchase")
    if inp.login_from_known_device:
        behavior_score += 3
        strengths.append("Login from known/previously-used device")

    dimensions.append(ScoringDimensionUnauth(
        "Prior account behavior", behavior_score, 20,
        f"prior_purchases={inp.prior_purchases_on_account} acct_age={inp.account_age_days}d",
    ))

    # ── Dimension 4: CE 3.0 (0–20, Visa only) ────────────────────
    ce3_applicable = (
        inp.network.upper() == "VISA"
        and inp.prior_non_disputed_txn_count >= 2
    )
    if ce3_applicable:
        ce3_score = 20
        strengths.append(
            f"CE 3.0 eligible: {inp.prior_non_disputed_txn_count} prior non-disputed Visa transactions"
        )
    elif inp.network.upper() == "VISA" and inp.prior_non_disputed_txn_count == 1:
        ce3_score = 8
        weaknesses.append("One prior non-disputed txn — need 2+ for full CE 3.0")
    elif inp.network.upper() != "VISA":
        ce3_score = 10   # Partial credit for non-Visa prior history
    else:
        ce3_score = 0
        weaknesses.append("No prior transactions — CE 3.0 not available")
        required_evidence.append("Document any prior transactions on same account/card")
    dimensions.append(ScoringDimensionUnauth(
        "CE 3.0 / prior txn history", ce3_score, 20,
        f"ce3={ce3_applicable} prior_txns={inp.prior_non_disputed_txn_count}",
    ))

    # ── Dimension 5: Chargeback pattern (0–10) ────────────────────
    if inp.prior_unauthorized_disputes == 0:
        pattern_score = 10
        strengths.append("No prior unauthorized disputes on this account")
    elif inp.prior_unauthorized_disputes <= 2:
        pattern_score = 5
        weaknesses.append(f"{inp.prior_unauthorized_disputes} prior unauthorized disputes on account")
    else:
        pattern_score = 0
        weaknesses.append(f"Serial disputer: {inp.prior_unauthorized_disputes} prior unauthorized disputes")
        required_evidence.append("Flag account for fraud team review")

    if not inp.dispute_filed_within_30_days:
        pattern_score = max(0, pattern_score - 3)
        strengths.append("Dispute filed > 30 days after transaction — late filing reduces legitimacy")

    dimensions.append(ScoringDimensionUnauth(
        "Chargeback pattern", pattern_score, 10,
        f"prior_unauth_disputes={inp.prior_unauthorized_disputes}",
    ))

    # ── Total and defensibility ───────────────────────────────────
    total = max(0, min(100, sum(d.score for d in dimensions)))
    if total >= 75:
        defensibility = UnauthorizedDefensibility.STRONG
    elif total >= 50:
        defensibility = UnauthorizedDefensibility.ADEQUATE
    elif total >= 25:
        defensibility = UnauthorizedDefensibility.WEAK
    else:
        defensibility = UnauthorizedDefensibility.INDEFENSIBLE

    win_adj_map = {
        UnauthorizedDefensibility.STRONG:       +0.12,
        UnauthorizedDefensibility.ADEQUATE:     +0.03,
        UnauthorizedDefensibility.WEAK:         -0.08,
        UnauthorizedDefensibility.INDEFENSIBLE: -0.18,
    }
    win_adj = win_adj_map[defensibility]
    if ce3_applicable:
        win_adj += 0.05   # CE 3.0 bonus

    if defensibility == UnauthorizedDefensibility.INDEFENSIBLE:
        rec = "Accept chargeback — insufficient evidence to contest unauthorized claim"
    elif defensibility == UnauthorizedDefensibility.WEAK:
        rec = "Consider accepting — gather stronger auth evidence for future disputes"
    elif defensibility == UnauthorizedDefensibility.ADEQUATE:
        rec = "Fight with available evidence; highlight any authentication that passed"
    else:
        rec = "File assertive response — lead with 3DS/CE3.0 evidence and account history"

    summary = (
        f"Unauthorized {inp.dispute_id}: "
        f"score={total}/100 ({defensibility.value}) | "
        f"ce3={ce3_applicable} | win_adj={win_adj:+.2f}"
    )

    return UnauthorizedTransactionReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        network=inp.network,
        dimensions=dimensions,
        total_score=total,
        defensibility=defensibility,
        ce3_applicable=ce3_applicable,
        win_rate_adjustment=win_adj,
        strengths=strengths,
        weaknesses=weaknesses,
        required_evidence=required_evidence,
        recommendation=rec,
        summary=summary,
    )
