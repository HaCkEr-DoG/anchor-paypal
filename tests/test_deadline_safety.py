from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agents.deadline_tracker import OpenDispute, check_deadlines


@pytest.mark.asyncio
async def test_deadline_sweep_never_auto_accepts_when_actions_disabled(monkeypatch):
    dispute = OpenDispute(
        dispute_id="PP-D-1", merchant_id="merchant", amount=42,
        reason_code="INR", opened_at="2026-01-01T00:00:00+00:00",
        response_deadline=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    monkeypatch.setattr("agents.deadline_tracker.get_open_disputes", AsyncMock(return_value=[dispute]))
    monkeypatch.setattr("agents.deadline_tracker.update_alert_level", AsyncMock(return_value=True))
    notify = AsyncMock()
    accept = AsyncMock()

    result = await check_deadlines("merchant", notify, accept, allow_auto_action=False)

    accept.assert_not_awaited()
    notify.assert_awaited_once()
    assert "Manual action required" in notify.await_args.args[0]
    assert result["auto_acted"] == 0

