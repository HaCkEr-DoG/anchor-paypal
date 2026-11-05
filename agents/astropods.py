"""
Astropods AI Governance Integration
https://astropods.com

Astropods is an AI governance platform. Its AI Gateway sits in front of
every LLM call — logging every decision, enforcing spend budgets, and
letting you swap models without touching code.

For Anchor, this means every Guardian Agent reasoning step and every
Recovery Agent message goes through Astropods' audit trail. Judges can
see real-time governance data: which model fired, how many tokens, how
much it cost, whether a spend cap was hit.

Required env vars (all optional — panel works in demo mode without them):
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
    OpenAI-compatible LLM gateway client.
    When env vars are set, all chat completion calls route through
    Astropods for governance, auditing, and spend control.
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
        Returns a dict the dashboard can render directly.
        Always returns something; uses demo data when keys are absent.
        """
        if not self.gateway_enabled() and not self.platform_enabled():
            return {
                "enabled": False,
                "note": "Set ASTRO_GATEWAY_URL + ASTRO_GATEWAY_API_KEY in Render env vars",
                "demo": _demo_status(),
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
# Guardian LLM routing helper
# ---------------------------------------------------------------------------

async def route_llm(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    fallback_fn=None,
    **kwargs,
) -> str:
    """
    Route an LLM call through Astropods when configured, fall back to
    fallback_fn() otherwise.

    Returns the assistant text content as a string.

    Usage in Guardian:
        from agents.astropods import route_llm

        text = await route_llm(
            messages=[{"role": "user", "content": prompt}],
            model="claude-sonnet-4-6",
            fallback_fn=lambda: direct_anthropic_call(prompt),
        )
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


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

def _demo_status() -> dict:
    return {
        "gateway_url": "https://gateway.astropods.com",
        "gateway_configured": True,
        "models": [
            {"id": "claude-sonnet-4-6", "maker": "Anthropic", "family": "claude",    "context_tokens": 200000, "input_per_million_usd": "3.00",  "output_per_million_usd": "15.00"},
            {"id": "claude-opus-5-5",   "maker": "Anthropic", "family": "claude",    "context_tokens": 200000, "input_per_million_usd": "15.00", "output_per_million_usd": "75.00"},
            {"id": "gpt-5-6-terra",     "maker": "OpenAI",    "family": "gpt",       "context_tokens": 128000, "input_per_million_usd": "5.00",  "output_per_million_usd": "20.00"},
            {"id": "nova-lite",         "maker": "Amazon",    "family": "nova",      "context_tokens": 300000, "input_per_million_usd": "0.60",  "output_per_million_usd": "2.40"},
            {"id": "mistral-large-3",   "maker": "Mistral",   "family": "mistral",   "context_tokens": 128000, "input_per_million_usd": "2.00",  "output_per_million_usd": "6.00"},
            {"id": "jev-1-13-0",        "maker": "Astropods", "family": "decision",  "context_tokens": None,   "input_per_million_usd": "1.00",  "output_per_million_usd": "1.00"},
        ],
        "billing": {
            "available": True,
            "data": {"total_usd": "0.42", "calls": 37},
        },
    }
