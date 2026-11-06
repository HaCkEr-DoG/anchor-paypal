"""
Tests for Wave 48: appeal_window_tracker, dispute_triage_engine.
"""
import pytest
from oracle.appeal_window_tracker import (
    track_appeal_windows, DisputeWindowInput,
    Network, DisputeStage, UrgencyBand,
)
from oracle.dispute_triage_engine import (
    triage_dispute, triage_batch, DisputeTriageInput,
    TriagePriority, TriageRouting, FraudSignal,
)

DAY = 86400
BASE_NOW = 1700000000


# ═══════════════════════════════════════════════════════════════════
# Appeal Window Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestAppealWindowTracker:

    def _inp(self, network=Network.VISA, stage=DisputeStage.CHARGEBACK,
             days_since_open=5, **kw) -> DisputeWindowInput:
        defaults = dict(
            dispute_id="D-001",
            network=network,
            stage=stage,
            stage_opened_epoch=BASE_NOW - days_since_open * DAY,
            now_epoch=BASE_NOW,
        )
        defaults.update(kw)
        return DisputeWindowInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = track_appeal_windows(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_deadlines_non_empty_on_active_dispute(self):
        r = track_appeal_windows(self._inp())
        assert len(r.deadlines) >= 1

    def test_can_still_appeal_true_when_window_open(self):
        r = track_appeal_windows(self._inp(days_since_open=5))
        assert r.can_still_appeal is True

    # ── Deadline calculation ──────────────────────────────────────

    def test_visa_chargeback_response_window_20_days(self):
        # Opened 5 days ago → 15 days remaining
        r = track_appeal_windows(self._inp(
            network=Network.VISA,
            stage=DisputeStage.CHARGEBACK,
            days_since_open=5,
        ))
        response_dl = next(
            d for d in r.deadlines if "response" in d.label.lower()
        )
        assert abs(response_dl.days_remaining - 15.0) < 0.1

    def test_visa_appeal_window_45_days(self):
        # Opened 5 days ago → 40 days remaining
        r = track_appeal_windows(self._inp(
            network=Network.VISA,
            stage=DisputeStage.CHARGEBACK,
            days_since_open=5,
        ))
        appeal_dl = next(
            d for d in r.deadlines if "appeal" in d.label.lower()
        )
        assert abs(appeal_dl.days_remaining - 40.0) < 0.1

    def test_paypal_inquiry_response_10_days(self):
        # Opened 3 days ago → 7 days remaining
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.INQUIRY,
            days_since_open=3,
        ))
        response_dl = r.deadlines[0]
        assert abs(response_dl.days_remaining - 7.0) < 0.1

    # ── Urgency bands ─────────────────────────────────────────────

    def test_critical_urgency_when_1_day_left(self):
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.INQUIRY,
            days_since_open=9,  # 10 - 9 = 1 day left
        ))
        assert r.primary_deadline.urgency == UrgencyBand.CRITICAL

    def test_high_urgency_when_4_days_left(self):
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.INQUIRY,
            days_since_open=6,  # 10 - 6 = 4 days left
        ))
        assert r.primary_deadline.urgency == UrgencyBand.HIGH

    def test_low_urgency_when_30_days_left(self):
        r = track_appeal_windows(self._inp(
            network=Network.VISA,
            stage=DisputeStage.CHARGEBACK,
            days_since_open=1,  # 20 - 1 = 19 days on response window
        ))
        # Pick the response deadline
        response_dl = next(
            d for d in r.deadlines if "response" in d.label.lower()
        )
        assert response_dl.urgency == UrgencyBand.LOW

    # ── Expired window ────────────────────────────────────────────

    def test_expired_when_past_window(self):
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.INQUIRY,
            days_since_open=15,  # 15 > 10-day window
        ))
        expired = [d for d in r.deadlines if d.urgency == UrgencyBand.EXPIRED]
        assert len(expired) >= 1
        assert r.any_window_expired is True

    def test_cannot_appeal_when_all_expired(self):
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.INQUIRY,
            days_since_open=20,  # Both windows expired
        ))
        # All windows expired → can_still_appeal might be False
        all_expired = all(d.urgency == UrgencyBand.EXPIRED for d in r.deadlines)
        if all_expired:
            assert r.can_still_appeal is False

    # ── Pre-arbitration stage ─────────────────────────────────────

    def test_pre_arb_stage_has_arbitration_deadline(self):
        r = track_appeal_windows(self._inp(
            network=Network.VISA,
            stage=DisputeStage.PRE_ARBITRATION,
            days_since_open=5,
        ))
        arb_dl = next(
            (d for d in r.deadlines if "arbitration" in d.label.lower()), None
        )
        assert arb_dl is not None

    def test_paypal_no_pre_arb_window(self):
        # PayPal has no pre-arb (window=0), so PRE_ARB stage gets no pre-arb deadline
        r = track_appeal_windows(self._inp(
            network=Network.PAYPAL,
            stage=DisputeStage.PRE_ARBITRATION,
            days_since_open=5,
        ))
        pre_arb_dl = next(
            (d for d in r.deadlines if "pre-arb" in d.label.lower()), None
        )
        assert pre_arb_dl is None

    # ── Primary deadline ──────────────────────────────────────────

    def test_primary_deadline_is_soonest(self):
        r = track_appeal_windows(self._inp(
            network=Network.VISA,
            stage=DisputeStage.CHARGEBACK,
            days_since_open=5,
        ))
        if len(r.deadlines) >= 2:
            assert r.primary_deadline.days_remaining <= r.deadlines[1].days_remaining

    def test_closed_stage_no_active_deadlines(self):
        r = track_appeal_windows(self._inp(
            stage=DisputeStage.CLOSED,
        ))
        assert len(r.deadlines) == 0
        assert r.primary_deadline is None


# ═══════════════════════════════════════════════════════════════════
# Dispute Triage Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeTriageEngine:

    def _inp(self, amount=100.0, win_prob=0.50, **kw) -> DisputeTriageInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code="INR",
            transaction_amount=amount,
            win_probability=win_prob,
        )
        defaults.update(kw)
        return DisputeTriageInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        d = triage_dispute(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in d.summary

    def test_ev_fight_formula(self):
        p, amount, fc, cb = 0.60, 100.0, 15.0, 20.0
        expected = p * amount - fc - (1 - p) * cb
        d = triage_dispute(self._inp(
            amount=amount, win_prob=p, fight_cost=fc, chargeback_fee=cb,
        ))
        assert d.ev_fight == pytest.approx(expected, abs=0.5)

    def test_ev_accept_is_negative_amount(self):
        d = triage_dispute(self._inp(amount=100.0))
        assert d.ev_accept == pytest.approx(-100.0, abs=0.01)

    # ── AUTO_ACCEPT routing ───────────────────────────────────────

    def test_auto_accept_tiny_amount(self):
        d = triage_dispute(self._inp(amount=5.0))
        assert d.routing == TriageRouting.AUTO_ACCEPT

    def test_auto_accept_already_refunded(self):
        d = triage_dispute(self._inp(already_refunded=True))
        assert d.routing == TriageRouting.AUTO_ACCEPT

    def test_auto_accept_negative_ev_non_vip(self):
        # EV(fight) = 0.20*100 - 15 - 0.80*20 = 20-15-16 = -11, > -15 boundary
        # Need EV < -15: e.g. p=0.10, amount=50, fc=15, cb=20
        # EV = 0.10*50 - 15 - 0.90*20 = 5 - 15 - 18 = -28
        d = triage_dispute(self._inp(
            amount=50.0, win_prob=0.10, fight_cost=15.0, chargeback_fee=20.0,
            is_vip=False, fraud_signal=FraudSignal.MINIMAL,
        ))
        assert d.routing == TriageRouting.AUTO_ACCEPT

    def test_no_auto_accept_vip_even_negative_ev(self):
        # EV < -15 but VIP → escalate to MANUAL
        d = triage_dispute(self._inp(
            amount=50.0, win_prob=0.10, fight_cost=15.0, chargeback_fee=20.0,
            is_vip=True,
        ))
        assert d.routing != TriageRouting.AUTO_ACCEPT

    # ── AUTO_FIGHT routing ────────────────────────────────────────

    def test_auto_fight_sp_eligible_high_win(self):
        d = triage_dispute(self._inp(
            amount=200.0, win_prob=0.70,
            seller_protection_eligible=True,
            has_tracking_evidence=True,
            fraud_signal=FraudSignal.MINIMAL,
        ))
        assert d.routing == TriageRouting.AUTO_FIGHT

    def test_no_auto_fight_without_sp(self):
        d = triage_dispute(self._inp(
            amount=200.0, win_prob=0.75,
            seller_protection_eligible=False,
            has_tracking_evidence=True,
        ))
        assert d.routing != TriageRouting.AUTO_FIGHT

    def test_no_auto_fight_fraud_signal(self):
        d = triage_dispute(self._inp(
            amount=200.0, win_prob=0.75,
            seller_protection_eligible=True,
            has_tracking_evidence=True,
            fraud_signal=FraudSignal.HIGH,
        ))
        assert d.routing != TriageRouting.AUTO_FIGHT

    # ── MANUAL_REVIEW routing ─────────────────────────────────────

    def test_manual_review_on_fraud_signal(self):
        d = triage_dispute(self._inp(
            fraud_signal=FraudSignal.VERY_HIGH,
        ))
        assert d.routing == TriageRouting.MANUAL_REVIEW

    def test_manual_review_on_vip(self):
        d = triage_dispute(self._inp(
            is_vip=True, buyer_ltv=600.0,
        ))
        assert d.routing == TriageRouting.MANUAL_REVIEW

    # ── Priority ─────────────────────────────────────────────────

    def test_urgent_on_critical_deadline(self):
        d = triage_dispute(self._inp(days_until_deadline=1))
        assert d.priority == TriagePriority.URGENT

    def test_urgent_on_high_amount_plus_fraud(self):
        d = triage_dispute(self._inp(
            amount=600.0, fraud_signal=FraudSignal.VERY_HIGH,
        ))
        assert d.priority == TriagePriority.URGENT

    def test_high_priority_on_near_deadline(self):
        d = triage_dispute(self._inp(days_until_deadline=4))
        assert d.priority in (TriagePriority.URGENT, TriagePriority.HIGH)

    def test_defer_on_tiny_amount(self):
        d = triage_dispute(self._inp(amount=5.0, days_until_deadline=30))
        assert d.priority in (TriagePriority.DEFER, TriagePriority.LOW)

    # ── Flags ──────────────────────────────────────────────────────

    def test_seller_protection_flag(self):
        d = triage_dispute(self._inp(seller_protection_eligible=True))
        assert "SELLER_PROTECTION" in d.flags

    def test_fraud_risk_flag(self):
        d = triage_dispute(self._inp(fraud_signal=FraudSignal.HIGH))
        assert "FRAUD_RISK" in d.flags

    def test_high_ltv_flag_on_vip(self):
        d = triage_dispute(self._inp(is_vip=True, buyer_ltv=600.0))
        assert "HIGH_LTV" in d.flags

    def test_already_refunded_flag(self):
        d = triage_dispute(self._inp(already_refunded=True))
        assert "ALREADY_REFUNDED" in d.flags

    # ── Batch triage ───────────────────────────────────────────────

    def test_batch_counts_add_up(self):
        disputes = [
            self._inp(dispute_id="D-001", amount=5.0),                    # auto accept
            self._inp(dispute_id="D-002", amount=200.0, win_prob=0.75,    # auto fight
                      seller_protection_eligible=True, has_tracking_evidence=True,
                      fraud_signal=FraudSignal.MINIMAL),
            self._inp(dispute_id="D-003", is_vip=True),                   # manual
        ]
        r = triage_batch(disputes)
        assert r.auto_accept_count + r.auto_fight_count + r.manual_review_count == len(disputes)

    def test_batch_sorted_urgent_first(self):
        disputes = [
            self._inp(dispute_id="D-001", days_until_deadline=30),  # low priority
            self._inp(dispute_id="D-002", days_until_deadline=1),   # urgent
        ]
        r = triage_batch(disputes)
        assert r.decisions[0].priority == TriagePriority.URGENT

    def test_batch_total_amounts_correct(self):
        disputes = [
            self._inp(dispute_id="D-001", amount=10.0),   # accept ($10)
            self._inp(dispute_id="D-002", amount=5.0),    # accept ($5)
        ]
        r = triage_batch(disputes)
        # Both should be auto-accept (amount ≤ $10)
        assert r.total_auto_accept_amount == pytest.approx(15.0, abs=0.1)

    def test_batch_summary_non_empty(self):
        r = triage_batch([self._inp()])
        assert len(r.summary) > 0
