"""
Postman API Integration
https://postman.com

Generates and publishes Anchor's complete API collection to Postman
so any developer or judge can import it in one click and start
testing the live webhook server immediately.

API base:  https://api.postman.com
Auth:      X-API-Key: <key>  (get one at postman.com → Settings → API Keys)

Collection v2.1 schema:
  https://schema.getpostman.com/json/collection/v2.1.0/collection.json

Endpoints used:
  GET  /workspaces              → list workspaces to find workspace_id
  POST /collections             → create new collection
  PUT  /collections/{uid}       → update existing collection
  GET  /collections/{uid}       → retrieve published collection
"""

import json
import os
import httpx
from typing import Optional

POSTMAN_API_KEY   = os.environ.get("POSTMAN_API_KEY", "")
POSTMAN_BASE      = "https://api.postman.com"
COLLECTION_UID_ENV = "POSTMAN_COLLECTION_UID"   # set after first publish


# ---------------------------------------------------------------------------
# Collection definition — Anchor's full API surface as Postman Collection v2.1
# ---------------------------------------------------------------------------

def build_collection(base_url: str = "https://anchor-paypal.onrender.com") -> dict:
    """
    Returns Anchor's complete API as a Postman Collection v2.1 dict.
    Groups: Health, Dashboard, Disputes, Recovery, APIMatic, Channel3, Webhooks.
    """
    def header_auth():
        return []   # Bearer token injected via collection variable {{access_token}}

    def json_header():
        return [{"key": "Content-Type", "value": "application/json"}]

    def url(path: str, variables: Optional[list] = None) -> dict:
        raw = f"{{{{base_url}}}}{path}"
        parts = path.lstrip("/").split("/")
        u = {"raw": raw, "host": ["{{base_url}}"], "path": parts}
        if variables:
            u["variable"] = variables
        return u

    collection = {
        "info": {
            "name": "Anchor — PayPal Dispute & Recovery Agent",
            "description": (
                "Anchor autonomously handles PayPal disputes (Guardian Agent) and "
                "recovers abandoned checkouts (Recovery Agent). "
                "PayPal AI Hackathon 2026.\n\n"
                "Set the `base_url` variable to your Render deployment URL.\n"
                "Live demo: https://anchor-paypal.onrender.com"
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "_postman_id": "anchor-paypal-hackathon-2026",
        },
        "variable": [
            {"key": "base_url", "value": base_url, "type": "string"},
        ],
        "item": [
            # ── Health ──────────────────────────────────────────────────────
            {
                "name": "Health",
                "item": [
                    {
                        "name": "Health check",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/health"),
                            "description": "Returns {\"status\":\"ok\"} when the server is up.",
                        },
                        "response": [{
                            "name": "200 OK",
                            "status": "OK",
                            "code": 200,
                            "header": [{"key": "Content-Type", "value": "application/json"}],
                            "body": "{\"status\":\"ok\"}",
                        }],
                    }
                ],
            },

            # ── Dashboard ───────────────────────────────────────────────────
            {
                "name": "Dashboard",
                "item": [
                    {
                        "name": "Open dashboard",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/dashboard"),
                            "description": "Full AG Grid + Bryntum Scheduler dashboard. Open in browser.",
                        },
                    },
                    {
                        "name": "Stats — header cards",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/stats"),
                            "description": "Aggregate dispute/recovery stats from Elasticsearch. Returns {demo:true} when ES not configured.",
                        },
                    },
                    {
                        "name": "Disputes — AG Grid rows",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/disputes"),
                            "description": "Returns up to 200 recent dispute actions. Each row drives the AG Grid disputes table and Bryntum timeline.",
                        },
                    },
                    {
                        "name": "Recoveries — AG Grid rows",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/recoveries"),
                            "description": "Returns up to 200 abandoned checkout recovery records.",
                        },
                    },
                ],
            },

            # ── Dispute Approval ────────────────────────────────────────────
            {
                "name": "Dispute Approval",
                "item": [
                    {
                        "name": "Approve dispute fight",
                        "request": {
                            "method": "POST",
                            "header": [
                                {"key": "Authorization", "value": "Bearer {{approval_token}}"},
                            ],
                            "url": url(
                                "/approve/:dispute_id",
                                [{"key": "dispute_id", "value": "PP-D-00112233"}],
                            ),
                            "description": (
                                "Merchant approves a pending dispute fight. "
                                "Token is HMAC-SHA256 signed, dispute-bound, expires in 48h. "
                                "Guardian sends this link in Slack when manual approval is needed."
                            ),
                        },
                    }
                ],
            },

            # ── Cron / Scheduler ────────────────────────────────────────────
            {
                "name": "Scheduler",
                "item": [
                    {
                        "name": "Trigger recovery check",
                        "request": {
                            "method": "POST",
                            "header": json_header(),
                            "url": url("/recovery-check"),
                            "description": "Manually trigger the recovery cron (runs automatically every 15 min via APScheduler).",
                        },
                    },
                    {
                        "name": "Trigger morning brief",
                        "request": {
                            "method": "POST",
                            "header": json_header(),
                            "url": url("/morning-brief"),
                            "description": "Manually trigger the morning brief (runs automatically at 08:00 UTC).",
                        },
                    },
                ],
            },

            # ── APIMatic ────────────────────────────────────────────────────
            {
                "name": "APIMatic Context Plugin",
                "item": [
                    {
                        "name": "Get PayPal API context",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/apimatic/context"),
                            "description": "Returns the APIMatic-generated PayPal Disputes API context document loaded by the Guardian agent before every API call.",
                        },
                    },
                    {
                        "name": "Lookup endpoint — accept",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/apimatic/lookup/accept"),
                            "description": "Resolves the correct PayPal endpoint, method, and required fields for the accept-claim action.",
                        },
                    },
                    {
                        "name": "Lookup endpoint — provide_evidence",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/apimatic/lookup/provide_evidence"),
                        },
                    },
                    {
                        "name": "Lookup endpoint — send_message",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/apimatic/lookup/send_message"),
                        },
                    },
                ],
            },

            # ── Channel3 ────────────────────────────────────────────────────
            {
                "name": "Channel3 Enrichment",
                "item": [
                    {
                        "name": "Enrich dispute with product data",
                        "request": {
                            "method": "POST",
                            "header": json_header(),
                            "url": url("/api/channel3/enrich"),
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({
                                    "item_description": "Nike Air Max 90",
                                    "amount": 89.99,
                                    "dispute_id": "PP-D-00112233",
                                }, indent=2),
                                "options": {"raw": {"language": "json"}},
                            },
                            "description": (
                                "Looks up the disputed product in Channel3's 100M+ product graph. "
                                "Returns canonical title, brand, price, and formatted "
                                "ITEM_DESCRIPTION evidence text for PayPal submission."
                            ),
                        },
                    }
                ],
            },

            # ── Postman ─────────────────────────────────────────────────────
            {
                "name": "Postman Collection",
                "item": [
                    {
                        "name": "Download collection JSON",
                        "request": {
                            "method": "GET",
                            "header": [],
                            "url": url("/api/postman/collection"),
                            "description": "Downloads this collection as Postman Collection v2.1 JSON.",
                        },
                    }
                ],
            },

            # ── Webhooks ────────────────────────────────────────────────────
            {
                "name": "PayPal Webhooks (inbound)",
                "item": [
                    {
                        "name": "Webhook receiver",
                        "request": {
                            "method": "POST",
                            "header": [
                                {"key": "PayPal-Transmission-Id", "value": "{{transmission_id}}"},
                                {"key": "PayPal-Transmission-Time", "value": "{{transmission_time}}"},
                                {"key": "PayPal-Cert-Url", "value": "{{cert_url}}"},
                                {"key": "PayPal-Transmission-Sig", "value": "{{transmission_sig}}"},
                                {"key": "PayPal-Auth-Algo", "value": "SHA256withRSA"},
                                {"key": "Content-Type", "value": "application/json"},
                            ],
                            "url": url("/webhooks/paypal"),
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({
                                    "id": "WH-XXXX",
                                    "event_type": "CUSTOMER.DISPUTE.CREATED",
                                    "create_time": "2026-11-05T08:00:00Z",
                                    "resource": {
                                        "dispute_id": "PP-D-00112233",
                                        "reason": "ITEM_NOT_RECEIVED",
                                        "dispute_amount": {"value": "245.00", "currency_code": "USD"},
                                    },
                                }, indent=2),
                                "options": {"raw": {"language": "json"}},
                            },
                            "description": (
                                "PayPal sends signed events here. "
                                "Signature verification runs synchronously; "
                                "processing runs in background. "
                                "⚠️ Real PayPal signatures required — "
                                "use PayPal Developer Dashboard to send test events."
                            ),
                        },
                    }
                ],
            },
        ],
    }
    return collection


# ---------------------------------------------------------------------------
# Postman API client
# ---------------------------------------------------------------------------

class PostmanClient:
    """
    Publishes Anchor's collection to Postman and returns the
    'Run in Postman' button URL.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or POSTMAN_API_KEY
        self.headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def enabled(self) -> bool:
        return bool(self.api_key)

    async def list_workspaces(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{POSTMAN_BASE}/workspaces", headers=self.headers)
            r.raise_for_status()
        return r.json().get("workspaces", [])

    async def create_collection(
        self,
        collection: dict,
        workspace_id: Optional[str] = None,
    ) -> dict:
        """
        POST /collections — creates a new collection.
        Returns {"collection": {"id": ..., "uid": ..., "name": ...}}
        """
        body: dict = {"collection": collection}
        params = {}
        if workspace_id:
            params["workspaceId"] = workspace_id

        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{POSTMAN_BASE}/collections",
                headers=self.headers,
                params=params,
                json=body,
            )
            r.raise_for_status()
        return r.json()

    async def update_collection(self, uid: str, collection: dict) -> dict:
        """
        PUT /collections/{uid} — replaces existing collection.
        """
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.put(
                f"{POSTMAN_BASE}/collections/{uid}",
                headers=self.headers,
                json={"collection": collection},
            )
            r.raise_for_status()
        return r.json()

    async def get_collection(self, uid: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{POSTMAN_BASE}/collections/{uid}", headers=self.headers)
            r.raise_for_status()
        return r.json()

    def run_in_postman_url(self, uid: str) -> str:
        return f"https://app.getpostman.com/run-collection/{uid}"

    def badge_url(self, uid: str) -> str:
        return f"https://run.pstmn.io/button.svg"

    async def publish(
        self,
        base_url: str = "https://anchor-paypal.onrender.com",
        workspace_id: Optional[str] = None,
    ) -> dict:
        """
        High-level publish: create or update the collection, return the
        'Run in Postman' URL and collection UID.
        """
        if not self.enabled():
            return {
                "enabled": False,
                "note": "POSTMAN_API_KEY not configured — set it in Render env vars",
                "collection_json": build_collection(base_url),
            }

        collection = build_collection(base_url)
        existing_uid = os.environ.get(COLLECTION_UID_ENV, "")

        try:
            if existing_uid:
                result = await self.update_collection(existing_uid, collection)
                uid = existing_uid
            else:
                result = await self.create_collection(collection, workspace_id)
                uid = result.get("collection", {}).get("uid", "")
        except httpx.HTTPStatusError as e:
            return {"enabled": True, "error": f"Postman API {e.response.status_code}: {e.response.text[:300]}"}
        except Exception as e:
            return {"enabled": True, "error": str(e)}

        return {
            "enabled": True,
            "uid": uid,
            "run_url": self.run_in_postman_url(uid),
            "collection_name": collection["info"]["name"],
            "item_count": sum(len(g["item"]) for g in collection["item"]),
        }
