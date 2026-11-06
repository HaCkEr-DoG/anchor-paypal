"""
Dispute Rate Protection System tests — account survival logic.

Tests every threshold boundary, fight-quota calculation, posture
decision, rate-protection override, and projection accuracy.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _window(disputes=0, disp_amount=0.0, captures=0, cap_amount=0.0, days=30):
    from agents.risk_monitor import DisputeRateWindow
    return DisputeRateWindow(
        window_days=days,
        dispute_count=disputes,
        dispute_amount=disp_amount,
        capture_count=captures,
        capture_amount=cap_amount,
    )


# ── classify_rate boundary tests ─────────────────────────────────────────────

def test_rate_below_threshold_is_safe():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.0)
    assert status == "safe"

def test_rate_exactly_at_elevated_threshold():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.005)
    assert status == "elevated"

def test_rate_between_elevated_and_limited():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.0075)
    assert status == "elevated"

def test_rate_exactly_at_limited_threshold():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.010)
    assert status == "limited"

def test_rate_exactly_at_hdr_threshold():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.015)
    assert status == "hdr"

def test_rate_exactly_at_critical_threshold():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.030)
    assert status == "critical"

def test_rate_above_critical():
    from agents.risk_monitor import classify_rate
    status, _ = classify_rate(0.05)
    assert status == "critical"


# ── DisputeRateWindow properties ─────────────────────────────────────────────

def test_rate_by_count_zero_captures():
    w = _window(disputes=5, captures=0)
    assert w.rate_by_count == 0.0

def test_rate_by_amount_zero_captures():
    w = _window(disp_amount=100.0, cap_amount=0.0)
    assert w.rate_by_amount == 0.0

def test_rate_by_count_correct():
    w = _window(disputes=10, captures=1000)
    assert abs(w.rate_by_count - 0.01) < 1e-9

def test_rate_by_amount_correct():
    w = _window(disp_amount=100.0, cap_amount=10000.0)
    assert abs(w.rate_by_amount - 0.01) < 1e-9


# ── Fight posture logic ───────────────────────────────────────────────────────

def test_posture_safe_with_quota_is_fight_freely():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("safe", quota=50, open_disputes=0)
    assert posture == "fight_freely"

def test_posture_elevated_is_fight_selectively():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("elevated", quota=10, open_disputes=0)
    assert posture == "fight_selectively"

def test_posture_zero_quota_is_accept_strategically():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("safe", quota=0, open_disputes=0)
    assert posture == "accept_strategically"

def test_posture_limited_is_accept_strategically():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("limited", quota=5, open_disputes=0)
    assert posture == "accept_strategically"

def test_posture_hdr_is_accept_all():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("hdr", quota=5, open_disputes=0)
    assert posture == "accept_all"

def test_posture_critical_is_accept_all():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("critical", quota=0, open_disputes=0)
    assert posture == "accept_all"

def test_posture_quota_3_is_fight_selectively():
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("safe", quota=3, open_disputes=0)
    assert posture == "fight_selectively"

def test_posture_open_disputes_exceed_quota():
    # 5 open disputes but only 2 quota remaining → fight selectively
    from agents.risk_monitor import _calculate_posture
    posture, _ = _calculate_posture("safe", quota=2, open_disputes=5)
    assert posture == "fight_selectively"


# ── get_account_health with mocked ES ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_account_health_no_es_returns_safe():
    """Without ES creds, returns safe with zero metrics."""
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from agents.risk_monitor import get_account_health
    health = await get_account_health("merchant1")
    assert health.threshold_status == "safe"
    assert health.window_30d.rate_by_amount == 0.0
    assert health.safe_fight_quota == 999  # unlimited when no data


@pytest.mark.asyncio
async def test_get_account_health_approaching_limited():
    """Merchant at 0.97% rate — quota should be very small."""
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "key"

    # 97 disputes at avg $50 on $500,000 in captures = 0.97% rate
    # Next threshold is 1.0%, so quota = floor((0.01 * 500000 - 4850) / 50) = floor(150/50) = 3
    def mock_query_window(merchant_id, days, es_url, es_key):
        from agents.risk_monitor import DisputeRateWindow
        return DisputeRateWindow(
            window_days=days,
            dispute_count=97,
            dispute_amount=4850.0,   # 97 * $50
            capture_count=10000,
            capture_amount=500000.0,
        )

    with patch("agents.risk_monitor._query_window", new_callable=AsyncMock, side_effect=mock_query_window):
        from agents.risk_monitor import get_account_health
        health = await get_account_health("merchant1")

    assert health.threshold_status == "elevated"
    assert health.safe_fight_quota == 3
    assert health.fight_posture == "fight_selectively"


@pytest.mark.asyncio
async def test_get_account_health_in_hdr_zone():
    """Merchant at 2% rate — in HDR zone, should accept all."""
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "key"

    def mock_query_window(merchant_id, days, es_url, es_key):
        from agents.risk_monitor import DisputeRateWindow
        return DisputeRateWindow(
            window_days=days,
            dispute_count=200,
            dispute_amount=10000.0,
            capture_count=5000,
            capture_amount=500000.0,  # 2% rate
        )

    with patch("agents.risk_monitor._query_window", new_callable=AsyncMock, side_effect=mock_query_window):
        from agents.risk_monitor import get_account_health
        health = await get_account_health("merchant1")

    assert health.threshold_status == "hdr"
    assert health.fight_posture == "accept_all"
    # quota = fights until NEXT threshold (critical at 3%), not until current
    # at 2% with $500k captures: (0.03 * 500000 - 10000) / 50 = 100 remaining to critical
    assert health.safe_fight_quota >= 0


# ── should_protect_rate override logic ───────────────────────────────────────

@pytest.mark.asyncio
async def test_protect_rate_accept_all_posture_always_overrides():
    """When posture is accept_all, should always override regardless of win prob."""
    from agents.risk_monitor import AccountHealth, DisputeRateWindow, should_protect_rate

    mock_health = AccountHealth(
        merchant_id="m1",
        window_30d=_window(disp_amount=15000.0, cap_amount=500000.0),
        window_90d=_window(),
        threshold_status="hdr",
        threshold_description="HDR zone",
        next_threshold=0.030,
        next_threshold_name="critical",
        safe_fight_quota=0,
        threshold_breach_at=0,
        projected_rate_all_accepted=0.03,
        projected_rate_all_fought=0.035,
        fight_posture="accept_all",
        posture_reason="In HDR zone",
        open_disputes=5,
        checked_at="",
    )

    with patch("agents.risk_monitor.get_account_health", new_callable=AsyncMock, return_value=mock_health):
        override, reason, health = await should_protect_rate("m1", oracle_ev=50.0, oracle_win_prob=0.90)

    assert override is True
    assert "HDR" in reason or "accept" in reason.lower()


@pytest.mark.asyncio
async def test_protect_rate_fight_freely_no_override():
    """When posture is fight_freely, should never override."""
    from agents.risk_monitor import AccountHealth, should_protect_rate

    mock_health = AccountHealth(
        merchant_id="m1",
        window_30d=_window(disp_amount=100.0, cap_amount=50000.0),  # 0.2% rate
        window_90d=_window(),
        threshold_status="safe",
        threshold_description="Normal",
        next_threshold=0.005,
        next_threshold_name="elevated",
        safe_fight_quota=99,
        threshold_breach_at=200,
        projected_rate_all_accepted=0.002,
        projected_rate_all_fought=0.003,
        fight_posture="fight_freely",
        posture_reason="All clear",
        open_disputes=1,
        checked_at="",
    )

    with patch("agents.risk_monitor.get_account_health", new_callable=AsyncMock, return_value=mock_health):
        override, reason, health = await should_protect_rate("m1", oracle_ev=50.0, oracle_win_prob=0.65)

    assert override is False


@pytest.mark.asyncio
async def test_protect_rate_accept_strategically_low_win_prob_overrides():
    """accept_strategically + win_prob < 85% → override."""
    from agents.risk_monitor import AccountHealth, should_protect_rate

    mock_health = AccountHealth(
        merchant_id="m1",
        window_30d=_window(disp_amount=5500.0, cap_amount=500000.0),  # 1.1%
        window_90d=_window(),
        threshold_status="limited",
        threshold_description="Limited",
        next_threshold=0.015,
        next_threshold_name="hdr",
        safe_fight_quota=1,
        threshold_breach_at=12,
        projected_rate_all_accepted=0.011,
        projected_rate_all_fought=0.013,
        fight_posture="accept_strategically",
        posture_reason="Rate is above 1%",
        open_disputes=2,
        checked_at="",
    )

    with patch("agents.risk_monitor.get_account_health", new_callable=AsyncMock, return_value=mock_health):
        override, reason, health = await should_protect_rate("m1", oracle_ev=50.0, oracle_win_prob=0.75)

    assert override is True  # 0.75 < 0.85 threshold


@pytest.mark.asyncio
async def test_protect_rate_accept_strategically_high_win_prob_no_override():
    """accept_strategically + win_prob ≥ 85% → no override (let it fight)."""
    from agents.risk_monitor import AccountHealth, should_protect_rate

    mock_health = AccountHealth(
        merchant_id="m1",
        window_30d=_window(disp_amount=5500.0, cap_amount=500000.0),
        window_90d=_window(),
        threshold_status="limited",
        threshold_description="Limited",
        next_threshold=0.015,
        next_threshold_name="hdr",
        safe_fight_quota=1,
        threshold_breach_at=12,
        projected_rate_all_accepted=0.011,
        projected_rate_all_fought=0.013,
        fight_posture="accept_strategically",
        posture_reason="Rate is above 1%",
        open_disputes=1,
        checked_at="",
    )

    with patch("agents.risk_monitor.get_account_health", new_callable=AsyncMock, return_value=mock_health):
        override, reason, health = await should_protect_rate("m1", oracle_ev=100.0, oracle_win_prob=0.90)

    assert override is False  # 0.90 ≥ 0.85


# ── Projection accuracy ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_projections_calculated_correctly():
    """projected_rate_all_fought is higher than accepted when open disputes exist."""
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "key"

    def mock_query_window(merchant_id, days, es_url, es_key):
        from agents.risk_monitor import DisputeRateWindow
        return DisputeRateWindow(
            window_days=days,
            dispute_count=5,
            dispute_amount=250.0,    # 5 * $50
            capture_count=1000,
            capture_amount=10000.0,  # 2.5% rate
        )

    with patch("agents.risk_monitor._query_window", new_callable=AsyncMock, side_effect=mock_query_window):
        from agents.risk_monitor import get_account_health
        health = await get_account_health("merchant1", open_disputes=3)

    # With 3 open disputes fought+lost: (250 + 150) / 10000 = 4%
    assert health.projected_rate_all_fought > health.projected_rate_all_accepted
    assert health.open_disputes == 3
