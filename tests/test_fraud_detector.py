"""
Buyer Risk Profiler tests — scoring logic, serial disputer detection, ES queries.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch


# ── Scoring logic ─────────────────────────────────────────────────────────────

def test_zero_disputes_is_low_risk():
    from agents.fraud_detector import _score_buyer
    score, level, is_serial, flags = _score_buyer(0, 0, 0, 0.0)
    assert level == "low"
    assert is_serial is False
    assert score == 0.0

def test_two_disputes_90d_is_low_or_medium():
    from agents.fraud_detector import _score_buyer
    score, level, is_serial, flags = _score_buyer(2, 2, 0, 0.0)
    assert level in ("low", "medium")  # 2 disputes alone = low risk (score 0.15)
    assert is_serial is False

def test_three_disputes_90d_is_serial():
    from agents.fraud_detector import _score_buyer
    score, level, is_serial, flags = _score_buyer(3, 3, 0, 0.0)
    assert is_serial is True

def test_five_disputes_90d_is_critical():
    from agents.fraud_detector import _score_buyer
    score, level, is_serial, flags = _score_buyer(5, 7, 0, 0.0)
    assert level in ("high", "critical")
    assert is_serial is True

def test_reversals_boost_score():
    from agents.fraud_detector import _score_buyer
    # 1 dispute but seller won before → strong fraud signal
    score_no_reversal, _, _, _ = _score_buyer(1, 1, 0, 0.0)
    score_with_reversal, _, _, _ = _score_buyer(1, 1, 1, 0.0)
    assert score_with_reversal > score_no_reversal

def test_two_reversals_raises_risk_level():
    from agents.fraud_detector import _score_buyer
    # 2 disputes + 2 reversals: score = 0.15 + 0.30 = 0.45 → "medium" (≥0.20, <0.45 is high)
    # Actually 0.45 is exactly the boundary — medium is <0.45, high is ≥0.45
    score, level, is_serial, flags = _score_buyer(2, 2, 2, 0.0)
    assert level in ("medium", "high")  # well above low, reversal flag present
    assert any("reversal" in f.lower() or "reversed" in f.lower() for f in flags)

def test_high_dispute_rate_adds_flag():
    from agents.fraud_detector import _score_buyer
    _, _, _, flags = _score_buyer(2, 2, 0, 0.6)  # 60% dispute rate
    assert any("dispute rate" in f.lower() for f in flags)

def test_score_capped_at_1():
    from agents.fraud_detector import _score_buyer
    score, _, _, _ = _score_buyer(10, 20, 5, 0.9)
    assert score <= 1.0


# ── Recommendations ───────────────────────────────────────────────────────────

def test_low_risk_recommendation():
    from agents.fraud_detector import _make_recommendation
    rec = _make_recommendation("low", False, 0.05)
    assert "Normal" in rec or "normal" in rec.lower()

def test_critical_recommendation_mentions_id():
    from agents.fraud_detector import _make_recommendation
    rec = _make_recommendation("critical", True, 0.9)
    assert "verification" in rec.lower() or "serial" in rec.lower()

def test_high_risk_recommendation_mentions_signature():
    from agents.fraud_detector import _make_recommendation
    rec = _make_recommendation("high", False, 0.6)
    assert "signature" in rec.lower()


# ── score_buyer with no ES ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_buyer_no_es_returns_low_risk():
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)

    from agents.fraud_detector import score_buyer
    profile = await score_buyer("buyer@example.com", "merchant1")

    assert profile.risk_level == "low"
    assert profile.risk_score == 0.0
    assert profile.is_serial_disputer is False
    assert "No ES" in profile.recommendation or "unavailable" in profile.recommendation


# ── score_buyer with mocked ES ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_buyer_serial_disputer_detected():
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "key"

    # Return 4 disputes in 90d, 6 in 180d, 1 reversal, 10 transactions
    call_sequence = [4, 6, 1, 10]
    call_index = [0]

    async def mock_count(buyer_email, since, es_url, es_key):
        idx = call_index[0]
        call_index[0] += 1
        return call_sequence[idx] if idx < len(call_sequence) else 0

    with patch("agents.fraud_detector._count_buyer_disputes", side_effect=mock_count), \
         patch("agents.fraud_detector._count_buyer_disputes") as p1, \
         patch("agents.fraud_detector._count_buyer_reversals") as p2, \
         patch("agents.fraud_detector._count_buyer_transactions") as p3:

        # Set up specific mocks
        # disputes_90d=4, disputes_180d=6, reversals=1, transactions=10
        async def disputes_mock(email, since, url, key):
            return 4

        async def reversals_mock(email, since, url, key):
            return 1

        async def transactions_mock(email, since, url, key):
            return 10

        p1.side_effect = disputes_mock
        p2.side_effect = reversals_mock
        p3.side_effect = transactions_mock

        # Mock 180d call too — it's also _count_buyer_disputes
        # Trick: patch the whole thing at the module level
        with patch("agents.fraud_detector._count_buyer_disputes", side_effect=[4, 6]):
            from agents.fraud_detector import score_buyer
            profile = await score_buyer("fraud@example.com", "merchant1")

    # 4 disputes + 1 reversal should trigger serial/high
    assert profile.is_serial_disputer is True or profile.risk_level in ("high", "critical")


@pytest.mark.asyncio
async def test_score_buyer_low_history_is_low_risk():
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "key"

    with patch("agents.fraud_detector._count_buyer_disputes", new_callable=AsyncMock, return_value=0), \
         patch("agents.fraud_detector._count_buyer_reversals", new_callable=AsyncMock, return_value=0), \
         patch("agents.fraud_detector._count_buyer_transactions", new_callable=AsyncMock, return_value=50):

        from agents.fraud_detector import score_buyer
        profile = await score_buyer("good@example.com", "merchant1")

    assert profile.risk_level == "low"
    assert profile.is_serial_disputer is False
    assert profile.risk_score == 0.0


# ── BuyerRiskProfile dataclass ────────────────────────────────────────────────

def test_buyer_risk_profile_has_checked_at():
    from agents.fraud_detector import BuyerRiskProfile
    profile = BuyerRiskProfile(
        buyer_email="test@test.com",
        merchant_id="m1",
        total_disputes_90d=1,
        total_disputes_180d=1,
        disputes_reversed_90d=0,
        total_transactions_90d=100,
        dispute_rate=0.01,
        risk_level="low",
        risk_score=0.1,
        is_serial_disputer=False,
        recommendation="Normal",
    )
    assert profile.checked_at != ""
    assert "2026" in profile.checked_at or "202" in profile.checked_at  # ISO datetime


# ── API endpoint tests ────────────────────────────────────────────────────────

def test_buyer_risk_api_endpoint_exists():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)

    with patch("agents.fraud_detector.score_buyer", new_callable=AsyncMock) as mock_score:
        from agents.fraud_detector import BuyerRiskProfile
        mock_score.return_value = BuyerRiskProfile(
            buyer_email="test@test.com", merchant_id="m1",
            total_disputes_90d=0, total_disputes_180d=0,
            disputes_reversed_90d=0, total_transactions_90d=5,
            dispute_rate=0.0, risk_level="low", risk_score=0.0,
            is_serial_disputer=False, recommendation="Normal",
        )
        resp = client.get("/api/buyer-risk?buyer_email=test@test.com")

    assert resp.status_code == 200
    data = resp.json()
    assert data["risk_level"] == "low"


def test_buyer_risk_api_missing_email():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/buyer-risk")
    # Missing required query param → 422
    assert resp.status_code == 422


def test_account_health_api_endpoint_exists():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)

    with patch("agents.risk_monitor.get_account_health", new_callable=AsyncMock) as mock_health:
        from agents.risk_monitor import AccountHealth, DisputeRateWindow
        mock_health.return_value = AccountHealth(
            merchant_id="m1",
            window_30d=DisputeRateWindow(30, 0, 0.0, 0, 0.0),
            window_90d=DisputeRateWindow(90, 0, 0.0, 0, 0.0),
            threshold_status="safe",
            threshold_description="Normal",
            next_threshold=0.005,
            next_threshold_name="elevated",
            safe_fight_quota=999,
            threshold_breach_at=999,
            projected_rate_all_accepted=0.0,
            projected_rate_all_fought=0.0,
            fight_posture="fight_freely",
            posture_reason="All clear",
            open_disputes=0,
            checked_at="2026-11-06T00:00:00Z",
        )
        resp = client.get("/api/account-health/merchant1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["threshold_status"] == "safe"
    assert data["fight_posture"] == "fight_freely"
