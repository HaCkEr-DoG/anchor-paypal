"""
Guardian pre-authorization config.

Set once during onboarding. Controls what Anchor does autonomously
vs. what it asks the merchant about. Stored in Elasticsearch so the
guardian remembers it across restarts.

Key insight: without pre-authorization, every action requires a login.
With it, Anchor handles 80% of disputes while the merchant sleeps.
"""

import os
import math
from dataclasses import dataclass, asdict


@dataclass
class GuardianConfig:
    merchant_id: str

    # Dispute thresholds
    auto_accept_below: float = 60.0       # Accept without asking if amount < this
    auto_fight_above: float = 200.0       # Fight without asking if amount > this AND win prob > auto_fight_min_prob
    auto_fight_min_prob: float = 0.60     # Minimum win probability to auto-fight
    ask_range_low: float = 60.0           # Always ask in this range
    ask_range_high: float = 200.0

    # Account health guardrails
    dispute_rate_alert: float = 0.020     # Alert when dispute rate crosses 2%
    dispute_rate_critical: float = 0.035  # Defer autonomous actions when rate approaches this level
    auto_protect_account: bool = True     # Defer to manual review when rate approaches critical
    # US PayPal high-volume eligibility: amount-based ratio ≥1.5% AND >100 sales
    # transactions in the previous three full calendar months. Confirm against PayPal.
    high_volume_fee_tier: bool = False

    # Invoice
    reminder_sequence_days: list = None   # Days overdue to send reminders
    high_risk_score_threshold: float = 0.6

    # Communication
    slack_channel: str = "#anchor-alerts"
    notify_on_auto_action: bool = True    # Send Slack message for every autonomous action
    morning_brief_enabled: bool = True
    morning_brief_hour: int = 8           # 8 AM merchant local time
    morning_brief_timezone: str = "UTC"   # IANA timezone, e.g. "America/New_York"

    # PayPal integration (per-merchant, set during onboarding)
    paypal_webhook_id: str = ""  # Merchant's own PayPal webhook registration ID

    # Merchant profile (used by Oracle for win rate priors)
    account_age_days: int = 730
    hourly_rate: float = 50.0
    business_type: str = "general"        # "freelancer", "ecommerce", "saas", "general"

    def __post_init__(self):
        if self.reminder_sequence_days is None:
            self.reminder_sequence_days = [3, 7, 14]

    def auto_accept(self, amount: float) -> bool:
        return amount < self.auto_accept_below

    def auto_fight(self, amount: float, win_prob: float) -> bool:
        return amount >= self.auto_fight_above and win_prob >= self.auto_fight_min_prob

    def needs_approval(self, amount: float, win_prob: float) -> bool:
        return not self.auto_accept(amount) and not self.auto_fight(amount, win_prob)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "GuardianConfig":
        data.pop("_type", None)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


DEFAULT_CONFIG = GuardianConfig(
    merchant_id="default",
    auto_accept_below=float(os.environ.get("AUTO_ACCEPT_BELOW", "60")),
    auto_fight_above=float(os.environ.get("AUTO_FIGHT_ABOVE", "200")),
    auto_fight_min_prob=float(os.environ.get("AUTO_FIGHT_MIN_PROB", "0.60")),
    account_age_days=int(os.environ.get("SELLER_ACCOUNT_AGE_DAYS", "730")),
    hourly_rate=float(os.environ.get("SELLER_HOURLY_RATE", "50")),
)

# Fail-closed config: all autonomous thresholds set to impossible values so
# every dispute routes to human review. Used when policy storage is unavailable.
MANUAL_REVIEW_CONFIG = GuardianConfig(
    merchant_id="default",
    auto_accept_below=0.0,           # never auto-accept
    auto_fight_above=1_000_000_000.0, # never auto-fight (above any real dispute)
    auto_fight_min_prob=1.01,         # impossible win-probability threshold (> 1.0)
)


async def detect_fee_tier(merchant_id: str) -> bool:
    """
    Auto-detect PayPal US high-volume fee tier eligibility by querying ES.
    Criteria: amount-based dispute ratio ≥1.5% AND >100 sales in previous
    3 full calendar months.  Returns False (standard tier) if ES is absent
    or the data is insufficient — the merchant can override via config.
    """
    from datetime import datetime, timezone, timedelta
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return False

    now = datetime.now(timezone.utc)
    # Three complete calendar months back
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    window_end = first_of_this_month
    window_start = (first_of_this_month - timedelta(days=92)).replace(day=1)

    import httpx
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"merchant_id": merchant_id}},
                    {"range": {"timestamp": {
                        "gte": window_start.isoformat(),
                        "lt": window_end.isoformat(),
                    }}},
                ]
            }
        },
        "aggs": {
            "captures": {"filter": {"term": {"event_type": "capture"}}},
            "eligible_disputes": {"filter": {"term": {"event_type": "eligible_dispute"}}},
        },
        "size": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(
                f"{es_url}/anchor-events/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query,
            )
        aggs = r.json().get("aggregations", {})
        capture_count = aggs.get("captures", {}).get("doc_count", 0)
        dispute_count = aggs.get("eligible_disputes", {}).get("doc_count", 0)
        if capture_count > 100 and capture_count > 0:
            ratio = dispute_count / capture_count
            if ratio >= 0.015:
                print(f"[Config] High-volume tier detected: {dispute_count}/{capture_count} = {ratio:.1%}")
                return True
    except Exception as exc:
        print(f"[Config] Fee-tier detection failed ({exc}) — defaulting to standard tier")
    return False


async def load_config(merchant_id: str) -> GuardianConfig:
    """
    Load merchant policy from PostgreSQL. Elasticsearch is a legacy fallback
    only when PostgreSQL is not configured; a failed configured database never
    falls back to a potentially stale policy.
    """
    if os.environ.get("DATABASE_URL"):
        try:
            import asyncio
            import agents.pg_store as pg_store
            data = await asyncio.to_thread(pg_store.get_policy, merchant_id)
            if data:
                cfg = GuardianConfig.from_dict(dict(data))
                if _valid_merchant_config(cfg, merchant_id):
                    if not cfg.high_volume_fee_tier:
                        cfg.high_volume_fee_tier = await detect_fee_tier(merchant_id)
                    return cfg
        except Exception as exc:
            print(f"[Config] PostgreSQL policy unavailable for {merchant_id} ({exc}); fail-closed")
        return _manual_review_for(merchant_id)

    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        print(f"[Config] No ES credentials for merchant {merchant_id} — fail-closed: all disputes route to human review")
        return _manual_review_for(merchant_id)

    import httpx
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{es_url}/anchor-config/_doc/{merchant_id}",
                headers={"Authorization": f"ApiKey {es_key}"},
                timeout=3.0,
            )
            if r.status_code == 200:
                cfg = GuardianConfig.from_dict(r.json().get("_source", {}))
            else:
                print(f"[Config] ES returned {r.status_code} for merchant {merchant_id} — fail-closed: all disputes route to human review")
                cfg = _manual_review_for(merchant_id)
    except Exception as exc:
        print(f"[Config] ES unreachable for merchant {merchant_id} ({exc}) — fail-closed: all disputes route to human review")
        cfg = _manual_review_for(merchant_id)

    # Auto-detect fee tier unless the merchant explicitly set it
    if not cfg.high_volume_fee_tier:
        cfg.high_volume_fee_tier = await detect_fee_tier(merchant_id)
    return cfg


def _manual_review_for(merchant_id: str) -> GuardianConfig:
    return GuardianConfig(
        merchant_id=merchant_id,
        auto_accept_below=0.0,
        auto_fight_above=1_000_000_000.0,
        auto_fight_min_prob=1.01,
    )


def _valid_merchant_config(config: GuardianConfig, merchant_id: str) -> bool:
    """Reject malformed or cross-merchant policy records before using them."""
    try:
        finite_values = (
            config.auto_accept_below, config.auto_fight_above, config.auto_fight_min_prob,
            config.ask_range_low, config.ask_range_high, config.dispute_rate_alert,
            config.dispute_rate_critical, config.high_risk_score_threshold,
            config.hourly_rate,
        )
        from zoneinfo import ZoneInfo
        ZoneInfo(config.morning_brief_timezone)
        return (
            config.merchant_id == merchant_id
            and all(type(value) in (int, float) and math.isfinite(value) for value in finite_values)
            and type(config.account_age_days) is int
            and type(config.morning_brief_hour) is int
            and 0 <= config.morning_brief_hour <= 23
            and type(config.morning_brief_enabled) is bool
            and type(config.auto_protect_account) is bool
            and type(config.high_volume_fee_tier) is bool
            and type(config.reminder_sequence_days) is list
            and all(type(day) is int and day >= 0 for day in config.reminder_sequence_days)
            and config.business_type in {"general", "freelancer", "ecommerce", "saas"}
            and config.auto_accept_below >= 0
            and config.auto_fight_above > config.auto_accept_below
            and 0 <= config.auto_fight_min_prob <= 1
            and 0 <= config.dispute_rate_alert <= config.dispute_rate_critical <= 1
            and config.hourly_rate >= 0
            and config.account_age_days >= 0
            and 0 <= config.high_risk_score_threshold <= 1
        )
    except (TypeError, ValueError):
        return False


async def policy_is_loaded(merchant_id: str) -> bool:
    """Return true only for a valid, explicitly operator-reviewed policy."""
    try:
        if os.environ.get("DATABASE_URL"):
            import asyncio
            import agents.pg_store as pg_store
            data = await asyncio.to_thread(pg_store.get_policy, merchant_id)
            if not data:
                return False
            cfg = GuardianConfig.from_dict(dict(data))
            return data.get("_reviewed") is True and _valid_merchant_config(cfg, merchant_id)
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return False
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{es_url}/anchor-config/_doc/{merchant_id}",
                headers={"Authorization": f"ApiKey {es_key}"},
            )
        if response.status_code != 200:
            return False
        cfg = GuardianConfig.from_dict(response.json().get("_source", {}))
        source = response.json().get("_source", {})
        return source.get("_reviewed") is True and _valid_merchant_config(cfg, merchant_id)
    except Exception:
        return False


async def save_config(config: GuardianConfig) -> bool:
    """Persist merchant policy to PostgreSQL, or legacy Elasticsearch."""
    if not _valid_merchant_config(config, config.merchant_id):
        return False
    policy = config.to_dict()
    policy["_reviewed"] = True
    policy.pop("_auto_seeded", None)
    if os.environ.get("DATABASE_URL"):
        try:
            import asyncio
            import agents.pg_store as pg_store
            return await asyncio.to_thread(pg_store.save_policy, config.merchant_id, policy)
        except Exception as exc:
            print(f"[Config] PostgreSQL policy save failed for {config.merchant_id}: {exc}")
            return False
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return False

    import httpx
    try:
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{es_url}/anchor-config/_doc/{config.merchant_id}",
                headers={
                    "Authorization": f"ApiKey {es_key}",
                    "Content-Type": "application/json",
                },
                json=policy,
                timeout=3.0,
            )
            return r.status_code in (200, 201)
    except Exception:
        return False
