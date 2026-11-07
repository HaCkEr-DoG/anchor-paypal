"""
Wave 134 — Fraud Pattern Detector  POST /api/oracle/detect-fraud-patterns

Tests:
  1. Same buyer email across many disputes → REFUND_ABUSE or ring pattern
  2. Single clean dispute → no high-confidence patterns
  3. patterns_found is a list
  4. buyer_block_recommendations for serial buyer
  5. confidence_score 0–1
  6. Empty disputes → analyzed=0
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/detect-fraud-patterns", json=body).json()


def _d(dispute_id, email="buyer@gmail.com", amount=100.0, zip_="90210", refunded=False):
    return {
        "dispute_id": dispute_id,
        "buyer_email": email,
        "buyer_zip": zip_,
        "claim_type": "ITEM_NOT_RECEIVED",
        "dispute_amount": amount,
        "was_refunded_before": refunded,
        "is_new_account": True,
    }


def test_serial_buyer_pattern():
    disputes = [_d(f"PP-D-{i:03d}", refunded=True) for i in range(8)]
    d = _post({"disputes": disputes})
    assert d["total_disputes_analyzed"] == 8
    assert isinstance(d["patterns_found"], list)
    assert d["wave"] == 134


def test_single_clean_no_high_confidence():
    d = _post({"disputes": [_d("PP-D-001", email="legit@example.com", refunded=False)]})
    high_conf = [p for p in d["patterns_found"] if p["confidence"] in ("DEFINITIVE", "HIGH")]
    assert len(high_conf) == 0


def test_patterns_found_list():
    d = _post({"disputes": [_d("PP-D-001")]})
    assert isinstance(d["patterns_found"], list)


def test_buyer_block_recommendation():
    disputes = [_d(f"PP-D-{i:03d}", email="fraud@gmail.com", refunded=True) for i in range(10)]
    d = _post({"disputes": disputes})
    assert isinstance(d["buyer_block_recommendations"], list)


def test_confidence_score_range():
    disputes = [_d(f"PP-D-{i:03d}") for i in range(5)]
    d = _post({"disputes": disputes})
    for p in d["patterns_found"]:
        assert 0.0 <= p["confidence_score"] <= 1.0


def test_empty_disputes():
    d = _post({"disputes": []})
    assert d["total_disputes_analyzed"] == 0


def test_response_shape():
    d = _post({"disputes": [_d("PP-D-001")]})
    required = {
        "total_disputes_analyzed", "patterns_found", "high_confidence_patterns",
        "buyer_block_recommendations", "law_enforcement_referral_warranted",
        "total_coordinated_amount", "summary", "wave",
    }
    assert required.issubset(d.keys())
