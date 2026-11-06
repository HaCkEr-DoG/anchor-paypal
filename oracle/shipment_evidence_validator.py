"""
Shipment Evidence Validator — validate the quality and completeness of
shipping and delivery evidence before filing a dispute response.

Validates:
  - Tracking number format per carrier (UPS, USPS, FedEx, DHL)
  - Shipment date vs. transaction date (can't ship before purchase)
  - Delivery confirmation completeness
  - Carrier consistency (tracking number matches carrier code)
  - High-value signature requirements (≥$750)
  - Insurance documentation for high-value items
  - Address match (shipped to PayPal-confirmed address)

Output: a ValidationReport with pass/fail per check plus an overall
readiness score (0–100) indicating how strong the shipping evidence is.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import re


class Carrier(str, Enum):
    UPS      = "UPS"
    USPS     = "USPS"
    FEDEX    = "FEDEX"
    DHL      = "DHL"
    AMAZON   = "AMAZON"
    UNKNOWN  = "UNKNOWN"


class CheckStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"


class EvidenceReadiness(str, Enum):
    STRONG      = "STRONG"       # Score ≥ 80
    ADEQUATE    = "ADEQUATE"     # Score 60–79
    WEAK        = "WEAK"         # Score 40–59
    INSUFFICIENT = "INSUFFICIENT" # Score < 40


# ── Carrier tracking number patterns (simplified) ────────────────
_TRACKING_PATTERNS: dict[Carrier, list[str]] = {
    Carrier.UPS:   [r"^1Z[A-Z0-9]{16}$", r"^\d{18}$"],
    Carrier.USPS:  [r"^\d{20,22}$", r"^[A-Z]{2}\d{9}US$", r"^9[0-9]{15,21}$"],
    Carrier.FEDEX: [r"^\d{12}$", r"^\d{15}$", r"^\d{20}$"],
    Carrier.DHL:   [r"^\d{10}$", r"^[A-Z]{2}\d{9}[A-Z]{2}$"],
    Carrier.AMAZON: [r"^TBA\d{12}$", r"^TBA[0-9]{12}US$"],
}


def _validate_tracking_format(tracking: str, carrier: Carrier) -> bool:
    """Return True if tracking number matches carrier pattern."""
    patterns = _TRACKING_PATTERNS.get(carrier, [])
    if not patterns:
        return len(tracking) >= 8   # Unknown carrier — basic sanity check
    return any(re.match(p, tracking.upper().replace(" ", "")) for p in patterns)


@dataclass
class ShipmentEvidenceInput:
    """Input for shipment evidence validation."""
    dispute_id: str
    transaction_amount: float

    # Tracking info
    tracking_number: str = ""
    carrier: Carrier = Carrier.UNKNOWN
    ship_date_epoch: Optional[int] = None
    transaction_date_epoch: Optional[int] = None
    estimated_delivery_epoch: Optional[int] = None
    actual_delivery_epoch: Optional[int] = None

    # Address info
    shipped_to_address: str = ""
    paypal_confirmed_address: str = ""
    buyer_provided_address_match: bool = False

    # Confirmation types
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_insurance_documentation: bool = False
    has_commercial_invoice: bool = False

    # Proof files
    has_shipment_label: bool = False
    has_carrier_receipt: bool = False
    has_photo_proof_of_delivery: bool = False


@dataclass
class ValidationCheck:
    """Result of a single validation check."""
    name: str
    status: CheckStatus
    description: str
    score_impact: int    # Points added/removed from readiness score


@dataclass
class ShipmentEvidenceReport:
    """Full validation report for shipping evidence."""
    dispute_id: str
    transaction_amount: float

    checks: list[ValidationCheck]
    readiness_score: int      # 0–100
    readiness: EvidenceReadiness

    critical_failures: list[str]
    warnings: list[str]
    passing_checks: int
    failing_checks: int

    can_file_with_current_evidence: bool
    missing_to_strengthen: list[str]

    summary: str


def validate_shipment_evidence(inp: ShipmentEvidenceInput) -> ShipmentEvidenceReport:
    """Validate shipping and delivery evidence for dispute filing."""
    checks: list[ValidationCheck] = []
    critical_failures: list[str] = []
    warnings: list[str] = []
    missing_to_strengthen: list[str] = []

    # ── Check 1: Tracking number present ─────────────────────────
    if inp.tracking_number:
        checks.append(ValidationCheck(
            "Tracking number present", CheckStatus.PASS,
            f"Tracking: {inp.tracking_number}", +15,
        ))
    else:
        checks.append(ValidationCheck(
            "Tracking number present", CheckStatus.FAIL,
            "No tracking number provided — required for INR disputes", -20,
        ))
        critical_failures.append("Missing tracking number")
        missing_to_strengthen.append("Add tracking number")

    # ── Check 2: Tracking number format ──────────────────────────
    if inp.tracking_number:
        if _validate_tracking_format(inp.tracking_number, inp.carrier):
            checks.append(ValidationCheck(
                "Tracking number format valid", CheckStatus.PASS,
                f"Format matches {inp.carrier.value} pattern", +10,
            ))
        else:
            checks.append(ValidationCheck(
                "Tracking number format valid", CheckStatus.WARNING,
                f"Tracking {inp.tracking_number!r} does not match known {inp.carrier.value} pattern",
                -5,
            ))
            warnings.append(f"Tracking number format may not match {inp.carrier.value}")
    else:
        checks.append(ValidationCheck(
            "Tracking number format valid", CheckStatus.SKIPPED,
            "No tracking number to validate", 0,
        ))

    # ── Check 3: Ship date vs. transaction date ───────────────────
    if inp.ship_date_epoch and inp.transaction_date_epoch:
        if inp.ship_date_epoch >= inp.transaction_date_epoch:
            checks.append(ValidationCheck(
                "Ship date after transaction date", CheckStatus.PASS,
                "Item shipped on or after purchase — consistent timeline", +10,
            ))
        else:
            checks.append(ValidationCheck(
                "Ship date after transaction date", CheckStatus.FAIL,
                "Ship date is before transaction date — impossible timeline", -25,
            ))
            critical_failures.append("Ship date precedes transaction date — invalid evidence")
    else:
        checks.append(ValidationCheck(
            "Ship date after transaction date", CheckStatus.SKIPPED,
            "Dates not provided", 0,
        ))

    # ── Check 4: Delivery confirmed ───────────────────────────────
    if inp.has_delivery_confirmation:
        checks.append(ValidationCheck(
            "Delivery confirmation present", CheckStatus.PASS,
            "Carrier delivery confirmation available", +15,
        ))
    else:
        checks.append(ValidationCheck(
            "Delivery confirmation present", CheckStatus.WARNING,
            "No delivery confirmation — tracking scan may be insufficient", -5,
        ))
        warnings.append("Delivery confirmation not provided")
        missing_to_strengthen.append("Obtain carrier delivery confirmation")

    # ── Check 5: Signature for high-value ─────────────────────────
    if inp.transaction_amount >= 750:
        if inp.has_signature_confirmation:
            checks.append(ValidationCheck(
                "Signature confirmation (≥$750)", CheckStatus.PASS,
                "Signature obtained — required for high-value shipments", +15,
            ))
        else:
            checks.append(ValidationCheck(
                "Signature confirmation (≥$750)", CheckStatus.FAIL,
                "Signature required for transactions ≥$750 (Seller Protection requirement)",
                -20,
            ))
            critical_failures.append("Missing signature confirmation for high-value item")
            missing_to_strengthen.append("Obtain signature confirmation record")
    else:
        checks.append(ValidationCheck(
            "Signature confirmation (≥$750)", CheckStatus.SKIPPED,
            f"Transaction ${inp.transaction_amount:.2f} below $750 threshold", 0,
        ))

    # ── Check 6: Address match ────────────────────────────────────
    if inp.buyer_provided_address_match:
        checks.append(ValidationCheck(
            "Shipped to PayPal-confirmed address", CheckStatus.PASS,
            "Shipping address matches PayPal-confirmed buyer address", +15,
        ))
    elif inp.shipped_to_address and inp.paypal_confirmed_address:
        # Simple substring match
        if (inp.shipped_to_address.lower()[:20] in inp.paypal_confirmed_address.lower()
                or inp.paypal_confirmed_address.lower()[:20] in inp.shipped_to_address.lower()):
            checks.append(ValidationCheck(
                "Shipped to PayPal-confirmed address", CheckStatus.PASS,
                "Shipping address appears to match PayPal address", +15,
            ))
        else:
            checks.append(ValidationCheck(
                "Shipped to PayPal-confirmed address", CheckStatus.FAIL,
                "Shipping address does not match PayPal-confirmed address", -20,
            ))
            critical_failures.append("Address mismatch — Seller Protection requires confirmed address")
    else:
        checks.append(ValidationCheck(
            "Shipped to PayPal-confirmed address", CheckStatus.WARNING,
            "Could not verify address match — confirm manually", -5,
        ))
        warnings.append("Address match not confirmed")

    # ── Check 7: Shipment label ───────────────────────────────────
    if inp.has_shipment_label or inp.has_carrier_receipt:
        checks.append(ValidationCheck(
            "Shipment proof document", CheckStatus.PASS,
            "Shipping label or carrier receipt available", +10,
        ))
    else:
        checks.append(ValidationCheck(
            "Shipment proof document", CheckStatus.WARNING,
            "No shipment label or carrier receipt", -5,
        ))
        missing_to_strengthen.append("Add shipping label or carrier receipt")

    # ── Check 8: Insurance (for high-value) ──────────────────────
    if inp.transaction_amount >= 200 and not inp.has_insurance_documentation:
        checks.append(ValidationCheck(
            "Insurance documentation", CheckStatus.WARNING,
            f"No insurance documentation for ${inp.transaction_amount:.0f} shipment", -3,
        ))
        warnings.append("Insurance documentation missing for significant shipment")
    else:
        checks.append(ValidationCheck(
            "Insurance documentation", CheckStatus.PASS if inp.has_insurance_documentation
            else CheckStatus.SKIPPED,
            "Insurance documentation present" if inp.has_insurance_documentation
            else "Below threshold — insurance optional", +5 if inp.has_insurance_documentation else 0,
        ))

    # ── Scoring ───────────────────────────────────────────────────
    base_score = 50
    score = base_score + sum(c.score_impact for c in checks)
    score = max(0, min(100, score))

    if score >= 80:
        readiness = EvidenceReadiness.STRONG
    elif score >= 60:
        readiness = EvidenceReadiness.ADEQUATE
    elif score >= 40:
        readiness = EvidenceReadiness.WEAK
    else:
        readiness = EvidenceReadiness.INSUFFICIENT

    passing = sum(1 for c in checks if c.status == CheckStatus.PASS)
    failing = sum(1 for c in checks if c.status == CheckStatus.FAIL)

    can_file = len(critical_failures) == 0

    summary = (
        f"Shipment {inp.dispute_id}: "
        f"score={score}/100 ({readiness.value}) | "
        f"pass={passing} fail={failing} | can_file={can_file}"
    )

    return ShipmentEvidenceReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        checks=checks,
        readiness_score=score,
        readiness=readiness,
        critical_failures=critical_failures,
        warnings=warnings,
        passing_checks=passing,
        failing_checks=failing,
        can_file_with_current_evidence=can_file,
        missing_to_strengthen=missing_to_strengthen,
        summary=summary,
    )
