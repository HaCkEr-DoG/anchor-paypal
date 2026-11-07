"""
Wave 100 — Dispute Intelligence Summary  GET /api/oracle/intelligence-summary

Tests:
  1. Basic response with no DATABASE_URL → operational, velocity unavailable
  2. merchant_id param used (custom)
  3. wave field = 100
  4. All 28 oracle endpoints listed in capabilities
  5. Response shape complete
  6. velocity data populated when DATABASE_URL set
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. No DATABASE_URL → operational, velocity unavailable
# ---------------------------------------------------------------------------

def test_no_db_operational():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        r = _client.get("/api/oracle/intelligence-summary")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "operational"
        assert d["velocity"]["available"] is False


# ---------------------------------------------------------------------------
# 2. Custom merchant_id via query param
# ---------------------------------------------------------------------------

def test_custom_merchant_id():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        r = _client.get("/api/oracle/intelligence-summary?merchant_id=my-shop")
        assert r.json()["merchant_id"] == "my-shop"


# ---------------------------------------------------------------------------
# 3. wave = 100
# ---------------------------------------------------------------------------

def test_wave_number():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        r = _client.get("/api/oracle/intelligence-summary")
        assert r.json()["wave"] == 100


# ---------------------------------------------------------------------------
# 4. 28+ oracle endpoints listed
# ---------------------------------------------------------------------------

def test_oracle_endpoints_count():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        r = _client.get("/api/oracle/intelligence-summary")
        d = r.json()
        assert d["capabilities"]["total_oracle_endpoints"] >= 28
        endpoints = d["capabilities"]["endpoints"]
        assert "/api/oracle/intelligence-summary" in " ".join(endpoints)


# ---------------------------------------------------------------------------
# 5. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        r = _client.get("/api/oracle/intelligence-summary")
        d = r.json()
        required = {
            "merchant_id", "generated_at_utc", "wave", "status",
            "capabilities", "velocity", "health", "features",
        }
        assert required.issubset(d.keys())


# ---------------------------------------------------------------------------
# 6. Velocity populated when DB available
# ---------------------------------------------------------------------------

def test_velocity_populated_with_db():
    from datetime import datetime, timezone
    mock_buckets = [
        {"bucket_utc": datetime(2026, 11, 7, h, 0, 0, tzinfo=timezone.utc).isoformat(),
         "dispute_count": 2, "amount_total": 100.0}
        for h in range(5)
    ]
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@h/db", "MERCHANT_ID": "m1"}):
        with patch("agents.pg_store.get_dispute_velocity", return_value=mock_buckets):
            r = _client.get("/api/oracle/intelligence-summary")
            d = r.json()
            assert d["velocity"]["available"] is True
            assert d["velocity"]["total_disputes_7d"] == 10
