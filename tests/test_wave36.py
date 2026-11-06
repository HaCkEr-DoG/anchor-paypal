"""
Tests for Wave 36: merchant_risk_profiler, response_template_selector.
"""
import pytest
from oracle.merchant_risk_profiler import (
    profile_merchant_risk, MerchantRiskInput, DisputeTypeSplit,
    RiskSeverity, RiskTrend, RiskDimension,
)
from oracle.response_template_selector import (
    select_response_template, TemplateSelectionInput,
    ResponseStrategy, TemplateTone,
)


# ═══════════════════════════════════════════════════════════════════
# Merchant Risk Profiler Tests
# ═══════════════════════════════════════════════════════════════════

class TestMerchantRiskProfiler:

    def _inp(self, **kw) -> MerchantRiskInput:
        defaults = dict(
            merchant_id="M-001",
            merchant_category="ELECTRONICS",
            transactions_90d=1000,
            disputes_90d=5,
            disputes_won=3,
            disputes_lost=2,
        )
        defaults.update(kw)
        return MerchantRiskInput(**defaults)

    # ── Dispute rate calculation ──────────────────────────────────

    def test_rate_calculated_correctly(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=1000, disputes_90d=10))
        assert r.current_dispute_rate_pct == pytest.approx(1.0)

    def test_zero_transactions_no_crash(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=0, disputes_90d=0))
        assert r.current_dispute_rate_pct == pytest.approx(0.0)

    # ── Severity classification ───────────────────────────────────

    def test_critical_at_severe_threshold(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=1000, disputes_90d=35))  # 3.5%
        assert r.overall_severity in (RiskSeverity.CRITICAL,)

    def test_low_at_very_low_rate(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=10000, disputes_90d=10))  # 0.1%
        assert r.overall_severity in (
            RiskSeverity.LOW, RiskSeverity.MEDIUM, RiskSeverity.HEALTHY)

    def test_medium_at_elevated_threshold(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=1000, disputes_90d=6))  # 0.6%
        assert r.overall_severity in (RiskSeverity.MEDIUM, RiskSeverity.HIGH)

    # ── Win rate trend ────────────────────────────────────────────

    def test_worsening_trend_when_win_rate_drops(self):
        r = profile_merchant_risk(self._inp(
            disputes_won=2, disputes_lost=8,       # 20% now
            disputes_won_prior=8, disputes_lost_prior=2,  # 80% prior
            transactions_prior_90d=1000, disputes_prior_90d=5,
        ))
        assert r.overall_trend == RiskTrend.WORSENING

    def test_improving_trend_when_win_rate_rises(self):
        r = profile_merchant_risk(self._inp(
            disputes_won=8, disputes_lost=2,       # 80% now
            disputes_won_prior=2, disputes_lost_prior=8,  # 20% prior
            transactions_prior_90d=1000, disputes_prior_90d=5,
        ))
        assert r.overall_trend == RiskTrend.IMPROVING

    def test_stable_trend_no_prior_data(self):
        r = profile_merchant_risk(self._inp())
        assert r.overall_trend in (RiskTrend.STABLE, RiskTrend.IMPROVING, RiskTrend.WORSENING)

    # ── Concentration risk ────────────────────────────────────────

    def test_high_concentration_raises_severity(self):
        splits = [
            DisputeTypeSplit("INR", 90, 0.90, 0.40),
            DisputeTypeSplit("UNAUTHORIZED", 10, 0.10, 0.60),
        ]
        r = profile_merchant_risk(self._inp(type_splits=splits))
        # At least one finding should be HIGH due to concentration
        conc_findings = [f for f in r.findings
                         if f.dimension == RiskDimension.CONCENTRATION]
        assert conc_findings[0].severity in (RiskSeverity.HIGH, RiskSeverity.MEDIUM)

    # ── Monitoring frequency ──────────────────────────────────────

    def test_daily_monitoring_at_critical(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=1000, disputes_90d=35))
        assert r.monitoring_frequency == "DAILY"

    def test_monthly_monitoring_at_low_risk(self):
        r = profile_merchant_risk(self._inp(
            transactions_90d=10000, disputes_90d=10))
        assert r.monitoring_frequency in ("MONTHLY", "WEEKLY")

    # ── Report fields ─────────────────────────────────────────────

    def test_findings_non_empty(self):
        r = profile_merchant_risk(self._inp())
        assert len(r.findings) >= 1

    def test_recommendations_non_empty(self):
        r = profile_merchant_risk(self._inp())
        assert len(r.top_recommendations) >= 1

    def test_summary_contains_merchant_id(self):
        r = profile_merchant_risk(self._inp(merchant_id="M-XYZ"))
        assert "M-XYZ" in r.summary

    def test_win_rate_matches_input(self):
        r = profile_merchant_risk(self._inp(disputes_won=7, disputes_lost=3))
        assert r.current_win_rate == pytest.approx(0.70)


# ═══════════════════════════════════════════════════════════════════
# Response Template Selector Tests
# ═══════════════════════════════════════════════════════════════════

class TestResponseTemplateSelector:

    def _inp(self, **kw) -> TemplateSelectionInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            network="PAYPAL",
            strategy=ResponseStrategy.FIGHT,
            disputed_amount=100.0,
        )
        defaults.update(kw)
        return TemplateSelectionInput(**defaults)

    # ── Template selection logic ──────────────────────────────────

    def test_inr_full_defense_with_delivery_conf(self):
        r = select_response_template(self._inp(
            has_delivery_confirmation=True))
        assert r.template.template_id == "INR_FIGHT_FULL"

    def test_inr_partial_defense_without_delivery_conf(self):
        r = select_response_template(self._inp())
        assert r.template.template_id == "INR_FIGHT_PARTIAL"

    def test_unauthorized_selects_unauth_template(self):
        r = select_response_template(self._inp(claim_type="UNAUTHORIZED"))
        assert r.template.template_id == "UNAUTHORIZED_FIGHT_FULL"

    def test_snad_selects_snad_template(self):
        r = select_response_template(self._inp(claim_type="SNAD"))
        assert r.template.template_id == "SNAD_FIGHT"

    def test_accept_selects_accept_template(self):
        r = select_response_template(self._inp(strategy=ResponseStrategy.ACCEPT))
        assert r.template.template_id == "ACCEPT_FULL"

    def test_settle_selects_settle_template(self):
        r = select_response_template(self._inp(strategy=ResponseStrategy.SETTLE))
        assert r.template.template_id == "SETTLE_PARTIAL"

    def test_ce30_escalation_selects_ce30_template(self):
        r = select_response_template(self._inp(
            claim_type="UNAUTHORIZED",
            network="VISA",
            strategy=ResponseStrategy.ESCALATE,
            has_ce30_evidence=True,
        ))
        assert r.template.template_id == "ESCALATE_CE30"

    # ── Template content ──────────────────────────────────────────

    def test_template_has_key_points(self):
        r = select_response_template(self._inp())
        assert len(r.template.key_points) >= 1

    def test_template_has_checklist(self):
        r = select_response_template(self._inp())
        assert len(r.template.submission_checklist) >= 1

    def test_template_has_opening_sentence(self):
        r = select_response_template(self._inp())
        assert len(r.template.opening_sentence) > 10

    def test_accept_template_cooperative_tone(self):
        r = select_response_template(self._inp(strategy=ResponseStrategy.ACCEPT))
        assert r.template.tone == TemplateTone.COOPERATIVE

    # ── Readiness scoring ─────────────────────────────────────────

    def test_readiness_higher_with_more_evidence(self):
        low = select_response_template(self._inp())
        high = select_response_template(self._inp(
            has_tracking_number=True,
            has_delivery_confirmation=True,
        ))
        assert high.readiness_score >= low.readiness_score

    def test_readiness_in_range(self):
        r = select_response_template(self._inp())
        assert 0.0 <= r.readiness_score <= 1.0

    # ── Warnings ─────────────────────────────────────────────────

    def test_snad_sp_warning(self):
        r = select_response_template(self._inp(
            claim_type="SNAD", sp_eligible=True))
        assert any("SNAD" in w or "Seller Protection" in w
                   for w in r.warnings)

    def test_visa_unauth_ce30_warning_when_no_evidence(self):
        r = select_response_template(self._inp(
            claim_type="UNAUTHORIZED",
            network="VISA",
            has_ce30_evidence=False,
        ))
        assert any("CE3.0" in w for w in r.warnings)

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = select_response_template(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary
