# Anchor — Autonomous PayPal Dispute & Recovery Agent

> **PayPal AI Hackathon 2026**
> While you sleep, Anchor handles your PayPal disputes and recovers your abandoned sales.

**[Live demo →](https://anchor-paypal.onrender.com)**

---

## For Judges — 90-second eval path

**No credentials needed.** Everything below runs live on the demo.

| What to try | Where |
|---|---|
| **Live Dispute Simulator** — pick a scenario, see Oracle + Guardian + API calls | [Dashboard → Live Dispute Simulator panel](https://anchor-paypal.onrender.com) |
| **Settlement Oracle Calculator** — adjust amount / evidence / tier, recalculate | Same page → Settlement Oracle panel |
| **AG Grid disputes table** — sortable, filterable, column-grouped | Same page → Disputes section |
| **API surface** — full endpoint list with curl examples | Same page → Postman panel |
| **Sponsor integrations** | Same page → APIMatic / Channel3 / Render / Astropods panels |

**Sponsor prizes targeted:**
- **AG Grid** ($5K) — Community 31.3.2 disputes + recovery tables with live data, column grouping, custom renderers
- **Render** (credits) — live deployment auto-deployed from GitHub, free tier, `render.yaml` in repo
- **Best Use of AI + PayPal** ($5K) — Settlement Oracle calculates fight-vs-accept EV using the actual PayPal fee model; Guardian Agent checks HATEOAS links before every call
- **Best Use of Agentic Commerce** ($5K) — full autonomous loop: webhook → Oracle → Guardian → PayPal action, plus abandoned checkout recovery via PayPal Invoicing

**Key innovation:** No other tool does the EV calculation. Every existing product (Chargeflow, Disputifier, Chargehound) fights every dispute automatically. Anchor is the first to calculate whether fighting is economically worth it, using PayPal's actual two-tier dispute fee model.

---

## What it does

PayPal merchants lose money in two silent ways:

1. **Disputes they should have won** — manually fighting every chargeback is expensive and inconsistent. Most merchants accept everything or fight everything. Neither is right.
2. **Abandoned checkouts** — buyers approve the PayPal payment but the merchant's page errors before capture. The sale disappears.

Anchor solves both using two AI agents powered by PayPal's APIs:

### Guardian Agent — dispute economics
- Receives `CUSTOMER.DISPUTE.CREATED` webhooks instantly
- Runs the **Settlement Oracle**: calculates EV(fight) vs EV(accept) using PayPal's two-tier dispute fee model ($15 refundable vs $30 non-refundable at high-volume)
- Checks PayPal's **HATEOAS links** before every action — knows whether the dispute is in INQUIRY or CLAIM stage and acts accordingly
- During INQUIRY: sends a buyer message via `send-message` to resolve before escalation (fee-free window)
- Autonomously fights high-EV disputes, accepts low-EV ones, asks the merchant for decisions in between
- Approval links are **HMAC-SHA256 signed**, bound to the specific dispute, expire in 48h

### Recovery Agent — abandoned checkout recovery
- Receives `CHECKOUT.ORDER.APPROVED` webhooks (buyer authorized, merchant hasn't captured)
- Cron every 15 minutes: finds approved-but-uncaptured orders older than 30 minutes
- Verifies the order is still open via `GET /v2/checkout/orders/{id}`
- Creates a **PayPal invoice** via `POST /v2/invoicing/invoices` and sends it directly to the buyer
- Buyer gets a PayPal-branded email with one-click payment — familiar and trusted

### Morning Brief
- Every morning at 8 AM: "2 disputes fought ($445 protected), 3 abandoned orders recovered ($312)"
- No dashboard to open. No login required. It reports to you.

---

## Integrations

| Integration | Role |
|---|---|
| **PayPal API** | Disputes v1, Orders v2, Invoicing v2, Webhooks, OAuth2 — the entire runtime |
| **Settlement Oracle** | Closed-form EV engine: `P(win) × amount − effective_cost` with evidence scoring |
| **AG Grid** | Community 31.3.2 — disputes and recovery tables with live row data |
| **Bryntum Scheduler** | Timeline view of disputes and recoveries with CrudManager format |
| **APIMatic** | Context Plugin — deterministic PayPal endpoint routing, eliminates hallucinated paths |
| **Channel3** | 100M+ product graph — enriches dispute evidence with canonical product data |
| **Postman** | Collection v2.1 (7 groups, 20+ requests) — one-click import for judges |
| **Render** | Web service with auto-deploy on push; deploy trigger + status panel on dashboard |
| **Astropods** | AI governance gateway — routes Guardian LLM calls for auditing and spend control |
| **Kernel** | Bot-resistant cloud browser — carrier tracking screenshots for dispute evidence |

---

## Built With

- **PayPal APIs** — Disputes v1, Orders v2, Invoicing v2, Webhooks, OAuth2
- **Settlement Oracle** — closed-form expected-value engine with evidence scoring and historical win-rate blending
- **Claude** (Anthropic) — classification, evidence reasoning, buyer message generation
- **FastAPI** + **APScheduler** — webhook server, cron scheduling (recovery every 15 min, brief at 08:00 UTC)
- **AG Grid Community 31.3.2** — disputes and recoveries data grid
- **Bryntum Scheduler 6.0.7** — timeline / Gantt view
- **APIMatic** — deterministic API context plugin for grounded LLM calls
- **Channel3** — product graph enrichment for ITEM_DESCRIPTION evidence
- **Postman** — Anchor's full API surface as a shareable collection
- **Render** — free-tier deployment, auto-deploys on GitHub push
- **Astropods** — AI governance gateway
- **Kernel** — stealth browser for carrier tracking
- **Elasticsearch** — action log, dispute history, deduplication

---

## Try it — zero credentials needed

The Settlement Oracle runs entirely offline. See how Anchor makes the fight-vs-accept decision:

```bash
git clone https://github.com/HaCkEr-DoG/anchor-paypal
cd anchor-paypal
pip install -r requirements.txt
python scripts/demo.py
```

Expected output: four dispute scenarios, HATEOAS lifecycle demo, Recovery agent flow, and a sample morning brief.

---

## Full deployment (PayPal sandbox)

### 1. Set up credentials

```bash
cp .env.example .env
# Fill in: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID
# Optional: ELASTICSEARCH_URL, CHANNEL3_API_KEY, APIMATIC_API_KEY, POSTMAN_API_KEY ...
```

All secrets are environment variables. Nothing is committed.

### 2. Register the PayPal webhook

In your PayPal developer dashboard, add a webhook pointing to `https://<your-host>/webhooks/paypal` and subscribe to:

```
CUSTOMER.DISPUTE.CREATED
CUSTOMER.DISPUTE.UPDATED
CHECKOUT.ORDER.APPROVED
PAYMENT.CAPTURE.COMPLETED
INVOICING.INVOICE.PAYMENT.PAID
INVOICING.INVOICE.UPDATED
```

### 3. Run locally

```bash
uvicorn agents.watcher:app --reload
```

### 4. Deploy to Render (free, no card)

**Live demo:** `https://anchor-paypal.onrender.com`

1. Render → New → Web Service → GitHub → `HaCkEr-DoG/anchor-paypal`
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn agents.watcher:app --host 0.0.0.0 --port $PORT`
4. Plan: **Free** ($0/month)
5. Add env vars from `.env.example`
6. Register webhook: `https://anchor-paypal.onrender.com/webhooks/paypal`

The app runs recovery (every 15 min) and morning brief (8 AM UTC) internally via APScheduler. No separate cron service needed.

---

## How the Settlement Oracle works

For every dispute, Anchor calculates:

```
EV(fight)  = P(win) × amount − effective_cost
EV(accept) = −amount

Standard tier ($15 refundable on win):
  effective_cost = time_cost + (1 − p_win) × 15
  break_even     = (time_cost + 15) / (amount + 15)

High-volume tier ($30 non-refundable, triggered at ≥1.5% dispute rate):
  effective_cost = time_cost + 30
  break_even     = (time_cost + 30) / amount
```

A $245 INR dispute with delivery confirmation: **FIGHT** (92% win probability, EV = +$148).
An $18.50 INR dispute without tracking: **ACCEPT** (EV = −$78 to fight).

**Try it live** on the dashboard at [anchor-paypal.onrender.com](https://anchor-paypal.onrender.com) — the Settlement Oracle panel lets you adjust any input and recalculates in real time.

No other PayPal dispute tool does this calculation.

---

## Project structure

```
agents/
  guardian.py        # Dispute agent — HATEOAS, thresholds, INQUIRY handling
  recovery.py        # Abandoned checkout recovery agent
  classifier.py      # Dispute intake + Oracle routing
  evidence.py        # Evidence assembly + PayPal multipart submission
  briefing.py        # Morning brief generator
  watcher.py         # FastAPI webhook server + all API endpoints
  invoice.py         # Invoice follow-up
  config.py          # Pre-authorization thresholds (configurable per merchant)
  aggrid.py          # AG Grid exporter — dispute + recovery column defs
  apimatic.py        # APIMatic Context Plugin — PayPal endpoint grounding
  bryntum.py         # Bryntum Scheduler data — CrudManager format
  channel3.py        # Channel3 product graph — dispute evidence enrichment
  kernel_browser.py  # Kernel cloud browser — carrier tracking screenshots
  paypal_client.py   # PayPal REST API client (OAuth2, Disputes, Orders, Invoices)
  postman.py         # Postman Collection v2.1 — full API surface

oracle/
  decision.py        # Settlement EV engine — the economic brain

scripts/
  demo.py               # Full offline demo — run this first
  cron_recovery.py      # Recovery cron entry point
  cron_morning_brief.py # Morning brief cron entry point

static/
  dashboard.html     # AG Grid + Bryntum + Oracle + all integration panels

render.yaml          # Render deployment config
.env.example         # All environment variables documented
```

---

## Why Anchor is different from Chargeflow / Disputifier / Chargehound

| | Anchor | Competitors |
|---|---|---|
| EV calculation (fight vs accept) | ✅ Closed-form math, two-tier fees | ❌ Always fight |
| HATEOAS lifecycle awareness | ✅ Checks PayPal's live links | ❌ Static assumptions |
| INQUIRY stage handling | ✅ Sends buyer message before chargeback | ❌ Skip entirely |
| High-volume tier ($30 fee) | ✅ Separate break-even formula | ❌ Flat fee assumption |
| Abandoned checkout recovery | ✅ PayPal invoice on approved+uncaptured | ❌ Not in scope |
| PayPal-native (no card network) | ✅ Disputes v1 + Orders v2 + Invoicing v2 | ❌ Card-network chargebacks |
| Product evidence enrichment | ✅ Channel3 100M+ product graph | ❌ Manual only |
| API grounding | ✅ APIMatic context plugin | ❌ Prompt engineering |
