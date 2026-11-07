"""
Wave 92 — Dispute Surge Alerter  POST /api/oracle/surge-alert

Tests:
  1. No DATABASE_URL → severity normal, source unavailable
  2. Empty velocity data → severity normal, 0 disputes
  3. Normal rate (no spikes) → severity normal
  4. Elevated (1 spike in window) → severity elevated
  5. Critical (current hour is a spike) → severity critical + shadow_mode_advised
  6. Custom spike_multiplier respected
  7. hours_lookback clamped to 1–168
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_buckets(counts):
    """Return mock bucket list from counts (oldest→newest)."""
    return [
        {
            "bucket_utc": datetime(2026, 11, 7, h, 0, 0, tzinfo=timezone.utc).isoformat(),
            "dispute_count": c,
            "amount_total": float(c * 50),
        }
        for h, c in enumerate(counts, start=0)
    ]


# ---------------------------------------------------------------------------
# 1. No DATABASE_URL
# ---------------------------------------------------------------------------

def test_surge_no_db():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        from agents.watcher import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/oracle/surge-alert", json={"merchant_id": "m1"})
        assert r.status_code == 200
        d = r.json()
        assert d["alert_severity"] == "normal"
        assert d["source"] == "unavailable"
        assert d["shadow_mode_advised"] is False


# ---------------------------------------------------------------------------
# 2. Empty velocity → normal
# ---------------------------------------------------------------------------

def test_surge_empty_data():
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=[]):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/oracle/surge-alert", json={})
            assert r.status_code == 200
            d = r.json()
            assert d["alert_severity"] == "normal"
            assert d["total_disputes_window"] == 0
            assert d["hours_analyzed"] == 0


# ---------------------------------------------------------------------------
# 3. Normal rate (counts all similar, no spikes)
# ---------------------------------------------------------------------------

def test_surge_normal_rate():
    # counts: [2, 2, 2, 2] — avg=2, threshold=max(2×2,2)=4; none ≥ 4
    buckets = _make_buckets([2, 2, 2, 2])
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db", "MERCHANT_ID": "m1"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=buckets):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/oracle/surge-alert", json={})
            d = r.json()
            assert d["alert_severity"] == "normal"
            assert d["spike_hours"] == []
            assert d["shadow_mode_advised"] is False


# ---------------------------------------------------------------------------
# 4. Elevated (1 spike in window, current hour is not spiking)
# ---------------------------------------------------------------------------

def test_surge_elevated():
    # counts: [1, 1, 1, 8, 1] — avg=2.4, threshold=max(2×2.4,2)=4.8; bucket[3]=8 is a spike
    # current hour count=1 < threshold → elevated (not critical)
    buckets = _make_buckets([1, 1, 1, 8, 1])
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=buckets):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/oracle/surge-alert", json={})
            d = r.json()
            assert d["alert_severity"] == "elevated"
            assert len(d["spike_hours"]) == 1
            assert d["shadow_mode_advised"] is False


# ---------------------------------------------------------------------------
# 5. Critical (current hour IS a spike)
# ---------------------------------------------------------------------------

def test_surge_critical():
    # counts: [1, 1, 1, 20] — avg=5.75, threshold=11.5; current hour=20 ≥ 11.5
    buckets = _make_buckets([1, 1, 1, 20])
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=buckets):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/oracle/surge-alert", json={})
            d = r.json()
            assert d["alert_severity"] == "critical"
            assert d["shadow_mode_advised"] is True
            assert d["current_hour_count"] == 20
            assert "CRITICAL" in d["recommendation"]


# ---------------------------------------------------------------------------
# 6. Custom spike_multiplier respected
# ---------------------------------------------------------------------------

def test_surge_custom_multiplier():
    # counts: [2, 2, 2, 5] — avg=2.75
    # With multiplier=1.5: threshold=max(1.5×2.75,1.5)=4.125; 5≥4.125 → spike
    # With multiplier=3.0: threshold=max(3×2.75,3)=8.25; 5<8.25 → no spike
    buckets = _make_buckets([2, 2, 2, 5])
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=buckets):
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            # low multiplier → spike detected
            r = client.post("/api/oracle/surge-alert", json={"spike_multiplier": 1.5})
            assert len(r.json()["spike_hours"]) >= 1
            # high multiplier → no spike
            r2 = client.post("/api/oracle/surge-alert", json={"spike_multiplier": 3.0})
            assert r2.json()["spike_hours"] == []


# ---------------------------------------------------------------------------
# 7. hours_lookback clamped
# ---------------------------------------------------------------------------

def test_surge_hours_clamped():
    buckets = _make_buckets([1, 1])
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=buckets) as mock_vel:
            from agents.watcher import app
            client = TestClient(app, raise_server_exceptions=False)
            # hours=0 should be clamped to 1
            client.post("/api/oracle/surge-alert", json={"hours_lookback": 0})
            called_hours = mock_vel.call_args[0][1]
            assert called_hours == 1
            # hours=9999 should be clamped to 168
            client.post("/api/oracle/surge-alert", json={"hours_lookback": 9999})
            called_hours2 = mock_vel.call_args[0][1]
            assert called_hours2 == 168
