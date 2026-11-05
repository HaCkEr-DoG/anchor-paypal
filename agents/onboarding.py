"""
Anchor Onboarding — First-run conversation.

Walks a new merchant through setting their pre-authorization thresholds
in plain English. No PayPal jargon. No dashboard forms.

After onboarding, GuardianConfig is persisted to Elasticsearch so the
Guardian remembers preferences across restarts.

This runs as a FastAPI endpoint that Zapier or a web widget can drive:
    POST /onboard  {"merchant_id": "...", "message": "...", "session": {...}}

Or run interactively in the terminal:
    python agents/onboarding.py
"""

import asyncio
import os

from agents.config import GuardianConfig, save_config


# ── Conversation state ────────────────────────────────────────────────────────

class OnboardingSession:

    STEPS = [
        "welcome",
        "auto_accept",
        "auto_fight",
        "win_prob",
        "slack",
        "confirm",
        "done",
    ]

    def __init__(self, merchant_id: str):
        self.merchant_id = merchant_id
        self.step = "welcome"
        self.config = GuardianConfig(merchant_id=merchant_id)
        self.history: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "merchant_id": self.merchant_id,
            "step": self.step,
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OnboardingSession":
        s = cls(d["merchant_id"])
        s.step = d.get("step", "welcome")
        s.config = GuardianConfig.from_dict(d.get("config", {"merchant_id": d["merchant_id"]}))
        return s

    def next_step(self):
        idx = self.STEPS.index(self.step)
        if idx + 1 < len(self.STEPS):
            self.step = self.STEPS[idx + 1]


# ── Message handler ───────────────────────────────────────────────────────────

async def process_message(session: OnboardingSession, user_input: str) -> str:
    """
    Takes the current session state and the merchant's reply.
    Returns Anchor's next message and advances the session step.
    """
    msg = user_input.strip().lower()

    if session.step == "welcome":
        session.next_step()
        return (
            "👋 Welcome to Anchor! I'll handle your PayPal disputes automatically "
            "while you sleep.\n\n"
            "First, let's set your thresholds. These are the rules I'll follow "
            "without waking you up.\n\n"
            "**What's the most you'd accept losing on a dispute without even fighting?**\n"
            "For example: 'anything under $50' or 'under $75'.\n"
            "(Small disputes often cost more to fight than they're worth — "
            "the dispute fee alone is $15.)"
        )

    elif session.step == "auto_accept":
        amount = _parse_dollar(msg)
        if amount is None:
            return (
                "I didn't catch a dollar amount. Try something like "
                "'under $50' or '$40'."
            )
        session.config.auto_accept_below = amount
        session.next_step()
        return (
            f"Got it — I'll automatically accept disputes under ${amount:.0f}.\n\n"
            "**Now, what's the minimum dispute amount worth fighting automatically?**\n"
            "For these, I'll gather evidence and submit without asking — "
            "as long as the odds are good.\n"
            "Example: 'anything over $150' or '$200'."
        )

    elif session.step == "auto_fight":
        amount = _parse_dollar(msg)
        if amount is None:
            return "I didn't catch a dollar amount. Try 'over $150' or '$200'."
        if amount <= session.config.auto_accept_below:
            return (
                f"That's below your auto-accept threshold (${session.config.auto_accept_below:.0f}). "
                f"Pick a higher amount — try something like '${session.config.auto_accept_below * 3:.0f}'."
            )
        session.config.auto_fight_above = amount
        session.next_step()
        return (
            f"Got it — for disputes over ${amount:.0f} with good evidence, I'll fight automatically.\n\n"
            "**What's the minimum win probability before I fight without asking?**\n"
            "I calculate this based on your evidence and claim type.\n"
            "I'd suggest 60% — conservative but not overly cautious.\n"
            "Reply with a percentage like '60%' or '70%'."
        )

    elif session.step == "win_prob":
        prob = _parse_percent(msg)
        if prob is None:
            return "I didn't catch a percentage. Try '60%' or '65'."
        if prob < 0.4 or prob > 0.95:
            return "That's outside a sensible range (40%–95%). Try something like '60%'."
        session.config.auto_fight_min_prob = prob
        session.next_step()
        return (
            f"Perfect — I'll auto-fight when my win estimate is at least {prob:.0%}.\n\n"
            "**Where should I send alerts?** Paste your Slack webhook URL, "
            "or say 'skip' to configure later.\n"
            "I'll notify you for:\n"
            "• Every autonomous action I take\n"
            "• Disputes that need your decision\n"
            "• Your morning brief at 8 AM"
        )

    elif session.step == "slack":
        if msg in ("skip", "later", "no"):
            session.next_step()
            return _confirm_message(session)
        if msg.startswith("https://hooks.slack.com") or msg.startswith("http"):
            session.config.slack_channel = "#anchor-alerts"
            # Store webhook in env var recommendation — not in config for security
            session.next_step()
            return (
                "✅ Great — set `SLACK_WEBHOOK_URL` to that URL in your Render environment.\n\n"
            ) + _confirm_message(session)
        return (
            "That doesn't look like a Slack webhook URL (should start with "
            "https://hooks.slack.com). Try again or say 'skip'."
        )

    elif session.step == "confirm":
        if msg in ("yes", "y", "confirm", "looks good", "ok", "done"):
            await save_config(session.config)
            session.next_step()
            return (
                "✅ **Anchor is configured!**\n\n"
                f"• Auto-accept disputes under **${session.config.auto_accept_below:.0f}**\n"
                f"• Auto-fight disputes over **${session.config.auto_fight_above:.0f}** "
                f"with **{session.config.auto_fight_min_prob:.0%}+** win probability\n"
                f"• Morning brief enabled at 8 AM\n\n"
                "I'll handle disputes silently and alert you when I need a decision "
                "or when something interesting happens overnight.\n\n"
                "Sleep well — I've got the disputes. 🛡️"
            )
        elif msg in ("no", "change", "edit"):
            session.step = "auto_accept"
            return (
                "No problem — let's go through it again.\n\n"
                "**What's the most you'd accept losing on a dispute without fighting?**"
            )
        return "Reply *yes* to confirm or *no* to change the settings."

    elif session.step == "done":
        return (
            "You're all set! Anchor is running.\n"
            "For changes, start over with /anchor-setup or edit your Render environment vars."
        )

    return "I didn't understand that. Can you rephrase?"


def _confirm_message(session: OnboardingSession) -> str:
    cfg = session.config
    return (
        f"Here's what I'll do:\n\n"
        f"• **Auto-accept** disputes under **${cfg.auto_accept_below:.0f}** — not worth fighting\n"
        f"• **Auto-fight** disputes over **${cfg.auto_fight_above:.0f}** "
        f"with **{cfg.auto_fight_min_prob:.0%}+** win probability — submit evidence immediately\n"
        f"• **Ask you** for everything in between\n"
        f"• Send a **morning brief at 8 AM** covering overnight activity\n\n"
        "Does this look right? Reply **yes** to save, or **no** to change."
    )


def _parse_dollar(text: str) -> float | None:
    import re
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return None


def _parse_percent(text: str) -> float | None:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    if m:
        v = float(m.group(1))
        return v / 100 if v > 1 else v
    return None


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

# Import lazily to avoid circular imports when running interactively
def register_routes(app):
    from fastapi import Request
    from fastapi.responses import JSONResponse

    _sessions: dict[str, dict] = {}

    @app.post("/onboard")
    async def onboard(request: Request):
        body = await request.json()
        merchant_id = body.get("merchant_id", os.environ.get("MERCHANT_ID", "default"))
        user_message = body.get("message", "")

        if merchant_id not in _sessions:
            _sessions[merchant_id] = OnboardingSession(merchant_id).to_dict()

        session = OnboardingSession.from_dict(_sessions[merchant_id])
        reply = await process_message(session, user_message)
        _sessions[merchant_id] = session.to_dict()

        return JSONResponse({"reply": reply, "step": session.step, "done": session.step == "done"})


# ── Interactive terminal mode ─────────────────────────────────────────────────

async def run_interactive():
    merchant_id = os.environ.get("MERCHANT_ID", "demo-merchant")
    session = OnboardingSession(merchant_id)

    print("\n" + "="*60)
    print("  Anchor Onboarding (interactive terminal mode)")
    print("="*60)

    # Kick off welcome
    reply = await process_message(session, "start")
    print(f"\nAnchor: {reply}\n")

    while session.step != "done":
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nOnboarding cancelled.")
            break
        if not user_input:
            continue
        reply = await process_message(session, user_input)
        print(f"\nAnchor: {reply}\n")

    if session.step == "done":
        print("\n✅ Onboarding complete. Config saved.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(run_interactive())
