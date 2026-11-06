"""
Oracle Calibration Feedback Loop — the system that makes Anchor smarter over time.

The Settlement Oracle's win probability estimates start as educated guesses
calibrated from published PayPal dispute resolution statistics. Over time,
those estimates should converge to reality based on actual outcomes.

Without this module:
  - Oracle predicts 60% win probability for INR disputes with tracking
  - Actual win rate turns out to be 78%
  - Oracle keeps betting conservatively, leaving money on the table
  - Merchant fights fewer disputes than they should

With this module:
  - Every dispute resolution is recorded: dispute_id, oracle_prediction, actual_outcome
  - Calibration stats computed per claim_type × evidence_profile
  - Oracle predictions gradually corrected: if INR+tracking is 78% not 60%, say so
  - `GET /api/oracle/calibration` returns accuracy stats for dashboard
  - Merchants and developers can see: "Anchor predicted 65% wins, actual was 71%"

Data model per outcome record:
  {
    "dispute_id":        "PP-D-123",
    "merchant_id":       "merch_1",
    "claim_type":        "ITEM_NOT_RECEIVED",
    "oracle_prediction": 0.65,
    "oracle_ev":         42.50,
    "oracle_action":     "FIGHT",   // what Oracle recommended
    "actual_action":     "FIGHT",   // what Guardian actually did
    "actual_outcome":    "WON",     // WON | LOST | SETTLED | ACCEPTED
    "dispute_amount":    95.00,
    "settled_amount":    null,       // set when outcome = SETTLED
    "evidence_flags": {
      "has_tracking":           true,
      "has_delivery_confirm":   false,
      "has_communication":      true,
      "has_signature":          false
    },
    "recorded_at": "2026-11-06T12:00:00Z"
  }

Calibration is computed as:
  - Brier score: mean((prediction − outcome)²) — lower is better
  - Accuracy: fraction of disputes where Oracle's binary recommendation (fight vs accept)
              matched what a correct decision would have been
  - Win rate by claim type: actual win % vs Oracle predicted %
  - Overconfidence / underconfidence indicators
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx


@dataclass
class OutcomeRecord:
    dispute_id: str
    merchant_id: str
    claim_type: str
    oracle_prediction: float
    oracle_ev: float
    oracle_action: str         # "FIGHT" | "ACCEPT" | "NEGOTIATE"
    actual_action: str         # what Guardian actually did
    actual_outcome: str        # "WON" | "LOST" | "SETTLED" | "ACCEPTED"
    dispute_amount: float
    settled_amount: Optional[float] = None
    evidence_flags: dict = field(default_factory=dict)
    recorded_at: str = ""

    def __post_init__(self):
        if not self.recorded_at:
            self.recorded_at = datetime.now(timezone.utc).isoformat()

    def as_binary_win(self) -> Optional[int]:
        """Returns 1 if WON, 0 if LOST, None if outcome not binary."""
        if self.actual_outcome == "WON":
            return 1
        if self.actual_outcome == "LOST":
            return 0
        return None

    def prediction_error(self) -> Optional[float]:
        """Returns (prediction - actual) for calibration. None for non-binary outcomes."""
        actual = self.as_binary_win()
        if actual is None:
            return None
        return self.oracle_prediction - float(actual)

    def brier_score_contribution(self) -> Optional[float]:
        """Brier score = (prediction - actual)². Lower is better."""
        err = self.prediction_error()
        if err is None:
            return None
        return err * err


@dataclass
class CalibrationStats:
    merchant_id: str
    total_records: int
    brier_score: float          # 0.25 = random, 0.0 = perfect
    mean_prediction: float      # average Oracle prediction
    actual_win_rate: float      # actual fraction of fights won
    calibration_error: float    # mean_prediction - actual_win_rate (+ = overconfident)
    win_rate_by_claim_type: dict
    oracle_accuracy: float      # fraction where Oracle's binary call was correct
    recommendation_quality: str  # "excellent" | "good" | "fair" | "needs_improvement"
    computed_at: str


async def record_outcome(record: OutcomeRecord) -> bool:
    """Persist a dispute outcome to the feedback index."""
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return False

    doc = {
        "dispute_id": record.dispute_id,
        "merchant_id": record.merchant_id,
        "claim_type": record.claim_type,
        "oracle_prediction": record.oracle_prediction,
        "oracle_ev": record.oracle_ev,
        "oracle_action": record.oracle_action,
        "actual_action": record.actual_action,
        "actual_outcome": record.actual_outcome,
        "dispute_amount": record.dispute_amount,
        "settled_amount": record.settled_amount,
        "evidence_flags": record.evidence_flags,
        "recorded_at": record.recorded_at,
        "brier_contribution": record.brier_score_contribution(),
        "prediction_error": record.prediction_error(),
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.put(
                f"{es_url}/anchor-outcomes/_doc/{record.dispute_id}",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=doc,
            )
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"[OracleFeedback] record_outcome failed: {exc}")
        return False


async def get_calibration_stats(merchant_id: str, limit: int = 500) -> CalibrationStats:
    """
    Compute Oracle calibration statistics from recorded outcomes.

    Returns a CalibrationStats dataclass with accuracy, Brier score, and
    win-rate-by-claim-type breakdown.
    """
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    es_key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not es_url or not es_key:
        return _empty_stats(merchant_id)

    query = {
        "query": {"term": {"merchant_id": merchant_id}},
        "size": limit,
        "sort": [{"recorded_at": {"order": "desc"}}],
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(
                f"{es_url}/anchor-outcomes/_search",
                headers={"Authorization": f"ApiKey {es_key}", "Content-Type": "application/json"},
                json=query,
            )
        records_raw = r.json().get("hits", {}).get("hits", [])
    except Exception as exc:
        print(f"[OracleFeedback] calibration query failed: {exc}")
        return _empty_stats(merchant_id)

    records = [OutcomeRecord(**h["_source"]) for h in records_raw]
    return _compute_stats(merchant_id, records)


def _compute_stats(merchant_id: str, records: list[OutcomeRecord]) -> CalibrationStats:
    if not records:
        return _empty_stats(merchant_id)

    # Binary records (WON or LOST only — ignore SETTLED/ACCEPTED for calibration)
    binary = [r for r in records if r.as_binary_win() is not None]

    brier_scores = [r.brier_score_contribution() for r in binary if r.brier_score_contribution() is not None]
    brier = sum(brier_scores) / len(brier_scores) if brier_scores else 0.25

    predictions = [r.oracle_prediction for r in binary]
    actuals = [float(r.as_binary_win()) for r in binary]

    mean_prediction = sum(predictions) / len(predictions) if predictions else 0.0
    actual_win_rate = sum(actuals) / len(actuals) if actuals else 0.0
    calibration_error = mean_prediction - actual_win_rate

    # Win rate by claim type
    win_rate_by_type: dict = {}
    for r in binary:
        ct = r.claim_type
        if ct not in win_rate_by_type:
            win_rate_by_type[ct] = {"wins": 0, "total": 0, "avg_prediction": 0.0, "_sum_pred": 0.0}
        win_rate_by_type[ct]["total"] += 1
        win_rate_by_type[ct]["wins"] += r.as_binary_win()
        win_rate_by_type[ct]["_sum_pred"] += r.oracle_prediction

    for ct, stats in win_rate_by_type.items():
        total = stats["total"]
        stats["actual_win_rate"] = round(stats["wins"] / total, 3) if total > 0 else 0.0
        stats["avg_prediction"] = round(stats["_sum_pred"] / total, 3) if total > 0 else 0.0
        stats["calibration_error"] = round(stats["avg_prediction"] - stats["actual_win_rate"], 3)
        del stats["_sum_pred"]

    # Oracle accuracy: fraction of FIGHT disputes where it was correct
    # (Oracle said FIGHT → actually WON counts as correct)
    fought_records = [r for r in binary if r.oracle_action == "FIGHT"]
    if fought_records:
        correct = sum(1 for r in fought_records if r.as_binary_win() == 1)
        oracle_accuracy = correct / len(fought_records)
    else:
        oracle_accuracy = 0.0

    # Quality classification
    if brier < 0.10 and abs(calibration_error) < 0.05:
        quality = "excellent"
    elif brier < 0.15 and abs(calibration_error) < 0.10:
        quality = "good"
    elif brier < 0.20:
        quality = "fair"
    else:
        quality = "needs_improvement"

    return CalibrationStats(
        merchant_id=merchant_id,
        total_records=len(records),
        brier_score=round(brier, 4),
        mean_prediction=round(mean_prediction, 3),
        actual_win_rate=round(actual_win_rate, 3),
        calibration_error=round(calibration_error, 3),
        win_rate_by_claim_type=win_rate_by_type,
        oracle_accuracy=round(oracle_accuracy, 3),
        recommendation_quality=quality,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


def _empty_stats(merchant_id: str) -> CalibrationStats:
    return CalibrationStats(
        merchant_id=merchant_id,
        total_records=0,
        brier_score=0.25,  # random baseline
        mean_prediction=0.0,
        actual_win_rate=0.0,
        calibration_error=0.0,
        win_rate_by_claim_type={},
        oracle_accuracy=0.0,
        recommendation_quality="needs_improvement",
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
