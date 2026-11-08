"""
AG Grid Integration
https://ag-grid.com

AG Grid Community 31.3.2 powers the Disputes and Recoveries tables in
Anchor's dashboard. This module exports dispute and recovery records in
AG Grid's exact rowData + columnDefs format, so any AG Grid component
(React, Angular, Vue, or vanilla JS) can consume the endpoint directly:

    const grid = createGrid(el, {
        columnDefs: data.columnDefs,
        rowData:    data.rowData,
    });

or via the grid API after mount:

    gridApi.setGridOption('rowData', data.rowData);

Features used:
  columnDefs          typed column definitions with width, sort, type
  valueFormatter      date, currency, percentage cell formatting
  cellRenderer        custom HTML chips for action type and status
  tooltipField        full reasoning text on hover
  animateRows         smooth row-add / row-update animation
  resizable/sortable  per-column, all enabled
  filter              per-column text/number/set filter
  CSV export          gridApi.exportDataAsCsv()
  statusBar           row count display
"""

from datetime import datetime, timezone
from typing import Optional


VERSION = "31.3.2"
CDN_URL = f"https://cdn.jsdelivr.net/npm/ag-grid-community@{VERSION}/dist/ag-grid-community.min.js"


# ── Column definitions (mirror dashboard.html DISPUTE_COLS / RECOVERY_COLS) ──

DISPUTE_COLDEFS = [
    {"field": "timestamp",          "headerName": "Date / Time",     "width": 160,
     "sort": "desc", "type": "dateColumn"},
    {"field": "dispute_id",         "headerName": "Dispute ID",      "width": 170,
     "cellStyle": {"fontFamily": "monospace", "fontSize": "12px"}},
    {"field": "amount",             "headerName": "Amount",          "width": 100,
     "type": "numericColumn"},
    {"field": "action_type",        "headerName": "Action",          "width": 130},
    {"field": "win_probability",    "headerName": "Win Prob",        "width": 100,
     "type": "numericColumn"},
    {"field": "outcome",            "headerName": "Outcome",         "width": 160},
    {"field": "requires_attention", "headerName": "⚑",              "width": 60},
    {"field": "reasoning",          "headerName": "Oracle Reasoning","flex": 1,
     "tooltipField": "reasoning"},
]

RECOVERY_COLDEFS = [
    {"field": "approved_at",       "headerName": "Approved At",   "width": 160,
     "sort": "desc", "type": "dateColumn"},
    {"field": "order_id",          "headerName": "Order ID",      "width": 200,
     "cellStyle": {"fontFamily": "monospace", "fontSize": "12px"}},
    {"field": "buyer_email",       "headerName": "Buyer Email",   "width": 200},
    {"field": "amount",            "headerName": "Amount",        "width": 100,
     "type": "numericColumn"},
    {"field": "currency",          "headerName": "Currency",      "width": 90},
    {"field": "recovery_attempts", "headerName": "Attempts",      "width": 100,
     "type": "numericColumn"},
    {"field": "status",            "headerName": "Status",        "width": 160},
]

DEFAULT_COL_DEF = {
    "resizable": True,
    "sortable": True,
    "filter": True,
    "suppressMovable": False,
}

GRID_OPTIONS = {
    "rowHeight": 44,
    "headerHeight": 40,
    "animateRows": True,
    "suppressCellFocus": True,
    "tooltipShowDelay": 300,
    "defaultColDef": DEFAULT_COL_DEF,
}


class AGGridExporter:
    """
    Converts Anchor's dispute + recovery records to AG Grid payloads.

    Usage:
        exporter = AGGridExporter()
        payload  = exporter.export_disputes(disputes)
        # payload['columnDefs'] + payload['rowData'] → pass to createGrid()
    """

    def export_disputes(self, disputes: list[dict]) -> dict:
        rows = []
        for d in disputes:
            rows.append({
                "timestamp":          d.get("timestamp", ""),
                "dispute_id":         d.get("dispute_id", ""),
                "amount":             d.get("amount", 0),
                "action_type":        _fmt_action(d.get("action_type", "")),
                "win_probability":    _fmt_pct(d.get("win_probability")),
                "outcome":            (d.get("outcome") or "").replace("_", " "),
                "requires_attention": d.get("requires_attention", False),
                "reasoning":          d.get("reasoning", ""),
            })
        return {
            "columnDefs":    DISPUTE_COLDEFS,
            "rowData":       rows,
            "defaultColDef": DEFAULT_COL_DEF,
            "gridOptions":   GRID_OPTIONS,
        }

    def export_recoveries(self, recoveries: list[dict]) -> dict:
        rows = []
        for r in recoveries:
            rows.append({
                "approved_at":       r.get("approved_at", ""),
                "order_id":          r.get("order_id", ""),
                "buyer_email":       r.get("buyer_email", ""),
                "amount":            r.get("amount", 0),
                "currency":          r.get("currency", "USD"),
                "recovery_attempts": r.get("recovery_attempts", 0),
                "status":            r.get("status", ""),
            })
        return {
            "columnDefs":    RECOVERY_COLDEFS,
            "rowData":       rows,
            "defaultColDef": DEFAULT_COL_DEF,
            "gridOptions":   GRID_OPTIONS,
        }

    # ── Status summary ────────────────────────────────────────────────────────

    def status_summary(
        self,
        disputes: Optional[list[dict]] = None,
        recoveries: Optional[list[dict]] = None,
    ) -> dict:
        demo_disputes   = _demo_disputes() if disputes is None else disputes
        demo_recoveries = _demo_recoveries() if recoveries is None else recoveries
        d_payload = self.export_disputes(demo_disputes)
        r_payload = self.export_recoveries(demo_recoveries)
        return {
            "enabled": True,
            "demo": disputes is None and recoveries is None,
            "version": VERSION,
            "cdn_url": CDN_URL,
            "npm_pkg": "ag-grid-community",
            "grids": {
                "disputes":   {"rows": len(d_payload["rowData"]), "columns": len(DISPUTE_COLDEFS)},
                "recoveries": {"rows": len(r_payload["rowData"]), "columns": len(RECOVERY_COLDEFS)},
            },
            "features": [
                {"name": "columnDefs",      "detail": "Typed columns — date, numeric, text, custom width"},
                {"name": "valueFormatter",  "detail": "Date, currency (USD), win-probability % cells"},
                {"name": "cellRenderer",    "detail": "Action-type chips, attention flag, status badges"},
                {"name": "tooltipField",    "detail": "Full Oracle reasoning text on row hover"},
                {"name": "animateRows",     "detail": "Smooth row transitions on data refresh"},
                {"name": "CSV Export",      "detail": "gridApi.exportDataAsCsv() — built-in download"},
                {"name": "filter",          "detail": "Per-column text / number / set filters"},
                {"name": "sort",            "detail": "Multi-column sort; timestamp desc by default"},
            ],
            "sample": {
                "disputes_coldefs":   DISPUTE_COLDEFS[:3],
                "disputes_row0":      d_payload["rowData"][0] if d_payload["rowData"] else {},
                "recoveries_coldefs": RECOVERY_COLDEFS[:3],
                "recoveries_row0":    r_payload["rowData"][0] if r_payload["rowData"] else {},
            },
        }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_action(a: str) -> str:
    return {"dispute_fought": "FOUGHT", "dispute_accepted": "ACCEPTED",
            "dispute_pending": "PENDING"}.get(a, a)


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:.0f}%"


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

def _demo_disputes() -> list[dict]:
    return [
        {"timestamp": "2026-11-05T08:14:22Z", "dispute_id": "PP-D-00112233",
         "amount": 245.00, "action_type": "dispute_fought",
         "win_probability": 0.92, "outcome": "evidence_submitted",
         "requires_attention": False,
         "reasoning": "Delivery confirmation + tracking match buyer address"},
        {"timestamp": "2026-11-04T22:03:11Z", "dispute_id": "PP-D-00112201",
         "amount": 18.50,  "action_type": "dispute_accepted",
         "win_probability": 0.21, "outcome": "accepted",
         "requires_attention": False,
         "reasoning": "No tracking info — EV(fight)=−$78, EV(accept)=−$18.50"},
        {"timestamp": "2026-11-04T17:51:44Z", "dispute_id": "PP-D-00112185",
         "amount": 189.99, "action_type": "dispute_pending",
         "win_probability": 0.61, "outcome": "awaiting_approval",
         "requires_attention": True,
         "reasoning": "Win prob 61% — merchant approval needed"},
        {"timestamp": "2026-11-04T09:22:08Z", "dispute_id": "PP-D-00112140",
         "amount": 67.00,  "action_type": "dispute_fought",
         "win_probability": 0.84, "outcome": "evidence_submitted",
         "requires_attention": False,
         "reasoning": "INQUIRY stage — escalated after no response"},
    ]


def _demo_recoveries() -> list[dict]:
    return [
        {"approved_at": "2026-11-05T07:45:00Z", "order_id": "ORD-20261105-0042",
         "buyer_email": "alice@example.com", "amount": 134.00, "currency": "USD",
         "recovery_attempts": 1, "status": "completed_externally"},
        {"approved_at": "2026-11-04T18:12:33Z", "order_id": "ORD-20261104-0031",
         "buyer_email": "bob@example.com",   "amount": 89.50,  "currency": "USD",
         "recovery_attempts": 1, "status": "pending_recovery"},
    ]
