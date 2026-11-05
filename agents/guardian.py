"""
Guardian Agent

The core of Anchor's "sleeping guardian" model.

While the merchant sleeps, the Guardian:
  1. Intercepts every dispute event from watcher.py
  2. Applies pre-authorized thresholds (config.py) to decide: auto-act or ask
  3. Logs every autonomous action (briefing.py)
  4. Sends proactive Slack/SMS alerts when something needs attention
  5. Generates a morning brief summarizing what happened overnight

The key insight: this is not a dashboard you open. It's a system
that acts for you and reports back. Merchants don't need to know
about PayPal's dispute window, evidence requirements, or freeze
thresholds — Anchor does.
"""

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone, timedelta

import httpx

from agents.config import GuardianConfig, load_config
from agents.briefing import ActionRecord, Brief, generate_brief, log_action
from oracle.decision import (
    calculate as oracle_calculate,
    ClaimType,
    Evidence,
    Recommendation,
)


MERCHANT_ID = os.environ.get("MERCHANT_ID", "default")


class Guardian:

    def __init__(self, config: GuardianConfig | None = None):
        self.config = config
        self.paypal_base = "https://api.sandbox.paypal.com"
        self._token: str | None = None
        self._token_expires_at: float = 0

    async def _ensure_config(self):
        if self.config is None:
            self.config = await load_config(MERCHANT_ID)

    # -------------------------------------------------------------------------
    # Dispute routing — called by watcher.py instead of classifier directly
    # -------------------------------------------------------------------------

    async def handle_dispute(
        self,
        dispute_id: str,
        dispute_amount: float,
        reason_code: str,
        transaction_id: str | None,
        event_time: str,
        win_probability: float | None = None,
        decision=None,
    ):
        """
        Main entry point for dispute events. Applies pre-auth thresholds
        to decide whether to act autonomously or send a decision request.
        """
        await self._ensure_config()

        # If win_probability not yet calculated, run Oracle now
        if win_probability is None or decision is None:
            from agents.classifier import DisputeClassifier
            classifier = DisputeClassifier()
            decision = await classifier._run_oracle(
                dispute_id, dispute_amount, reason_code, transaction_id, event_time,
                high_volume_tier=self.config.high_volume_fee_tier if self.config else False,
            )
            if decision is None:
                # Fallback: let classifier handle it normally
                await classifier.process(
                    dispute_id, dispute_amount, reason_code, transaction_id, event_time,
                    high_volume_tier=self.config.high_volume_fee_tier if self.config else False,
                )
                return
            win_probability = decision.win_probability

        cfg = self.config

        # Account health guardrail
        if cfg.auto_protect_account:
            current_rate = await self._get_dispute_rate()
            if current_rate >= cfg.dispute_rate_critical:
                print(
                    f"[Guardian] Dispute rate {current_rate:.1%} ≥ critical "
                    f"{cfg.dispute_rate_critical:.1%} — deferring to manual review to protect account"
                )
                await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
                await self._notify(
                    f"⚠️ Account health: dispute rate at {current_rate:.1%}. "
                    f"Deferred ${dispute_amount:.2f} dispute to your review — dispute rate trending high."
                )
                return

        # State-awareness: check which actions the API currently permits for this dispute.
        # PayPal's dispute lifecycle has stages (INQUIRY, CLAIM, etc.) and only exposes
        # HATEOAS links for actions valid in the current state. Acting outside those links
        # will fail — check before committing to an autonomous action.
        token = await self._get_token()
        permitted = await self._fetch_permitted_actions(dispute_id, token)

        # Pre-authorization routing
        if cfg.auto_accept(dispute_amount):
            if "accept-claim" not in permitted:
                print(
                    f"[Guardian] {dispute_id}: accept-claim not available "
                    f"(permitted: {permitted or 'none'}) — deferring to approval"
                )
                await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
                return
            await self._accept_dispute(dispute_id, dispute_amount, reason="below_threshold")

        elif cfg.auto_fight(dispute_amount, win_probability):
            if "provide-evidence" not in permitted:
                # During INQUIRY stage PayPal exposes send-message instead of provide-evidence.
                # Posting tracking/delivery details directly to the buyer often closes the
                # inquiry before it escalates to a chargeback — better EV than deferring.
                if "send-message" in permitted:
                    print(
                        f"[Guardian] {dispute_id}: INQUIRY stage detected — "
                        f"attempting buyer message before chargeback escalation"
                    )
                    await self._send_inquiry_message(dispute_id, dispute_amount, decision)
                else:
                    print(
                        f"[Guardian] {dispute_id}: provide-evidence not available "
                        f"(permitted: {permitted or 'none'}) — deferring to approval"
                    )
                    await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
                return
            await self._fight_dispute(dispute_id, dispute_amount, win_probability, decision)

        else:
            await self._request_approval(dispute_id, dispute_amount, win_probability, decision)

    async def _accept_dispute(self, dispute_id: str, amount: float, reason: str):
        token = await self._get_token()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.paypal_base}/v1/customer/disputes/{dispute_id}/accept-claim",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "note": f"Anchor: auto-accepted ({reason}). "
                    f"Settlement Oracle recommended accept based on amount/evidence profile."
                },
            )
            success = r.status_code == 200

        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="dispute_accepted",
            dispute_id=dispute_id,
            amount=amount,
            outcome="accepted" if success else "failed",
            reasoning=f"Auto-accepted: {reason}",
            requires_attention=False,
        )
        await log_action(MERCHANT_ID, action)

        if self.config.notify_on_auto_action:
            await self._notify(
                f"✅ Auto-accepted ${amount:.2f} dispute `{dispute_id}` ({reason})"
            )

    async def _send_inquiry_message(self, dispute_id: str, amount: float, decision):
        """
        During INQUIRY stage, `provide-evidence` is not yet available — PayPal exposes
        `send-message` instead. Posting tracking/delivery details directly to the buyer
        can resolve the inquiry before it escalates to a chargeback.
        """
        token = await self._get_token()
        # Build a concise message from available evidence reasoning
        evidence_summary = "; ".join(
            r for r in (decision.reasoning if decision else [])
            if any(kw in r.lower() for kw in ["tracking", "delivery", "evidence", "win"])
        ) or "tracking and delivery records available on request"

        message = (
            f"Hi — I see you've opened an inquiry on your recent ${amount:.2f} order. "
            f"I'd like to resolve this quickly. Here's what I have on file: "
            f"{evidence_summary}. "
            f"Please let me know if this resolves your concern, or what additional "
            f"information would help."
        )

        url = f"{self.paypal_base}/v1/customer/disputes/{dispute_id}/send-message"
        async with httpx.AsyncClient() as c:
            # Try JSON first (text-only message). If PayPal returns 415, fall back
            # to multipart/form-data — required when attaching a message_document file.
            r = await c.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"message": message},
            )
            if r.status_code == 415:
                r = await c.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"message": message},
                )
            success = r.status_code in (200, 204)

        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="inquiry_message_sent",
            dispute_id=dispute_id,
            amount=amount,
            outcome="sent" if success else "failed",
            reasoning="INQUIRY stage: buyer message sent before chargeback escalation",
            requires_attention=True,  # merchant should monitor for buyer reply
        )
        await log_action(MERCHANT_ID, action)

        if self.config.notify_on_auto_action:
            await self._notify(
                f"📨 Sent inquiry message for `{dispute_id}` (${amount:.2f}) — "
                f"monitoring for buyer reply before chargeback escalation"
            )

    async def _fight_dispute(
        self, dispute_id: str, amount: float, win_prob: float, decision
    ):
        from agents.classifier import DisputeClassifier
        from agents.evidence import EvidenceAssembler, EvidencePackage
        from pathlib import Path
        import tempfile

        token = await self._get_token()
        assembler = EvidenceAssembler(access_token=token)
        tmp_dir = Path(tempfile.mkdtemp())
        package = EvidencePackage(
            dispute_id=dispute_id,
            notes=(
                f"Anchor automated evidence. Win probability: {win_prob:.0%}. "
                f"{' | '.join(decision.reasoning)}"
            ),
        )

        # Fetch dispute for evidence inventory
        classifier = DisputeClassifier()
        dispute_detail = await classifier._fetch_dispute(dispute_id, token)
        transaction = dispute_detail.get("disputed_transactions", [{}])[0]

        tracking = transaction.get("items", [{}])[0].get("tracking_info", {}) if transaction.get("items") else {}
        if tracking.get("tracking_number"):
            f = await assembler.gather_tracking_evidence(
                tracking_number=tracking["tracking_number"],
                carrier=tracking.get("carrier_name", "USPS"),
                tmp_dir=tmp_dir,
            )
            if f:
                package.add(f)

        buyer_email = transaction.get("buyer", {}).get("email", "")
        if buyer_email:
            for f in await assembler.gather_communication_evidence(buyer_email, tmp_dir):
                package.add(f)

        result = None
        if package.is_sufficient():
            result = await assembler.submit(package)

        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="dispute_fought",
            dispute_id=dispute_id,
            amount=amount,
            win_probability=win_prob,
            outcome="evidence_submitted" if result else "insufficient_evidence",
            reasoning=f"Auto-fought: {win_prob:.0%} win probability",
            requires_attention=result is None,
        )
        await log_action(MERCHANT_ID, action)

        if self.config.notify_on_auto_action:
            if result:
                await self._notify(
                    f"⚔️ Fighting ${amount:.2f} dispute `{dispute_id}` "
                    f"({win_prob:.0%} win probability) — evidence submitted at "
                    f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                )
            else:
                await self._notify(
                    f"⚠️ ${amount:.2f} dispute `{dispute_id}` — evidence insufficient, "
                    "needs your documents."
                )

    def _make_approval_token(self, dispute_id: str, action: str) -> str:
        """Generate a time-limited HMAC token bound to this dispute + action."""
        secret = os.environ.get("APPROVAL_TOKEN_SECRET", "")
        if not secret:
            return ""
        expiry = int(time.time()) + 48 * 3600
        payload = f"{dispute_id}:{MERCHANT_ID}:{action}:{expiry}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{sig}"

    async def _request_approval(
        self, dispute_id: str, amount: float, win_prob: float, decision
    ):
        """
        Dispute falls in the ask-me zone. Alert the merchant with
        a one-tap approval link containing a signed token.
        """
        action = ActionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action_type="dispute_pending",
            dispute_id=dispute_id,
            amount=amount,
            win_probability=win_prob,
            outcome="pending_approval",
            reasoning=" | ".join(decision.reasoning) if decision else "manual review needed",
            requires_attention=True,
        )
        await log_action(MERCHANT_ID, action)

        token = self._make_approval_token(dispute_id, "fight")
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-api.onrender.com")
        approve_url = f"{base_url}/approve/{dispute_id}?token={token}"

        msg = (
            f"🔔 Decision needed: ${amount:.2f} dispute `{dispute_id}`\n"
            f"Win probability: *{win_prob:.0%}*\n"
            f"Evidence ready. <{approve_url}|Approve fight> or reply *NO* to accept the claim.\n"
            f"_(This link expires in 48h and is specific to this dispute)_"
        )
        await self._notify(msg)

    # -------------------------------------------------------------------------
    # Morning brief
    # -------------------------------------------------------------------------

    async def send_morning_brief(self, merchant_id: str, since: datetime | None = None):
        """
        Generate and send the morning brief.
        Called by a cron job at 8 AM merchant time, or via Render scheduled task.
        """
        await self._ensure_config()

        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)

        brief = await generate_brief(merchant_id, since)

        if not self.config.morning_brief_enabled:
            return brief

        message = brief.to_slack_message()
        await self._notify(message)
        print(f"[Guardian] Morning brief sent for {merchant_id}: "
              f"{brief.disputes_handled} disputes, "
              f"{brief.disputes_pending_approval} pending, "
              f"${brief.amount_recovered:.2f} recovered, "
              f"{brief.orders_recovered} abandoned orders recovered")
        return brief

    # -------------------------------------------------------------------------
    # Account health
    # -------------------------------------------------------------------------

    async def _fetch_permitted_actions(self, dispute_id: str, token: str) -> set[str]:
        """
        Fetch the dispute's current HATEOAS links and return the set of permitted
        action rels (e.g. 'accept-claim', 'provide-evidence', 'make-offer').
        Returns an empty set on any error so callers fall back to manual review.
        """
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    f"{self.paypal_base}/v1/customer/disputes/{dispute_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5.0,
                )
                r.raise_for_status()
                links = r.json().get("links", [])
                # Normalize rel strings: PayPal sandbox sometimes serializes
                # "send_message" (underscore) while docs show "send-message" (hyphen).
                # Also accept href-path matching as a fallback.
                return {
                    link["rel"].replace("_", "-").lower()
                    for link in links if "rel" in link
                }
        except Exception as e:
            print(f"[Guardian] Could not fetch permitted actions for {dispute_id}: {e}")
            return set()

    async def _get_dispute_rate(self) -> float:
        """
        Query Elasticsearch for dispute rate over the last 30 days.
        Dispute rate = disputes / total transactions.
        Also updates high_volume_fee_tier on config:
          True when rate ≥ 1.5% AND transaction count > 100.
        """
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return 0.0

        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        query = {
            "query": {
                "range": {
                    "timestamp": {"gte": since}
                }
            },
            "aggs": {
                "dispute_count": {"filter": {"term": {"event_type": "dispute"}}},
                "transaction_count": {"filter": {"term": {"event_type": "capture"}}},
            },
            "size": 0,
        }
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{es_url}/anchor-events/_search",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=query,
                    timeout=3.0,
                )
                aggs = r.json().get("aggregations", {})
                disputes = aggs.get("dispute_count", {}).get("doc_count", 0)
                transactions = aggs.get("transaction_count", {}).get("doc_count", 1)
                rate = disputes / max(transactions, 1)
                if self.config is not None:
                    self.config.high_volume_fee_tier = (
                        rate >= 0.015 and transactions > 100
                    )
                return rate
        except Exception:
            return 0.0

    # -------------------------------------------------------------------------
    # Notifications via Zapier MCP → Slack
    # -------------------------------------------------------------------------

    async def _notify(self, message: str):
        """
        Send a Slack notification via Zapier webhook.
        In production: use Zapier MCP tool directly.
        """
        slack_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not slack_url:
            print(f"[Guardian] NOTIFY (no Slack configured): {message}")
            return

        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    slack_url,
                    json={
                        "text": message,
                        "channel": self.config.slack_channel if self.config else "#anchor-alerts",
                    },
                    timeout=5.0,
                )
        except Exception as e:
            print(f"[Guardian] Slack notification failed: {e}")

    async def _get_token(self) -> str:
        # PayPal tokens expire after ~9h (expires_in=32400). Re-fetch 60s before expiry.
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        import base64
        creds = base64.b64encode(
            f"{os.environ['PAYPAL_CLIENT_ID']}:{os.environ['PAYPAL_CLIENT_SECRET']}".encode()
        ).decode()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.paypal_base}/v1/oauth2/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            )
            data = r.json()
            self._token = data["access_token"]
            self._token_expires_at = time.monotonic() + data.get("expires_in", 32400)
            return self._token
