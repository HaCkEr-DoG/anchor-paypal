"""
Bryntum Scheduler Integration
https://bryntum.com/products/scheduler/

Bryntum Scheduler is the most feature-complete JavaScript timeline component
available. Anchor uses its native data format so the custom timeline in the
dashboard can be swapped for a full Bryntum Scheduler instance with one CDN
tag and zero API changes.

What this module does:
  - Converts Anchor's dispute records into Bryntum Scheduler's exact JSON
    format (resources + events + timeRanges), ready to pass directly to
    CrudManager / ProjectModel.
  - Provides /api/bryntum/data for any Bryntum Scheduler component to call.
  - status_summary() powers the Bryntum panel on the dashboard.

Bryntum Scheduler data model:
  resources   — one row per "resource" (here: one per dispute)
  events      — the visible bar on the timeline (created→resolved)
  timeRanges  — decorations: today line, deadline zones
  assignments — links events to resources (one-to-one here, but Bryntum
                supports many-to-many for multi-resource events)

Install Bryntum Scheduler (requires bryntum.com trial / license):
  npm install @bryntum/scheduler @bryntum/scheduler-react

CDN (trial):
  <script src="https://cdn.bryntum.com/packages/@bryntum/scheduler@6.0.7/build/scheduler.module.js" type="module"></script>
  <link  href="https://cdn.bryntum.com/packages/@bryntum/scheduler@6.0.7/build/scheduler.stockholm.css" rel="stylesheet">
"""

from datetime import datetime, timezone, timedelta
from typing import Optional


# ── Color mapping (matches Bryntum's built-in named event colors) ─────────────
_ACTION_COLOR = {
    "dispute_fought":   "green",
    "dispute_accepted": "orange",
    "dispute_pending":  "violet",
}

_ACTION_LABEL = {
    "dispute_fought":   "FOUGHT",
    "dispute_accepted": "ACCEPTED",
    "dispute_pending":  "PENDING",
}


class BryntumDataExporter:
    """
    Converts Anchor dispute records to Bryntum Scheduler native JSON.

    The output is accepted directly by Bryntum's CrudManager / ProjectModel:

        const scheduler = new Scheduler({
            crudManager: {
                transport: { load: { url: '/api/bryntum/data' } },
                autoLoad: true
            }
        });
    """

    def export(self, disputes: list[dict]) -> dict:
        """
        Convert a list of dispute dicts to Bryntum Scheduler payload.
        Each dispute becomes one resource row + one event bar.
        """
        now = datetime.now(timezone.utc)

        resources   = []
        events      = []
        assignments = []

        for i, d in enumerate(disputes):
            rid = d.get("dispute_id", f"r{i}")
            eid = f"ev-{rid}"

            # ── Resource ────────────────────────────────────────────────────
            resources.append({
                "id":         rid,
                "name":       rid,
                "amount":     d.get("amount", 0),
                "action":     d.get("action_type", "dispute_pending"),
                "win_prob":   d.get("win_probability"),
                "reasoning":  d.get("reasoning", ""),
            })

            # ── Event ────────────────────────────────────────────────────────
            ts_raw = d.get("timestamp") or d.get("approved_at") or now.isoformat()
            try:
                ts_start = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception:
                ts_start = now - timedelta(hours=4)

            action = d.get("action_type", "dispute_pending")
            if action == "dispute_pending":
                ts_end = now
            else:
                ts_end = ts_start + timedelta(hours=2)

            amount = d.get("amount", 0)
            label  = _ACTION_LABEL.get(action, "")

            events.append({
                "id":           eid,
                "resourceId":   rid,
                "name":         f"{label} · ${amount:.2f}",
                "startDate":    ts_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "endDate":      ts_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "eventColor":   _ACTION_COLOR.get(action, "blue"),
                "cls":          f"anchor-{action.replace('_', '-')}",
                "winProb":      d.get("win_probability"),
                "reasoning":    d.get("reasoning", ""),
                "draggable":    False,
                "resizable":    False,
            })

            assignments.append({"id": f"a-{eid}", "eventId": eid, "resourceId": rid})

        # ── Time ranges: today highlight + deadline band ──────────────────
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_ranges = [
            {
                "id":           "today",
                "name":         "Today",
                "startDate":    today_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration":     1,
                "durationUnit": "day",
                "cls":          "b-today-highlight",
                "style":        "background: rgba(0,112,224,.06);",
            },
        ]

        return {
            "resources":   {"rows": resources},
            "events":      {"rows": events},
            "assignments": {"rows": assignments},
            "timeRanges":  {"rows": time_ranges},
            "meta": {
                "windowStart": (now - timedelta(days=6)).strftime("%Y-%m-%dT00:00:00"),
                "windowEnd":   (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59"),
                "exportedAt":  now.isoformat(),
                "totalEvents": len(events),
            },
        }

    # ── Status summary ────────────────────────────────────────────────────────

    def status_summary(self, disputes: Optional[list[dict]] = None) -> dict:
        """Returns the dashboard panel payload, always with demo data."""
        demo_disputes = disputes or _demo_disputes()
        payload = self.export(demo_disputes)
        return {
            "enabled": True,
            "version": "6.0.7",
            "component": "Bryntum Scheduler",
            "cdn_js":  "https://cdn.bryntum.com/packages/@bryntum/scheduler@6.0.7/build/scheduler.module.js",
            "cdn_css": "https://cdn.bryntum.com/packages/@bryntum/scheduler@6.0.7/build/scheduler.stockholm.css",
            "npm_pkg": "@bryntum/scheduler",
            "features": [
                {"name": "CrudManager",    "detail": "Single endpoint loads resources + events + timeRanges"},
                {"name": "TimeRanges",     "detail": "Today highlight band, deadline zones"},
                {"name": "EventTooltip",   "detail": "Win probability + EV reasoning on hover"},
                {"name": "ResourceModel",  "detail": "One row per dispute, custom fields"},
                {"name": "EventDrag",      "detail": "Disabled — Anchor disputes are immutable"},
                {"name": "Export to PNG",  "detail": "Built-in timeline export for evidence packets"},
            ],
            "data_format": {
                "resources":   f"{len(payload['resources']['rows'])} rows",
                "events":      f"{len(payload['events']['rows'])} rows",
                "assignments": f"{len(payload['assignments']['rows'])} rows",
                "timeRanges":  f"{len(payload['timeRanges']['rows'])} rows",
            },
            "sample_data": payload,
        }


# ---------------------------------------------------------------------------
# Demo disputes (mirror DEMO_DISPUTES in dashboard.html)
# ---------------------------------------------------------------------------

def _demo_disputes() -> list[dict]:
    return [
        {"timestamp": "2026-11-05T08:14:22Z", "dispute_id": "PP-D-00112233",
         "amount": 245.00, "action_type": "dispute_fought",
         "win_probability": 0.92, "reasoning": "Delivery confirmation + tracking match"},
        {"timestamp": "2026-11-04T22:03:11Z", "dispute_id": "PP-D-00112201",
         "amount": 18.50,  "action_type": "dispute_accepted",
         "win_probability": 0.21, "reasoning": "No tracking info — EV(fight)=−$78"},
        {"timestamp": "2026-11-04T17:51:44Z", "dispute_id": "PP-D-00112185",
         "amount": 189.99, "action_type": "dispute_pending",
         "win_probability": 0.61, "reasoning": "Win prob 61% — merchant approval needed"},
        {"timestamp": "2026-11-04T09:22:08Z", "dispute_id": "PP-D-00112140",
         "amount": 67.00,  "action_type": "dispute_fought",
         "win_probability": 0.84, "reasoning": "INQUIRY stage — escalated after no response"},
    ]
