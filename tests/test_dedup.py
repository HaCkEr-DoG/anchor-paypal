"""
Dedup tests — ES op_type=create atomic dedup + memory fallback.
"""
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# ── Basic dedup behavior ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_event_not_duplicate():
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    mock_resp = MagicMock()
    mock_resp.status_code = 201  # Created — new event

    import httpx
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from agents.watcher import _is_duplicate
        result = await _is_duplicate("event-new-001")

    assert result is False


@pytest.mark.asyncio
async def test_duplicate_event_detected():
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    mock_resp = MagicMock()
    mock_resp.status_code = 409  # Conflict — already exists

    import httpx
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from agents.watcher import _is_duplicate
        result = await _is_duplicate("event-dup-001")

    assert result is True


@pytest.mark.asyncio
async def test_es_failure_falls_back_to_memory():
    """When ES is unreachable, dedup falls back to in-memory set."""
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    import httpx

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(side_effect=Exception("ES unreachable"))
        mock_cls.return_value = mock_client

        from agents import watcher
        # Reset memory dict for clean test
        watcher._processed_events.clear()

        result1 = await watcher._is_duplicate("event-mem-001")
        result2 = await watcher._is_duplicate("event-mem-001")  # second call = dup

    assert result1 is False  # first time — not a duplicate
    assert result2 is True   # second time — duplicate in memory


@pytest.mark.asyncio
async def test_no_es_config_uses_memory():
    """Without ES creds, falls back to memory."""
    os.environ.pop("ELASTICSEARCH_URL", None)
    os.environ.pop("ELASTICSEARCH_API_KEY", None)

    from agents import watcher
    watcher._processed_events.clear()

    result1 = await watcher._is_duplicate("event-noes-001")
    result2 = await watcher._is_duplicate("event-noes-001")

    assert result1 is False
    assert result2 is True


@pytest.mark.asyncio
async def test_concurrent_duplicates_only_one_passes():
    """Simulating concurrent calls with ES — only the 201 wins, 409s are duplicates."""
    os.environ["ELASTICSEARCH_URL"] = "http://es:9200"
    os.environ["ELASTICSEARCH_API_KEY"] = "test_key"

    call_count = 0

    async def mock_put(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        # First call gets 201, rest get 409
        resp.status_code = 201 if call_count == 1 else 409
        return resp

    import httpx
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = mock_put
        mock_cls.return_value = mock_client

        from agents.watcher import _is_duplicate
        results = await asyncio.gather(*[_is_duplicate("event-concurrent") for _ in range(5)])

    # Exactly one False (not duplicate), rest are True (duplicate)
    false_count = results.count(False)
    true_count = results.count(True)
    assert false_count == 1, f"Expected exactly 1 non-duplicate, got {false_count}"
    assert true_count == 4, f"Expected 4 duplicates, got {true_count}"
