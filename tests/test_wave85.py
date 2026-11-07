"""Wave 85 — Production Safety Checker tests."""
import pytest
from oracle.production_safety import (
    CheckStatus, SafetyLevel,
    ProductionSafetyInput, ProductionSafetyReport,
    check_production_safety,
)


def _safe_inp(**overrides):
    base = dict(
        merchant_policy_loaded=True,
        paypal_env="sandbox",
        shadow_mode=False,
        api_secret_set=True,
        approval_secret_set=True,
        durable_storage_configured=True,
        webhook_outbox_enabled=True,
        recovery_validation_enabled=True,
        approval_link_max_age_seconds=3600,
    )
    base.update(overrides)
    return ProductionSafetyInput(**base)


def _unsafe_inp(**overrides):
    base = dict(
        merchant_policy_loaded=False,
        paypal_env="sandbox",
        shadow_mode=False,
        api_secret_set=False,
        approval_secret_set=False,
        durable_storage_configured=False,
        webhook_outbox_enabled=False,
        recovery_validation_enabled=False,
        approval_link_max_age_seconds=0,
    )
    base.update(overrides)
    return ProductionSafetyInput(**base)


# ─── Safety level ─────────────────────────────────────────────────────────────

class TestSafetyLevel:
    def test_all_pass_sandbox_is_safe(self):
        r = check_production_safety(_safe_inp())
        assert r.safety_level == SafetyLevel.SAFE
        assert r.allow_autonomous_action is True

    def test_shadow_mode_is_shadow(self):
        r = check_production_safety(_safe_inp(shadow_mode=True))
        assert r.safety_level == SafetyLevel.SHADOW
        assert r.allow_autonomous_action is False

    def test_missing_policy_live_is_blocked(self):
        r = check_production_safety(_safe_inp(
            merchant_policy_loaded=False, paypal_env="live",
        ))
        assert r.safety_level == SafetyLevel.BLOCKED
        assert r.allow_autonomous_action is False

    def test_missing_policy_sandbox_is_human_review(self):
        # In sandbox, merchant policy missing is a soft check
        r = check_production_safety(_safe_inp(merchant_policy_loaded=False))
        assert r.safety_level in (SafetyLevel.HUMAN_REVIEW, SafetyLevel.SAFE)

    def test_recovery_validation_missing_always_blocked(self):
        r = check_production_safety(_safe_inp(recovery_validation_enabled=False))
        assert r.safety_level == SafetyLevel.BLOCKED

    def test_no_durable_storage_live_is_blocked(self):
        r = check_production_safety(_safe_inp(
            durable_storage_configured=False, paypal_env="live",
        ))
        assert r.safety_level == SafetyLevel.BLOCKED


# ─── Live credentials guard ───────────────────────────────────────────────────

class TestLiveCredentials:
    def test_allow_live_only_when_fully_safe(self):
        r = check_production_safety(_safe_inp(paypal_env="live"))
        assert r.allow_live_credentials is True

    def test_no_allow_live_in_sandbox_mode(self):
        r = check_production_safety(_safe_inp(paypal_env="sandbox"))
        assert r.allow_live_credentials is False

    def test_no_allow_live_when_blocked(self):
        r = check_production_safety(_safe_inp(
            recovery_validation_enabled=False, paypal_env="live",
        ))
        assert r.allow_live_credentials is False


# ─── Individual checks ────────────────────────────────────────────────────────

class TestIndividualChecks:
    def _check(self, r: ProductionSafetyReport, name: str):
        return next((c for c in r.checks if c.name == name), None)

    def test_approval_link_no_expiry_warns(self):
        r = check_production_safety(_safe_inp(approval_link_max_age_seconds=0))
        c = self._check(r, "approval_link_expiry")
        assert c is not None
        assert c.status in (CheckStatus.WARN, CheckStatus.FAIL)

    def test_approval_link_too_long_warns(self):
        r = check_production_safety(_safe_inp(approval_link_max_age_seconds=90000))
        c = self._check(r, "approval_link_expiry")
        assert c is not None
        assert c.status in (CheckStatus.WARN, CheckStatus.FAIL)

    def test_approval_link_1h_passes(self):
        r = check_production_safety(_safe_inp(approval_link_max_age_seconds=3600))
        c = self._check(r, "approval_link_expiry")
        assert c is not None
        assert c.status == CheckStatus.PASS

    def test_webhook_outbox_missing_live_blocked(self):
        r = check_production_safety(_safe_inp(
            webhook_outbox_enabled=False, paypal_env="live",
        ))
        assert r.safety_level == SafetyLevel.BLOCKED

    def test_shadow_mode_check_present(self):
        r = check_production_safety(_safe_inp(shadow_mode=True))
        names = [c.name for c in r.checks]
        assert "shadow_mode" in names


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_recommendation_non_empty(self):
        r = check_production_safety(_safe_inp())
        assert len(r.recommendation) > 10

    def test_blocked_has_blocking_reasons(self):
        r = check_production_safety(_safe_inp(recovery_validation_enabled=False))
        assert len(r.blocking_reasons) > 0

    def test_checks_list_non_empty(self):
        r = check_production_safety(_safe_inp())
        assert len(r.checks) > 3

    def test_all_checks_have_valid_status(self):
        r = check_production_safety(_safe_inp())
        for c in r.checks:
            assert c.status in list(CheckStatus)
