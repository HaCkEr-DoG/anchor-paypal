"""
Kernel Browser Integration
https://kernel.sh

Kernel is a hosted, bot-resistant cloud browser for AI agents.
Each Chromium instance is pre-configured with anti-detection defaults and
runs in an isolated VM. Creation latency is ~30ms (p50).

For Anchor, Kernel powers the evidence-gathering pipeline:
  - Screenshots of carrier tracking pages (UPS, FedEx, DHL, USPS)
  - Delivery-confirmation evidence for ITEM_NOT_RECEIVED disputes
  - Communication screenshots when needed for SNAD disputes

Carrier sites aggressively block regular scrapers and Python-driven
Playwright with standard fingerprints. Kernel's stealth mode + proxy
stack bypasses these protections transparently.

Required env vars (all optional — panel works in demo mode without them):
  KERNEL_API_KEY   API key from https://dashboard.onkernel.com

API reference:
  REST base: https://api.onkernel.com
  POST /browsers               — create a browser session
  POST /browsers/{id}/playwright/execute  — run Playwright code in VM
  DELETE /browsers/{id}        — release the session

Python SDK (pip install kernel):
  from kernel import Kernel
  client = Kernel(api_key=os.environ["KERNEL_API_KEY"])
  browser = client.browsers.create(stealth=True, timeout_seconds=120)
  client.browsers.playwright.execute(browser.session_id, code="...")
  client.browsers.delete_by_id(browser.session_id)
"""

import os
import httpx
from typing import Optional

KERNEL_API_KEY = os.environ.get("KERNEL_API_KEY", "")
KERNEL_API_BASE = "https://api.onkernel.com"


class KernelBrowser:
    """
    Thin async wrapper around the Kernel REST API.

    Usage in evidence pipeline:
        async with KernelBrowser() as k:
            screenshot_bytes = await k.screenshot_url(url)
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or KERNEL_API_KEY
        self._session_id: Optional[str] = None

    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def create_session(
        self,
        stealth: bool = True,
        timeout_seconds: int = 120,
    ) -> dict:
        """POST /browsers — returns session dict with session_id and cdp_ws_url."""
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{KERNEL_API_BASE}/browsers",
                headers=self._headers,
                json={
                    "stealth": stealth,
                    "headless": True,
                    "timeout_seconds": timeout_seconds,
                },
            )
            r.raise_for_status()
        session = r.json()
        self._session_id = session.get("session_id") or session.get("id")
        return session

    async def execute_playwright(self, session_id: str, playwright_code: str) -> dict:
        """POST /browsers/{id}/playwright/execute — runs code in the browser VM."""
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{KERNEL_API_BASE}/browsers/{session_id}/playwright/execute",
                headers=self._headers,
                json={"code": playwright_code},
            )
            r.raise_for_status()
        return r.json()

    async def delete_session(self, session_id: str) -> None:
        """DELETE /browsers/{id} — release the session."""
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.delete(
                f"{KERNEL_API_BASE}/browsers/{session_id}",
                headers=self._headers,
            )

    async def screenshot_url(
        self,
        url: str,
        wait_selector: Optional[str] = None,
        timeout_ms: int = 10000,
    ) -> bytes:
        """
        High-level helper: spin up a stealth browser, navigate, screenshot,
        return PNG bytes, then delete the session.

        wait_selector: CSS selector to wait for before screenshotting.
        """
        if not self.enabled():
            raise RuntimeError("Kernel not configured — set KERNEL_API_KEY")

        session = await self.create_session(stealth=True)
        session_id = session.get("session_id") or session.get("id")

        wait_line = ""
        if wait_selector:
            wait_line = f'await page.wait_for_selector("{wait_selector}", timeout={timeout_ms});'

        code = f"""
const page = await context.newPage();
await page.goto("{url}", {{ waitUntil: "networkidle", timeout: 30000 }});
{wait_line}
const buf = await page.screenshot({{ fullPage: true }});
buf.toString("base64");
"""
        try:
            result = await self.execute_playwright(session_id, code)
            b64 = result.get("result", "")
            if b64:
                import base64
                return base64.b64decode(b64)
            return b""
        finally:
            try:
                await self.delete_session(session_id)
            except Exception:
                pass

    # ── Status summary (for the dashboard) ──────────────────────────────────

    async def status_summary(self) -> dict:
        """
        Returns a dict the dashboard can render directly.
        Always returns something; uses demo data when key is absent.
        """
        if not self.enabled():
            return {
                "enabled": False,
                "note": "Set KERNEL_API_KEY in Render env vars (get one at dashboard.onkernel.com)",
                "demo": _demo_status(),
            }

        # Verify the key works with a lightweight list call
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{KERNEL_API_BASE}/browsers",
                    headers=self._headers,
                )
                r.raise_for_status()
            live_sessions = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
            return {
                "enabled": True,
                "gateway_configured": True,
                "api_base": KERNEL_API_BASE,
                "live_sessions": len(live_sessions),
                "capabilities": _capabilities(),
            }
        except Exception as e:
            return {
                "enabled": True,
                "gateway_configured": True,
                "api_base": KERNEL_API_BASE,
                "error": str(e),
                "demo": _demo_status(),
            }


# ---------------------------------------------------------------------------
# Demo / static data
# ---------------------------------------------------------------------------

def _capabilities() -> list[dict]:
    return [
        {"name": "Stealth browsing",        "detail": "Anti-detection fingerprints, residential proxy"},
        {"name": "CAPTCHA solver",           "detail": "Automatic, included with stealth=True"},
        {"name": "Carrier tracking",         "detail": "UPS · FedEx · DHL · USPS screenshots"},
        {"name": "30ms spin-up (p50)",       "detail": "Each browser in its own isolated VM"},
        {"name": "Playwright execution",     "detail": "Full CDP + Playwright inside the VM"},
        {"name": "Evidence screenshots",     "detail": "Delivery-confirmation PNG for disputes"},
    ]


def _demo_status() -> dict:
    return {
        "api_base": KERNEL_API_BASE,
        "gateway_configured": True,
        "capabilities": _capabilities(),
        "carrier_support": ["USPS", "UPS", "FedEx", "DHL"],
        "sessions_captured": 3,
        "last_capture": "tracking #1Z999AA10123456784 (UPS) → delivery confirmed 2026-10-28",
    }
