"""
Carrier auto-verification tests — tracking number detection and delivery confirmation.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── Carrier detection ─────────────────────────────────────────────────────────

def test_ups_tracking_number_detected():
    from agents.carrier import detect_carrier
    assert detect_carrier("1Z999AA10123456784") == "ups"

def test_ups_lowercase_accepted():
    from agents.carrier import detect_carrier
    assert detect_carrier("1z999aa10123456784") == "ups"

def test_usps_tracking_number_detected():
    from agents.carrier import detect_carrier
    assert detect_carrier("9400111899223397775493") == "usps"

def test_usps_gs1_prefix_detected():
    from agents.carrier import detect_carrier
    # 92 prefix USPS
    assert detect_carrier("9261292700769829143505") == "usps"

def test_fedex_12_digit_detected():
    from agents.carrier import detect_carrier
    assert detect_carrier("123456789012") == "fedex"

def test_fedex_15_digit_detected():
    from agents.carrier import detect_carrier
    assert detect_carrier("123456789012345") == "fedex"

def test_unknown_format_returns_none():
    from agents.carrier import detect_carrier
    assert detect_carrier("UNKNOWN123") is None

def test_empty_string_returns_none():
    from agents.carrier import detect_carrier
    assert detect_carrier("") is None

def test_spaces_stripped_before_detection():
    from agents.carrier import detect_carrier
    # 1Z prefix with spaces — should still detect as UPS
    assert detect_carrier("1Z 999 AA1 012 345 678 4") == "ups"


# ── verify_delivery: empty input ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_delivery_empty_tracking_returns_none():
    from agents.carrier import verify_delivery
    result = await verify_delivery("")
    assert result is None

@pytest.mark.asyncio
async def test_verify_delivery_none_tracking_returns_none():
    from agents.carrier import verify_delivery
    result = await verify_delivery(None)  # type: ignore
    assert result is None


# ── verify_delivery: no credentials → 17track fallback ───────────────────────

@pytest.mark.asyncio
async def test_verify_delivery_no_creds_returns_none():
    """With no USPS/FedEx/UPS/17track keys, returns None gracefully."""
    import os
    os.environ.pop("USPS_USER_ID", None)
    os.environ.pop("FEDEX_API_KEY", None)
    os.environ.pop("UPS_CLIENT_ID", None)
    os.environ.pop("TRACK17_API_KEY", None)

    from agents.carrier import verify_delivery
    # UPS format — no UPS creds, no 17track → None
    result = await verify_delivery("1Z999AA10123456784")
    assert result is None


# ── USPS delivery confirmed ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_usps_delivered_returns_tracking_result():
    import os
    os.environ["USPS_USER_ID"] = "test_user"

    mock_xml = """
    <TrackSummary>
      <EventTime>12:00 pm</EventTime>
      <EventDate>November 05, 2026</EventDate>
      <Event>DELIVERED</Event>
      <EventCity>SEATTLE</EventCity>
      <EventState>WA</EventState>
    </TrackSummary>
    """
    mock_resp = MagicMock()
    mock_resp.text = mock_xml

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from agents.carrier import _verify_usps
        result = await _verify_usps("9400111899223397775493")

    assert result is not None
    assert result.delivered is True
    assert result.carrier == "USPS"
    assert result.delivery_location == "SEATTLE, WA"
    assert "tools.usps.com" in result.carrier_url


@pytest.mark.asyncio
async def test_usps_not_delivered():
    import os
    os.environ["USPS_USER_ID"] = "test_user"

    mock_resp = MagicMock()
    mock_resp.text = "<TrackSummary><Event>IN TRANSIT</Event></TrackSummary>"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from agents.carrier import _verify_usps
        result = await _verify_usps("9400111899223397775493")

    assert result is not None
    assert result.delivered is False
    assert result.delivery_date is None


@pytest.mark.asyncio
async def test_usps_error_returns_none():
    import os
    os.environ["USPS_USER_ID"] = "test_user"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))
        mock_cls.return_value = mock_client

        from agents.carrier import _verify_usps
        result = await _verify_usps("9400111899223397775493")

    assert result is None


# ── 17track fallback ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_17track_delivered():
    import os
    os.environ["TRACK17_API_KEY"] = "test_key"

    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "data": {
            "accepted": [{
                "track": {
                    "e": 40,  # delivered
                    "c": "USPS",
                    "z0": {"a": "2026-11-05T12:00:00Z", "z": "Delivered", "c": "Seattle, WA"},
                }
            }]
        }
    })

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        from agents.carrier import _verify_17track
        result = await _verify_17track("SOMETRACKING123")

    assert result is not None
    assert result.delivered is True
    assert result.delivery_date == "2026-11-05T12:00:00Z"
    assert "17track" in result.carrier_url


@pytest.mark.asyncio
async def test_17track_no_key_returns_none():
    import os
    os.environ.pop("TRACK17_API_KEY", None)

    from agents.carrier import _verify_17track
    result = await _verify_17track("SOMETRACKING123")
    assert result is None


# ── verify_delivery falls through to 17track on unknown carrier ───────────────

@pytest.mark.asyncio
async def test_verify_delivery_unknown_carrier_uses_17track():
    import os
    os.environ["TRACK17_API_KEY"] = "test_key"

    mock_17track_result = MagicMock()
    mock_17track_result.delivered = True

    with patch("agents.carrier._verify_17track", new_callable=AsyncMock, return_value=mock_17track_result):
        from agents.carrier import verify_delivery
        result = await verify_delivery("UNKNOWNFORMAT999")

    assert result is not None
    assert result.delivered is True
