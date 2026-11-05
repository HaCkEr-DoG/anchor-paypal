"""
Recovery cron entry point — called by Render scheduled job every 15 minutes.

Scans for PayPal orders approved but never captured (highest-intent abandoned carts)
and sends buyer recovery invoices via PayPal's invoicing API.
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()


async def main():
    from agents.recovery import RecoveryAgent

    merchant_id = os.environ.get("MERCHANT_ID", "default")
    agent = RecoveryAgent()
    result = await agent.scan_and_recover(merchant_id)
    print(
        f"[Recovery cron] scanned={result['scanned']} "
        f"recovered={result['recovered']} skipped={result['skipped']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
