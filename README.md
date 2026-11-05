# Anchor — Autonomous PayPal Dispute & Recovery Agent

> **PayPal AI Hackathon 2026**
> While you sleep, Anchor handles your PayPal disputes and recovers your abandoned sales.

---

## What it does

PayPal merchants lose money in two silent ways:

1. **Disputes they should have won** — manually fighting every chargeback is expensive and inconsistent. Most merchants accept everything or fight everything. Neither is right.
2. **Abandoned checkouts** — buyers approve the PayPal payment but the merchant's page errors before capture. The sale disappears.

Anchor solves both using two AI agents powered by PayPal's APIs:

### Guardian Agent — dispute economics
- Receives `CUSTOMER.DISPUTE.CREATED` webhooks instantly
- Runs a **Settlement Oracle**: calculates EV(fight) vs EV(accept) using PayPal's two-tier dispute fee model ($15 refundable vs $30 non-refundable at high-volume)
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

## Built With

- **PayPal APIs**: Disputes v1, Orders v2, Invoicing v2, Webhooks, OAuth2
- **AI**: Settlement Oracle — a closed-form expected-value engine with evidence scoring and historical win-rate blending
- **Claude** (Anthropic): powers classification, evidence reasoning, and buyer message generation
- **FastAPI** — webhook server, approval endpoints
- **Elasticsearch** — action log, dispute history, abandoned order tracking, deduplication
- **Render** — deployment, cron scheduling

---

## Try it — zero credentials needed

The Settlement Oracle runs entirely offline. See how Anchor makes the fight-vs-accept decision:

```bash
git clone https://github.com/HaCkEr-DoG/anchor-paypal
cd paypal
pip install -r requirements.txt
python scripts/demo.py
```

Expected output shows four dispute scenarios, the HATEOAS awareness demo, the Recovery agent flow, and a sample morning brief.

---

## Full deployment (PayPal sandbox)

### 1. Set up credentials

```bash
cp .env.example .env
# Fill in: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID
# Optional: ELASTICSEARCH_URL, ELASTICSEARCH_API_KEY, SLACK_WEBHOOK_URL
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

1. Go to [render.com](https://render.com) → New → Web Service → GitHub → pick `HaCkEr-DoG/anchor-paypal`
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn agents.watcher:app --host 0.0.0.0 --port $PORT`
4. Plan: **Free** ($0/month)
5. Add env vars from `.env.example` in the Render dashboard
6. Register the Render URL as your PayPal webhook: `https://anchor-paypal.onrender.com/webhooks/paypal`

The app runs the recovery check (every 15 min) and morning brief (8 AM UTC) internally via APScheduler — no separate cron service needed. Note: the free tier sleeps after 15 min of inactivity (~30s cold start on wake).

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

No other PayPal dispute tool does this calculation.

---

## Project structure

```
agents/
  guardian.py      # Dispute agent — HATEOAS, thresholds, INQUIRY handling
  recovery.py      # Abandoned checkout recovery agent
  classifier.py    # Dispute intake + Oracle routing
  evidence.py      # Evidence assembly + PayPal multipart submission
  briefing.py      # Morning brief generator
  watcher.py       # FastAPI webhook server + cron endpoints
  invoice.py       # Invoice follow-up
  config.py        # Pre-authorization thresholds (configurable per merchant)

oracle/
  decision.py      # Settlement EV engine — the economic brain

scripts/
  demo.py               # Full offline demo — run this first
  cron_recovery.py      # Recovery cron entry point (Render)
  cron_morning_brief.py # Morning brief cron entry point (Render)

render.yaml        # Render deployment config
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
