"""
Approval token tests — push every boundary of token creation and verification.
Covers both guardian._make_approval_token and watcher._verify_approval_token.
"""
import time
import hmac
import hashlib
import pytest
import importlib
import sys


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_token(secret: str, dispute_id: str, merchant_id: str, action: str, offset_secs: int = 0) -> str:
    expiry = int(time.time()) + 48 * 3600 + offset_secs
    payload = f"{dispute_id}:{merchant_id}:{action}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _load_watcher_verify(secret: str):
    """Reload watcher with APPROVAL_TOKEN_SECRET set to secret."""
    import agents.watcher as w
    w.APPROVAL_TOKEN_SECRET = secret
    return w._verify_approval_token


# ── Guardian token creation ───────────────────────────────────────────────────

def test_guardian_empty_secret_returns_empty_token():
    from agents.guardian import Guardian
    import os
    os.environ.pop("APPROVAL_TOKEN_SECRET", None)
    g = Guardian(approval_secret="")
    token = g._make_approval_token("PP-D-001", "fight")
    assert token == ""


def test_guardian_token_format():
    from agents.guardian import Guardian
    g = Guardian(approval_secret="testsecret123")
    token = g._make_approval_token("PP-D-001", "fight")
    parts = token.split(".")
    assert len(parts) == 2
    expiry = int(parts[0])
    assert expiry > int(time.time())
    assert len(parts[1]) == 64  # SHA-256 hex = 64 chars


def test_guardian_uses_instance_merchant_id():
    from agents.guardian import Guardian
    import os
    os.environ["MERCHANT_ID"] = "global_merchant"
    g = Guardian(approval_secret="secret", merchant_id="instance_merchant")
    token = g._make_approval_token("PP-D-999", "fight")
    # Verify token was built with instance_merchant, not global_merchant
    expiry_str, sig = token.split(".", 1)
    payload = f"PP-D-999:instance_merchant:fight:{expiry_str}"
    expected = hmac.new("secret".encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_guardian_falls_back_to_env_secret():
    from agents.guardian import Guardian
    import os
    os.environ["APPROVAL_TOKEN_SECRET"] = "env_secret"
    g = Guardian(approval_secret="")
    token = g._make_approval_token("PP-D-123", "fight")
    assert token != ""  # env var picked up
    os.environ.pop("APPROVAL_TOKEN_SECRET", None)


def test_guardian_instance_secret_beats_env():
    from agents.guardian import Guardian
    import os
    os.environ["APPROVAL_TOKEN_SECRET"] = "env_secret"
    g = Guardian(approval_secret="instance_secret")
    token = g._make_approval_token("PP-D-123", "fight")
    expiry_str, sig = token.split(".", 1)
    # Should match instance_secret, not env_secret
    # mid falls through to the module-level MERCHANT_ID env var
    from agents.guardian import MERCHANT_ID
    mid = g.merchant_id or MERCHANT_ID
    payload = f"PP-D-123:{mid}:fight:{expiry_str}"
    expected = hmac.new("instance_secret".encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert sig == expected
    os.environ.pop("APPROVAL_TOKEN_SECRET", None)


# ── Watcher token verification ────────────────────────────────────────────────

def test_valid_token_verifies():
    verify = _load_watcher_verify("good_secret")
    token = _make_token("good_secret", "PP-D-100", "merch1", "fight")
    assert verify(token, "PP-D-100", "merch1", "fight") is True


def test_expired_token_rejected():
    verify = _load_watcher_verify("good_secret")
    # Create a token that expired 1 second ago
    expiry = int(time.time()) - 1
    payload = f"PP-D-100:merch1:fight:{expiry}"
    sig = hmac.new("good_secret".encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = f"{expiry}.{sig}"
    assert verify(token, "PP-D-100", "merch1", "fight") is False


def test_wrong_dispute_id_rejected():
    verify = _load_watcher_verify("good_secret")
    token = _make_token("good_secret", "PP-D-100", "merch1", "fight")
    assert verify(token, "PP-D-WRONG", "merch1", "fight") is False


def test_wrong_action_rejected():
    verify = _load_watcher_verify("good_secret")
    token = _make_token("good_secret", "PP-D-100", "merch1", "fight")
    assert verify(token, "PP-D-100", "merch1", "accept") is False  # wrong action


def test_wrong_merchant_rejected():
    verify = _load_watcher_verify("good_secret")
    token = _make_token("good_secret", "PP-D-100", "merch1", "fight")
    assert verify(token, "PP-D-100", "merch2", "fight") is False


def test_tampered_signature_rejected():
    verify = _load_watcher_verify("good_secret")
    token = _make_token("good_secret", "PP-D-100", "merch1", "fight")
    expiry_str, _ = token.split(".", 1)
    tampered = f"{expiry_str}.{'a' * 64}"
    assert verify(tampered, "PP-D-100", "merch1", "fight") is False


def test_key_split_guardian_watcher_mismatch():
    """Guardian created token with one secret; watcher verifies with different → reject."""
    # Guardian side: uses instance_secret
    from agents.guardian import Guardian
    g = Guardian(approval_secret="guardian_secret", merchant_id="merch1")
    guardian_token = g._make_approval_token("PP-D-200", "fight")

    # Watcher side: has different secret
    verify = _load_watcher_verify("different_watcher_secret")
    assert verify(guardian_token, "PP-D-200", "merch1", "fight") is False


def test_key_match_guardian_watcher_accepts():
    """Guardian and watcher share the same secret → accept."""
    shared_secret = "shared_production_secret_xyz"
    from agents.guardian import Guardian
    g = Guardian(approval_secret=shared_secret, merchant_id="merch1")
    token = g._make_approval_token("PP-D-200", "fight")

    verify = _load_watcher_verify(shared_secret)
    assert verify(token, "PP-D-200", "merch1", "fight") is True


def test_malformed_token_no_dot_rejected():
    verify = _load_watcher_verify("good_secret")
    assert verify("nodottoken", "PP-D-100", "merch1", "fight") is False


def test_empty_token_rejected():
    verify = _load_watcher_verify("good_secret")
    assert verify("", "PP-D-100", "merch1", "fight") is False


def test_token_without_secret_rejected():
    """If watcher has empty secret, all tokens should fail (not silently pass)."""
    verify = _load_watcher_verify("")
    token = _make_token("some_secret", "PP-D-100", "merch1", "fight")
    # An empty secret means HMAC("", payload) — different from real secret
    assert verify(token, "PP-D-100", "merch1", "fight") is False
