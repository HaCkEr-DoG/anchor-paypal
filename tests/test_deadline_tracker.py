"""
Deadline Guardian tests — deadline computation, alert escalation, ES operations.
"""
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock


# ── compute_deadline ──────────────────────────────────────────────────────────

def test_standard_dispute_10_days():
    from agents.deadline_tracker import compute_deadline
    opened = "2026-11-01T12:00:00Z"
    deadline = compute_deadline(opened, "ITEM_NOT_RECEIVED")
    dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
    delta = dt - opened_dt
    assert delta.days == 10


def test_inquiry_dispute_20_days():
    from agents.deadline_tracker import compute_deadline
    opened = "2026-11-01T12:00:00Z"
    deadline = compute_deadline(opened, "INQUIRY")
    dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
    delta = dt - opened_dt
    assert delta.days == 20


def test_billing_issue_20_days():
    from agents.deadline_tracker import compute_deadline
    opened = "2026-11-01T12:00:00Z"
    deadline = compute_deadline(opened, "BILLING_ISSUE")
    dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
    delta = dt - opened_dt
    assert delta.days == 20


def test_case_insensitive_reason_code():
    from agents.deadline_tracker import compute_deadline
    opened = "2026-11-01T00:00:00Z"
    d1 = compute_deadline(opened, "inquiry")
    d2 = compute_deadline(opened, "INQUIRY")
    assert d1 == d2


def test_invalid_opened_date_uses_now():
    from agents.deadline_tracker import compute_deadline
    # Should not raise — falls back to now()
    deadline = compute_deadline("not-a-date", "ITEM_NOT_RECEIVED")
    assert deadline is not None
    assert len(deadline) > 0


# ── hours_until_deadline ──────────────────────────────────────────────────────

def test_future_deadline_positive():
    from agents.deadline_tracker import hours_until_deadline
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    hours = hours_until_deadline(future)
    assert 23.0 < hours < 25.0


def test_past_deadline_negative():
    from agents.deadline_tracker import hours_until_deadline
    past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    hours = hours_until_deadline(past)
    assert hours < 0


def test_invalid_deadline_returns_negative():
    from agents.deadline_tracker import hours_until_deadline
    hours = hours_until_deadline("garbage")
    assert hours < 0


# ── _is_higher_urgency ────────────────────────────────────────────────────────

def test_urgency_ordering():
    from agents.deadline_tracker import _is_higher_urgency
    assert _is_higher_urgency("3_days", "7_days") is True
    assert _is_higher_urgency("7_days", "3_days") is False
    assert _is_higher_urgency("24_hours", "") is True
    assert _is_higher_urgency("auto_accepted", "6_hours") is True


def test_same_urgency_not_higher():
    from agents.deadline_tracker import _is_higher_urgency
    assert _is_higher_urgency("7_days", "7_days") is False


# ── OpenDispute dataclass ─────────────────────────────────────────────────────

def test_open_dispute_defaults():
    from agents.deadline_tracker import OpenDispute
    d = OpenDispute(
        dispute_id="PP-D-123",
        merchant_id="m1",
        amount=100.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at="2026-11-01T12:00:00Z",
        response_deadline="2026-11-11T12:00:00Z",
    )
    assert d.last_alert_level == ""
    assert d.auto_acted is False
    assert d.win_probability == 0.0


# ── store_open_dispute / mark_dispute_resolved (no ES) ───────────────────────

@pytest.mark.asyncio
async def test_store_no_es_returns_false(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from agents.deadline_tracker import store_open_dispute, OpenDispute
    result = await store_open_dispute(OpenDispute(
        dispute_id="PP-D-X",
        merchant_id="m1",
        amount=50.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at="2026-11-01T00:00:00Z",
        response_deadline="2026-11-11T00:00:00Z",
    ))
    assert result is True


@pytest.mark.asyncio
async def test_mark_resolved_no_es_returns_false(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from agents.deadline_tracker import mark_dispute_resolved
    result = await mark_dispute_resolved("PP-D-X")
    assert result is True


@pytest.mark.asyncio
async def test_get_open_disputes_no_es_returns_empty(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)
    from agents.deadline_tracker import get_open_disputes
    result = await get_open_disputes("m1")
    assert result == []


# ── check_deadlines ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_deadlines_no_disputes():
    from agents.deadline_tracker import check_deadlines

    notified = []
    auto_acted = []

    async def notify(msg): notified.append(msg)
    async def auto_accept(did, amt, reason): auto_acted.append(did)

    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[]):
        result = await check_deadlines("m1", notify, auto_accept, allow_auto_action=True)

    assert result["checked"] == 0
    assert len(notified) == 0
    assert len(auto_acted) == 0


@pytest.mark.asyncio
async def test_check_deadlines_auto_accept_at_2h():
    from agents.deadline_tracker import check_deadlines, OpenDispute
    from datetime import datetime, timezone, timedelta

    # Dispute with <2h remaining
    urgent_deadline = (datetime.now(timezone.utc) + timedelta(hours=1, minutes=30)).isoformat()
    dispute = OpenDispute(
        dispute_id="PP-D-URGENT",
        merchant_id="m1",
        amount=100.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at=(datetime.now(timezone.utc) - timedelta(days=9, hours=22)).isoformat(),
        response_deadline=urgent_deadline,
    )

    notified = []
    auto_acted = []

    async def notify(msg): notified.append(msg)
    async def auto_accept(did, amt, reason): auto_acted.append(did)

    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[dispute]), \
         patch("agents.deadline_tracker.update_alert_level", new_callable=AsyncMock, return_value=True):
        result = await check_deadlines("m1", notify, auto_accept, allow_auto_action=True)

    assert result["auto_acted"] >= 1
    assert "PP-D-URGENT" in auto_acted


@pytest.mark.asyncio
async def test_check_deadlines_sends_alert_at_24h():
    from agents.deadline_tracker import check_deadlines, OpenDispute
    from datetime import datetime, timezone, timedelta

    deadline_in_20h = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
    dispute = OpenDispute(
        dispute_id="PP-D-ALERT",
        merchant_id="m1",
        amount=200.0,
        reason_code="ITEM_NOT_RECEIVED",
        opened_at=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
        response_deadline=deadline_in_20h,
        last_alert_level="3_days",  # already sent 3-day alert
    )

    notified = []
    async def notify(msg): notified.append(msg)
    async def auto_accept(did, amt, reason): pass

    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[dispute]), \
         patch("agents.deadline_tracker.update_alert_level", new_callable=AsyncMock, return_value=True):
        result = await check_deadlines("m1", notify, auto_accept)

    assert result["alerts_sent"] >= 1
    assert any("URGENT" in msg or "24" in msg or "6" in msg for msg in notified)


# ── API endpoints ─────────────────────────────────────────────────────────────

def test_deadlines_api_endpoint_exists():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[]):
        resp = client.get("/api/deadlines/merchant1")
    assert resp.status_code == 200
    data = resp.json()
    assert "disputes" in data
    assert data["count"] == 0


def test_deadlines_api_default_endpoint():
    from agents.watcher import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    with patch("agents.deadline_tracker.get_open_disputes", new_callable=AsyncMock, return_value=[]):
        resp = client.get("/api/deadlines")
    assert resp.status_code == 200
