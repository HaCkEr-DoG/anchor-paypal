"""
Google Drive Evidence Vault — collect, organize, and package dispute evidence
stored in a merchant's Google Drive.

Sponsor: Google Drive (plugin:small-business:google-drive / mcp__800b84e4)

Evidence types auto-collected:
  - Invoices (PDF, Sheets, Docs matching order ID)
  - Shipping labels / tracking confirmations (images, PDFs)
  - Policy documents (terms of service, return policy)
  - Buyer communications (screenshots, email threads)
  - Product photos (for SNAD disputes)

Usage at runtime (when Drive MCP is connected):
    vault = DriveEvidenceVault(drive_mcp_client)
    package = await vault.build_evidence_package(dispute_id, order_id, reason_code)

This module also provides a pure dataclass layer for passing evidence
metadata through oracle modules (evidence_quality_scorer, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceType(str, Enum):
    INVOICE             = "INVOICE"
    SHIPPING_LABEL      = "SHIPPING_LABEL"
    TRACKING_SCREENSHOT = "TRACKING_SCREENSHOT"
    PROOF_OF_DELIVERY   = "PROOF_OF_DELIVERY"
    POLICY_DOCUMENT     = "POLICY_DOCUMENT"
    BUYER_COMMUNICATION = "BUYER_COMMUNICATION"
    PRODUCT_PHOTO       = "PRODUCT_PHOTO"
    TRANSACTION_RECORD  = "TRANSACTION_RECORD"
    FULFILLMENT_RECORD  = "FULFILLMENT_RECORD"
    UNKNOWN             = "UNKNOWN"


class EvidenceSource(str, Enum):
    GOOGLE_DRIVE = "GOOGLE_DRIVE"
    GMAIL        = "GMAIL"
    SHOPIFY      = "SHOPIFY"
    MANUAL       = "MANUAL"
    FIRECRAWL    = "FIRECRAWL"   # carrier tracking scraped by Firecrawl


@dataclass
class EvidenceFile:
    file_id: str
    file_name: str
    evidence_type: EvidenceType
    source: EvidenceSource
    mime_type: str
    size_bytes: int
    drive_url: Optional[str] = None
    relevance_score: float = 0.0    # 0-1 confidence this file is relevant
    description: str = ""


@dataclass
class EvidencePackage:
    dispute_id: str
    order_id: str
    reason_code: str

    files: list[EvidenceFile] = field(default_factory=list)
    missing_evidence_types: list[str] = field(default_factory=list)
    completeness_score: float = 0.0     # 0-100
    is_ready_to_submit: bool = False
    submission_notes: str = ""

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def has_tracking_evidence(self) -> bool:
        return any(f.evidence_type in (
            EvidenceType.TRACKING_SCREENSHOT,
            EvidenceType.PROOF_OF_DELIVERY,
            EvidenceType.SHIPPING_LABEL,
        ) for f in self.files)

    @property
    def has_policy_evidence(self) -> bool:
        return any(f.evidence_type == EvidenceType.POLICY_DOCUMENT for f in self.files)

    @property
    def has_communication_log(self) -> bool:
        return any(f.evidence_type == EvidenceType.BUYER_COMMUNICATION for f in self.files)

    @property
    def has_invoice(self) -> bool:
        return any(f.evidence_type == EvidenceType.INVOICE for f in self.files)


# Required evidence per reason code
_REQUIRED_EVIDENCE: dict[str, list[str]] = {
    "INR":        ["TRACKING_SCREENSHOT", "PROOF_OF_DELIVERY", "INVOICE"],
    "SNAD":       ["PRODUCT_PHOTO", "INVOICE", "POLICY_DOCUMENT"],
    "UNAUTHORIZED": ["TRANSACTION_RECORD", "INVOICE"],
    "SUBSCRIPTION": ["TRANSACTION_RECORD", "POLICY_DOCUMENT", "BUYER_COMMUNICATION"],
    "DEFAULT":    ["INVOICE", "TRANSACTION_RECORD"],
}


def _required_for(reason_code: str) -> list[str]:
    code = reason_code.upper()
    return _REQUIRED_EVIDENCE.get(code, _REQUIRED_EVIDENCE["DEFAULT"])


def score_evidence_package(pkg: EvidencePackage) -> EvidencePackage:
    """
    Score an evidence package's completeness against the required evidence
    for its reason code and update pkg.completeness_score and missing_evidence_types.
    """
    required = _required_for(pkg.reason_code)
    present_types = {f.evidence_type.value for f in pkg.files}
    missing = [r for r in required if r not in present_types]

    filled = len(required) - len(missing)
    score = round((filled / max(len(required), 1)) * 100.0, 1)

    pkg.missing_evidence_types = missing
    pkg.completeness_score = score
    pkg.is_ready_to_submit = score >= 80.0 and len(missing) == 0

    if pkg.is_ready_to_submit:
        pkg.submission_notes = f"Package complete ({pkg.file_count} files). Ready for PayPal submission."
    elif missing:
        pkg.submission_notes = (
            f"Package {score:.0f}% complete. Missing: {', '.join(missing)}. "
            "Collect from Drive or Shopify before submitting."
        )
    else:
        pkg.submission_notes = f"Package {score:.0f}% complete — submit when ready."

    return pkg


def build_search_queries(order_id: str, reason_code: str) -> list[str]:
    """
    Return Google Drive search queries to find relevant evidence files.
    Used by the Drive MCP integration at runtime.
    """
    base = [
        f"fullText contains '{order_id}'",
        f"name contains '{order_id}'",
    ]
    code = reason_code.upper()
    if code in ("INR", "ITEM_NOT_RECEIVED"):
        base += [
            "name contains 'tracking'",
            "name contains 'shipping'",
            "name contains 'delivery'",
        ]
    elif code in ("SNAD", "SIGNIFICANTLY_NOT_AS_DESCRIBED"):
        base += [
            "name contains 'invoice'",
            "name contains 'policy'",
            "name contains 'return'",
            "name contains 'product'",
        ]
    elif code in ("UNAUTHORIZED",):
        base += [
            "name contains 'receipt'",
            "name contains 'transaction'",
            "name contains 'authorization'",
        ]
    return base


def classify_drive_file(file_name: str, mime_type: str) -> EvidenceType:
    """Classify a Google Drive file into an EvidenceType based on name + MIME."""
    name = file_name.lower()
    if any(kw in name for kw in ("tracking", "shipment", "carrier", "usps", "fedex", "ups", "dhl")):
        return EvidenceType.TRACKING_SCREENSHOT
    if any(kw in name for kw in ("delivery", "delivered", "pod", "proof")):
        return EvidenceType.PROOF_OF_DELIVERY
    if any(kw in name for kw in ("invoice", "receipt", "order")):
        return EvidenceType.INVOICE
    if any(kw in name for kw in ("label", "shipping_label", "ship_label")):
        return EvidenceType.SHIPPING_LABEL
    if any(kw in name for kw in ("policy", "terms", "tos", "return")):
        return EvidenceType.POLICY_DOCUMENT
    if any(kw in name for kw in ("email", "chat", "communication", "message", "buyer")):
        return EvidenceType.BUYER_COMMUNICATION
    if any(kw in name for kw in ("photo", "image", "product", "item")):
        return EvidenceType.PRODUCT_PHOTO
    if any(kw in name for kw in ("transaction", "payment", "charge")):
        return EvidenceType.TRANSACTION_RECORD
    if "fulfillment" in name:
        return EvidenceType.FULFILLMENT_RECORD
    if mime_type.startswith("image/"):
        return EvidenceType.PRODUCT_PHOTO
    return EvidenceType.UNKNOWN
