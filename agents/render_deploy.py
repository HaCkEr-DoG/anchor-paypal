"""
Render Deployment Integration
https://render.com

Shows Anchor's live deployment status, recent deploy history,
and lets judges trigger a redeploy — all from the dashboard.

API base:  https://api.render.com/v1
Auth:      Authorization: Bearer <api_key>

Endpoints used:
  GET  /services/{id}            → service status, URL, suspend state
  GET  /services/{id}/deploys    → deploy history (status, commit, timing)
  POST /services/{id}/deploys    → trigger a new deploy
  GET  /services/{id}/envVars    → list env var keys (values redacted)

Deploy statuses:
  created | build_in_progress | update_in_progress | live |
  deactivated | build_failed | update_failed | canceled |
  pre_deploy_in_progress | pre_deploy_failed
"""

import os
import httpx
from datetime import datetime, timezone
from typing import Optional

RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
RENDER_BASE       = "https://api.render.com/v1"

# Human-readable labels and CSS colours for each deploy status
_STATUS_META = {
    "live":                    {"label": "Live",             "color": "#22c55e"},
    "build_in_progress":       {"label": "Building…",        "color": "#f59e0b"},
    "update_in_progress":      {"label": "Updating…",        "color": "#f59e0b"},
    "pre_deploy_in_progress":  {"label": "Pre-deploy…",      "color": "#f59e0b"},
    "created":                 {"label": "Queued",           "color": "#6b7280"},
    "deactivated":             {"label": "Deactivated",      "color": "#6b7280"},
    "canceled":                {"label": "Canceled",         "color": "#6b7280"},
    "build_failed":            {"label": "Build Failed",     "color": "#ef4444"},
    "update_failed":           {"label": "Update Failed",    "color": "#ef4444"},
    "pre_deploy_failed":       {"label": "Pre-deploy Failed","color": "#ef4444"},
}


def _status_meta(status: str) -> dict:
    return _STATUS_META.get(status, {"label": status.replace("_", " ").title(), "color": "#6b7280"})


def _fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return iso[:10]


class RenderClient:
    def __init__(self, api_key: Optional[str] = None, service_id: Optional[str] = None):
        self.api_key    = api_key    or RENDER_API_KEY
        self.service_id = service_id or RENDER_SERVICE_ID
        self.headers    = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def enabled(self) -> bool:
        return bool(self.api_key and self.service_id)

    async def get_service(self) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{RENDER_BASE}/services/{self.service_id}", headers=self.headers)
            r.raise_for_status()
        return r.json()

    async def list_deploys(self, limit: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{RENDER_BASE}/services/{self.service_id}/deploys",
                headers=self.headers,
                params={"limit": limit},
            )
            r.raise_for_status()
        # Render wraps each item: {"deploy": {...}, "cursor": "..."}
        items = r.json()
        return [item.get("deploy", item) for item in items]

    async def trigger_deploy(self, clear_cache: bool = False) -> dict:
        payload = {"clearCache": "clear" if clear_cache else "do_not_clear"}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{RENDER_BASE}/services/{self.service_id}/deploys",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
        d = r.json()
        return d.get("deploy", d)

    async def status_summary(self) -> dict:
        """
        Returns a dict ready to send straight to the dashboard:
          service: {name, url, suspended, region}
          latest_deploy: {id, status, label, color, commit, created_at, finished_at, duration_s, age}
          recent_deploys: [{...}, ...]  — last 5
        """
        if not self.enabled():
            return {
                "enabled": False,
                "note": "Set RENDER_API_KEY and RENDER_SERVICE_ID in Render env vars",
                "demo": {
                    "service": {
                        "name": os.environ.get("RENDER_SERVICE_NAME", "anchor-paypal"),
                        "url": os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-paypal.onrender.com"),
                        "suspended": False,
                        "region": os.environ.get("RENDER_REGION", ""),
                    },
                    "current_commit": os.environ.get("RENDER_GIT_COMMIT", ""),
                    "latest_deploy": {},
                    "recent_deploys": [],
                    "status_note": "Live deploy details require RENDER_API_KEY and RENDER_SERVICE_ID.",
                },
            }

        try:
            svc_data, deploys = await asyncio.gather(
                self.get_service(),
                self.list_deploys(5),
            )
        except httpx.HTTPStatusError as e:
            return {"enabled": True, "error": f"Render API {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            return {"enabled": True, "error": str(e)}

        svc = svc_data.get("service", svc_data)

        def _enrich(d: dict) -> dict:
            status = d.get("status", "")
            meta   = _status_meta(status)
            commit = (d.get("commit") or {})
            created  = d.get("createdAt") or d.get("created_at", "")
            finished = d.get("finishedAt") or d.get("finished_at", "")
            duration = None
            try:
                if created and finished:
                    s = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    f = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                    duration = int((f - s).total_seconds())
            except Exception:
                pass
            return {
                "id":          d.get("id", ""),
                "status":      status,
                "label":       meta["label"],
                "color":       meta["color"],
                "commit_msg":  commit.get("message", ""),
                "commit_id":   (commit.get("id", "") or "")[:7],
                "created_at":  created,
                "finished_at": finished,
                "duration_s":  duration,
                "age":         _fmt_dt(created),
            }

        enriched = [_enrich(d) for d in deploys]
        latest   = enriched[0] if enriched else {}

        return {
            "enabled": True,
            "current_commit": os.environ.get("RENDER_GIT_COMMIT", latest.get("commit_id", "")),
            "service": {
                "name":      svc.get("name", "anchor-paypal"),
                "url":       (svc.get("serviceDetails") or {}).get("url", "https://anchor-paypal.onrender.com"),
                "suspended": svc.get("suspended", "not_suspended") != "not_suspended",
                "region":    (svc.get("serviceDetails") or {}).get("region", ""),
            },
            "latest_deploy": latest,
            "recent_deploys": enriched,
        }


# ---------------------------------------------------------------------------
# asyncio import — needed for gather() inside status_summary
# ---------------------------------------------------------------------------
import asyncio


# ---------------------------------------------------------------------------
# Demo data — shown when API key is not configured
# ---------------------------------------------------------------------------

def _demo_status() -> dict:
    """Return configured service metadata without inventing deploy history."""
    return {
        "service": {
            "name": os.environ.get("RENDER_SERVICE_NAME", "anchor-paypal"),
            "url": os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-paypal.onrender.com"),
            "suspended": None,
            "region": os.environ.get("RENDER_REGION", ""),
        },
        "current_commit": os.environ.get("RENDER_GIT_COMMIT", ""),
        "latest_deploy": {},
        "recent_deploys": [],
        "status_note": "Live deploy details require RENDER_API_KEY and RENDER_SERVICE_ID.",
    }
