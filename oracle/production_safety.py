"""
Production Safety Checker — validates that Anchor meets minimum safety
requirements before autonomous actions are taken on live disputes.

Checks the six blockers identified by production review:
  1. Fail-closed policy   — explicit merchant policy loaded, not defaults
  2. Shadow mode          — SHADOW_MODE=1 means log-only, no PayPal calls
  3. Durable storage      — webhook outbox configured (not ephemeral)
  4. Auth strength        — API_SECRET set, approval links have expiry
  5. Recovery validation  — order verification enabled
  6. Live credentials     — PAYPAL_ENV is 'live' (needs all above green first)

Returns a readiness report that the guardian uses to decide whether
to act autonomously, request human approval, or hard-block the action.

PayPal benefit:
  - Prevents incorrect autonomous actions on live transactions
  - Provides a clear audit trail of safety gate decisions
  - Fail-closed by default protects both merchants and buyers
  - Gradual enablement through shadow→pilot→production stages
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SafetyLevel(str, Enum):
    SAFE       = "SAFE"       # all required checks pass for current env
    SHADOW     = "SHADOW"     # checks pass but SHADOW_MODE=1 — log only
    HUMAN_REVIEW = "HUMAN_REVIEW"  # a soft check failed — require approval
    BLOCKED    = "BLOCKED"    # a hard check failed — no autonomous action


class CheckStatus(str, Enum):
    PASS    = "PASS"
    WARN    = "WARN"    # soft: degrades to HUMAN_REVIEW
    FAIL    = "FAIL"    # hard: escalates to BLOCKED


@dataclass
class SafetyCheck:
    name: str
    status: CheckStatus
    detail: str
    is_hard: bool   # True → FAIL means BLOCKED, not just HUMAN_REVIEW


@dataclass
class ProductionSafetyInput:
    merchant_policy_loaded: bool        # config was loaded from storage
    paypal_env: str                     # 'sandbox' or 'live'
    shadow_mode: bool                   # SHADOW_MODE env var
    api_secret_set: bool                # API_SECRET env var present
    approval_secret_set: bool           # APPROVAL_TOKEN_SECRET persistent
    durable_storage_configured: bool    # ES/DB available
    webhook_outbox_enabled: bool        # events saved before processing
    recovery_validation_enabled: bool   # order/merchant check before capture
    approval_link_max_age_seconds: int  # 0 = no expiry (bad)


@dataclass
class ProductionSafetyReport:
    safety_level: SafetyLevel
    checks: List[SafetyCheck] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    recommendation: str = ""
    allow_autonomous_action: bool = False
    allow_live_credentials: bool = False


def check_production_safety(inp: ProductionSafetyInput) -> ProductionSafetyReport:
    checks: List[SafetyCheck] = []
    is_live = inp.paypal_env.lower() == "live"

    def add(name: str, condition: bool, fail_detail: str, pass_detail: str,
            is_hard: bool = True) -> CheckStatus:
        if condition:
            checks.append(SafetyCheck(name, CheckStatus.PASS, pass_detail, is_hard))
            return CheckStatus.PASS
        else:
            status = CheckStatus.FAIL if is_hard else CheckStatus.WARN
            checks.append(SafetyCheck(name, status, fail_detail, is_hard))
            return status

    # 1. Shadow mode (soft — it's a valid operational mode, not a failure)
    if inp.shadow_mode:
        checks.append(SafetyCheck(
            "shadow_mode", CheckStatus.PASS,
            "SHADOW_MODE=1 — oracle decisions logged but no PayPal calls made. "
            "Safe to run; disable shadow mode after validating shadow logs.",
            is_hard=False,
        ))

    # 2. Merchant policy (hard for live, soft for sandbox)
    add(
        "merchant_policy_loaded",
        inp.merchant_policy_loaded,
        fail_detail="No explicit merchant policy loaded — would use unsafe defaults. "
                    "Fail-closed: routing to human review.",
        pass_detail="Merchant policy loaded from configuration storage.",
        is_hard=is_live,
    )

    # 3. API_SECRET
    add(
        "api_secret",
        inp.api_secret_set,
        fail_detail="API_SECRET not set — protected routes fail closed. "
                    "Set a strong random secret in environment.",
        pass_detail="API_SECRET is set.",
        is_hard=is_live,
    )

    # 4. Approval link expiry
    add(
        "approval_link_expiry",
        inp.approval_link_max_age_seconds > 0 and inp.approval_link_max_age_seconds <= 86400,
        fail_detail=f"Approval links have no expiry or expiry > 24h "
                    f"(current: {inp.approval_link_max_age_seconds}s). "
                    "Replay attacks are possible.",
        pass_detail=f"Approval links expire in {inp.approval_link_max_age_seconds}s.",
        is_hard=False,
    )

    # 5. Persistent approval secret
    add(
        "approval_secret_persistent",
        inp.approval_secret_set,
        fail_detail="Approval token secret is ephemeral — restarts invalidate all pending approvals.",
        pass_detail="Approval token secret is persisted (survives restarts).",
        is_hard=False,
    )

    # 6. Durable storage (hard for live)
    add(
        "durable_storage",
        inp.durable_storage_configured,
        fail_detail="No durable storage configured — financial and workflow state is lost on restart.",
        pass_detail="Durable storage configured.",
        is_hard=is_live,
    )

    # 7. Webhook outbox (hard for live)
    add(
        "webhook_outbox",
        inp.webhook_outbox_enabled,
        fail_detail="Webhook events not persisted before processing — events can be lost on crash. "
                    "PayPal retries, but re-delivery after crash may be blocked by duplicate check.",
        pass_detail="Webhook outbox enabled — events saved before processing.",
        is_hard=is_live,
    )

    # 8. Recovery validation (hard — wrong-merchant capture risk)
    add(
        "recovery_validation",
        inp.recovery_validation_enabled,
        fail_detail="Recovery does not validate order ownership / amount match before capture. "
                    "Risk of capturing wrong-merchant or mismatched orders.",
        pass_detail="Recovery validates order merchant + amount before capture.",
        is_hard=True,
    )

    # 9. Live credentials check
    if is_live:
        failing = [c for c in checks if c.status in (CheckStatus.FAIL, CheckStatus.WARN)]
        add(
            "live_credentials_safe",
            len(failing) == 0,
            fail_detail=f"PAYPAL_ENV=live with {len(failing)} unresolved check(s). "
                        "Do not use live credentials until all hard checks pass.",
            pass_detail="All checks pass — live credentials are safe to use.",
            is_hard=True,
        )

    # Compute overall safety level
    hard_fails = [c for c in checks if c.status == CheckStatus.FAIL and c.is_hard]
    soft_fails = [c for c in checks if c.status in (CheckStatus.FAIL, CheckStatus.WARN) and not c.is_hard]

    blocking_reasons = [c.detail for c in hard_fails]
    warnings = [c.detail for c in soft_fails]

    if hard_fails:
        level = SafetyLevel.BLOCKED
        allow_auto = False
    elif inp.shadow_mode:
        level = SafetyLevel.SHADOW
        allow_auto = False   # shadow mode: log but don't act
    elif soft_fails:
        level = SafetyLevel.HUMAN_REVIEW
        allow_auto = False
    else:
        level = SafetyLevel.SAFE
        allow_auto = True

    all_hard_pass = len(hard_fails) == 0
    allow_live = all_hard_pass and inp.durable_storage_configured and is_live

    # Recommendation
    if level == SafetyLevel.BLOCKED:
        rec = (
            f"Autonomous action BLOCKED — {len(hard_fails)} hard check(s) failed. "
            f"Fix: {'; '.join(c.name for c in hard_fails)}. "
            f"Route ALL disputes to human review until resolved."
        )
    elif level == SafetyLevel.SHADOW:
        rec = (
            "SHADOW_MODE active — oracle decisions are being recorded but no PayPal actions "
            "are taken. Review shadow log to validate decision quality before enabling live actions. "
            "Disable SHADOW_MODE=1 when satisfied."
        )
    elif level == SafetyLevel.HUMAN_REVIEW:
        rec = (
            f"{len(soft_fails)} soft check(s) need attention. "
            "Autonomous actions require human approval until resolved: "
            + "; ".join(c.name for c in soft_fails) + "."
        )
    else:
        rec = "All safety checks pass. Autonomous dispute actions are permitted within configured thresholds."

    return ProductionSafetyReport(
        safety_level=level,
        checks=checks,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        recommendation=rec,
        allow_autonomous_action=allow_auto,
        allow_live_credentials=allow_live,
    )


def check_from_env(merchant_policy_loaded: Optional[bool] = None) -> ProductionSafetyReport:
    """Convenience: build input from current environment variables."""
    if merchant_policy_loaded is None:
        merchant_policy_loaded = os.environ.get("MERCHANT_POLICY_LOADED", "").strip().lower() in {"1", "true", "yes"}
    approval_secret_set = bool(os.environ.get("APPROVAL_TOKEN_SECRET", ""))
    if os.environ.get("DATABASE_URL"):
        try:
            import agents.pg_store as pg_store
            approval_secret_set = bool(pg_store.get_config_value("approval_token_secret")) or approval_secret_set
        except Exception:
            approval_secret_set = False
    return check_production_safety(ProductionSafetyInput(
        merchant_policy_loaded=merchant_policy_loaded,
        paypal_env=os.environ.get("PAYPAL_ENV", "sandbox"),
        shadow_mode=os.environ.get("SHADOW_MODE", "0").strip().lower() in {"1", "true", "yes"},
        api_secret_set=bool(os.environ.get("API_SECRET", "")),
        approval_secret_set=approval_secret_set,
        durable_storage_configured=bool(os.environ.get("DATABASE_URL", "") or os.environ.get("ELASTICSEARCH_URL", "")),
        webhook_outbox_enabled=os.environ.get("WEBHOOK_OUTBOX_ENABLED", "0") == "1",
        recovery_validation_enabled=os.environ.get("RECOVERY_VALIDATION_ENABLED", "1") != "0",
        approval_link_max_age_seconds=int(os.environ.get("APPROVAL_LINK_MAX_AGE", "3600")),
    ))
