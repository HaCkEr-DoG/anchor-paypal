"""
Network Rule Checker — validate a dispute response against card network
rules to ensure it meets technical requirements before filing.

Checks:
  1. Chargeback reason code validity (is this code valid for this network?)
  2. Required evidence fields for the specific reason code
  3. Amount limits and partial dispute rules
  4. Currency matching (transaction currency must match dispute currency)
  5. Time-bar rules (too old to dispute? Too old to fight?)
  6. Second chargeback / pre-arbitration rules
  7. Jurisdiction-specific rules (EU strong customer authentication, etc.)

Supported reason code families:
  PayPal: INR, SNAD, UNAUTHORIZED, SUBSCRIPTION
  Visa:   10.4 (unauthorized), 13.1 (merchandise not received),
          13.3 (not as described), 12.6 (duplicate), 12.7 (invalid account)
  MC:     4853 (not as described / not received), 4863 (unauthorized),
          4834 (duplicate), 4807 (warnings)
  Amex:   C02 (credit not processed), C08 (not received), C14 (cancelled),
          C31 (not as described), FR2 (fraud), FR6 (partial immediacy)
  Discover: AP (authorization processing), NF (non-fulfillment),
            RG (non-receipt of goods), RM (quality disputes)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CheckStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    NA      = "NA"


class NetworkRuleViolation(str, Enum):
    INVALID_REASON_CODE        = "INVALID_REASON_CODE"
    MISSING_REQUIRED_EVIDENCE  = "MISSING_REQUIRED_EVIDENCE"
    AMOUNT_MISMATCH            = "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH          = "CURRENCY_MISMATCH"
    TIME_BARRED                = "TIME_BARRED"
    DUPLICATE_DISPUTE          = "DUPLICATE_DISPUTE"
    EXCEEDS_TRANSACTION_AMOUNT = "EXCEEDS_TRANSACTION_AMOUNT"
    MISSING_AUTHORIZATION      = "MISSING_AUTHORIZATION"
    INVALID_NETWORK            = "INVALID_NETWORK"


# Reason code -> required evidence fields
_REQUIRED_EVIDENCE: dict[str, list[str]] = {
    # PayPal
    "INR":          ["tracking_number", "shipment_date"],
    "SNAD":         ["product_listing", "return_policy"],
    "UNAUTHORIZED": ["ip_log", "order_history"],
    "SUBSCRIPTION": ["terms_disclosure", "cancellation_policy"],
    # Visa
    "10.4":         ["ip_log", "order_history", "device_id"],
    "13.1":         ["tracking_number", "proof_of_delivery"],
    "13.3":         ["product_listing", "photos"],
    "12.6":         ["proof_only_one_charge", "order_id"],
    "12.7":         ["valid_account_number_proof"],
    # Mastercard
    "4853":         ["product_listing", "proof_of_delivery"],
    "4863":         ["ip_log", "account_history"],
    "4834":         ["proof_only_one_charge"],
    "4807":         [],   # Warning bulletins — no specific evidence
    # Amex
    "C02":          ["proof_refund_issued"],
    "C08":          ["tracking_number", "proof_of_delivery"],
    "C14":          ["cancellation_policy", "terms"],
    "C31":          ["product_listing"],
    "FR2":          ["ip_log", "device_id"],
    "FR6":          ["partial_fulfillment_proof"],
    # Discover
    "AP":           ["valid_authorization"],
    "NF":           ["tracking_number"],
    "RG":           ["proof_of_delivery"],
    "RM":           ["product_listing"],
}

# Valid reason codes per network
_VALID_CODES: dict[str, set[str]] = {
    "PAYPAL":      {"INR", "SNAD", "UNAUTHORIZED", "SUBSCRIPTION"},
    "VISA":        {"10.4", "13.1", "13.3", "12.6", "12.7"},
    "MASTERCARD":  {"4853", "4863", "4834", "4807"},
    "AMEX":        {"C02", "C08", "C14", "C31", "FR2", "FR6"},
    "DISCOVER":    {"AP", "NF", "RG", "RM"},
}

# Max dispute age in days (time-bar)
_TIME_BAR_DAYS: dict[str, int] = {
    "PAYPAL":     180,
    "VISA":       120,
    "MASTERCARD": 120,
    "AMEX":       180,
    "DISCOVER":   120,
}

# Max response age (how old can the dispute be when we file our response)
_RESPONSE_BAR_DAYS: dict[str, int] = {
    "PAYPAL":     10,
    "VISA":       20,
    "MASTERCARD": 20,
    "AMEX":       20,
    "DISCOVER":   20,
}


@dataclass
class RuleCheckResult:
    rule: str
    status: CheckStatus
    detail: str
    violation: Optional[NetworkRuleViolation] = None


@dataclass
class NetworkRuleInput:
    """Input for network rule validation."""
    dispute_id: str
    network: str
    reason_code: str
    transaction_amount: float
    disputed_amount: float
    transaction_currency: str
    dispute_currency: str

    # Timeline
    transaction_date_epoch: int
    dispute_filed_epoch: int
    response_to_be_filed_epoch: int   # When we plan to file our response

    # Evidence available
    evidence_fields_present: list[str] = field(default_factory=list)

    # Flags
    is_second_chargeback: bool = False
    has_prior_dispute_on_same_txn: bool = False
    is_eu_transaction: bool = False
    has_sca_authentication: bool = False    # Strong Customer Authentication


_DAY_SECS = 86400


def _check_reason_code(inp: NetworkRuleInput) -> RuleCheckResult:
    valid = _VALID_CODES.get(inp.network.upper(), set())
    if inp.reason_code in valid:
        return RuleCheckResult("Reason code validity", CheckStatus.PASS,
                                f"Code {inp.reason_code} is valid for {inp.network}")
    return RuleCheckResult("Reason code validity", CheckStatus.FAIL,
                            f"Code {inp.reason_code} is not valid for {inp.network}",
                            NetworkRuleViolation.INVALID_REASON_CODE)


def _check_required_evidence(inp: NetworkRuleInput) -> list[RuleCheckResult]:
    required = _REQUIRED_EVIDENCE.get(inp.reason_code, [])
    present  = set(inp.evidence_fields_present)
    results  = []
    for field_name in required:
        if field_name in present:
            results.append(RuleCheckResult(
                f"Evidence: {field_name}", CheckStatus.PASS,
                f"Required field '{field_name}' present"))
        else:
            results.append(RuleCheckResult(
                f"Evidence: {field_name}", CheckStatus.FAIL,
                f"Required field '{field_name}' missing for {inp.reason_code}",
                NetworkRuleViolation.MISSING_REQUIRED_EVIDENCE))
    return results


def _check_amount(inp: NetworkRuleInput) -> list[RuleCheckResult]:
    results = []
    if inp.disputed_amount > inp.transaction_amount + 0.01:
        results.append(RuleCheckResult(
            "Disputed amount", CheckStatus.FAIL,
            f"Disputed ${inp.disputed_amount:.2f} exceeds transaction ${inp.transaction_amount:.2f}",
            NetworkRuleViolation.EXCEEDS_TRANSACTION_AMOUNT))
    else:
        results.append(RuleCheckResult(
            "Disputed amount", CheckStatus.PASS,
            f"Disputed amount ${inp.disputed_amount:.2f} within transaction"))
    return results


def _check_currency(inp: NetworkRuleInput) -> RuleCheckResult:
    if inp.transaction_currency.upper() == inp.dispute_currency.upper():
        return RuleCheckResult("Currency match", CheckStatus.PASS,
                                f"Currency {inp.transaction_currency} matches")
    return RuleCheckResult("Currency match", CheckStatus.WARNING,
                            f"Transaction {inp.transaction_currency} ≠ dispute {inp.dispute_currency}",
                            NetworkRuleViolation.CURRENCY_MISMATCH)


def _check_time_bar(inp: NetworkRuleInput) -> RuleCheckResult:
    limit = _TIME_BAR_DAYS.get(inp.network.upper(), 120)
    age_days = (inp.dispute_filed_epoch - inp.transaction_date_epoch) // _DAY_SECS
    if age_days > limit:
        return RuleCheckResult("Time-bar", CheckStatus.FAIL,
                                f"Dispute filed {age_days}d after transaction (limit {limit}d)",
                                NetworkRuleViolation.TIME_BARRED)
    return RuleCheckResult("Time-bar", CheckStatus.PASS,
                            f"Dispute within {limit}d time limit ({age_days}d old)")


def _check_response_deadline(inp: NetworkRuleInput) -> RuleCheckResult:
    limit = _RESPONSE_BAR_DAYS.get(inp.network.upper(), 20)
    age_days = (inp.response_to_be_filed_epoch - inp.dispute_filed_epoch) // _DAY_SECS
    if age_days > limit:
        return RuleCheckResult("Response deadline", CheckStatus.FAIL,
                                f"Response filed {age_days}d after dispute (limit {limit}d)",
                                NetworkRuleViolation.TIME_BARRED)
    return RuleCheckResult("Response deadline", CheckStatus.PASS,
                            f"Response within deadline ({age_days}d / {limit}d)")


def _check_duplicate(inp: NetworkRuleInput) -> RuleCheckResult:
    if inp.has_prior_dispute_on_same_txn:
        return RuleCheckResult("Duplicate dispute", CheckStatus.WARNING,
                                "Prior dispute exists on this transaction — may be second chargeback",
                                NetworkRuleViolation.DUPLICATE_DISPUTE)
    return RuleCheckResult("Duplicate dispute", CheckStatus.PASS,
                            "No prior dispute on this transaction")


def _check_sca(inp: NetworkRuleInput) -> Optional[RuleCheckResult]:
    if not inp.is_eu_transaction:
        return None
    if inp.reason_code in ("10.4", "UNAUTHORIZED", "FR2", "4863"):
        if inp.has_sca_authentication:
            return RuleCheckResult("EU SCA", CheckStatus.PASS,
                                    "SCA authentication present — liability shifts to issuer")
        return RuleCheckResult("EU SCA", CheckStatus.WARNING,
                                "EU unauthorized dispute without SCA — may face liability")
    return None


@dataclass
class NetworkRuleReport:
    """Full network rule validation report."""
    dispute_id: str
    network: str
    reason_code: str

    all_checks: list[RuleCheckResult]
    failed_checks: list[RuleCheckResult]
    warning_checks: list[RuleCheckResult]

    can_file: bool           # True only if no FAIL results
    missing_evidence: list[str]
    violations: list[str]
    summary: str


def check_network_rules(inp: NetworkRuleInput) -> NetworkRuleReport:
    """Run all network rule checks on a proposed dispute response."""
    checks: list[RuleCheckResult] = []

    checks.append(_check_reason_code(inp))
    checks.extend(_check_required_evidence(inp))
    checks.extend(_check_amount(inp))
    checks.append(_check_currency(inp))
    checks.append(_check_time_bar(inp))
    checks.append(_check_response_deadline(inp))
    checks.append(_check_duplicate(inp))

    sca = _check_sca(inp)
    if sca:
        checks.append(sca)

    failed   = [c for c in checks if c.status == CheckStatus.FAIL]
    warnings = [c for c in checks if c.status == CheckStatus.WARNING]

    missing = [c.rule.replace("Evidence: ", "") for c in failed
               if c.violation == NetworkRuleViolation.MISSING_REQUIRED_EVIDENCE]
    violations = [c.violation.value for c in failed if c.violation]

    can_file = len(failed) == 0

    summary = (
        f"Rules {inp.dispute_id}: "
        f"can_file={can_file} | fails={len(failed)} | "
        f"warnings={len(warnings)} | code={inp.reason_code}"
    )

    return NetworkRuleReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        reason_code=inp.reason_code,
        all_checks=checks,
        failed_checks=failed,
        warning_checks=warnings,
        can_file=can_file,
        missing_evidence=missing,
        violations=violations,
        summary=summary,
    )
