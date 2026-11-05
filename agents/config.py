"""
Guardian pre-authorization config.

Set once during onboarding. Controls what Anchor does autonomously
vs. what it asks the merchant about. Stored in Elasticsearch so the
guardian remembers it across restarts.

Key insight: without pre-authorization, every action requires a login.
With it, Anchor handles 80% of disputes while the merchant sleeps.
"""

import os
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
    # High-volume fee tier: ≥1.5% dispute rate AND >100 transactions triggers $30 non-refundable fee.
    # Guardian sets this dynamically; stored here so Oracle can use it in EV math.
    high_volume_fee_tier: bool = False

    # Invoice
    reminder_sequence_days: list = None   # Days overdue to send reminders
    high_risk_score_threshold: float = 0.6

    # Communication
    slack_channel: str = "#anchor-alerts"
    notify_on_auto_action: bool = True    # Send Slack message for every autonomous action
    morning_brief_enabled: bool = True
    morning_brief_hour: int = 8           # 8 AM merchant local time

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


async def load_config(merchant_id: str) -> GuardianConfig:
    """
    Load config from Elasticsearch. Falls back to env-var defaults.
    This is what makes Anchor remember the merchant's preferences
    across restarts and across the onboarding conversation.
    """
    es_url = os.environ.get("ELASTICSEARCH_URL")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not es_url or not es_key:
        return DEFAULT_CONFIG

    import httpx
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{es_url}/anchor-config/_doc/{merchant_id}",
                headers={"Authorization": f"ApiKey {es_key}"},
                timeout=3.0,
            )
            if r.status_code == 200:
                return GuardianConfig.from_dict(r.json().get("_source", {}))
    except Exception:
        pass
    return DEFAULT_CONFIG


async def save_config(config: GuardianConfig) -> bool:
    """Persist merchant config to Elasticsearch."""
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
                json=config.to_dict(),
                timeout=3.0,
            )
            return r.status_code in (200, 201)
    except Exception:
        return False
