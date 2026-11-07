"""
Astropods AI Governance Integration
https://astropods.com

Anchor uses the gateway for an optional, source-cited dispute evidence review.
The deterministic Settlement Oracle and PayPal action policy do not depend on
the model. Missing credentials are reported as unconfigured; no sample usage is
presented as live provider activity.

Required env vars:
  ASTRO_GATEWAY_URL       e.g. https://gateway.astropods.com
  ASTRO_GATEWAY_API_KEY   gateway API key (from Astropods Dashboard)
  ASTRO_ACCOUNT           account slug (e.g. "anchor-team")
  ASTRO_API_TOKEN         platform token for billing/models API
                          (Bearer token from `ast login`)

API references:
  Gateway (OpenAI-compatible):
    POST {ASTRO_GATEWAY_URL}/v1/chat/completions
    GET  {ASTRO_GATEWAY_URL}/v1/models
  Platform API (https://astropods.com/api/v1):
    GET  /accounts/{account}/devtool-gateway/models
    GET  /accounts/{account}/billing/usage
"""

import os
import httpx
from typing import Optional

ASTRO_GATEWAY_URL = os.environ.get("ASTRO_GATEWAY_URL", "")
ASTRO_GATEWAY_KEY = os.environ.get("ASTRO_GATEWAY_API_KEY", "")
ASTRO_ACCOUNT     = os.environ.get("ASTRO_ACCOUNT", "")
ASTRO_API_TOKEN   = os.environ.get("ASTRO_API_TOKEN", "")

ASTRO_PLATFORM    = "https://astropods.com/api/v1"


class AstropodsGateway:
    """
    OpenAI-compatible gateway client used by Anchor's optional evidence review.
    This client does not itself enforce governance policies or payment actions.
    """

    def __init__(
        self,
        gateway_url: Optional[str] = None,
        api_key: Optional[str] = None,
        account: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.gateway_url = (gateway_url or ASTRO_GATEWAY_URL).rstrip("/")
        self.api_key     = api_key    or ASTRO_GATEWAY_KEY
        self.account     = account    or ASTRO_ACCOUNT
        self.api_token   = api_token  or ASTRO_API_TOKEN

    def gateway_enabled(self) -> bool:
        return bool(self.gateway_url and self.api_key)

    def platform_enabled(self) -> bool:
        return bool(self.account and self.api_token)

    # ── Gateway (OpenAI-compatible) ──────────────────────────────────────────

    async def chat_completion(
        self,
        messages: list[dict],
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> dict:
        """
        POST {gateway_url}/v1/chat/completions
        Returns the full OpenAI-format response dict.
        """
        if not self.gateway_enabled():
            raise RuntimeError("Astropods gateway not configured (ASTRO_GATEWAY_URL + ASTRO_GATEWAY_API_KEY required)")

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self.gateway_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
        return r.json()

    async def list_gateway_models_openai(self) -> list[dict]:
        """GET {gateway_url}/v1/models — standard OpenAI models list."""
        if not self.gateway_enabled():
            return []
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{self.gateway_url}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
        return r.json().get("data", [])

    # ── Platform API ─────────────────────────────────────────────────────────

    async def list_models_with_pricing(self) -> dict:
        """
        GET /accounts/{account}/devtool-gateway/models
        Returns base_url + list of models with per-million-token pricing.
        """
        if not self.platform_enabled():
            return {}
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{ASTRO_PLATFORM}/accounts/{self.account}/devtool-gateway/models",
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
            r.raise_for_status()
        return r.json()

    async def get_billing_usage(
        self,
        from_dt: Optional[str] = None,
        to_dt: Optional[str] = None,
    ) -> dict:
        """
        GET /accounts/{account}/billing/usage
        from_dt / to_dt: RFC3339 strings, default = current month
        Returns available, limit_lift_failed, refunds, data.
        """
        if not self.platform_enabled():
            return {}
        params: dict = {}
        if from_dt:
            params["from"] = from_dt
        if to_dt:
            params["to"] = to_dt
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{ASTRO_PLATFORM}/accounts/{self.account}/billing/usage",
                headers={"Authorization": f"Bearer {self.api_token}"},
                params=params,
            )
            r.raise_for_status()
        return r.json()

    # ── Status summary (for the dashboard) ───────────────────────────────────

    async def status_summary(self) -> dict:
        """
        Returns configured provider data only; absent credentials are explicit.
        """
        if not self.gateway_enabled() and not self.platform_enabled():
            return {
                "enabled": False,
                "demo": True,
                "gateway_configured": False,
                "note": "Set ASTRO_GATEWAY_URL + ASTRO_GATEWAY_API_KEY in Render env vars",
                "models": [],
                "billing": None,
            }

        result: dict = {"enabled": True}

        # Models with pricing
        if self.platform_enabled():
            try:
                mdl_resp = await self.list_models_with_pricing()
                result["gateway_base_url"] = mdl_resp.get("base_url", self.gateway_url)
                result["models"] = mdl_resp.get("models", [])
            except Exception as e:
                result["models_error"] = str(e)

        # Billing usage
        if self.platform_enabled():
            try:
                usage = await self.get_billing_usage()
                result["billing"] = usage
            except Exception as e:
                result["billing_error"] = str(e)

        # Gateway connectivity check (cheap)
        if self.gateway_enabled():
            result["gateway_url"] = self.gateway_url
            result["gateway_configured"] = True

        return result


# ---------------------------------------------------------------------------
# Optional generic gateway helper
# ---------------------------------------------------------------------------

async def route_llm(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    fallback_fn=None,
    **kwargs,
) -> str:
    """
    Route a generic LLM call through Astropods when configured, or call an
    explicitly supplied fallback. The dispute evidence review uses its own
    fail-closed path and does not use this fallback helper.

    Returns the assistant text content as a string.

    """
    gw = AstropodsGateway()
    if gw.gateway_enabled():
        try:
            resp = await gw.chat_completion(messages, model=model, **kwargs)
            choices = resp.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"[Astropods] Gateway call failed, falling back: {e}")

    if fallback_fn:
        result = fallback_fn()
        if hasattr(result, "__await__"):
            return await result
        return result

    return ""
