import os
import sys
import pytest

# Add project root to sys.path so "agents" and other top-level packages resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure tests don't hit real external services
os.environ.setdefault("ELASTICSEARCH_URL", "")
os.environ.setdefault("ELASTICSEARCH_API_KEY", "")
os.environ.setdefault("PAYPAL_CLIENT_ID", "test_id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test_secret")
os.environ.setdefault("PAYPAL_ENV", "sandbox")
os.environ.setdefault("MERCHANT_ID", "test_merchant")
os.environ.setdefault("APPROVAL_TOKEN_SECRET", "")
# Disable rate limiting in tests so the full suite doesn't exhaust the per-IP bucket
os.environ.setdefault("ORACLE_RATE_LIMIT", "999999")
