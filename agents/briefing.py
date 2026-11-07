"""
Morning Brief Generator

Queries Elasticsearch for actions in the reporting window, then produces
a human-readable summary that can be sent through the configured notification
integration.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ActionRecord:
    """A single autonomous action Anchor took."""
    timestamp: str
    action_type: str          # "dispute_fought", "dispute_accepted", "invoice_reminder", "risk_alert"
    dispute_id: Optional[str] = None
    invoice_id: Optional[str] = None
    amount: Optional[float] = None
    win_probability: Optional[float] = None
    outcome: Optional[str] = None   # "submitted", "accepted", "reminder_sent", "escalated"
    reasoning: Optional[str] = None
    requires_attention: bool = False


@dataclass
class Brief:
    merchant_id: str
    period_start: str
    period_end: str
    actions: list[ActionRecord] = field(default_factory=list)

    # Aggregates
    disputes_handled: int = 0
    disputes_fought: int = 0
    disputes_accepted: int = 0
    disputes_pending_approval: int = 0
    invoices_reminded: int = 0
    invoices_paid: int = 0
    amount_recovered: float = 0.0
    account_health_actions: int = 0
    orders_recovered: int = 0
    recovered_revenue: float = 0.0

    def add(self, action: ActionRecord):
        self.actions.append(action)
        if action.action_type == "dispute_fought":
            self.disputes_fought += 1
            self.disputes_handled += 1
        elif action.action_type == "dispute_accepted":
            self.disputes_accepted += 1
            self.disputes_handled += 1
        elif action.action_type == "dispute_pending":
            self.disputes_pending_approval += 1
        elif action.action_type == "invoice_paid":
            self.invoices_paid += 1
            self.amount_recovered += action.amount or 0
        elif action.action_type == "invoice_reminder":
            self.invoices_reminded += 1
        elif action.action_type == "account_health":
            self.account_health_actions += 1
        elif action.action_type == "recovery_capture" and action.outcome == "captured":
            self.orders_recovered += 1
            self.recovered_revenue += action.amount or 0

    @property
    def needs_attention(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.requires_attention]

    def to_slack_message(self) -> str:
        """
        Formats the brief as a Slack message.
        Short enough to read in 30 seconds.
        """
        lines = ["*Anchor — morning brief*\n"]

        quiet = (
            self.disputes_handled == 0
            and self.invoices_reminded == 0
            and not self.needs_attention
        )
        if quiet:
            lines.append("Nothing to report. Your PayPal account is healthy.")
            return "\n".join(lines)

        if self.disputes_handled or self.disputes_pending_approval:
            parts = []
            if self.disputes_fought:
                parts.append(f"fought {self.disputes_fought}")
            if self.disputes_accepted:
                parts.append(f"accepted {self.disputes_accepted} (low EV)")
            summary = ", ".join(parts) if parts else "reviewed"
            total = self.disputes_handled
            line = f"*Disputes:* {total} arrived — {summary}"
            if self.disputes_pending_approval:
                line += f", {self.disputes_pending_approval} need your decision"
            lines.append(line)

        if self.invoices_reminded or self.invoices_paid:
            line = f"*Invoices:* sent {self.invoices_reminded} reminder(s)"
            if self.invoices_paid:
                line += f", {self.invoices_paid} paid (${self.amount_recovered:,.2f} recovered)"
            lines.append(line)

        if self.orders_recovered:
            lines.append(
                f"*Abandoned checkouts:* {self.orders_recovered} recovered "
                f"(${self.recovered_revenue:,.2f} confirmed via PayPal capture)"
            )

        if self.account_health_actions:
            lines.append(
                f"*Account health:* {self.account_health_actions} protective action(s) taken "
                f"to keep dispute rate below PayPal's freeze threshold"
            )

        attention = self.needs_attention
        if attention:
            lines.append(f"\n*Needs your attention ({len(attention)}):*")
            for a in attention:
                if a.action_type == "dispute_pending":
                    prob_str = f"{a.win_probability:.0%}" if a.win_probability else "unknown"
                    lines.append(
                        f"• ${a.amount:.2f} dispute — {prob_str} win probability, evidence ready. "
                        f"Reply *YES* to submit or open Anchor to review."
                    )
                else:
                    lines.append(f"• {a.reasoning or a.action_type}")

        return "\n".join(lines)

    def to_sms(self) -> str:
        """Ultra-short SMS version."""
        parts = []
        if self.disputes_handled:
            parts.append(f"{self.disputes_handled} dispute(s) handled")
        if self.amount_recovered:
            parts.append(f"${self.amount_recovered:,.0f} recovered")
        if self.orders_recovered:
            parts.append(f"{self.orders_recovered} abandoned order(s) recovered")
        attention = len(self.needs_attention)
        if attention:
            parts.append(f"{attention} need your decision")

        if not parts:
            return "Anchor: all quiet, account healthy."
        return "Anchor: " + " · ".join(parts) + ". Open app or reply YES to approve pending actions."


async def generate_brief(merchant_id: str, since: datetime) -> Brief:
    """
    Query Elasticsearch for all anchor-actions since `since`.
    Returns a Brief ready to send.
    """
    import os
    import httpx

    brief = Brief(
        merchant_id=merchant_id,
        period_start=since.isoformat(),
        period_end=datetime.now(timezone.utc).isoformat(),
    )

    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return brief

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"merchant_id": merchant_id}},
                    {"range": {"timestamp": {"gte": since.isoformat()}}},
                ]
            }
        },
        "sort": [{"timestamp": "asc"}],
        "size": 100,
    }

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{es_url}/anchor-actions/_search",
                headers={
                    "Authorization": f"ApiKey {es_key}",
                    "Content-Type": "application/json",
                },
                json=query,
                timeout=5.0,
            )
            hits = r.json().get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit["_source"]
                brief.add(ActionRecord(**{
                    k: src.get(k)
                    for k in ActionRecord.__dataclass_fields__
                    if src.get(k) is not None
                }))
    except Exception as e:
        print(f"[Briefing] Elasticsearch query failed: {e}")

    return brief


async def log_action(merchant_id: str, action: ActionRecord) -> None:
    """
    Index an autonomous action so it appears in future briefs.
    Called by classifier.py and invoice.py after every autonomous action.
    """
    import os
    import httpx

    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return

    doc = {
        "merchant_id": merchant_id,
        **{k: v for k, v in action.__dict__.items() if v is not None},
    }

    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"{es_url}/anchor-actions/_doc",
                headers={
                    "Authorization": f"ApiKey {es_key}",
                    "Content-Type": "application/json",
                },
                json=doc,
                timeout=3.0,
            )
    except Exception:
        pass
