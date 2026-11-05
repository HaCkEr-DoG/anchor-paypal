"""
Invoice Collector Agent

Manages the full invoice lifecycle and proactively flags dispute risk
on unpaid invoices BEFORE the buyer files a dispute.

Key insight: most dispute tools wait for a dispute to happen.
PayGuard can predict dispute risk on open invoices and preempt them
by proactively reaching out to buyers who match dispute-risk patterns.

Dispute risk signals on invoices:
- Buyer has disputed before (lookup via Elasticsearch)
- Invoice overdue > 14 days with no buyer response
- Large amount on a new client relationship
- Category with high SNAD rate (digital services, custom work)
"""

import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import httpx


@dataclass
class InvoiceRisk:
    invoice_id: str
    risk_score: float         # 0-1, higher = more likely to dispute
    risk_factors: list[str]
    recommended_action: str   # "send_reminder", "call_client", "require_milestone_payment"


class InvoiceCollector:

    def __init__(self):
        self.paypal_base = "https://api.sandbox.paypal.com"

    async def check_overdue(self, invoice_id: str, invoice_data: dict):
        """
        Called when INVOICING.INVOICE.UPDATED fires.
        Runs overdue reminder sequence + dispute risk scoring.
        """
        status = invoice_data.get("status")
        due_date_str = invoice_data.get("detail", {}).get("payment_term", {}).get("due_date")
        amount = float(invoice_data.get("amount", {}).get("value", 0))
        buyer_email = invoice_data.get("primary_recipients", [{}])[0].get(
            "billing_info", {}
        ).get("email_address", "")

        if status not in ("SENT", "VIEWED"):
            return  # Not overdue — nothing to do

        days_overdue = self._days_overdue(due_date_str)
        risk = await self._score_dispute_risk(invoice_id, buyer_email, amount, days_overdue)

        print(
            f"[Invoice] {invoice_id}: {days_overdue}d overdue, "
            f"risk={risk.risk_score:.0%} ({', '.join(risk.risk_factors)})"
        )

        # Escalating reminder sequence
        if days_overdue == 3:
            await self._send_reminder(invoice_id, buyer_email, sequence=1, amount=amount)
        elif days_overdue == 7:
            await self._send_reminder(invoice_id, buyer_email, sequence=2, amount=amount)
        elif days_overdue == 14:
            await self._send_reminder(invoice_id, buyer_email, sequence=3, amount=amount)
            if risk.risk_score > 0.6:
                # High dispute risk — escalate to phone/direct contact recommendation
                await self._escalate(invoice_id, risk)

    async def mark_paid(self, invoice_id: str):
        """Invoice paid — update dashboard, stop reminder sequence."""
        print(f"[Invoice] {invoice_id} paid — marking resolved")
        # Update Bryntum Gantt task to "complete"
        # AG Studio: update cash flow projection

    async def _score_dispute_risk(
        self, invoice_id: str, buyer_email: str, amount: float, days_overdue: int
    ) -> InvoiceRisk:
        """
        Multi-factor dispute risk scoring.
        Higher score = buyer more likely to file a dispute instead of paying.

        This is the proactive insight no invoice tool has.
        """
        risk_score = 0.0
        factors = []

        # Factor 1: Past dispute history (query Elasticsearch)
        past_disputes = await self._check_buyer_dispute_history(buyer_email)
        if past_disputes > 0:
            risk_score += min(0.4, past_disputes * 0.15)
            factors.append(f"buyer has {past_disputes} prior dispute(s)")

        # Factor 2: Days overdue
        if days_overdue > 14:
            risk_score += 0.25
            factors.append(f"{days_overdue}d overdue (no response)")
        elif days_overdue > 7:
            risk_score += 0.10

        # Factor 3: Amount — larger amounts attract more disputes
        if amount > 500:
            risk_score += 0.15
            factors.append(f"high value (${amount:.0f})")
        elif amount > 200:
            risk_score += 0.08

        # Factor 4: No buyer communication since invoice sent
        # (would check Zapier Gmail integration in production)
        if days_overdue > 5:
            risk_score += 0.10
            factors.append("no buyer response to invoice")

        risk_score = min(risk_score, 1.0)

        action = "send_reminder"
        if risk_score > 0.7:
            action = "call_client_directly"
        elif risk_score > 0.5:
            action = "send_personal_email"

        return InvoiceRisk(
            invoice_id=invoice_id,
            risk_score=round(risk_score, 2),
            risk_factors=factors,
            recommended_action=action,
        )

    async def _check_buyer_dispute_history(self, buyer_email: str) -> int:
        """Query Elasticsearch for past disputes from this buyer."""
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key or not buyer_email:
            return 0

        query = {
            "query": {"term": {"buyer_email": buyer_email}},
            "size": 0,
            "_source": False,
        }
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{es_url}/payguard-disputes/_count",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=query,
                    timeout=3.0,
                )
                return r.json().get("count", 0)
        except Exception:
            return 0

    async def _send_reminder(
        self, invoice_id: str, buyer_email: str, sequence: int, amount: float
    ):
        """
        Send overdue reminder via PayPal's invoice remind endpoint.
        Escalates tone: gentle → firm → formal demand.
        """
        messages = [
            f"Just a friendly reminder that invoice #{invoice_id} for ${amount:.2f} is due. "
            "Please let me know if you have any questions.",
            f"Invoice #{invoice_id} for ${amount:.2f} is now {7 if sequence == 2 else 3} days overdue. "
            "Please arrange payment or contact me to discuss.",
            f"This is a final notice for invoice #{invoice_id} (${amount:.2f}). "
            "Payment is now 14 days overdue. Please respond within 48 hours.",
        ]
        note = messages[min(sequence - 1, 2)]

        token = await self._get_token()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.paypal_base}/v2/invoicing/invoices/{invoice_id}/remind",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"note": note, "send_to_recipient": True},
            )
            if r.status_code == 204:
                print(f"[Invoice] Reminder #{sequence} sent for {invoice_id}")
            else:
                print(f"[Invoice] Reminder failed: {r.status_code} {r.text}")

    async def _escalate(self, invoice_id: str, risk: InvoiceRisk):
        """High dispute-risk invoice — alert seller via Zapier → Slack."""
        message = (
            f"⚠️ High dispute risk on invoice {invoice_id} "
            f"(score: {risk.risk_score:.0%})\n"
            f"Factors: {', '.join(risk.risk_factors)}\n"
            f"Recommended: {risk.recommended_action}"
        )
        print(f"[Invoice] ESCALATE: {message}")
        # zapier_mcp.slack_message("#payguard-alerts", message)

    def _days_overdue(self, due_date_str: str | None) -> int:
        if not due_date_str:
            return 0
        try:
            due = datetime.strptime(due_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - due).days)
        except Exception:
            return 0

    async def _get_token(self) -> str:
        import base64
        creds = base64.b64encode(
            f"{os.environ['PAYPAL_CLIENT_ID']}:{os.environ['PAYPAL_CLIENT_SECRET']}".encode()
        ).decode()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.paypal_base}/v1/oauth2/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            )
            return r.json()["access_token"]
