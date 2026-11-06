"""
Carrier Auto-Verifier — automatically confirms delivery status before dispute decisions.

68% of PayPal disputes are Item Not Received (INR). The single most valuable evidence
in any INR dispute is confirmed delivery with a timestamp and delivery address.

Without this module:
  - Merchant sees INR dispute
  - Manually searches for tracking number in order system
  - Manually visits carrier website
  - Copies tracking info into dispute response
  - Takes 15–30 minutes per dispute
  - Often forgotten under time pressure, leading to auto-loss

With this module:
  - INR dispute arrives → tracking number extracted from order metadata
  - Carrier API queried automatically in <2 seconds
  - If delivered: TrackingResult with confirmed delivery date + signature
  - Oracle evidence auto-updated: has_delivery_confirmation=True
  - Win probability jumps from ~50% to ~85% with no human effort

Carrier priority:
  1. USPS (most common, free tier via tracking number format detection)
  2. FedEx (requires FEDEX_API_KEY env var)
  3. UPS (requires UPS_CLIENT_ID + UPS_CLIENT_SECRET env vars)
  4. 17track (universal fallback, requires TRACK17_API_KEY env var)
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx


@dataclass
class TrackingResult:
    tracking_number: str
    carrier: str
    delivered: bool
    delivery_date: Optional[str]       # ISO 8601 or None
    delivery_location: Optional[str]   # city/state or "Front Door" etc.
    signature_obtained: bool
    latest_status: str
    carrier_url: str                   # deep link for dispute evidence


USPS_TRACKING_PATTERN = re.compile(
    r"^(94[0-9]{20}|92[0-9]{20}|420[0-9]{27}|9[23456][0-9]{18,22})$"
)
FEDEX_TRACKING_PATTERN = re.compile(
    r"^([0-9]{12}|[0-9]{15}|[0-9]{20}|96[0-9]{18})$"
)
UPS_TRACKING_PATTERN = re.compile(
    r"^1Z[A-Z0-9]{16}$"
)
DHL_TRACKING_PATTERN = re.compile(
    r"^([0-9]{10,11}|[A-Z]{2}[0-9]{9}[A-Z]{2}|[0-9]{20}|JD[0-9]{18})$"
)


def detect_carrier(tracking_number: str) -> Optional[str]:
    t = tracking_number.upper().replace(" ", "")
    if UPS_TRACKING_PATTERN.match(t):
        return "ups"
    if USPS_TRACKING_PATTERN.match(t):
        return "usps"
    if t.startswith("JD") or (len(t) >= 10 and DHL_TRACKING_PATTERN.match(t) and not FEDEX_TRACKING_PATTERN.match(t)):
        return "dhl"
    if FEDEX_TRACKING_PATTERN.match(t):
        return "fedex"
    return None


def delivery_timing_risk(result: TrackingResult, dispute_filed_at_iso: str) -> str:
    """
    Detect suspiciously timed disputes relative to delivery confirmation.

    A buyer who files a dispute within hours of confirmed delivery is almost
    certainly committing friendly fraud — the item arrived but they want a free
    refund. This signal is admissible PayPal evidence ("dispute filed X hours
    after carrier-confirmed delivery").

    Returns a risk signal string; empty string if no anomaly detected.
    """
    if not result.delivered or not result.delivery_date or not dispute_filed_at_iso:
        return ""

    try:
        def _parse(s: str):
            s = s.strip()
            # Handle various ISO formats and naive datetimes
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    dt = datetime.strptime(s.replace("Z", "").split("+")[0], fmt)
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            # fromisoformat fallback
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        delivery_dt = _parse(result.delivery_date)
        filed_dt = _parse(dispute_filed_at_iso)
        hours_after = (filed_dt - delivery_dt).total_seconds() / 3600

        if hours_after < 0:
            return ""  # filed before delivery — genuine INR
        if hours_after < 2:
            return (
                f"⚠️ SUSPICIOUS: dispute filed {hours_after:.1f}h AFTER carrier-confirmed delivery "
                f"({result.carrier} shows delivered {result.delivery_date}). "
                "High probability of friendly fraud — submit delivery timestamp as primary evidence."
            )
        if hours_after < 24:
            return (
                f"Dispute filed {hours_after:.1f}h after confirmed delivery. "
                "Include delivery timestamp in evidence response."
            )
        if hours_after < 72:
            return (
                f"Dispute filed {hours_after/24:.1f} days after confirmed delivery. "
                "Carrier confirmation available as evidence."
            )
    except Exception:
        pass
    return ""


async def verify_delivery(tracking_number: str) -> Optional[TrackingResult]:
    """
    Verify delivery status for a tracking number.
    Returns None if the carrier is unknown or verification fails.
    """
    if not tracking_number:
        return None

    carrier = detect_carrier(tracking_number)

    handlers = {
        "usps": _verify_usps,
        "fedex": _verify_fedex,
        "ups": _verify_ups,
        "dhl": _verify_dhl,
    }

    if carrier and carrier in handlers:
        result = await handlers[carrier](tracking_number)
        if result is not None:
            return result

    # Fallback: 17track universal API
    return await _verify_17track(tracking_number)


async def _verify_usps(tracking_number: str) -> Optional[TrackingResult]:
    user_id = os.environ.get("USPS_USER_ID", "")
    if not user_id:
        return None

    url = "https://secure.shippingapis.com/ShippingAPI.dll"
    xml = (
        f'<TrackFieldRequest USERID="{user_id}">'
        f'<Revision>1</Revision>'
        f'<ClientIp>127.0.0.1</ClientIp>'
        f'<SourceId>Anchor</SourceId>'
        f'<TrackID ID="{tracking_number}"/>'
        f'</TrackFieldRequest>'
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(url, params={"API": "TrackV2", "XML": xml})
        body = r.text

        delivered = "DELIVERED" in body.upper()
        sig = "SIGNATURE" in body.upper() or "SIGNED" in body.upper()

        # Very basic XML extraction — avoids lxml dependency
        event_match = re.search(r"<EventTime>(.*?)</EventTime>.*?<EventDate>(.*?)</EventDate>", body, re.DOTALL)
        city_match = re.search(r"<EventCity>(.*?)</EventCity>", body)
        state_match = re.search(r"<EventState>(.*?)</EventState>", body)
        status_match = re.search(r"<Event>(.*?)</Event>", body)

        location = None
        if city_match and state_match:
            location = f"{city_match.group(1)}, {state_match.group(1)}"

        return TrackingResult(
            tracking_number=tracking_number,
            carrier="USPS",
            delivered=delivered,
            delivery_date=event_match.group(2).strip() if event_match and delivered else None,
            delivery_location=location,
            signature_obtained=sig,
            latest_status=status_match.group(1).strip() if status_match else "Unknown",
            carrier_url=f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
        )
    except Exception as exc:
        print(f"[Carrier] USPS verify failed: {exc}")
        return None


async def _verify_fedex(tracking_number: str) -> Optional[TrackingResult]:
    api_key = os.environ.get("FEDEX_API_KEY", "")
    secret = os.environ.get("FEDEX_SECRET_KEY", "")
    if not api_key or not secret:
        return None

    try:
        # Get OAuth token
        async with httpx.AsyncClient(timeout=5.0) as c:
            tok_r = await c.post(
                "https://apis.fedex.com/oauth/token",
                data={"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if tok_r.status_code != 200:
                return None
            token = tok_r.json().get("access_token", "")
            if not token:
                return None

            track_r = await c.post(
                "https://apis.fedex.com/track/v1/trackingnumbers",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"includeDetailedScans": True, "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}]},
            )

        data = track_r.json()
        output = data.get("output", {})
        complete_track = output.get("completeTrackResults", [{}])[0]
        result_list = complete_track.get("trackResults", [{}])
        result = result_list[0] if result_list else {}

        status_detail = result.get("latestStatusDetail", {})
        status = status_detail.get("code", "")
        status_desc = status_detail.get("description", "Unknown")
        delivered = status == "DL"

        events = result.get("dateAndTimes", [])
        actual_delivery = next((e.get("dateTime") for e in events if e.get("type") == "ACTUAL_DELIVERY"), None)

        delivery_location = None
        if delivered:
            addr = result.get("deliveryDetails", {}).get("actualDeliveryAddress", {})
            city = addr.get("city", "")
            state = addr.get("stateOrProvinceCode", "")
            if city:
                delivery_location = f"{city}, {state}" if state else city

        sig = result.get("deliveryDetails", {}).get("deliveryAttempts", "0") != "0"

        return TrackingResult(
            tracking_number=tracking_number,
            carrier="FedEx",
            delivered=delivered,
            delivery_date=actual_delivery,
            delivery_location=delivery_location,
            signature_obtained=sig,
            latest_status=status_desc,
            carrier_url=f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
        )
    except Exception as exc:
        print(f"[Carrier] FedEx verify failed: {exc}")
        return None


async def _verify_ups(tracking_number: str) -> Optional[TrackingResult]:
    client_id = os.environ.get("UPS_CLIENT_ID", "")
    client_secret = os.environ.get("UPS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            tok_r = await c.post(
                "https://onlinetools.ups.com/security/v1/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if tok_r.status_code != 200:
                return None
            token = tok_r.json().get("access_token", "")

            track_r = await c.get(
                f"https://onlinetools.ups.com/api/track/v1/details/{tracking_number}",
                headers={"Authorization": f"Bearer {token}", "transactionSrc": "Anchor"},
            )

        data = track_r.json()
        shipment = data.get("trackResponse", {}).get("shipment", [{}])[0]
        pkg = shipment.get("package", [{}])[0]
        activity = pkg.get("activity", [{}])[0]  # most recent

        status_code = activity.get("status", {}).get("code", "")
        status_desc = activity.get("status", {}).get("description", "Unknown")
        delivered = status_code == "011"  # UPS delivered status code

        location_data = activity.get("location", {}).get("address", {})
        city = location_data.get("city", "")
        state = location_data.get("stateProvince", "")
        delivery_location = f"{city}, {state}" if city else None

        date_str = activity.get("date", "")
        time_str = activity.get("time", "")
        delivery_date = f"{date_str}T{time_str}" if date_str and delivered else None

        sig = any(
            a.get("status", {}).get("code") == "007"
            for a in pkg.get("activity", [])
        )

        return TrackingResult(
            tracking_number=tracking_number,
            carrier="UPS",
            delivered=delivered,
            delivery_date=delivery_date,
            delivery_location=delivery_location,
            signature_obtained=sig,
            latest_status=status_desc,
            carrier_url=f"https://www.ups.com/track?tracknum={tracking_number}",
        )
    except Exception as exc:
        print(f"[Carrier] UPS verify failed: {exc}")
        return None


async def _verify_dhl(tracking_number: str) -> Optional[TrackingResult]:
    api_key = os.environ.get("DHL_API_KEY", "")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"https://api-eu.dhl.com/track/shipments",
                headers={"DHL-API-Key": api_key},
                params={"trackingNumber": tracking_number},
            )
        if r.status_code != 200:
            return None

        data = r.json()
        shipments = data.get("shipments", [])
        if not shipments:
            return None

        shipment = shipments[0]
        status = shipment.get("status", {})
        status_code = status.get("statusCode", "")
        status_desc = status.get("description", "Unknown")
        delivered = status_code == "delivered"

        events = shipment.get("events", [])
        delivery_event = next(
            (e for e in events if e.get("description", "").lower().startswith("delivered")),
            None
        )
        delivery_date = delivery_event.get("timestamp") if delivery_event else None

        location_data = (delivery_event or {}).get("location", {}).get("address", {})
        city = location_data.get("addressLocality", "")
        country = location_data.get("countryCode", "")
        delivery_location = f"{city}, {country}" if city else None

        sig = any(
            "signature" in e.get("description", "").lower()
            for e in events
        )

        return TrackingResult(
            tracking_number=tracking_number,
            carrier="DHL",
            delivered=delivered,
            delivery_date=delivery_date,
            delivery_location=delivery_location,
            signature_obtained=sig,
            latest_status=status_desc,
            carrier_url=f"https://www.dhl.com/en/express/tracking.html?AWB={tracking_number}",
        )
    except Exception as exc:
        print(f"[Carrier] DHL verify failed: {exc}")
        return None


async def _verify_17track(tracking_number: str) -> Optional[TrackingResult]:
    api_key = os.environ.get("TRACK17_API_KEY", "")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(
                "https://api.17track.net/track/v2/gettracklist",
                headers={"17token": api_key, "Content-Type": "application/json"},
                json=[{"number": tracking_number}],
            )
        data = r.json()
        accepted = data.get("data", {}).get("accepted", [{}])[0]
        track_data = accepted.get("track", {})

        status_code = track_data.get("e", 0)  # 40 = delivered in 17track
        delivered = status_code == 40

        latest_event = track_data.get("z0", {})
        event_time = latest_event.get("a", "")
        event_desc = latest_event.get("z", "Unknown")
        event_location = latest_event.get("c", None)

        carrier_name = track_data.get("c", "Unknown Carrier")

        return TrackingResult(
            tracking_number=tracking_number,
            carrier=carrier_name,
            delivered=delivered,
            delivery_date=event_time if delivered else None,
            delivery_location=event_location,
            signature_obtained=False,  # 17track doesn't surface this easily
            latest_status=event_desc,
            carrier_url=f"https://t.17track.net/en#nums={tracking_number}",
        )
    except Exception as exc:
        print(f"[Carrier] 17track verify failed: {exc}")
        return None
