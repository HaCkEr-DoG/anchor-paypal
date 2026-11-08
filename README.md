# Anchor — Autonomous PayPal Dispute & Recovery Agent

> **PayPal AI Hackathon 2026**
> Anchor helps merchants make evidence-aware dispute decisions and complete buyer-approved PayPal orders that were never captured.

**[Live dashboard →](https://anchor-paypal.onrender.com/dashboard)** · [API health](https://anchor-paypal.onrender.com/health)

---

## For Judges — 90-second eval path

The sample decision flow needs no PayPal credentials. A live AI evidence review requires Astropods gateway credentials; live PayPal actions require sandbox credentials and a registered webhook.

| What to try | Where |
|---|---|
| **Live PayPal overview** — see aggregate counts read from the connected sandbox; the sample decision is a separate, no-action walkthrough | [Dashboard](https://anchor-paypal.onrender.com/dashboard) |
| **Settlement Oracle Calculator** — adjust amount / evidence / tier, recalculate | Same page → Settlement Oracle panel |
| **AG Grid disputes table** — grouped columns, floating filters, global search, CSV, row detail | Same page → Disputes tab |
| **Merchant ROI Calculator** — edit assumptions and see annual value estimate | Same page → Merchant ROI Calculator |
| **API collection** — import the committed Postman v2.1 collection | [Postman collection JSON](postman_collection.json) |
| **Sponsor integrations** | Same page → APIMatic / Channel3 / Render / Astropods panels |

**Sponsor prizes targeted:**
- **AG Grid** ($5K) — Community 31.3.2 disputes + recovery tables with live data, column grouping, custom renderers
- **Render** (credits) — live deployment auto-deployed from GitHub; production blueprint includes a web service, worker, and PostgreSQL database
- **Best Use of PayPal + AI** — AI summarizes only supplied PayPal/merchant records with source IDs; the deterministic Oracle owns EV math and Guardian owns permitted actions
- **Agentic commerce** — webhook → evidence review → Oracle → Guardian → state-checked PayPal action; approved orders are resumed through idempotent capture

**Submission screenshots to capture:** (1) sample decision with the visible “No PayPal action will be executed” disclosure; (2) Oracle calculator with its assumptions visible; (3) dispute table with filters and row details; (4) ROI calculator with editable assumptions; (5) live `/health` response. The public dashboard is a demo surface; screenshots should not imply that simulated events are production merchant activity.

**Key innovation:** Anchor makes the fight-or-accept decision visible and cost-aware, using a merchant-configurable handling cost and the standard/high-volume fee assumptions shown in the calculator. Those fee assumptions and win probabilities are estimates that merchants should validate against their own PayPal account and outcomes.

---

## What it does

PayPal merchants lose money in two silent ways:

1. **Disputes they should have won** — manually fighting every chargeback is expensive and inconsistent. Most merchants accept everything or fight everything. Neither is right.
2. **Abandoned checkouts** — buyers approve the PayPal payment but the merchant's page errors before capture. The sale disappears.

Anchor pairs two event-driven agents with PayPal APIs. A configured Astropods model reviews supplied dispute records and returns source-cited claims plus missing-evidence suggestions. The Settlement Oracle remains deterministic: AI does not set win probability, compute EV, or authorize a PayPal action.

### Guardian Agent — dispute economics
- Receives `CUSTOMER.DISPUTE.CREATED` webhooks instantly
- Runs the **Settlement Oracle**: calculates EV(fight) vs EV(accept) using PayPal's two-tier dispute fee model ($15 refundable vs $30 non-refundable at high-volume)
- Checks PayPal's live **HATEOAS action links** before choosing an action; it does not assume a stage from a local timer
- Uses the dispute's current HATEOAS links to choose among actions PayPal exposes; a buyer message is sent only when `send-message` is permitted
- Autonomously fights high-EV disputes, accepts low-EV ones, asks the merchant for decisions in between
- Approval links are **HMAC-SHA256 signed**, bound to the specific dispute, expire in 48h
- When configured, Astropods reviews a PII-minimized evidence pack and returns claims only when they cite supplied source IDs; model output never becomes submitted evidence

### Recovery Agent — abandoned checkout recovery
- Receives `CHECKOUT.ORDER.APPROVED` webhooks (buyer authorized, merchant hasn't captured)
- Cron every 15 minutes: finds approved-but-uncaptured orders older than 30 minutes
- Re-fetches `GET /v2/checkout/orders/{id}` and proceeds only when PayPal explicitly reports `APPROVED`
- Resumes the existing checkout through `POST /v2/checkout/orders/{id}/capture` with a stable `PayPal-Request-Id`
- Unknown or unexpected states stop for review; a completed capture webhook closes the recovery record

**Demo boundary:** the overview reads aggregate counts from PayPal's connected sandbox and never returns case identifiers or buyer data. Dashboard scenarios are labeled samples. They run the deterministic Oracle and, if configured, a live Astropods evidence-review call against sample records. They never call PayPal or submit evidence. Live PayPal actions require configured sandbox credentials and a registered webhook.

### Morning Brief
- The app's APScheduler can prepare a morning brief at 08:00 UTC while the web process is running
- No dashboard to open. No login required. It reports to you.

---

## Integrations

| Integration | Role |
|---|---|
| **PayPal API** | Disputes v1, Orders v2, Webhooks, OAuth2; recovery resumes the approved order capture |
| **Settlement Oracle** | Closed-form EV engine: `P(win) × amount − effective_cost` with evidence scoring |
| **AG Grid Community 31.3.2** | Disputes and recoveries tables: grouped columns, filters, status bar, export |
| **Bryntum Scheduler 6.0.7** | Timeline view using CrudManager data format |
| **APIMatic** | PayPal endpoint and parameter context used by Guardian before API actions |
| **Channel3** | Optional product context; it is not submitted as evidence without merchant verification |
| **Postman Collection v2.1** | Importable API examples in [`postman_collection.json`](postman_collection.json) |
| **Render** | FastAPI demo deployment; deploy status requires Render API credentials |
| **Astropods** | Optional live model gateway for source-cited dispute evidence review; no authority over EV or payment actions |
| **Kernel** | Optional browser-based carrier tracking capture |

---

## Built With

- **PayPal APIs** — Disputes v1, Orders v2, Webhooks, OAuth2
- **Settlement Oracle** — closed-form expected-value engine with evidence scoring and historical win-rate blending
- **FastAPI** + **APScheduler** — webhook server, in-process recovery scan and morning brief
- **AG Grid Community 31.3.2** — disputes and recoveries data grid
- **Bryntum Scheduler 6.0.7** — timeline / Gantt view
- **APIMatic** — deterministic PayPal endpoint and parameter context
- **Channel3** — product graph enrichment for ITEM_DESCRIPTION evidence
- **Postman** — Anchor's full API surface as a shareable collection
- **Render** — free-tier deployment, auto-deploys on GitHub push
- **Astropods** — configured model gateway for evidence review (`ASTRO_GATEWAY_URL`, `ASTRO_GATEWAY_API_KEY`, optional `ANCHOR_AI_MODEL`)
- **Kernel** — stealth browser for carrier tracking
- **Elasticsearch** — action log, dispute history, deduplication

---

## Try it — zero credentials needed

The deterministic Settlement Oracle runs offline. The dashboard sample flow can also invoke Astropods when gateway credentials are configured. Live PayPal actions always require sandbox credentials and a webhook:

```bash
git clone https://github.com/HaCkEr-DoG/anchor-paypal
cd anchor-paypal
pip install -r requirements.txt
python scripts/demo.py
```

Expected output: five dispute scenarios, a HATEOAS walkthrough, Recovery agent flow, and a sample morning brief.

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

### 4. Deploy to Render

**Live demo:** `https://anchor-paypal.onrender.com`

1. Apply the repository `render.yaml` Blueprint. It defines a web service, a background worker, and PostgreSQL.
2. Set the PayPal Sandbox client credentials, webhook ID, merchant ID, and `MERCHANT_ID` in Render.
3. Confirm `/health/ready` is healthy and the web service reports a recent worker heartbeat.
4. Send signed Sandbox dispute and approved-order webhooks, then confirm the outbox, retry behavior, and dashboard records.

The Blueprint uses always-on Starter services and a persistent database. These resources can incur recurring charges; review the current Render pricing before applying the Blueprint. The web process verifies and stores PayPal webhooks. The worker drains durable webhook and approval jobs and runs the recovery, deadline, and morning-brief schedules. Sandbox mode, shadow mode, the autonomous-action kill switch, and recovery opt-in are configured to keep payment actions disabled by default.

For a cost-free dashboard-only demo, keep the current deployment and do not apply the production Blueprint; it cannot provide durable 24/7 webhook processing. [Render documents its Free instance limitations](https://render.com/docs/free).

Merchant endpoints (`/api/stats`, `/api/disputes`, `/api/recoveries`, `/api/paypal/disputes`) and the paid model chat endpoint require the `X-API-Key` header matching `API_SECRET`. Render generates `API_SECRET` from `render.yaml`; keep it private and do not embed it in the public dashboard. For a local setup, set a random `API_SECRET` in `.env` and send it from a trusted admin client.

---

## How the Settlement Oracle works

For every dispute, Anchor calculates:

```
EV(fight)  = P(win) × amount − effective_cost
EV(accept) = $0 incremental recovery (accepting produces no recovery)

Standard tier ($15 refundable on win):
  effective_cost = time_cost + (1 − p_win) × 15
  break_even     = (time_cost + 15) / (amount + 15)

US high-volume tier ($30 non-refundable; eligible merchants have a dispute ratio of at least 1.5% and more than 100 sales transactions in the previous three full calendar months):
  effective_cost = time_cost + 30
  break_even     = (time_cost + 30) / amount
```

A $245 INR dispute with delivery confirmation and neutral seller-history assumptions: **FIGHT** (91.8% model estimate, EV = +$148.68).
An $18.50 INR dispute without tracking: **ACCEPT** (EV(fight) = −$77.47, EV(accept) = $0 incremental recovery).

PayPal's current US criteria use the previous three complete months, and the dispute ratio is amount-based for eligible claim types; unauthorized disputes and billing errors are treated differently. Anchor's fee-tier estimate uses recorded events and may be incomplete for an existing merchant. Confirm the actual fee in the PayPal account before relying on a live decision. [PayPal dispute-rate criteria](https://www.paypal.com/us/cshelp/article/when-is-the-dispute-rate-applied-and-how-is-it-calculated-help350).

**Try it live** on the dashboard at [anchor-paypal.onrender.com](https://anchor-paypal.onrender.com) — the Settlement Oracle panel lets you adjust any input and recalculates in real time.

The model is illustrative. Its base rates and evidence multipliers are assumptions, not a validated prediction model; production use needs merchant-specific calibration and outcome tracking.

### Evidence integrity and merchant controls

Anchor may submit only evidence actually present in merchant records or verified carrier/PayPal data. Catalog descriptions, generated text, or a browser screenshot are not proof of delivery or buyer communications. The agent checks PayPal's current permitted actions and keeps cases pending when required evidence is missing. Merchants remain responsible for reviewing submissions, maintaining accurate records, and following PayPal's Acceptable Use Policy and dispute deadlines. [PayPal Acceptable Use Policy](https://pep.paypal.com/us/legalhub/paypal/acceptableuse-full?country.x=US&locale.x=en_US).

## Illustrative merchant impact

The dashboard's editable ROI calculator makes each assumption visible. With the defaults below, it estimates **about $12,048/year**, not a measured result:

| Input / output | Estimate |
|---|---:|
| Annual GMV / average order value | $500,000 / $80 |
| Approximate annual orders | 6,250 |
| Dispute rate / estimated dispute count | 0.5% / 31.25 |
| Average dispute / amount at risk | $80 / $2,500 |
| Modeled win-rate difference (45% − 8.1%) | $922.50 protected |
| Abandoned checkout rate / recovery rate | 2% / 20% |
| Modeled recovered revenue | $2,000 |
| Review time saved (0.5 h/day × 365 × $50/h) | $9,125 |
| **Illustrative total annual value** | **$12,047.50** |

The number of disputes is calculated as orders × dispute rate. Multiplying GMV by 0.5% gives dollars, not a count of 2,500 disputes. Actual results depend on merchant data, PayPal eligibility, evidence quality, win-rate calibration, and recovery conversion.

## Competitive positioning

Anchor focuses on merchant-configurable economics and PayPal lifecycle actions. Competitors cover overlapping ground: Chargeflow advertises 25% of recovered chargebacks and PayPal inquiry automation; Disputifier advertises 20% of won chargebacks; Chargehound says it supports PayPal among other processors. Avoid assuming these products always fight, lack PayPal support, or lack their own decision logic.

| Dimension | Anchor | Publicly stated alternatives |
|---|---|---|
| Pricing model | Proposed pricing below; not yet commercial | Chargeflow: 25% per recovered chargeback; Disputifier: 20% per won chargeback |
| PayPal scope | PayPal Disputes, Orders, and Webhooks APIs in this prototype | Chargeflow advertises PayPal inquiry automation; Chargehound lists PayPal support |
| Decision economics | Explicit EV calculator with editable fees and cost assumptions | Verify each vendor's current contract and workflow before comparing decisions |
| Evidence | PayPal dispute data plus optional Channel3 / carrier evidence paths | Vendor-specific integrations and evidence pipelines vary |
| Best fit | Teams that want to inspect and tune their PayPal decision policy | Merchants seeking an established managed service and broader integrations |

Illustrative micro-dispute example: on an $18.50 recovery, a hypothetical 25% success fee is $4.63. Adding a $15 dispute fee yields $19.63 of fees against $18.50 recovered, a net loss of $1.13 before operating costs. This is a scenario calculation, not a claim that any provider's fees or PayPal fee treatment applies to every case. [Chargeflow pricing](https://www.chargeflow.io/pricing) · [Disputifier pricing](https://www.disputifier.com/pricing) · [Chargehound supported processors](https://www.chargehound.com/support).

## Pricing hypothesis

For a commercial product, start with a low base subscription plus a capped success fee on verified incremental recovery. A pure percentage of EV is difficult to audit because EV is modeled rather than realized; a flat $99 / $499 tier is easier to explain but may not fit merchants with very different dispute volumes. Validate willingness to pay with merchants before setting prices. An enterprise API for PSPs is a later channel, not a current capability.

## Architecture and scale

Today, FastAPI handles webhooks and dashboard APIs; the process-local scheduler runs recovery and briefing jobs; Elasticsearch stores action history and configuration; PayPal remains the source of truth for dispute and order state. In-memory webhook deduplication is suitable only for a single instance. At higher volume, move deduplication and scheduled work to durable shared storage and a queue, make workers idempotent, partition merchant data, and add per-merchant PayPal rate limiting. Fee-tier inference and win-rate blending require complete, labeled history before they should influence automated actions.

## Roadmap

Next: add durable queues and complete merchant outcome logging; then deliver morning briefs by Slack/email and show calibrated historical win rates. After that, add a second processor integration and a multi-merchant API for PSPs. Use accumulated, permissioned outcomes to validate or replace the hand-set win-rate priors only after enough representative data is available.

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
  paypal_client.py   # PayPal REST API client (OAuth2, Disputes, Orders)
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

## Evidence and API references

- [PayPal Disputes API guide](https://developer.paypal.com/api/disputes/) — lifecycle, HATEOAS links, messages, evidence
- [PayPal current dispute-rate criteria](https://www.paypal.com/us/cshelp/article/when-is-the-dispute-rate-applied-and-how-is-it-calculated-help350)
- [PayPal Acceptable Use Policy](https://pep.paypal.com/us/legalhub/paypal/acceptableuse-full?country.x=US&locale.x=en_US)
- [Render Free service limits](https://render.com/docs/free)
- [Chargeflow pricing](https://www.chargeflow.io/pricing)
- [Disputifier pricing](https://www.disputifier.com/pricing)
- [Chargehound supported processors](https://www.chargehound.com/support)
