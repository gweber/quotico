"""Meta-learning confidence calibration — reliability analysis per league.

Analyzes historical tip performance vs. confidence scores to detect
systematic overconfidence/underconfidence.  Produces per-league calibration
parameters that adjust the raw confidence output of _calculate_confidence().

Parameters produced:
- multiplier:        global scaling factor (e.g. 0.60 → raw confidence is 60% reliable)
- cap:               hard ceiling (e.g. 0.72 → no tip exceeds 72% confidence)
- regression_factor: pull toward league-average win rate (0.0 = none, 0.40 = heavy)
- avg_win_rate:      baseline win rate used as regression target

Runs weekly (Sunday 23:00) via calibration_worker.run_reliability_check().
"""

import logging
import math
from datetime import datetime, timedelta

import app.database as _db
from app.services.optimizer_service import CALIBRATED_LEAGUES
from app.utils import utcnow

logger = logging.getLogger("quotico.reliability")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum resolved tips to produce reliability params for a league
MIN_TIPS_FOR_RELIABILITY = 100

# Confidence band boundaries (lower-inclusive, upper-exclusive except last)
CONFIDENCE_BANDS: list[tuple[float, float]] = [
    (0.10, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.95),
]

# Minimum tips in a band to consider it statistically meaningful
MIN_BAND_SIZE = 10

# Lookback window
RELIABILITY_WINDOW_DAYS = 365

# Bayesian smoothing: pull toward 33.3% (3-way baseline)
PRIOR_WIN_RATE = 0.333
PRIOR_STRENGTH = 20  # equivalent phantom tips at prior rate

# Multiplier bounds — conservative: prefer underconfidence over overconfidence
MULTIPLIER_FLOOR = 0.30
MULTIPLIER_CAP = 1.10  # slightly below 1.20 for betting conservatism

# Cap bounds
CAP_FLOOR = 0.50
CAP_CEILING = 0.95
CAP_BUFFER = 0.05  # added above highest observed band WR

# Regression bounds
REGRESSION_MAX = 0.40


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

async def analyze_engine_reliability(
    sport_key: str,
    before_date: datetime | None = None,
) -> dict | None:
    """Analyze historical tip performance and compute reliability parameters.

    Returns a dict suitable for storing as ``engine_config.reliability``,
    or ``None`` if insufficient data.

    When *before_date* is set, only tips generated before that date are
    considered (point-in-time reliability for the time machine).

    Algorithm
    ---------
    1. Fetch last RELIABILITY_WINDOW_DAYS of resolved tips for *sport_key*.
    2. Bucket by confidence bands.
    3. Per band: Bayesian-smoothed win rate, reliability factor.
    4. Weighted-average reliability factor → multiplier.
    5. Monotonicity check → regression_factor (penalises inversions where
       higher confidence bands have *lower* win rates — the "arrogance"
       detector).
    6. Cap derived from highest observed band win rate + buffer.
    """
    reference = before_date or utcnow()
    cutoff = reference - timedelta(days=RELIABILITY_WINDOW_DAYS)

    tips = await _db.db.quotico_tips.find(
        {
            "sport_key": sport_key,
            "status": "resolved",
            "was_correct": {"$ne": None},
            "confidence": {"$gt": 0},
            "generated_at": {"$gte": cutoff, "$lt": reference},
        },
        {"confidence": 1, "was_correct": 1},
    ).to_list(length=2000)

    if len(tips) < MIN_TIPS_FOR_RELIABILITY:
        logger.info(
            "Skipping reliability for %s: only %d tips (need %d)",
            sport_key, len(tips), MIN_TIPS_FOR_RELIABILITY,
        )
        return None

    # Overall stats
    total_correct = sum(1 for t in tips if t["was_correct"])
    avg_win_rate = total_correct / len(tips)

    # ── Band analysis ──────────────────────────────────────────────────
    bands: list[dict] = []
    weighted_rf_sum = 0.0
    weighted_rf_weight = 0.0
    max_observed_wr = 0.0

    for lo, hi in CONFIDENCE_BANDS:
        band_tips = [t for t in tips if lo <= t["confidence"] < hi]
        if len(band_tips) < MIN_BAND_SIZE:
            continue

        band_correct = sum(1 for t in band_tips if t["was_correct"])
        avg_conf = sum(t["confidence"] for t in band_tips) / len(band_tips)

        # Bayesian-smoothed win rate
        smoothed_wr = (
            (band_correct + PRIOR_STRENGTH * PRIOR_WIN_RATE)
            / (len(band_tips) + PRIOR_STRENGTH)
        )

        reliability_factor = smoothed_wr / avg_conf if avg_conf > 0 else 1.0

        bands.append({
            "band": f"{lo:.2f}-{hi:.2f}",
            "count": len(band_tips),
            "avg_confidence": round(avg_conf, 4),
            "observed_win_rate": round(smoothed_wr, 4),
            "reliability_factor": round(reliability_factor, 4),
        })

        # Weight by sample size (log-scaled to avoid large-band dominance)
        weight = math.log1p(len(band_tips))
        weighted_rf_sum += reliability_factor * weight
        weighted_rf_weight += weight

        if smoothed_wr > max_observed_wr:
            max_observed_wr = smoothed_wr

    if not bands or weighted_rf_weight == 0:
        return None

    # ── Multiplier ─────────────────────────────────────────────────────
    multiplier = weighted_rf_sum / weighted_rf_weight
    multiplier = max(MULTIPLIER_FLOOR, min(multiplier, MULTIPLIER_CAP))

    # ── Cap ────────────────────────────────────────────────────────────
    cap = min(max_observed_wr + CAP_BUFFER, CAP_CEILING)
    cap = max(cap, CAP_FLOOR)

    # ── Regression factor (monotonicity check) ─────────────────────────
    # If higher-confidence bands do NOT produce higher win rates the model
    # is "arrogant" at the top end.  Each inversion increases regression.
    # An *inverted* band where WR drops by >5pp from the previous band
    # receives double penalty weight — this directly addresses the Serie A
    # pattern (80-90% confidence, 32% WR while 70-80% hits 45%).
    if len(bands) >= 3:
        wrs = [b["observed_win_rate"] for b in bands]
        penalty = 0.0
        for i in range(len(wrs) - 1):
            if wrs[i] >= wrs[i + 1]:
                drop = wrs[i] - wrs[i + 1]
                # Severe drop (>5pp) counts double
                penalty += 2.0 if drop > 0.05 else 1.0
        max_possible = len(wrs) - 1
        inversion_rate = penalty / (max_possible * 2)  # normalise to [0, 1]
        regression_factor = round(min(inversion_rate * 0.80, REGRESSION_MAX), 3)
    else:
        regression_factor = 0.0

    now = utcnow()
    result = {
        "multiplier": round(multiplier, 4),
        "cap": round(cap, 4),
        "regression_factor": regression_factor,
        "avg_win_rate": round(avg_win_rate, 4),
        "total_tips": len(tips),
        "bands": bands,
        "last_analyzed": now,
    }

    logger.info(
        "Reliability for %s: mult=%.3f cap=%.3f reg=%.3f "
        "avg_WR=%.3f N=%d bands=%d",
        sport_key, multiplier, cap, regression_factor,
        avg_win_rate, len(tips), len(bands),
    )
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def update_reliability(sport_key: str) -> dict | None:
    """Analyze and persist reliability params to engine_config."""
    result = await analyze_engine_reliability(sport_key)
    if not result:
        return None

    await _db.db.engine_config.update_one(
        {"_id": sport_key},
        {"$set": {
            "reliability": result,
            "reliability_updated_at": utcnow(),
        }},
        upsert=True,
    )
    return result


async def run_reliability_analysis() -> dict:
    """Entry point: analyze all calibrated leagues.

    Called by calibration_worker.run_reliability_check().
    """
    results: dict[str, dict] = {}
    for sport_key in CALIBRATED_LEAGUES:
        try:
            r = await update_reliability(sport_key)
            results[sport_key] = {
                "status": "analyzed" if r else "skipped",
                **(r or {}),
            }
        except Exception:
            logger.exception("Reliability analysis failed for %s", sport_key)
            results[sport_key] = {"status": "error"}
    return results
