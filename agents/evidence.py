"""
Evidence Assembler Agent

This is the component that requires real engineering — not a chatbot suggestion.

The PayPal Agent Toolkit DOES NOT have a provide-evidence tool.
We call POST /v1/customer/disputes/{dispute_id}/provide-evidence directly.

PayPal's evidence endpoint accepts multipart form data with specific evidence_type
values. We automate the gathering of each type using KERNEL browser automation
for sources that can't be reached via API (email threads, shipping carrier sites).
"""

import httpx
import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from enum import Enum


class EvidenceType(str, Enum):
    # PayPal's accepted evidence_type values
    PROOF_OF_FULFILLMENT = "PROOF_OF_FULFILLMENT"           # tracking + delivery
    PROOF_OF_REFUND = "PROOF_OF_REFUND_OR_REPLACEMENT"
    PROOF_OF_DELIVERY_SIGNATURE = "PROOF_OF_DELIVERY_SIGNATURE"
    PROOF_OF_SHIPMENT = "PROOF_OF_SHIPMENT"
    ITEM_DESCRIPTION = "ITEM_DESCRIPTION"
    COMPANY_INFORMATION = "COMPANY_INFORMATION"
    PROOF_OF_PURCHASE = "PROOF_OF_PURCHASE_PAYMENT_TRACE"
    SCREENSHOTS = "SCREENSHOTS"                              # buyer communication
    CANCEL_SUBSCRIPTION = "CANCEL_SUBSCRIPTION"


@dataclass
class EvidenceFile:
    evidence_type: EvidenceType
    file_path: Path
    notes: str = ""         # free-text context sent with the evidence
    source: str = ""        # where this file came from (for audit trail)


@dataclass
class EvidencePackage:
    dispute_id: str
    files: list[EvidenceFile] = field(default_factory=list)
    notes: str = ""         # top-level notes for the whole package

    def add(self, f: EvidenceFile):
        self.files.append(f)

    def is_sufficient(self) -> bool:
        """Minimum viable evidence package to submit."""
        types_present = {f.evidence_type for f in self.files}
        return bool(types_present)


class EvidenceAssembler:
    """
    Orchestrates evidence gathering from multiple sources and submits
    to PayPal's disputes API. Uses KERNEL for sources that require
    browser automation (carrier sites, email threads).
    """

    def __init__(self, access_token: str, sandbox: bool = True):
        self.access_token = access_token
        self.base_url = (
            "https://api.sandbox.paypal.com"
            if sandbox
            else "https://api.paypal.com"
        )
        self.client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def gather_tracking_evidence(
        self, tracking_number: str, carrier: str, tmp_dir: Path
    ) -> Optional[EvidenceFile]:
        """
        Uses KERNEL to screenshot the carrier's delivery confirmation page.
        This is evidence PayPal accepts but that can't be gathered via API alone.

        Carriers: USPS, UPS, FedEx, DHL
        """
        carrier_urls = {
            "USPS": f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
            "UPS": f"https://www.ups.com/track?tracknum={tracking_number}",
            "FEDEX": f"https://www.fedex.com/apps/fedextrack/?tracknumbers={tracking_number}",
            "DHL": f"https://www.dhl.com/en/express/tracking.html?AWB={tracking_number}",
        }

        url = carrier_urls.get(carrier.upper())
        if not url:
            return None

        # KERNEL call: spin up a browser, navigate, screenshot delivery confirmation
        # KERNEL spins up in <30ms and is bot-resistant — carrier sites block regular scrapers
        screenshot_path = tmp_dir / f"delivery_{tracking_number}.png"

        kernel_script = f"""
import asyncio
from kernel import Browser

async def capture():
    async with Browser() as b:
        page = await b.new_page()
        await page.goto("{url}", wait_until="networkidle")
        # Wait for tracking result to load
        await page.wait_for_selector(".tracking-result, .status, .delivery-status", timeout=10000)
        await page.screenshot(path="{screenshot_path}", full_page=True)
        return str("{screenshot_path}")

asyncio.run(capture())
"""
        # In production: subprocess.run(["kernel", "exec", kernel_script])
        # For now, write the script for manual/automated execution
        script_path = tmp_dir / "kernel_tracking.py"
        script_path.write_text(kernel_script)

        if screenshot_path.exists():
            return EvidenceFile(
                evidence_type=EvidenceType.PROOF_OF_FULFILLMENT,
                file_path=screenshot_path,
                notes=f"Delivery confirmation from {carrier} for tracking #{tracking_number}",
                source=f"KERNEL browser automation → {url}",
            )
        return None

    async def gather_communication_evidence(
        self, buyer_email: str, tmp_dir: Path
    ) -> list[EvidenceFile]:
        """
        Screenshots of buyer communication (email threads, messages).
        Critical for SNAD disputes where you need to show buyer accepted the item,
        or for unauthorized disputes where buyer themselves messaged you.

        In practice: integrates with Gmail/Outlook via Zapier MCP or direct OAuth
        to export the thread as PDF.
        """
        files = []
        # Export email thread as PDF via Gmail API
        # This is where Zapier MCP is genuinely useful:
        # search gmail for emails from buyer_email → export to PDF
        # zapier action: Gmail → Find Email → Export as PDF
        thread_pdf = tmp_dir / f"thread_{buyer_email.split('@')[0]}.pdf"
        if thread_pdf.exists():
            files.append(EvidenceFile(
                evidence_type=EvidenceType.SCREENSHOTS,
                file_path=thread_pdf,
                notes=f"Email communication history with buyer {buyer_email}",
                source="Gmail export via Zapier MCP",
            ))
        return files

    async def submit(self, package: EvidencePackage) -> dict:
        """
        Submits evidence package to PayPal disputes API.

        POST /v1/customer/disputes/{dispute_id}/provide-evidence
        Content-Type: multipart/form-data

        This is the HARD part. PayPal's provide-evidence endpoint requires:
        1. A JSON "input" part with evidence metadata
        2. One or more file parts named "file1", "file2", etc.

        The Agent Toolkit has NO tool for this — it only has accept_dispute_claim.
        We do it raw.
        """
        if not package.files:
            raise ValueError("Cannot submit empty evidence package")

        url = f"{self.base_url}/v1/customer/disputes/{package.dispute_id}/provide-evidence"

        # Build multipart payload
        # PayPal expects: {"evidences": [{"evidence_type": "...", "notes": "...", "evidence_info": {...}}]}
        evidences = []
        for i, ef in enumerate(package.files):
            evidences.append({
                "evidence_type": ef.evidence_type.value,
                "notes": ef.notes,
                "evidence_info": {
                    "tracking_info": [],  # populated for PROOF_OF_FULFILLMENT
                },
            })

        input_payload = json.dumps({
            "evidences": evidences,
            "notes": package.notes or "Evidence submitted via PayGuard",
        })

        # Build multipart form
        files_payload = {"input": (None, input_payload, "application/json")}
        for i, ef in enumerate(package.files):
            mime_type = mimetypes.guess_type(str(ef.file_path))[0] or "application/octet-stream"
            files_payload[f"file{i+1}"] = (
                ef.file_path.name,
                ef.file_path.read_bytes(),
                mime_type,
            )

        # Submit — must use raw httpx here, not the Agent Toolkit
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                files=files_payload,
                timeout=60.0,
            )

        if resp.status_code == 200:
            return {"status": "submitted", "dispute_id": package.dispute_id}
        else:
            raise RuntimeError(
                f"Evidence submission failed: {resp.status_code} — {resp.text}"
            )

    async def gather_product_evidence(
        self,
        item_description: str,
        dispute_id: str,
        transaction_amount: float | None,
        tmp_dir: "Path",
    ) -> "Optional[EvidenceFile]":
        """
        Uses Channel3's product graph to look up the disputed item and
        generates an ITEM_DESCRIPTION evidence file for PayPal's
        provide-evidence endpoint.

        No API key → returns None gracefully (not a hard failure).
        """
        from agents.channel3 import Channel3Client

        client = Channel3Client()
        result = await client.enrich_dispute(dispute_id, item_description, transaction_amount)

        if not result.succeeded:
            return None

        txt_path = tmp_dir / f"channel3_{dispute_id}.txt"
        txt_path.write_text(result.evidence_text, encoding="utf-8")

        return EvidenceFile(
            evidence_type=EvidenceType.ITEM_DESCRIPTION,
            file_path=txt_path,
            notes=f"Product data for '{item_description}' — Channel3 catalog",
            source="Channel3 product graph (trychannel3.com)",
        )

    async def accept_dispute(self, dispute_id: str, note: str = "") -> dict:
        """
        Accept a dispute claim (Oracle recommends ACCEPT).
        This IS in the Agent Toolkit, but we call it directly for consistency.

        POST /v1/customer/disputes/{dispute_id}/accept-claim
        """
        url = f"{self.base_url}/v1/customer/disputes/{dispute_id}/accept-claim"
        resp = await self.client.post(url, json={"note": note or "Accepting claim."})
        resp.raise_for_status()
        return resp.json()
