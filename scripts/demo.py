"""
Anchor — Hackathon Demo Script
PayPal AI Hackathon 2026

Golden path: dispute arrives → Guardian wakes → HATEOAS check → Oracle decides
→ evidence assembled → submitted → morning brief sent.

Run:
    python scripts/demo.py

Uses PayPal sandbox. Set credentials in .env first.
"""

import asyncio
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on the path so `oracle` and `agents` are importable
# regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env not required — Oracle demo runs fully offline

# ── Pretty output ─────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
YELLOW= "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"
DIM   = "\033[2m"

def header(text):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

def step(n, text):
    print(f"\n{BOLD}[{n}]{RESET} {text}")

def ok(text):
    print(f"    {GREEN}✓{RESET} {text}")

def info(text):
    print(f"    {DIM}{text}{RESET}")

def warn(text):
    print(f"    {YELLOW}⚠{RESET}  {text}")


# ── Scenario definitions ───────────────────────────────────────────────────────

SCENARIOS = [
    {
        "name": "Item Not Received — auto-fight",
        "dispute_id": "PP-D-DEMO-001",
        "amount": 245.00,
        "reason_code": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": True,
        "expected": "FIGHT",
        "why": "High win probability with delivery proof; amount above auto-fight threshold",
    },
    {
        "name": "Micro-dispute — auto-accept",
        "dispute_id": "PP-D-DEMO-002",
        "amount": 18.50,
        "reason_code": "ITEM_NOT_RECEIVED",
        "has_tracking": False,
        "has_delivery_confirmation": False,
        "expected": "ACCEPT",
        "why": "Amount below auto-accept threshold ($60); fighting costs more than recovery",
    },
    {
        "name": "Unauthorized — no buyer comm → accept",
        "dispute_id": "PP-D-DEMO-003",
        "amount": 180.00,
        "reason_code": "UNAUTHORIZED",
        "has_tracking": False,
        "has_delivery_confirmation": False,
        "expected": "ACCEPT",
        "why": "Unauthorized base rate 10%; without buyer communication evidence, EV(fight) < 0",
    },
    {
        "name": "High-volume tier — non-refundable fee changes math",
        "dispute_id": "PP-D-DEMO-004",
        "amount": 95.00,
        "reason_code": "ITEM_NOT_RECEIVED",
        "has_tracking": True,
        "has_delivery_confirmation": False,
        "high_volume_tier": True,
        "expected": "ACCEPT",
        "why": "$30 non-refundable fee + no delivery confirmation → EV(fight) < 0; standard tier would fight this",
    },
    {
        "name": "SNAD — fight with item proof",
        "dispute_id": "PP-D-DEMO-005",
        "amount": 320.00,
        "reason_code": "SIGNIFICANTLY_NOT_AS_DESCRIBED",
        "has_item_description_proof": True,
        "has_buyer_communication": True,
        "expected": "FIGHT",
        "why": "Listing proof plus buyer communication raises the evidence-adjusted win rate",
    },
]


async def run_oracle_demo():
    from oracle.decision import calculate, ClaimType, Evidence

    CLAIM_MAP = {
        "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
        "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
        "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
        "BILLING_ISSUES": ClaimType.BILLING_ISSUE,
    }

    header("Settlement Oracle — Economic Decision Engine")
    print(f"\n  The Oracle calculates EV(fight) vs EV(accept) for every dispute.")
    print(f"  The core decision model runs locally without PayPal credentials.\n")

    for s in SCENARIOS:
        step(s["dispute_id"], f"{BOLD}{s['name']}{RESET}")
        info(f"Amount: ${s['amount']:.2f} | Reason: {s['reason_code']}")

        evidence = Evidence(
            has_tracking_number=s.get("has_tracking", False),
            has_delivery_confirmation=s.get("has_delivery_confirmation", False),
            has_buyer_communication=s.get("has_buyer_communication", False),
            has_item_description_proof=s.get("has_item_description_proof", False),
            seller_account_age_days=365,
            seller_dispute_win_rate=0.5,
        )

        result = calculate(
            dispute_amount=s["amount"],
            claim_type=CLAIM_MAP[s["reason_code"]],
            evidence=evidence,
            high_volume_tier=s.get("high_volume_tier", False),
        )

        color = GREEN if result.recommendation.value == s["expected"] else RED
        print(f"\n    {color}{BOLD}→ {result.recommendation.value}{RESET}")
        info(f"Win probability: {result.win_probability:.0%}")
        info(f"EV(fight): ${result.ev_fight:.2f} | EV(accept): ${result.ev_accept:.2f}")
        info(f"Break-even: {result.break_even_probability:.0%} | Confidence: {result.confidence:.0%}")
        print(f"    {DIM}Why: {s['why']}{RESET}")


async def run_guardian_demo():
    header("Guardian Agent — HATEOAS State Awareness")
    print("\n  Offline walkthrough — no PayPal credentials or optional integrations required.")
    print("  Live webhook actions require configured PayPal sandbox credentials.\n")

    step("HATEOAS", "Illustrative live-action gate (not called by this offline demo)")

    # Show the decision and state checks without implying a PayPal call occurred.
    print(f"""
    {DIM}# Simplified Guardian flow:
    decision = oracle.calculate(dispute)
    permitted = await paypal.get_dispute(dispute_id)  # inspect current HATEOAS links

    if cfg.auto_accept(amount) and decision.recommendation == ACCEPT and decision.ev_fight <= 0:
        if "accept-claim" not in permitted:
            # State gate — defer to manual review
            await self._request_approval(...)
            return
        await self._accept_dispute(...)

    elif cfg.auto_fight(amount, decision.win_probability) and decision.recommendation == FIGHT and decision.ev_fight > 0:
        if "provide-evidence" not in permitted:
            # State gate — use another permitted action or defer
            await self._request_approval(...)
            return
        await self._fight_dispute(...){RESET}
    """)

    ok("Guardian fetches the current dispute and checks permitted HATEOAS links")
    ok("If PayPal does not permit the action, Guardian defers for merchant review")


async def run_recovery_demo():
    header("Recovery Agent — Abandoned Checkout Recovery")
    print(f"\n  When a buyer approves payment on PayPal but the merchant's page errors,")
    print(f"  the sale is lost. Recovery Agent detects this and sends a recovery invoice.\n")

    step("FLOW", "Abandoned checkout lifecycle")
    print(f"""
    {DIM}1. Buyer clicks PayPal button → authorized, order status = APPROVED
    2. Merchant's page crashes before calling /v2/checkout/orders/{{id}}/capture
    3. CHECKOUT.ORDER.APPROVED webhook → Recovery Agent indexes the order
    4. Cron (every 15 min): scan for APPROVED orders older than 30 min
    5. Verify order still uncaptured via GET /v2/checkout/orders/{{id}}
    6. Create PayPal invoice → POST /v2/invoicing/invoices
    7. Send invoice to buyer — they get a PayPal email with one-click pay
    8. Log recovery_attempted to Elasticsearch
    9. Morning brief: "3 abandoned orders recovered ($312)"{RESET}
    """)

    ok("Only triggers for highest-intent abandonment — buyer already authorized payment")
    ok("PayPal invoice goes directly from PayPal to buyer — familiar, trusted UI")
    ok("Max 2 attempts per order — never spams the buyer")

    step("BRIEF", "Morning brief with recovery stats")
    print(f"""
    {CYAN}*Anchor — morning brief*

    *Disputes:* 3 arrived — fought 2, accepted 1 (low EV)
    *Abandoned checkouts:* 3 recovered ($312.00 in recovery invoices sent)
    *Account health:* 1 protective action to keep dispute rate below freeze threshold{RESET}
    """)
    ok("Recovery revenue shows up in the same brief as dispute handling")
    ok("One sleep → two problems solved: disputes managed, abandoned sales recovered")


async def run_morning_brief_demo():
    header("Morning Brief — Illustrative Sample")
    print(f"\n  Example format only. A live brief requires configured data and delivery credentials.\n")

    # Show a sample brief structure
    sample_brief = """
┌──────────────────────────────────────────────────────┐
│  Anchor — Overnight Summary (Oct 19)                  │
├──────────────────────────────────────────────────────┤
│  Disputes handled:  3     Pending your review:  1    │
│  Amount recovered:  $445.00                           │
│  Abandoned orders recovered:  3  ($312.00)            │
│  Account health:    Normal (dispute rate: 0.8%)       │
├──────────────────────────────────────────────────────┤
│  FOUGHT (2):                                          │
│    PP-D-1234  $245 INR — evidence submitted (87% win) │
│    PP-D-1235  $200 INR — evidence submitted (71% win) │
│  ACCEPTED (1):                                        │
│    PP-D-1236  $18.50 — below threshold, not worth it  │
│  NEEDS REVIEW (1):                                    │
│    PP-D-1237  $180 UNAUTH — approve fight or accept?  │
│  RECOVERED (3):                                       │
│    ORD-5501  $104 — buyer re-invoiced, payment pending │
│    ORD-5502  $89  — buyer re-invoiced, payment pending │
│    ORD-5503  $119 — buyer completed payment ✓          │
└──────────────────────────────────────────────────────┘
    """
    print(CYAN + sample_brief + RESET)
    ok("Sample shows how recorded events can be summarized in one brief")
    ok("Pending items surfaced with one-tap approve link (48h token, dispute-bound)")
    ok("Recovery stats alongside dispute stats — single overnight view of lost revenue")


async def run_full_demo():
    print(f"\n{BOLD}{'='*60}")
    print(f"  ANCHOR — Autonomous Dispute Management")
    print(f"  PayPal AI Hackathon 2026")
    print(f"{'='*60}{RESET}")
    print(f"\n  Offline prototype walkthrough — live actions need PayPal credentials")
    print(f"  and an always-on deployment. The free demo may sleep between requests.")

    await run_oracle_demo()
    await run_guardian_demo()
    await run_recovery_demo()
    await run_morning_brief_demo()

    header("Summary")
    print(f"""
  Anchor prototype capabilities shown above:

  {GREEN}✓{RESET} HATEOAS-aware — checks PayPal's live permitted actions before acting
  {GREEN}✓{RESET} Economic Oracle — calculates EV(fight) vs EV(accept), not just "always fight"
  {GREEN}✓{RESET} Two-tier fee model — $15 refundable vs $30 non-refundable at high-volume
  {GREEN}✓{RESET} Assumed Unauthorized base rate — 10%, configurable and not validated
  {GREEN}✓{RESET} Configurable thresholds — live actions require credentials and permitted PayPal links
  {GREEN}✓{RESET} Morning brief — scheduled while the web process is running
  {GREEN}✓{RESET} Recovery Agent — finds approved-but-uncaptured orders, sends buyer invoices
  {GREEN}✓{RESET} Two problems solved: disputes managed AND abandoned sales recovered

  Integrations overlap and vary by plan. Compare current vendor capabilities,
  contracts, and supported workflows directly before choosing a product.
    """)


if __name__ == "__main__":
    asyncio.run(run_full_demo())
