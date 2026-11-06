"""
Adversarial Signal Detector — catches buyers gaming PayPal's dispute system.

PayPal's review AI is trainable: it has well-known patterns and edges that
sophisticated fraudsters exploit. This module detects those exploits so Anchor
can counter them before submitting evidence.

Key adversarial patterns detected:
  1. Deadline gaming       — filing disputes on day 7–9 (after return window closes)
  2. Policy-language cloning — dispute text uses exact PayPal policy phrases (copy-paste fraud)
  3. Burst filing          — buyer opens multiple disputes within hours of each other
  4. Velocity stacking     — multiple orders from same buyer/IP in short window before dispute
  5. Address mismatch      — shipping address ≠ billing country (reship fraud)
  6. Burner email signals  — disposable/throwaway email domain patterns
  7. Exact-amount targeting — disputing at common fraud amounts ($100, $200, $500, $750, $1000)
  8. Identical-text cloning — same dispute description used across multiple disputes
  9. VPN/proxy timing      — dispute filed outside buyer's normal timezone
 10. Partial-item pattern  — buyer claims item missing from multi-item order repeatedly

Each signal returns a numeric penalty (0.0–0.40) to subtract from Oracle win probability.
Positive signals (things that help the merchant) return a boost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


# ── Canonical fraud trigger amounts (common friendly-fraud targets) ───────────
FRAUD_TRIGGER_AMOUNTS = {100.0, 200.0, 250.0, 500.0, 750.0, 999.0, 1000.0}
_TRIGGER_PROXIMITY = 2.0  # Within $2 of a trigger amount = signal

# ── PayPal policy phrases that copy-paste fraudsters lift from help pages ─────
_PAYPAL_POLICY_PHRASES = [
    "item not as described",
    "significantly not as described",
    "item was not received",
    "item never arrived",
    "unauthorized transaction",
    "did not authorize",
    "i did not make this purchase",
    "as per paypal policy",
    "per paypal buyer protection",
    "paypal purchase protection",
    "requesting full refund under",
    "i am entitled to a refund",
]

# ── Known disposable email providers ─────────────────────────────────────────
_BURNER_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwaway.email", "yopmail.com", "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "guerrillamail.info", "spam4.me", "trashmail.com", "maildrop.cc",
    "getairmail.com", "fakeinbox.com", "dispostable.com", "mt2015.com",
    "discard.email", "tempr.email", "spamgourmet.com",
}


@dataclass
class AdversarialSignal:
    name: str
    description: str
    penalty: float          # negative = hurts merchant win prob; positive = helps
    severity: str           # "low" | "medium" | "high" | "critical"
    evidence_note: str = "" # what to include in PayPal response to counter this


@dataclass
class AdversarialReport:
    signals: list[AdversarialSignal] = field(default_factory=list)
    total_adjustment: float = 0.0   # sum of all penalties (can be positive = net boost)
    is_fraud_pattern: bool = False   # True if ≥2 high/critical signals
    counter_strategy: str = ""       # Recommended PayPal response strategy
    computed_at: str = ""

    def __post_init__(self):
        if not self.computed_at:
            self.computed_at = datetime.now(timezone.utc).isoformat()

    def add(self, signal: AdversarialSignal):
        self.signals.append(signal)
        self.total_adjustment += signal.penalty

    def finalize(self):
        high_sev = sum(1 for s in self.signals if s.severity in ("high", "critical"))
        self.is_fraud_pattern = high_sev >= 2
        penalties = [s for s in self.signals if s.penalty < 0]
        boosts = [s for s in self.signals if s.penalty > 0]
        if self.is_fraud_pattern:
            self.counter_strategy = (
                "Submit pattern-of-fraud evidence: include buyer's dispute history, "
                "delivery confirmation timestamps, and note the adversarial signals "
                "detected. Flag this for manual PayPal review escalation."
            )
        elif penalties:
            self.counter_strategy = (
                "Strengthen evidence package with: "
                + "; ".join(s.evidence_note for s in penalties if s.evidence_note)
            )
        else:
            self.counter_strategy = "Standard evidence submission — no adversarial patterns detected."
        return self


def analyze(
    dispute_text: str = "",
    dispute_amount: float = 0.0,
    buyer_email: str = "",
    dispute_filed_at: Optional[str] = None,
    purchase_date: Optional[str] = None,
    delivery_date: Optional[str] = None,
    shipping_country: str = "",
    billing_country: str = "",
    prior_dispute_texts: Optional[list[str]] = None,
    prior_disputes_last_7d: int = 0,
    buyer_timezone_offset_hours: Optional[float] = None,
    dispute_filed_hour_utc: Optional[int] = None,
    order_count_30d: int = 1,
) -> AdversarialReport:
    """
    Run all adversarial signal detectors and return a consolidated report.

    Parameters
    ----------
    dispute_text            : buyer's dispute description text
    dispute_amount          : dollar amount of the dispute
    buyer_email             : buyer's email address
    dispute_filed_at        : ISO timestamp when dispute was opened
    purchase_date           : ISO timestamp of original purchase
    delivery_date           : ISO timestamp of carrier-confirmed delivery
    shipping_country        : 2-letter country code where order was shipped
    billing_country         : 2-letter country code on buyer's payment
    prior_dispute_texts     : list of this buyer's previous dispute descriptions
    prior_disputes_last_7d  : how many disputes this buyer filed in last 7 days
    buyer_timezone_offset_hours : buyer's known timezone UTC offset
    dispute_filed_hour_utc  : hour (0–23) when dispute was filed (UTC)
    order_count_30d         : total orders from this buyer in last 30 days
    """
    report = AdversarialReport()

    # 1. Deadline gaming
    _check_deadline_gaming(report, dispute_filed_at, purchase_date)

    # 2. Policy-language cloning
    _check_policy_language(report, dispute_text)

    # 3. Burst filing
    _check_burst_filing(report, prior_disputes_last_7d)

    # 4. Address mismatch (reship fraud)
    _check_address_mismatch(report, shipping_country, billing_country)

    # 5. Burner email
    _check_burner_email(report, buyer_email)

    # 6. Exact-amount targeting
    _check_trigger_amount(report, dispute_amount)

    # 7. Identical-text cloning
    _check_text_clone(report, dispute_text, prior_dispute_texts or [])

    # 8. Post-delivery timing (friendly fraud window)
    _check_post_delivery_timing(report, dispute_filed_at, delivery_date)

    # 9. Timezone anomaly (VPN / scripted filing)
    _check_timezone_anomaly(report, buyer_timezone_offset_hours, dispute_filed_hour_utc)

    # 10. High-velocity ordering before dispute
    _check_order_velocity(report, order_count_30d)

    return report.finalize()


# ── Individual detector functions ─────────────────────────────────────────────

def _check_deadline_gaming(
    report: AdversarialReport,
    dispute_filed_at: Optional[str],
    purchase_date: Optional[str],
):
    """Disputes filed 7–9 days after purchase = after return windows close."""
    if not dispute_filed_at or not purchase_date:
        return
    try:
        filed = _parse_dt(dispute_filed_at)
        purchased = _parse_dt(purchase_date)
        days_after = (filed - purchased).days
        if 7 <= days_after <= 9:
            report.add(AdversarialSignal(
                name="deadline_gaming",
                description=f"Dispute filed {days_after}d after purchase — after most return windows close (day 7–9 pattern).",
                penalty=-0.08,
                severity="medium",
                evidence_note="show purchase timestamp and note dispute was filed after standard return window",
            ))
        elif days_after > 25:
            report.add(AdversarialSignal(
                name="late_filing",
                description=f"Dispute filed {days_after}d after purchase — unusually late.",
                penalty=-0.05,
                severity="low",
                evidence_note="include delivery confirmation to show long gap between receipt and dispute",
            ))
    except Exception:
        pass


def _check_policy_language(report: AdversarialReport, dispute_text: str):
    """Detect copy-paste PayPal policy phrases in buyer's dispute text."""
    if not dispute_text:
        return
    text_lower = dispute_text.lower()
    matched = [p for p in _PAYPAL_POLICY_PHRASES if p in text_lower]
    if len(matched) >= 3:
        report.add(AdversarialSignal(
            name="policy_language_clone",
            description=f"Dispute text contains {len(matched)} PayPal policy phrases — likely copy-pasted template.",
            penalty=-0.12,
            severity="high",
            evidence_note="note in response that buyer's claim uses verbatim PayPal policy language suggesting scripted dispute",
        ))
    elif len(matched) >= 1:
        report.add(AdversarialSignal(
            name="policy_language_partial",
            description=f"Dispute text contains {len(matched)} PayPal policy phrase(s).",
            penalty=-0.04,
            severity="low",
            evidence_note="highlight factual delivery evidence to contrast with buyer's generic claim",
        ))


def _check_burst_filing(report: AdversarialReport, prior_disputes_last_7d: int):
    """Multiple disputes filed within 7 days = coordinated fraud."""
    if prior_disputes_last_7d >= 5:
        report.add(AdversarialSignal(
            name="burst_filing_critical",
            description=f"Buyer filed {prior_disputes_last_7d} disputes in the last 7 days — burst fraud pattern.",
            penalty=-0.20,
            severity="critical",
            evidence_note="submit buyer's full dispute history as pattern-of-fraud evidence",
        ))
    elif prior_disputes_last_7d >= 3:
        report.add(AdversarialSignal(
            name="burst_filing_high",
            description=f"Buyer filed {prior_disputes_last_7d} disputes in the last 7 days.",
            penalty=-0.12,
            severity="high",
            evidence_note="include dispute history timeline showing abnormal frequency",
        ))
    elif prior_disputes_last_7d >= 2:
        report.add(AdversarialSignal(
            name="burst_filing_medium",
            description=f"Buyer filed {prior_disputes_last_7d} disputes in the last 7 days.",
            penalty=-0.06,
            severity="medium",
            evidence_note="note concurrent dispute activity in response",
        ))


def _check_address_mismatch(
    report: AdversarialReport,
    shipping_country: str,
    billing_country: str,
):
    """Shipping to different country than billing = reship fraud indicator."""
    if not shipping_country or not billing_country:
        return
    if shipping_country.upper() != billing_country.upper():
        report.add(AdversarialSignal(
            name="address_country_mismatch",
            description=f"Shipped to {shipping_country.upper()} but billing country is {billing_country.upper()} — reship fraud pattern.",
            penalty=-0.10,
            severity="high",
            evidence_note="include shipping address confirmation and note billing/shipping country mismatch in response",
        ))


def _check_burner_email(report: AdversarialReport, buyer_email: str):
    """Disposable/throwaway email = no long-term accountability."""
    if not buyer_email or "@" not in buyer_email:
        return
    domain = buyer_email.split("@")[-1].lower().strip()
    if domain in _BURNER_EMAIL_DOMAINS:
        report.add(AdversarialSignal(
            name="burner_email",
            description=f"Buyer email uses known disposable domain ({domain}).",
            penalty=-0.15,
            severity="high",
            evidence_note="note disposable email usage; request PayPal verify buyer identity",
        ))
    elif re.match(r"^[a-z0-9]{10,}@(gmail|yahoo|hotmail|outlook)\.com$", buyer_email.lower()):
        report.add(AdversarialSignal(
            name="random_string_email",
            description="Buyer email appears to be randomly generated (long alphanumeric string).",
            penalty=-0.06,
            severity="medium",
            evidence_note="note email pattern; include order IP geolocation if available",
        ))


def _check_trigger_amount(report: AdversarialReport, dispute_amount: float):
    """Common fraud amounts used to stay under review thresholds."""
    for trigger in FRAUD_TRIGGER_AMOUNTS:
        if abs(dispute_amount - trigger) <= _TRIGGER_PROXIMITY:
            report.add(AdversarialSignal(
                name="trigger_amount",
                description=f"Dispute amount ${dispute_amount:.2f} matches common fraud trigger amount (${trigger:.0f}).",
                penalty=-0.07,
                severity="medium",
                evidence_note="include itemized order receipt to prove actual value matches dispute amount",
            ))
            break  # only report once


def _check_text_clone(
    report: AdversarialReport,
    dispute_text: str,
    prior_dispute_texts: list[str],
):
    """Identical or near-identical text to prior disputes = automated fraud tool."""
    if not dispute_text or not prior_dispute_texts:
        return
    text_norm = _normalize_text(dispute_text)
    for prior in prior_dispute_texts:
        prior_norm = _normalize_text(prior)
        if not prior_norm:
            continue
        similarity = _text_similarity(text_norm, prior_norm)
        if similarity >= 0.90:
            report.add(AdversarialSignal(
                name="identical_text_clone",
                description=f"Dispute text is {similarity:.0%} identical to a prior dispute — automated fraud tool suspected.",
                penalty=-0.18,
                severity="critical",
                evidence_note="include side-by-side comparison of dispute texts to demonstrate templated fraud",
            ))
            return
        if similarity >= 0.70:
            report.add(AdversarialSignal(
                name="similar_text_clone",
                description=f"Dispute text is {similarity:.0%} similar to a prior dispute.",
                penalty=-0.08,
                severity="high",
                evidence_note="note repetitive dispute language pattern in response",
            ))
            return


def _check_post_delivery_timing(
    report: AdversarialReport,
    dispute_filed_at: Optional[str],
    delivery_date: Optional[str],
):
    """
    Positive signal: delivery confirmed before dispute = strong merchant position.
    Penalty signal: dispute filed very quickly after confirmed delivery.
    """
    if not dispute_filed_at or not delivery_date:
        return
    try:
        filed = _parse_dt(dispute_filed_at)
        delivered = _parse_dt(delivery_date)
        hours_after = (filed - delivered).total_seconds() / 3600

        if hours_after < 0:
            # Dispute filed before delivery — genuine INR (no adversarial signal)
            return
        if hours_after < 1:
            report.add(AdversarialSignal(
                name="instant_post_delivery_dispute",
                description=f"Dispute filed {hours_after:.1f}h after confirmed delivery — instant refund grab.",
                penalty=-0.25,
                severity="critical",
                evidence_note="lead with delivery timestamp as primary evidence; this timing is near-definitive friendly fraud",
            ))
        elif hours_after < 4:
            report.add(AdversarialSignal(
                name="rapid_post_delivery_dispute",
                description=f"Dispute filed {hours_after:.1f}h after confirmed delivery.",
                penalty=-0.18,
                severity="high",
                evidence_note="include carrier delivery timestamp and note rapid-fire dispute timing",
            ))
        elif hours_after < 24:
            report.add(AdversarialSignal(
                name="same_day_delivery_dispute",
                description=f"Dispute filed {hours_after:.1f}h after confirmed delivery (same day).",
                penalty=-0.10,
                severity="medium",
                evidence_note="include delivery timestamp showing item received before dispute",
            ))
        elif hours_after > 0:
            # Delivery confirmed before dispute = positive signal for merchant
            report.add(AdversarialSignal(
                name="delivery_before_dispute_boost",
                description=f"Delivery confirmed {hours_after/24:.1f} days before dispute — strong merchant evidence.",
                penalty=+0.08,
                severity="low",
                evidence_note="delivery confirmation is your strongest evidence",
            ))
    except Exception:
        pass


def _check_timezone_anomaly(
    report: AdversarialReport,
    buyer_tz_offset: Optional[float],
    filed_hour_utc: Optional[int],
):
    """
    Disputes filed at 3–5 AM local time are unusual for genuine buyers.
    Scripted fraud tools run 24/7 and don't respect business hours.
    """
    if buyer_tz_offset is None or filed_hour_utc is None:
        return
    local_hour = (filed_hour_utc + buyer_tz_offset) % 24
    if 2 <= local_hour <= 5:
        report.add(AdversarialSignal(
            name="off_hours_filing",
            description=f"Dispute filed at {local_hour:.0f}:00 local time — 2–5 AM filing consistent with scripted fraud tool.",
            penalty=-0.08,
            severity="medium",
            evidence_note="note unusual filing time in response; request IP verification from PayPal",
        ))


def _check_order_velocity(report: AdversarialReport, order_count_30d: int):
    """
    Legitimate buyers don't typically place 10+ orders then dispute everything.
    High velocity before dispute = inventory farming / resale fraud.
    """
    if order_count_30d >= 10:
        report.add(AdversarialSignal(
            name="high_velocity_ordering",
            description=f"Buyer placed {order_count_30d} orders in 30 days before disputing — inventory farming pattern.",
            penalty=-0.14,
            severity="high",
            evidence_note="submit full order history showing volume and dispute pattern",
        ))
    elif order_count_30d >= 5:
        report.add(AdversarialSignal(
            name="elevated_velocity_ordering",
            description=f"Buyer placed {order_count_30d} orders in 30 days before disputing.",
            penalty=-0.07,
            severity="medium",
            evidence_note="include order history in evidence package",
        ))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime:
    s = s.strip()
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _text_similarity(a: str, b: str) -> float:
    """
    Simple Jaccard similarity on word bigrams.
    Fast, no external dependencies, good enough for fraud detection.
    """
    def bigrams(text: str) -> set[str]:
        words = text.split()
        if len(words) < 2:
            return set(words)
        return {f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)}

    bg_a = bigrams(a)
    bg_b = bigrams(b)
    if not bg_a and not bg_b:
        return 1.0
    if not bg_a or not bg_b:
        return 0.0
    intersection = len(bg_a & bg_b)
    union = len(bg_a | bg_b)
    return intersection / union if union > 0 else 0.0
