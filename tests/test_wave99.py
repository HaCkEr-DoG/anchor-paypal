"""
Wave 99 — Chargeback Prevention Score  POST /api/oracle/chargeback-prevention

Tests:
  1. All protections enabled → maximum protection, low risk
  2. No protections → minimal, high risk
  3. 3D Secure provides biggest single boost (25pts)
  4. Digital goods path: no tracking needed, email confirmation counted
  5. Missing signature on high-value order → gap noted
  6. IP mismatch shows in gaps
  7. Response shape complete
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body):
    return _client.post("/api/oracle/chargeback-prevention", json=body).json()


_ALL_ON = {
    "used_3d_secure": True,
    "address_verified": True,
    "cvv_matched": True,
    "shipping_tracked": True,
    "signature_required": True,
    "product_description_clear": True,
    "terms_accepted_at_checkout": True,
    "customer_email_confirmed": True,
    "is_digital_goods": False,
    "ip_country_matches_billing": True,
}


# ---------------------------------------------------------------------------
# 1. All protections → maximum / low risk
# ---------------------------------------------------------------------------

def test_all_protections_maximum():
    d = _post({**_ALL_ON, "amount_usd": 200})
    assert d["protection_level"] == "maximum"
    assert d["chargeback_risk_estimate"] == "low"
    assert d["prevention_score"] >= 80


# ---------------------------------------------------------------------------
# 2. No protections → minimal / high risk
# ---------------------------------------------------------------------------

def test_no_protections_minimal():
    d = _post({
        "used_3d_secure": False,
        "address_verified": False,
        "cvv_matched": False,
        "shipping_tracked": False,
        "signature_required": False,
        "product_description_clear": False,
        "terms_accepted_at_checkout": False,
        "customer_email_confirmed": False,
        "is_digital_goods": False,
        "ip_country_matches_billing": False,
    })
    assert d["chargeback_risk_estimate"] == "high"
    assert d["prevention_score"] <= 20
    assert len(d["gaps"]) >= 3


# ---------------------------------------------------------------------------
# 3. 3DS alone gives 25pts
# ---------------------------------------------------------------------------

def test_3ds_boost():
    base = _post({"used_3d_secure": False, "address_verified": False, "cvv_matched": False})
    with_3ds = _post({"used_3d_secure": True, "address_verified": False, "cvv_matched": False})
    assert with_3ds["prevention_score"] - base["prevention_score"] == 25.0
    assert any("3d secure" in p.lower() for p in with_3ds["protections_in_place"])


# ---------------------------------------------------------------------------
# 4. Digital goods: tracking not required, email confirmation counted
# ---------------------------------------------------------------------------

def test_digital_goods_path():
    d = _post({
        "is_digital_goods": True,
        "customer_email_confirmed": True,
        "used_3d_secure": True,
        "address_verified": True,
        "cvv_matched": True,
        "product_description_clear": True,
        "terms_accepted_at_checkout": True,
        "ip_country_matches_billing": True,
    })
    # No "no tracking" gap for digital
    assert not any("tracking" in g.lower() for g in d["gaps"])
    # Email confirmation counts
    assert any("email confirmed" in p.lower() for p in d["protections_in_place"])


# ---------------------------------------------------------------------------
# 5. High-value order without signature → gap noted
# ---------------------------------------------------------------------------

def test_high_value_no_signature():
    d = _post({
        **_ALL_ON,
        "signature_required": False,
        "amount_usd": 600,
    })
    assert any("signature" in g.lower() for g in d["gaps"])


# ---------------------------------------------------------------------------
# 6. IP mismatch in gaps
# ---------------------------------------------------------------------------

def test_ip_mismatch_gap():
    d = _post({"ip_country_matches_billing": False})
    assert any("ip country" in g.lower() for g in d["gaps"])


# ---------------------------------------------------------------------------
# 7. Response shape
# ---------------------------------------------------------------------------

def test_response_shape():
    d = _post({"amount_usd": 100})
    required = {
        "transaction_id", "amount_usd", "prevention_score",
        "protection_level", "chargeback_risk_estimate",
        "protections_in_place", "gaps", "recommendations",
    }
    assert required.issubset(d.keys())
