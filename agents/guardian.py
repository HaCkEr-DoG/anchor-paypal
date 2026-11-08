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

    def __init__(
        self,
        config: GuardianConfig | None = None,
        merchant_id: str | None = None,
        approval_secret: str = "",
    ):
        self.config = config
        self.merchant_id = merchant_id
        self.approval_secret = approval_secret
        _env = os.environ.get("PAYPAL_ENV", "sandbox")
        self.paypal_base = (
            "https://api.paypal.com" if _env == "live" else "https://api.sandbox.paypal.com"
        )
        self._token: str | None = None
        self._token_expires_at: float = 0

    async def _ensure_config(self):
        if self.config is None:
            self.config = await load_config(self.merchant_id or MERCHANT_ID)

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
        # Emergency kill switch: operator must explicitly opt in to autonomous actions.
        # Default is 0 (disabled) — set AUTONOMOUS_ACTIONS_ENABLED=1 to enable.
        if os.environ.get("AUTONOMOUS_ACTIONS_ENABLED", "0") != "1":
            print(
                f"[Guardian] AUTONOMOUS_ACTIONS_ENABLED not set — routing {dispute_id} "
                f"to human review (kill switch active)"
            )
            await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
            return

        await self._ensure_config()
        cfg = self.config
        # Refresh the fee-tier estimate before the first Oracle pass so the
        # decision uses the current tier, not a stale default from app startup.
        current_rate = await self._get_dispute_rate()

        # If win_probability not yet calculated, run Oracle now
        if win_probability is None or decision is None:
            from agents.classifier import DisputeClassifier
            classifier = DisputeClassifier()
            decision = await classifier._run_oracle(
                dispute_id, dispute_amount, reason_code, transaction_id, event_time,
                high_volume_tier=(
                    self.config.high_volume_fee_tier
                    and reason_code.upper() not in {"UNAUTHORIZED", "UNAUTHORISED", "BILLING_ISSUES", "BILLING_ISSUE"}
                    if self.config else False
                ),
            )
            if decision is None:
                # Fallback: let classifier handle it normally
                await classifier.process(
                    dispute_id, dispute_amount, reason_code, transaction_id, event_time,
                    high_volume_tier=(
                        self.config.high_volume_fee_tier
                        and reason_code.upper() not in {"UNAUTHORIZED", "UNAUTHORISED", "BILLING_ISSUES", "BILLING_ISSUE"}
                        if self.config else False
                    ),
                )
                return
            win_probability = decision.win_probability

        # Keep per-dispute context on the decision object; this Guardian instance
        # handles concurrent webhook tasks, so mutable instance fields can cross cases.
        decision.reason_code = reason_code
        decision.oracle_win_probability = win_probability or 0.0

        # ── Oracle Enrichment Layer (Wave 52-58 modules) ─────────────────────────
        # Win probability calibrator — refine the base Oracle probability with
        # 14 signal adjustments (CE3.0, 3DS, SP eligibility, evidence completeness)
        try:
            from oracle.win_probability_calibrator import calibrate_win_probability, WinProbabilityInput
            _cal_inp = WinProbabilityInput(
                dispute_id=dispute_id,
                reason_code=reason_code,
                network="PAYPAL",
                days_remaining_in_window=14,
                response_window_days=20,
            )
            _cal = calibrate_win_probability(_cal_inp)
            if abs(_cal.final_probability - win_probability) >= 0.05:
                print(
                    f"[Guardian] WinCalibrator refined {dispute_id}: "
                    f"{win_probability:.0%} → {_cal.final_probability:.0%} "
                    f"({_cal.confidence.value} confidence)"
                )
                win_probability = _cal.final_probability
                decision.oracle_win_probability = win_probability
        except Exception as _exc:
            print(f"[Guardian] WinCalibrator error (non-fatal): {_exc}")

        # Reason code strategy advisor — surface evidence requirements and strategy
        try:
            from oracle.reason_code_strategy_advisor import advise_reason_code_strategy, ReasonCodeStrategyInput
            _strat = advise_reason_code_strategy(ReasonCodeStrategyInput(
                dispute_id=dispute_id,
                reason_code=reason_code,
                network="PAYPAL",
                transaction_amount=dispute_amount,
            ))
            print(
                f"[Guardian] Strategy {dispute_id}: {_strat.strategy_type.value} | "
                f"sp={_strat.sp_eligible} ce3={_strat.ce3_eligible} | "
                f"base_win={_strat.base_win_rate:.0%}"
            )
        except Exception as _exc:
            print(f"[Guardian] StrategyAdvisor error (non-fatal): {_exc}")

        # Buyer Fraud DNA — detect professional chargeback filers before acting
        try:
            from oracle.buyer_fraud_dna_profiler import profile_buyer_fraud_dna, FraudDNAInput, FraudRiskTier
            _dna = profile_buyer_fraud_dna(FraudDNAInput(
                dispute_id=dispute_id,
                buyer_id="unknown",
                reason_code=reason_code,
                dispute_reason_text="",
                transaction_amount=dispute_amount,
                dispute_amount=dispute_amount,
            ))
            if _dna.risk_tier in (FraudRiskTier.HIGH, FraudRiskTier.CRITICAL):
                print(
                    f"[Guardian] FraudDNA alert {dispute_id}: "
                    f"tier={_dna.risk_tier.value} score={_dna.fraud_score:.0f}/100"
                )
        except Exception as _exc:
            print(f"[Guardian] FraudDNA error (non-fatal): {_exc}")

        # Chargeback Prevention — prescribe proactive steps before dispute escalates
        try:
            from oracle.chargeback_prevention_advisor import advise_chargeback_prevention, PreventionInput
            _prev = advise_chargeback_prevention(PreventionInput(
                transaction_id=transaction_id or dispute_id,
                merchant_id="anchor",
                reason_code=reason_code,
                amount=dispute_amount,
                hours_since_purchase=72.0,
                is_inquiry_stage=True,
            ))
            print(
                f"[Guardian] Prevention {dispute_id}: "
                f"urgency={_prev.urgency.value} prob={_prev.prevention_probability:.0%} "
                f"action={_prev.primary_action.value}"
            )
        except Exception as _exc:
            print(f"[Guardian] Prevention error (non-fatal): {_exc}")

        # Buyer LTV — override fight/accept if relationship value changes the calculus
        try:
            from oracle.buyer_ltv_decision_engine import calculate_ltv_decision, LTVDecisionInput
            _ltv_oracle = calculate_ltv_decision(LTVDecisionInput(
                dispute_id=dispute_id,
                buyer_id="unknown",
                dispute_amount=dispute_amount,
                oracle_ev_fight=getattr(decision, "ev_fight", 0.0),
                oracle_win_probability=win_probability or 0.0,
                oracle_recommendation=decision.recommendation if hasattr(decision, "recommendation") else "FIGHT",
            ))
            if _ltv_oracle.overrides_oracle:
                print(
                    f"[Guardian] LTV override {dispute_id}: "
                    f"oracle={_ltv_oracle.oracle_recommendation} → "
                    f"ltv_rec={_ltv_oracle.ltv_recommendation.value} "
                    f"(RAEV=${_ltv_oracle.raev:.0f})"
                )
        except Exception as _exc:
            print(f"[Guardian] LTV error (non-fatal): {_exc}")

        # ── Dispute Rate Protection (account survival) ───────────────────────────
        # This is the most important guardrail: even if Oracle says FIGHT (EV > 0),
        # crossing PayPal's 1% threshold triggers account limitations that cost more
        # than any single dispute recovery. should_protect_rate() decides whether
        # to override Oracle's FIGHT recommendation to ACCEPT strategically.
        if cfg.auto_protect_account:
            try:
                from agents.risk_monitor import should_protect_rate
                override, override_reason, health = await should_protect_rate(
                    merchant_id=self.merchant_id or MERCHANT_ID,
                    oracle_ev=getattr(decision, "ev_fight", 0.0),
                    oracle_win_prob=win_probability or 0.0,
                )
                if override:
                    print(
                        f"[Guardian] Rate protection override for {dispute_id}: {override_reason}"
                    )
                    # Accept strategically but log rate-protection as the reason
                    await self._accept_dispute(
                        dispute_id, dispute_amount,
                        reason=f"rate_protection:{health.threshold_status}",
                        decision=decision,
                    )
                    await self._notify(
                        f"🛡️ Rate protection: accepted ${dispute_amount:.2f} dispute `{dispute_id}` "
                        f"strategically.\n"
                        f"Current rate: *{health.window_30d.rate_by_amount:.2%}* "
                        f"(status: {health.threshold_status}, "
                        f"quota remaining: {health.safe_fight_quota})\n"
                        f"Reason: {override_reason}"
                    )
                    return
                # Log posture even when not overriding, so merchant has visibility
                print(
                    f"[Guardian] Rate health: {health.threshold_status} "
                    f"({health.window_30d.rate_by_amount:.2%}), "
                    f"quota={health.safe_fight_quota}, posture={health.fight_posture}"
                )
            except Exception as exc:
                # Rate monitor failure must NEVER block dispute processing
                print(f"[Guardian] Rate monitor error (non-fatal): {exc}")
                # Fallback to old basic check
                if current_rate >= cfg.dispute_rate_critical:
                    print(
                        f"[Guardian] Fallback: dispute rate {current_rate:.1%} ≥ critical "
                        f"{cfg.dispute_rate_critical:.1%} — deferring to manual review"
                    )
                    await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
                    await self._notify(
                        f"⚠️ Account health: dispute rate at {current_rate:.1%}. "
                        f"Deferred ${dispute_amount:.2f} dispute to your review."
                    )
                    return

        # Source-cited AI evidence review is performed by the classifier. This
        # block reports provider configuration only; it does not imply a call.
        from agents.astropods import AstropodsGateway
        _astro = AstropodsGateway()
        if _astro.gateway_enabled():
            print(f"[Guardian] Astropods gateway configured for source-cited evidence review")
        else:
            print("[Guardian] AI evidence review unavailable; deterministic Oracle remains active")

        # APIMatic context grounding — verify correct endpoint + required fields before
        # making any PayPal API call. Prevents hallucinated paths or wrong parameters.
        from agents.apimatic import ApiMaticClient
        _apimatic = ApiMaticClient()
        _ep_accept  = _apimatic.lookup_endpoint("accept")
        _ep_evidence = _apimatic.lookup_endpoint("provide_evidence")
        _ep_message  = _apimatic.lookup_endpoint("send_message")
        print(
            f"[Guardian] APIMatic context loaded — "
            f"accept={_ep_accept['path']}, evidence={_ep_evidence['path']}"
        )

        # State-awareness: check which actions the API currently permits for this dispute.
        # PayPal's dispute lifecycle has stages (INQUIRY, CLAIM, etc.) and only exposes
        # HATEOAS links for actions valid in the current state. Acting outside those links
        # will fail — check before committing to an autonomous action.
        token = await self._get_token()
        permitted = await self._fetch_permitted_actions(dispute_id, token)

        # Pre-authorization routing
        if (
            cfg.auto_accept(dispute_amount)
            and decision.recommendation == Recommendation.ACCEPT
            and decision.ev_fight <= 0
        ):
            if "accept-claim" not in permitted:
                print(
                    f"[Guardian] {dispute_id}: accept-claim not available "
                    f"(permitted: {permitted or 'none'}) — deferring to approval"
                )
                await self._request_approval(dispute_id, dispute_amount, win_probability, decision)
                return
            await self._accept_dispute(dispute_id, dispute_amount, reason="below_threshold", decision=decision)

        elif (
            cfg.auto_fight(dispute_amount, win_probability)
            and decision.recommendation == Recommendation.FIGHT
            and decision.ev_fight > 0
        ):
            if "provide-evidence" not in permitted:
                # HATEOAS links, rather than our guessed lifecycle stage, govern actions.
                # PayPal supports evidence submission in some INQUIRY cases; when it is not
                # available, send a neutral message only if the live link permits it.
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

            # ── Settlement Negotiation Engine (make-offer third path) ────────────
            # Before fighting, check if a partial settlement offer would have higher
            # EV than fighting. Only valid when PayPal's HATEOAS links include make-offer.
            if "make-offer" in permitted:
                try:
                    from agents.negotiator import evaluate_negotiation, submit_offer
                    from oracle.decision import ClaimType
                    _ct_map = {
                        "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
                        "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
                        "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
                        "BILLING_ISSUES": ClaimType.BILLING_ISSUE,
                        "BILLING_ISSUE": ClaimType.BILLING_ISSUE,
                    }
                    _claim_type = _ct_map.get(reason_code.upper(), ClaimType.ITEM_NOT_RECEIVED)
                    negot = evaluate_negotiation(
                        dispute_amount=dispute_amount,
                        win_probability=win_probability or 0.0,
                        claim_type=_claim_type,
                        ev_fight=decision.ev_fight,
                        high_volume_tier=(self.config.high_volume_fee_tier if self.config else False),
                    )
                    if negot.should_negotiate:
                        print(
                            f"[Guardian] Negotiation: offering ${negot.offer_amount:.2f} "
                            f"({negot.offer_fraction:.0%}) for {dispute_id}. "
                            f"EV(negotiate)=${negot.ev_negotiate:.2f} > EV(fight)=${negot.ev_fight:.2f}"
                        )
                        _negot_token = await self._get_token()
                        success = await submit_offer(
                            dispute_id=dispute_id,
                            offer_amount=negot.offer_amount,
                            paypal_base=self.paypal_base,
                            token=_negot_token,
                        )
                        if success:
                            await self._notify(
                                f"🤝 Settlement offer: `{dispute_id}` — offered ${negot.offer_amount:.2f} "
                                f"({negot.offer_fraction:.0%} of ${dispute_amount:.2f}).\n"
                                f"{negot.reasoning}"
                            )
                            return
                except Exception as exc:
                    print(f"[Guardian] Negotiation error (non-fatal): {exc}")

            await self._fight_dispute(dispute_id, dispute_amount, win_probability, decision)

        else:
            await self._request_approval(dispute_id, dispute_amount, win_probability, decision)

    async def _accept_dispute(self, dispute_id: str, amount: float, reason: str, decision=None):
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
            reasoning=f"Auto-accepted: {reason}" + self._ai_review_log_suffix(decision),
            requires_attention=False,
        )
        await log_action(self.merchant_id or MERCHANT_ID, action)

        if self.config.notify_on_auto_action:
            await self._notify(
                f"✅ Auto-accepted ${amount:.2f} dispute `{dispute_id}` ({reason})"
            )

        # Oracle calibration: record accepted outcome
        try:
            from oracle.feedback import record_outcome, OutcomeRecord
            await record_outcome(OutcomeRecord(
                dispute_id=dispute_id,
                merchant_id=self.merchant_id or MERCHANT_ID,
                claim_type=getattr(decision, "reason_code", "UNKNOWN"),
                oracle_prediction=getattr(decision, "oracle_win_probability", 0.0),
                oracle_ev=getattr(decision, "ev_fight", 0.0),
                oracle_action="ACCEPT",
                actual_action="ACCEPT",
                actual_outcome="ACCEPTED",
                dispute_amount=amount,
            ))
        except Exception:
            pass

        # Deadline tracking: dispute is resolved — remove from deadline index
        try:
            from agents.deadline_tracker import mark_dispute_resolved
            await mark_dispute_resolved(dispute_id)
        except Exception:
            pass

    async def _send_inquiry_message(self, dispute_id: str, amount: float, decision):
        """
        During INQUIRY stage, `provide-evidence` is not yet available — PayPal exposes
        `send-message` instead. Posting tracking/delivery details directly to the buyer
        can resolve the inquiry before it escalates to a chargeback.
        """
        token = await self._get_token()
        # Build a concise message from available evidence reasoning
        message = (
            "Hello, we received your PayPal inquiry and would like to help resolve it. "
            "Please reply with any details that would help us understand the issue. "
            "We will review the order records and follow up through PayPal."
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
            r.raise_for_status()
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
        await log_action(self.merchant_id or MERCHANT_ID, action)

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
        tracking_number = tracking.get("tracking_number", "")

        # ── Carrier Auto-Verification ─────────────────────────────────────────
        # For INR disputes: auto-pull live delivery confirmation from the carrier.
        # This converts 15 minutes of manual tracking lookups into <2 seconds
        # and dramatically improves evidence quality for INR fights.
        carrier_result = None
        if tracking_number and getattr(decision, "reason_code", "").upper() in {
            "ITEM_NOT_RECEIVED", "INR", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
        }:
            try:
                from agents.carrier import verify_delivery
                carrier_result = await verify_delivery(tracking_number)
                if carrier_result and carrier_result.delivered:
                    print(
                        f"[Guardian] Carrier confirmed delivery for {dispute_id}: "
                        f"{carrier_result.carrier} — {carrier_result.delivery_date} "
                        f"at {carrier_result.delivery_location}"
                    )
                    # Auto-inject delivery confirmation into evidence package notes
                    package.notes += (
                        f"\nCarrier verification: {carrier_result.carrier} confirmed delivery "
                        f"on {carrier_result.delivery_date} to {carrier_result.delivery_location}. "
                        f"Tracking: {tracking_number}. "
                        f"Signature obtained: {carrier_result.signature_obtained}. "
                        f"Carrier URL: {carrier_result.carrier_url}"
                    )
                elif carrier_result:
                    print(
                        f"[Guardian] Carrier status for {dispute_id}: "
                        f"{carrier_result.latest_status} (not yet delivered)"
                    )
            except Exception as exc:
                print(f"[Guardian] Carrier verification error (non-fatal): {exc}")

        if tracking_number and (
            str(tracking.get("status", "")).upper() == "DELIVERED"
            or (carrier_result and carrier_result.delivered)
        ):
            f = await assembler.gather_tracking_evidence(
                tracking_number=tracking_number,
                carrier=tracking.get("carrier_name", carrier_result.carrier if carrier_result else "USPS"),
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
            action_type="dispute_fought" if result else "dispute_pending",
            dispute_id=dispute_id,
            amount=amount,
            win_probability=win_prob,
            outcome="evidence_submitted" if result else "insufficient_evidence",
            reasoning=(
                f"Evidence submitted after approval; model win estimate {win_prob:.0%}"
                if result else f"Evidence unavailable or insufficient; review needed ({win_prob:.0%} model win estimate)"
            ) + self._ai_review_log_suffix(decision),
            requires_attention=result is None,
        )
        await log_action(self.merchant_id or MERCHANT_ID, action)

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

    def _ai_review_log_suffix(self, decision=None) -> str:
        """Include source-cited AI context in the audit trail, never as proof."""
        review = getattr(decision, "ai_review", {}) or {} if decision is not None else {}
        if review.get("status") != "ready":
            return f" | AI evidence review: {review.get('status', 'not run')}"
        cited = "; ".join(
            f"{claim['statement']} [sources: {', '.join(claim['source_ids'])}]"
            for claim in review.get("claims", [])[:3]
        )
        return f" | AI evidence review ({review.get('model', 'model')}): {cited}"

    def _make_approval_token(self, dispute_id: str, action: str) -> str:
        """Generate a time-limited HMAC token bound to this dispute + merchant + action."""
        # Prefer the ES-persisted secret passed at construction time over raw env var
        secret = self.approval_secret or os.environ.get("APPROVAL_TOKEN_SECRET", "")
        if not secret:
            return ""
        mid = self.merchant_id or MERCHANT_ID
        max_age = int(os.environ.get("APPROVAL_LINK_MAX_AGE", "3600"))
        expiry = int(time.time()) + max_age
        payload = f"{dispute_id}:{mid}:{action}:{expiry}"
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
            reasoning=(" | ".join(decision.reasoning) if decision else "manual review needed")
            + self._ai_review_log_suffix(decision),
            requires_attention=True,
        )
        mid = self.merchant_id or MERCHANT_ID
        await log_action(mid, action)

        token = self._make_approval_token(dispute_id, "fight")
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://anchor-api.onrender.com")
        approve_url = f"{base_url}/approve/{dispute_id}?token={token}&mid={mid}"

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
                rels = set()
                for link in links:
                    rel = link.get("rel")
                    if rel:
                        rels.add(rel.replace("_", "-").lower())
                return rels
        except Exception as e:
            print(f"[Guardian] Could not fetch permitted actions for {dispute_id}: {e}")
            return set()

    async def _get_dispute_rate(self) -> float:
        """
        Approximate the PayPal tier using the previous three complete calendar months.
        PayPal's published US rule is amount-based for eligible INR/SNAD claims;
        this uses only recorded events and must remain a fallback when data is incomplete.
        Also updates high_volume_fee_tier on config:
          True when rate ≥ 1.5% AND transaction count > 100.
        """
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        confirmed_tier = os.environ.get("PAYPAL_HIGH_VOLUME_FEE_TIER")
        if self.config is not None and confirmed_tier is not None:
            self.config.high_volume_fee_tier = confirmed_tier.strip().lower() in {"1", "true", "yes", "high"}
        if not es_url or not es_key:
            return 0.0

        now = datetime.now(timezone.utc)
        month_index = now.year * 12 + now.month - 1
        first_this_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        start_index = month_index - 3
        since = datetime(start_index // 12, start_index % 12 + 1, 1, tzinfo=timezone.utc).isoformat()
        query = {
            "query": {"bool": {"filter": [
                {"term": {"merchant_id": self.config.merchant_id}},
                {"range": {"timestamp": {"gte": since, "lt": first_this_month.isoformat()}}},
            ]}},
            "aggs": {
                "eligible_disputes": {"filter": {"term": {"event_type": "eligible_dispute"}}, "aggs": {"amount": {"sum": {"field": "amount"}}}},
                "sales": {"filter": {"term": {"event_type": "capture"}}, "aggs": {"amount": {"sum": {"field": "amount"}}, "count": {"value_count": {"field": "event_id"}}}},
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
                disputes_amount = aggs.get("eligible_disputes", {}).get("amount", {}).get("value", 0) or 0
                sales = aggs.get("sales", {})
                sales_amount = sales.get("amount", {}).get("value", 0) or 0
                transactions = sales.get("doc_count", 0) or 0
                rate = disputes_amount / sales_amount if sales_amount > 0 else 0.0
                if self.config is not None:
                    confirmed_tier = os.environ.get("PAYPAL_HIGH_VOLUME_FEE_TIER")
                    if confirmed_tier is not None:
                        self.config.high_volume_fee_tier = confirmed_tier.strip().lower() in {"1", "true", "yes", "high"}
                    elif transactions > 100 and sales_amount > 0:
                        self.config.high_volume_fee_tier = rate >= 0.015
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
        from agents.paypal_client import get_client
        return await get_client().get_token()
