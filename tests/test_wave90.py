"""Wave 90 — Dispute Portfolio Risk Scorer tests."""
import pytest
from oracle.portfolio_risk_scorer import (
    PortfolioRiskLevel,
    DisputeSnapshot, ConcentrationRisk, PortfolioRiskReport,
    score_portfolio,
    _risk_level, _concentration, _top_pct_by_amount,
)


def _snap(dispute_id="D-001", claim_type="INR", amount=100.0,
          win_prob=0.60, days=7):
    return DisputeSnapshot(
        dispute_id=dispute_id,
        claim_type=claim_type,
        amount_usd=amount,
        win_probability=win_prob,
        days_until_deadline=days,
    )


# ─── Risk level thresholds ────────────────────────────────────────────────────

class TestRiskLevel:
    def test_zero_loss_low(self):
        assert _risk_level(0.0) == PortfolioRiskLevel.LOW

    def test_499_low(self):
        assert _risk_level(499.0) == PortfolioRiskLevel.LOW

    def test_500_moderate(self):
        assert _risk_level(500.0) == PortfolioRiskLevel.MODERATE

    def test_2000_high(self):
        assert _risk_level(2000.0) == PortfolioRiskLevel.HIGH

    def test_5000_severe(self):
        assert _risk_level(5000.0) == PortfolioRiskLevel.SEVERE


# ─── Concentration helper ─────────────────────────────────────────────────────

class TestConcentration:
    def test_all_same_claim_type_flag(self):
        disputes = [_snap(f"D-{i}", "UNAUTH") for i in range(5)]
        c = _concentration(disputes)
        assert c.flag is True
        assert c.dominant_claim_type == "UNAUTH"
        assert c.dominant_fraction == 1.0

    def test_mixed_no_flag(self):
        disputes = [
            _snap("D-1", "UNAUTH"),
            _snap("D-2", "INR"),
            _snap("D-3", "SNAD"),
            _snap("D-4", "OTHER"),
        ]
        c = _concentration(disputes)
        assert c.flag is False

    def test_empty_no_flag(self):
        c = _concentration([])
        assert c.flag is False

    def test_description_non_empty(self):
        disputes = [_snap(f"D-{i}", "INR") for i in range(3)]
        c = _concentration(disputes)
        assert len(c.description) > 5


# ─── Top pct by amount ────────────────────────────────────────────────────────

class TestTopPctByAmount:
    def test_empty_returns_empty(self):
        assert _top_pct_by_amount([]) == []

    def test_top_one_of_five(self):
        disputes = [_snap(f"D-{i}", amount=float(i * 10)) for i in range(1, 6)]
        top = _top_pct_by_amount(disputes, 0.20)
        assert len(top) == 1
        assert top[0] == "D-5"

    def test_all_returned_when_pct_is_1(self):
        disputes = [_snap(f"D-{i}") for i in range(5)]
        top = _top_pct_by_amount(disputes, 1.0)
        assert len(top) == 5


# ─── score_portfolio ──────────────────────────────────────────────────────────

class TestScorePortfolio:
    def test_empty_portfolio_returns_low(self):
        r = score_portfolio("M-001", [])
        assert r.portfolio_risk_level == PortfolioRiskLevel.LOW
        assert r.total_disputes == 0

    def test_total_exposure_sum(self):
        disputes = [_snap("D-1", amount=100.0), _snap("D-2", amount=200.0)]
        r = score_portfolio("M-001", disputes)
        assert abs(r.total_exposure_usd - 300.0) < 0.01

    def test_expected_loss_formula(self):
        # (1-0.6)*100 + (1-0.4)*200 = 40 + 120 = 160
        disputes = [
            _snap("D-1", amount=100.0, win_prob=0.6),
            _snap("D-2", amount=200.0, win_prob=0.4),
        ]
        r = score_portfolio("M-001", disputes)
        assert abs(r.expected_loss_usd - 160.0) < 0.01

    def test_expected_recovery_plus_loss_equals_exposure(self):
        disputes = [_snap(f"D-{i}", amount=100.0, win_prob=0.5) for i in range(4)]
        r = score_portfolio("M-001", disputes)
        assert abs(r.expected_loss_usd + r.expected_recovery_usd - r.total_exposure_usd) < 0.01

    def test_severe_risk_level_on_large_loss(self):
        # 10 disputes × $5000 × (1-0.1) loss each = $45000 expected loss
        disputes = [_snap(f"D-{i}", amount=5000.0, win_prob=0.1) for i in range(10)]
        r = score_portfolio("M-001", disputes)
        assert r.portfolio_risk_level == PortfolioRiskLevel.SEVERE

    def test_critical_disputes_detected(self):
        disputes = [
            _snap("D-crit", days=1),
            _snap("D-ok",   days=10),
        ]
        r = score_portfolio("M-001", disputes)
        assert "D-crit" in r.critical_disputes
        assert "D-ok" not in r.critical_disputes

    def test_concentration_flag_propagated(self):
        disputes = [_snap(f"D-{i}", "UNAUTH") for i in range(6)]
        r = score_portfolio("M-001", disputes)
        assert r.concentration.flag is True

    def test_avg_win_probability(self):
        disputes = [
            _snap("D-1", win_prob=0.4),
            _snap("D-2", win_prob=0.8),
        ]
        r = score_portfolio("M-001", disputes)
        assert abs(r.avg_win_probability - 0.6) < 1e-4

    def test_recommendation_non_empty(self):
        r = score_portfolio("M-001", [_snap()])
        assert len(r.recommendation) > 10

    def test_risk_factors_critical_in_list(self):
        disputes = [_snap("D-crit", days=0)]
        r = score_portfolio("M-001", disputes)
        assert any("deadline" in f.lower() or "critical" in f.lower()
                   for f in r.risk_factors)

    def test_high_value_disputes_non_empty(self):
        disputes = [_snap(f"D-{i}", amount=float((i+1)*100)) for i in range(5)]
        r = score_portfolio("M-001", disputes)
        assert len(r.high_value_disputes) >= 1
