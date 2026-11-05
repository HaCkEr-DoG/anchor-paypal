"""
Morning brief cron entry point — called by Render scheduled job at 8 AM UTC.

Queries Elasticsearch for overnight actions (disputes, recoveries, invoices),
formats a summary, and sends it to the merchant via Slack.
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()


async def main():
    from agents.guardian import Guardian

    merchant_id = os.environ.get("MERCHANT_ID", "default")
    brief = await Guardian().send_morning_brief(merchant_id)
    if brief:
        print(
            f"[Brief cron] disputes={brief.disputes_handled} "
            f"pending={brief.disputes_pending_approval} "
            f"recovered_orders={brief.orders_recovered}"
        )


if __name__ == "__main__":
    asyncio.run(main())
