"""
Gmail Prevention Outreach — compose and send proactive buyer emails to
prevent chargebacks before they're filed.

Sponsor: Gmail (plugin:small-business:gmail / mcp__c42f0408)

Triggered by: ChasebackPreventionReport (oracle/chargeback_prevention_advisor.py)
  when urgency is IMMEDIATE or URGENT and primary_action is BUYER_OUTREACH
  or PARTIAL_REFUND_OFFER.

Email templates per prevention action:
  - BUYER_OUTREACH:         "Hi, we noticed a question on your order — here's the update"
  - PARTIAL_REFUND_OFFER:   "We want to make this right — partial refund offer inside"
  - SEND_TRACKING_UPDATE:   "Your order status update — real-time tracking"
  - EXPEDITE_RESHIPMENT:    "We're sending a replacement right away"
  - PROACTIVE_REFUND:       "We've processed your refund — no action needed"
  - ESCALATE_TO_MANAGER:    Internal escalation email to merchant's support lead

All emails are personalized with order details and include PayPal-specific
messaging that reduces buyer confusion about the dispute process.

Usage at runtime (when Gmail MCP is connected):
    draft = compose_prevention_email(report, order_context)
    # pass draft.subject / draft.body to Gmail MCP create_draft / send_message
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EmailPriority(str, Enum):
    URGENT  = "URGENT"    # Send within 1 hour
    HIGH    = "HIGH"      # Send within 6 hours
    NORMAL  = "NORMAL"    # Send within 24 hours


@dataclass
class PreventionEmailContext:
    buyer_email: str
    buyer_first_name: str
    merchant_name: str
    merchant_support_email: str
    order_id: str
    order_amount: float
    item_name: str
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None
    partial_refund_amount: Optional[float] = None
    estimated_delivery: Optional[str] = None


@dataclass
class PreventionEmailDraft:
    to: str
    subject: str
    body: str
    priority: EmailPriority
    action_type: str
    send_within_hours: int
    # For PayPal disputes specifically
    paypal_dispute_id: Optional[str] = None
    cc_merchant_support: bool = False


_TEMPLATES: dict[str, dict] = {
    "BUYER_OUTREACH": {
        "subject": "Update on your order #{order_id} from {merchant_name}",
        "body": """Hi {buyer_first_name},

We noticed you may have a question about your recent order #{order_id} (${order_amount:.2f}).

We want to make sure you're completely taken care of. Here's the current status:

📦 Order: {item_name}
🔢 Order #: {order_id}
{tracking_line}

If there's anything that isn't right, please reply to this email directly — our team will respond within 2 hours and we'll make it right immediately.

You don't need to open a dispute with PayPal — we'll resolve this directly with you, faster.

Warm regards,
{merchant_name} Customer Care
{merchant_support_email}
""",
        "priority": EmailPriority.HIGH,
        "send_within_hours": 6,
    },
    "PARTIAL_REFUND_OFFER": {
        "subject": "We want to make this right — special offer for order #{order_id}",
        "body": """Hi {buyer_first_name},

We understand your order #{order_id} didn't meet your expectations, and we sincerely apologize.

As a gesture of goodwill, we'd like to offer you a partial refund of **${partial_refund_amount:.2f}** — no return required, no hassle.

To accept this offer, simply reply to this email with "Yes, I accept" and we'll process the refund to your original payment method within 24 hours.

📦 Order: {item_name} (#{order_id})
💰 Partial Refund Offer: ${partial_refund_amount:.2f}

This offer is valid for 48 hours. If we don't hear back, we'll follow up to find another way to help.

No need to open a PayPal dispute — we're here to resolve this directly and quickly for you.

Sincerely,
{merchant_name} Customer Care
{merchant_support_email}
""",
        "priority": EmailPriority.URGENT,
        "send_within_hours": 2,
    },
    "SEND_TRACKING_UPDATE": {
        "subject": "📦 Your order #{order_id} is on the way — live tracking inside",
        "body": """Hi {buyer_first_name},

Your order #{order_id} is on its way! Here are the latest tracking details:

📦 Item: {item_name}
🚚 Tracking #: {tracking_number}
{tracking_url_line}
{eta_line}

If the tracking shows any issues or the package doesn't arrive by the expected date, please reply to this email and we'll resolve it immediately — no need to wait or open a dispute.

Thank you for shopping with {merchant_name}!

{merchant_name} Customer Care
{merchant_support_email}
""",
        "priority": EmailPriority.HIGH,
        "send_within_hours": 4,
    },
    "EXPEDITE_RESHIPMENT": {
        "subject": "We're sending you a replacement right away — order #{order_id}",
        "body": """Hi {buyer_first_name},

We're so sorry your order #{order_id} didn't arrive as expected. We've already arranged an expedited replacement shipment at no cost to you.

📦 Replacement Item: {item_name}
🚀 Shipping: Express (2-3 business days)
💰 Cost to you: $0.00

You'll receive a new tracking number within 24 hours. No return necessary.

We want to make this right, and you don't need to open a dispute — we're handling it personally.

{merchant_name} Customer Care
{merchant_support_email}
""",
        "priority": EmailPriority.URGENT,
        "send_within_hours": 1,
    },
    "PROACTIVE_REFUND": {
        "subject": "Your refund for order #{order_id} has been processed",
        "body": """Hi {buyer_first_name},

We've processed a full refund of ${order_amount:.2f} for your order #{order_id}.

Refund details:
💰 Amount: ${order_amount:.2f}
📅 Processing time: 3-5 business days (depending on your bank)
💳 Refunded to: Your original payment method

There's nothing you need to do. If you filed a dispute with PayPal, it will be automatically closed once the refund processes.

We're sorry for the inconvenience and appreciate your patience.

{merchant_name} Customer Care
{merchant_support_email}
""",
        "priority": EmailPriority.NORMAL,
        "send_within_hours": 12,
    },
}


def compose_prevention_email(
    action_type: str,
    ctx: PreventionEmailContext,
    paypal_dispute_id: Optional[str] = None,
) -> PreventionEmailDraft:
    """
    Compose a prevention email draft using the action type from
    ChasebackPreventionReport.primary_action.

    Pass to Gmail MCP's create_draft or send_message.
    """
    template = _TEMPLATES.get(action_type, _TEMPLATES["BUYER_OUTREACH"])

    tracking_line = (
        f"🚚 Tracking #: {ctx.tracking_number}" if ctx.tracking_number
        else "📍 Fulfillment update: We're preparing your package."
    )
    tracking_url_line = (
        f"🔗 Track your package: {ctx.tracking_url}" if ctx.tracking_url else ""
    )
    eta_line = (
        f"📅 Estimated delivery: {ctx.estimated_delivery}" if ctx.estimated_delivery else ""
    )
    partial = ctx.partial_refund_amount or (ctx.order_amount * 0.65)

    subject = template["subject"].format(
        order_id=ctx.order_id,
        merchant_name=ctx.merchant_name,
    )

    body = template["body"].format(
        buyer_first_name=ctx.buyer_first_name or "Valued Customer",
        merchant_name=ctx.merchant_name,
        merchant_support_email=ctx.merchant_support_email,
        order_id=ctx.order_id,
        order_amount=ctx.order_amount,
        item_name=ctx.item_name,
        tracking_number=ctx.tracking_number or "Being processed",
        tracking_url_line=tracking_url_line,
        tracking_line=tracking_line,
        eta_line=eta_line,
        partial_refund_amount=partial,
    )

    return PreventionEmailDraft(
        to=ctx.buyer_email,
        subject=subject,
        body=body.strip(),
        priority=template["priority"],
        action_type=action_type,
        send_within_hours=template["send_within_hours"],
        paypal_dispute_id=paypal_dispute_id,
        cc_merchant_support=action_type in ("ESCALATE_TO_MANAGER", "EXPEDITE_RESHIPMENT"),
    )
