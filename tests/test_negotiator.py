"""
Settlement Negotiation Engine tests — EV model, offer fractions, acceptance prob.
"""
import pytest
from unittest.mock import AsyncMock, patch


# ── Offer fraction logic ──────────────────────────────────────────────────────

def test_unauthorized_never_negotiates():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    assert _offer_fraction(0.50, ClaimType.UNAUTHORIZED) == 0.0


def test_high_win_prob_no_offer():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    assert _offer_fraction(0.70, ClaimType.ITEM_NOT_RECEIVED) == 0.0
    assert _offer_fraction(0.85, ClaimType.ITEM_NOT_RECEIVED) == 0.0


def test_low_win_prob_no_offer():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    assert _offer_fraction(0.34, ClaimType.ITEM_NOT_RECEIVED) == 0.0
    assert _offer_fraction(0.10, ClaimType.ITEM_NOT_RECEIVED) == 0.0


def test_mid_range_produces_offer():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    # 65-70% → 30% offer (lean merchant)
    assert _offer_fraction(0.67, ClaimType.ITEM_NOT_RECEIVED) == 0.30
    # 50-65% → 50% offer (true uncertainty)
    assert _offer_fraction(0.55, ClaimType.ITEM_NOT_RECEIVED) == 0.50
    # 35-50% → 65% offer (lean buyer)
    assert _offer_fraction(0.40, ClaimType.ITEM_NOT_RECEIVED) == 0.65


def test_zone_boundary_exactly_70():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    # At exactly 70%, no offer (fight directly)
    assert _offer_fraction(0.70, ClaimType.ITEM_NOT_RECEIVED) == 0.0


def test_zone_boundary_exactly_35():
    from agents.negotiator import _offer_fraction
    from oracle.decision import ClaimType
    # At exactly 35%, 65% offer
    assert _offer_fraction(0.35, ClaimType.ITEM_NOT_RECEIVED) == 0.65


# ── Acceptance probability ────────────────────────────────────────────────────

def test_acceptance_prob_capped():
    from agents.negotiator import _estimate_acceptance_prob
    from oracle.decision import ClaimType
    # Maximum acceptance probability
    prob = _estimate_acceptance_prob(1.0, ClaimType.ITEM_NOT_RECEIVED)
    assert prob <= 0.85


def test_acceptance_prob_minimum():
    from agents.negotiator import _estimate_acceptance_prob
    from oracle.decision import ClaimType
    prob = _estimate_acceptance_prob(0.0, ClaimType.ITEM_NOT_RECEIVED)
    assert prob >= 0.30


def test_snad_acceptance_boost():
    from agents.negotiator import _estimate_acceptance_prob
    from oracle.decision import ClaimType
    inr_prob = _estimate_acceptance_prob(0.50, ClaimType.ITEM_NOT_RECEIVED)
    snad_prob = _estimate_acceptance_prob(0.50, ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED)
    assert snad_prob > inr_prob


# ── evaluate_negotiation ──────────────────────────────────────────────────────

def test_unauthorized_dispute_no_negotiate():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    result = evaluate_negotiation(
        dispute_amount=200.0,
        win_probability=0.50,
        claim_type=ClaimType.UNAUTHORIZED,
        ev_fight=50.0,
    )
    assert result.should_negotiate is False
    assert result.offer_amount == 0.0


def test_high_ev_fight_negotiation_outcome():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    # When EV(fight) is very positive and win_prob is very high, shouldn't negotiate
    result = evaluate_negotiation(
        dispute_amount=200.0,
        win_probability=0.90,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        ev_fight=150.0,
    )
    assert result.should_negotiate is False


def test_mid_range_negotiation_evaluates():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    result = evaluate_negotiation(
        dispute_amount=200.0,
        win_probability=0.55,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        ev_fight=30.0,
    )
    # Result should have computed EVs regardless of outcome
    assert result.ev_negotiate != 0.0
    assert result.offer_fraction == 0.50
    assert result.offer_amount == 100.0  # 50% of 200


def test_negotiation_result_has_reasoning():
    from agents.negotiator import evaluate_negotiation
    from oracle.decision import ClaimType
    result = evaluate_negotiation(
        dispute_amount=100.0,
        win_probability=0.50,
        claim_type=ClaimType.ITEM_NOT_RECEIVED,
        ev_fight=5.0,
    )
    assert isinstance(result.reasoning, str)
    assert len(result.reasoning) > 0


def test_no_offer_reason_messages():
    from agents.negotiator import _no_offer_reason
    high_msg = _no_offer_reason(0.80)
    assert "70%" in high_msg or "fight" in high_msg.lower()
    low_msg = _no_offer_reason(0.20)
    assert "accept" in low_msg.lower() or "35%" in low_msg


# ── submit_offer (mocked HTTP) ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_offer_success():
    from agents.negotiator import submit_offer
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        result = await submit_offer("PP-D-123", 50.0, "https://api.sandbox.paypal.com", "token123")
    assert result is True


@pytest.mark.asyncio
async def test_submit_offer_failure_returns_false():
    from agents.negotiator import submit_offer
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        result = await submit_offer("PP-D-123", 50.0, "https://api.sandbox.paypal.com", "token123")
    assert result is False


@pytest.mark.asyncio
async def test_submit_offer_network_error_returns_false():
    from agents.negotiator import submit_offer
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=Exception("network error"))
        mock_cls.return_value = mock_client
        result = await submit_offer("PP-D-123", 50.0, "https://api.sandbox.paypal.com", "token123")
    assert result is False


# ── API endpoint ──────────────────────────────────────────────────────────────

def test_negotiation_calculation_via_oracle_endpoint():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # The /api/oracle/calculate endpoint exists and returns a recommendation
    resp = client.get(
        "/api/oracle/calculate?amount=200&claim_type=ITEM_NOT_RECEIVED"
        "&has_tracking=true&has_delivery=true"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "recommendation" in data
    assert "win_probability" in data
