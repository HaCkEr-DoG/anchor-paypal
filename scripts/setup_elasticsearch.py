"""
Elasticsearch index setup for Anchor.

Run once before first deploy:
    python scripts/setup_elasticsearch.py

Creates four indices:
  anchor-config       — per-merchant Guardian configuration
  anchor-actions      — audit log of every autonomous action
  anchor-events       — raw webhook events (disputes + captures)
  payguard-disputes   — enriched dispute outcomes for Oracle training
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

ES_URL = os.environ.get("ELASTICSEARCH_URL", "").rstrip("/")
ES_KEY = os.environ.get("ELASTICSEARCH_API_KEY", "")


INDICES = {
    "anchor-config": {
        "mappings": {
            "properties": {
                "merchant_id":          {"type": "keyword"},
                "auto_accept_below":    {"type": "float"},
                "auto_fight_above":     {"type": "float"},
                "auto_fight_min_prob":  {"type": "float"},
                "dispute_rate_alert":   {"type": "float"},
                "dispute_rate_critical":{"type": "float"},
                "high_volume_fee_tier": {"type": "boolean"},
                "slack_channel":        {"type": "keyword"},
                "notify_on_auto_action":{"type": "boolean"},
                "morning_brief_enabled":{"type": "boolean"},
                "morning_brief_hour":   {"type": "integer"},
                "account_age_days":     {"type": "integer"},
                "hourly_rate":          {"type": "float"},
                "business_type":        {"type": "keyword"},
            }
        }
    },

    "anchor-actions": {
        "mappings": {
            "properties": {
                "merchant_id":      {"type": "keyword"},
                "timestamp":        {"type": "date"},
                "action_type":      {"type": "keyword"},
                "dispute_id":       {"type": "keyword"},
                "amount":           {"type": "float"},
                "win_probability":  {"type": "float"},
                "outcome":          {"type": "keyword"},
                "reasoning":        {"type": "text"},
                "requires_attention":{"type": "boolean"},
            }
        },
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            }
        }
    },

    "anchor-events": {
        "mappings": {
            "properties": {
                "event_id":         {"type": "keyword"},
                "merchant_id":      {"type": "keyword"},
                "event_type":       {"type": "keyword"},
                "timestamp":        {"type": "date"},
                "dispute_id":       {"type": "keyword"},
                "amount":           {"type": "float"},
                "reason_code":      {"type": "keyword"},
                "status":           {"type": "keyword"},
                "payload":          {"type": "object", "enabled": False},
            }
        },
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            }
        }
    },

    "payguard-disputes": {
        "mappings": {
            "properties": {
                "dispute_id":       {"type": "keyword"},
                "merchant_id":      {"type": "keyword"},
                "claim_type":       {"type": "keyword"},
                "amount":           {"type": "float"},
                "buyer_email":      {"type": "keyword"},
                "seller_won":       {"type": "float"},   # 1.0=won, 0.0=lost
                "evidence_flags":   {"type": "keyword"},
                "oracle_p_win":     {"type": "float"},
                "oracle_recommendation": {"type": "keyword"},
                "created_at":       {"type": "date"},
                "resolved_at":      {"type": "date"},
                "resolution":       {"type": "keyword"},
            }
        },
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            }
        }
    },
}


async def create_index(client: httpx.AsyncClient, name: str, body: dict) -> bool:
    # Check if already exists
    r = await client.head(f"{ES_URL}/{name}", headers={"Authorization": f"ApiKey {ES_KEY}"})
    if r.status_code == 200:
        print(f"  ✓ {name} already exists — skipped")
        return True

    r = await client.put(
        f"{ES_URL}/{name}",
        headers={
            "Authorization": f"ApiKey {ES_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=15.0,
    )
    if r.status_code in (200, 201):
        print(f"  ✓ {name} created")
        return True
    else:
        print(f"  ✗ {name} failed: {r.status_code} {r.text}")
        return False


async def main():
    if not ES_URL or not ES_KEY:
        print("ERROR: ELASTICSEARCH_URL and ELASTICSEARCH_API_KEY must be set.")
        print("Copy .env.example to .env and fill in your Elastic Cloud credentials.")
        sys.exit(1)

    print(f"\nAnchor — Elasticsearch setup")
    print(f"Target: {ES_URL}\n")

    ok = True
    async with httpx.AsyncClient() as client:
        # Verify connectivity
        try:
            r = await client.get(
                ES_URL,
                headers={"Authorization": f"ApiKey {ES_KEY}"},
                timeout=5.0,
            )
            info = r.json()
            print(f"Connected: Elasticsearch {info.get('version', {}).get('number', '?')}\n")
        except Exception as e:
            print(f"ERROR: Cannot reach Elasticsearch: {e}")
            sys.exit(1)

        for name, body in INDICES.items():
            success = await create_index(client, name, body)
            ok = ok and success

    if ok:
        print("\n✅ All indices ready. Anchor is good to deploy.")
    else:
        print("\n⚠️  Some indices failed — check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
