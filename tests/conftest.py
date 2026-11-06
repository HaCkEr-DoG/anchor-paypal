import os
import pytest

# Ensure tests don't hit real external services
os.environ.setdefault("ELASTICSEARCH_URL", "")
os.environ.setdefault("ELASTICSEARCH_API_KEY", "")
os.environ.setdefault("PAYPAL_CLIENT_ID", "test_id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test_secret")
os.environ.setdefault("PAYPAL_ENV", "sandbox")
os.environ.setdefault("MERCHANT_ID", "test_merchant")
os.environ.setdefault("APPROVAL_TOKEN_SECRET", "")
