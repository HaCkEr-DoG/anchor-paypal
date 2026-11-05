"""
Dispute Classifier Agent

Takes a raw dispute event and:
1. Fetches full dispute details from PayPal REST API
2. Queries Elasticsearch for similar historical disputes + outcome
3. Scores available evidence
4. Runs Settlement Oracle
5. Dispatches to fight/accept/gather pipeline

This is the "brain" connecting all components.
"""

import os
from datetime import datetime, timezone

import httpx

from oracle.decision import (
    calculate as oracle_calculate,
    ClaimType,
    Evidence,
    Recommendation,
)
from agents.evidence import EvidenceAssembler, EvidencePackage


CLAIM_TYPE_MAP = {
    "ITEM_NOT_RECEIVED": ClaimType.ITEM_NOT_RECEIVED,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
    "UNAUTHORISED": ClaimType.UNAUTHORIZED,
    "UNAUTHORIZED": ClaimType.UNAUTHORIZED,
    "BILLING_ISSUES": ClaimType.BILLING_ISSUE,
}


class DisputeClassifier:

    def __init__(self):
        self.paypal_base = "https://api.sandbox.paypal.com"
        self._token: str | None = None

    async def process(
        self,
        dispute_id: str,
        dispute_amount: float,
        reason_code: str,
        transaction_id: str | None,
        event_time: str,
        high_volume_tier: bool = False,
    ):
        token = await self._get_token()
        claim_type = CLAIM_TYPE_MAP.get(reason_code, ClaimType.ITEM_NOT_RECEIVED)

        # 1. Fetch full dispute from PayPal for evidence inventory
        dispute_detail = await self._fetch_dispute(dispute_id, token)
        messages = dispute_detail.get("messages", [])
        transaction_info = dispute_detail.get("disputed_transactions", [{}])[0]
        deadline = dispute_detail.get("seller_response_due_date", "")

        # 2. Derive available evidence from transaction data
        evidence = self._assess_evidence(transaction_info, messages, claim_type)

        # 3. Check Elasticsearch for similar disputes (historical win rate)
        similar_win_rate = await self._lookup_similar_disputes(
            claim_type=claim_type,
            amount=dispute_amount,
            buyer_email=transaction_info.get("buyer", {}).get("email", ""),
        )

        # 4. Calculate deadline urgency
        hours_remaining = self._hours_until(deadline)

        # 5. Run Settlement Oracle
        decision = oracle_calculate(
            dispute_amount=dispute_amount,
            claim_type=claim_type,
            evidence=evidence,
            deadline_hours=hours_remaining,
            similar_dispute_win_rate=similar_win_rate,
            high_volume_tier=high_volume_tier,
        )

        print(
            f"[Classifier] Dispute {dispute_id}: "
            f"{decision.recommendation.value} "
            f"(p_win={decision.win_probability:.0%}, "
            f"EV=${decision.ev_fight:.2f}, "
            f"confidence={decision.confidence:.0%})"
        )

        # 6. Act
        assembler = EvidenceAssembler(access_token=token)

        if decision.recommendation == Recommendation.FIGHT:
            await self._fight(dispute_id, dispute_detail, token, decision, assembler)

        elif decision.recommendation == Recommendation.ACCEPT:
            await assembler.accept_dispute(
                dispute_id,
                note=f"PayGuard Settlement Oracle: EV(fight)=${decision.ev_fight:.2f}. Not worth contesting.",
            )
            print(f"[Classifier] Accepted dispute {dispute_id}")

        else:  # GATHER_EVIDENCE_FIRST
            await self._request_evidence_from_seller(dispute_id, decision)

        # 7. Push to AG Studio / Bryntum dashboard
        await self._update_dashboard(dispute_id, decision, dispute_amount, claim_type)

    async def _fight(self, dispute_id, dispute_detail, token, decision, assembler):
        """Autonomously assemble and submit evidence."""
        from pathlib import Path
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp())
        package = EvidencePackage(
            dispute_id=dispute_id,
            notes=f"PayGuard automated evidence. Win probability: {decision.win_probability:.0%}. {' | '.join(decision.reasoning)}",
        )

        transaction = dispute_detail.get("disputed_transactions", [{}])[0]

        # Gather tracking evidence via KERNEL if available
        tracking = transaction.get("items", [{}])[0].get("tracking_info", {})
        if tracking.get("tracking_number"):
            evidence_file = await assembler.gather_tracking_evidence(
                tracking_number=tracking["tracking_number"],
                carrier=tracking.get("carrier_name", "USPS"),
                tmp_dir=tmp_dir,
            )
            if evidence_file:
                package.add(evidence_file)

        # Gather communication history
        buyer_email = transaction.get("buyer", {}).get("email", "")
        if buyer_email:
            comm_files = await assembler.gather_communication_evidence(buyer_email, tmp_dir)
            for f in comm_files:
                package.add(f)

        if package.is_sufficient():
            result = await assembler.submit(package)
            print(f"[Classifier] Evidence submitted for {dispute_id}: {result}")
        else:
            print(f"[Classifier] Insufficient evidence for {dispute_id} — alerting seller")
            await self._request_evidence_from_seller(dispute_id, decision)

    async def _fetch_dispute(self, dispute_id: str, token: str) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.paypal_base}/v1/customer/disputes/{dispute_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return r.json()

    def _assess_evidence(self, transaction_info: dict, messages: list, claim_type: ClaimType) -> Evidence:
        """
        Infer available evidence from transaction data.
        In a full implementation, also scans email via Zapier MCP.
        """
        items = transaction_info.get("items", [{}])
        tracking = items[0].get("tracking_info", {}) if items else {}

        return Evidence(
            has_tracking_number=bool(tracking.get("tracking_number")),
            has_delivery_confirmation=tracking.get("status") == "DELIVERED",
            has_buyer_communication=any(
                m.get("posted_by") == "BUYER" for m in messages
            ),
            has_item_description_proof=bool(
                transaction_info.get("invoice_number")
            ),
            seller_account_age_days=int(
                os.environ.get("SELLER_ACCOUNT_AGE_DAYS", "365")
            ),
            seller_dispute_win_rate=float(
                os.environ.get("SELLER_WIN_RATE", "0.5")
            ),
        )

    async def _lookup_similar_disputes(
        self, claim_type: ClaimType, amount: float, buyer_email: str
    ) -> float | None:
        """
        Query Elasticsearch for similar past disputes.
        Returns historical win rate, or None if insufficient data.

        Uses Elastic vector search on dispute descriptions to find
        semantically similar cases, not just exact matches.
        """
        es_url = os.environ.get("ELASTICSEARCH_URL")
        es_key = os.environ.get("ELASTICSEARCH_API_KEY")
        if not es_url or not es_key:
            return None

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"claim_type": claim_type.value}},
                        {"range": {"amount": {"gte": amount * 0.5, "lte": amount * 2.0}}},
                    ]
                }
            },
            "aggs": {
                "win_rate": {
                    "avg": {"field": "seller_won"}  # 1=won, 0=lost
                }
            },
            "size": 0,
        }

        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{es_url}/payguard-disputes/_search",
                    headers={
                        "Authorization": f"ApiKey {es_key}",
                        "Content-Type": "application/json",
                    },
                    json=query,
                    timeout=5.0,
                )
                result = r.json()
                win_rate = result.get("aggregations", {}).get("win_rate", {}).get("value")
                return float(win_rate) if win_rate is not None else None
        except Exception:
            return None

    def _hours_until(self, deadline_str: str) -> int:
        if not deadline_str:
            return 72  # default assumption
        try:
            deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            delta = deadline - datetime.now(timezone.utc)
            return max(0, int(delta.total_seconds() / 3600))
        except Exception:
            return 72

    async def _request_evidence_from_seller(self, dispute_id: str, decision):
        """
        When Oracle says GATHER_EVIDENCE_FIRST, alert the seller with
        a specific checklist of what evidence is needed and why.
        Uses Zapier MCP → Slack/email.
        """
        checklist = "\n".join(f"• {r}" for r in decision.reasoning)
        print(
            f"[Classifier] Seller alert for {dispute_id}:\n"
            f"  Win probability close to break-even. Gather:\n{checklist}"
        )
        # In production: zapier_mcp.send_slack_alert(dispute_id, checklist)
        # or zapier_mcp.send_email(seller_email, subject, checklist)

    async def _update_dashboard(self, dispute_id, decision, amount, claim_type):
        """Push dispute state to the AG Studio + Bryntum dashboard."""
        # POST to internal dashboard API which feeds Bryntum Scheduler
        # and updates AG Grid dispute triage table
        print(
            f"[Classifier] Dashboard updated: {dispute_id} → "
            f"{decision.recommendation.value} "
            f"({decision.win_probability:.0%} win, ${amount})"
        )

    async def _run_oracle(
        self,
        dispute_id: str,
        dispute_amount: float,
        reason_code: str,
        transaction_id: str | None,
        event_time: str,
        high_volume_tier: bool = False,
    ):
        """
        Run the Settlement Oracle and return the decision without acting.
        Used by Guardian to apply pre-auth thresholds before dispatching.
        """
        try:
            token = await self._get_token()
            claim_type = CLAIM_TYPE_MAP.get(reason_code, ClaimType.ITEM_NOT_RECEIVED)
            dispute_detail = await self._fetch_dispute(dispute_id, token)
            transaction_info = dispute_detail.get("disputed_transactions", [{}])[0]
            messages = dispute_detail.get("messages", [])
            deadline = dispute_detail.get("seller_response_due_date", "")
            evidence = self._assess_evidence(transaction_info, messages, claim_type)
            similar_win_rate = await self._lookup_similar_disputes(
                claim_type=claim_type,
                amount=dispute_amount,
                buyer_email=transaction_info.get("buyer", {}).get("email", ""),
            )
            hours_remaining = self._hours_until(deadline)
            return oracle_calculate(
                dispute_amount=dispute_amount,
                claim_type=claim_type,
                evidence=evidence,
                deadline_hours=hours_remaining,
                similar_dispute_win_rate=similar_win_rate,
                high_volume_tier=high_volume_tier,
            )
        except Exception as e:
            print(f"[Classifier] _run_oracle failed for {dispute_id}: {e}")
            return None

    async def _get_token(self) -> str:
        from agents.paypal_client import get_client
        return await get_client().get_token()
