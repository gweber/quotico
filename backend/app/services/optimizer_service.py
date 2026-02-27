"""
backend/app/services/optimizer_service.py

Purpose:
    Calibration engine for Dixon-Coles model parameters (rho, alpha, floor)
    using resolved matches and canonical Team Tower ids.

Dependencies:
    - app.database
    - app.services.quotico_tip_service
    - app.services.historical_service
"""

import logging
import math
import time as _time
from bisect import bisect_left
from datetime import datetime, timedelta
from typing import TypedDict

import app.database as _db
from app.services.quotico_tip_service import (
    ALPHA_TIME_DECAY,
    ALPHA_WEIGHT_FLOOR,
    DIXON_COLES_ADJ_FLOOR,
    DIXON_COLES_RHO_DEFAULT,
    H2H_WEIGHT_MAX,
    H2H_WEIGHT_SCALE,
    LAMBDA_CAP,
    LEAGUE_RHO,
    MIN_MATCHES_REQUIRED,
    N_TEAM_MATCHES,
    SCORELINE_MAX,
    _calculate_fatigue_penalty,
    _compute_h2h_lambdas,
    _dixon_coles_adjustment,
    _poisson_pmf,
    _time_weighted_average,
    blend_goals,
)
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.optimizer")

# ---------------------------------------------------------------------------
# Grid search configuration
# ---------------------------------------------------------------------------

# Full exploration grid (monthly) — 770 combinations
RHO_GRID_FULL = [round(-0.20 + i * 0.02, 2) for i in range(11)]
ALPHA_GRID_FULL = [round(0.002 + i * 0.001, 3) for i in range(14)]
FLOOR_GRID_FULL = [0.01, 0.03, 0.05, 0.08, 0.10]

# Refinement grid (weekly) — narrow ± around current
REFINE_RHO_STEP = 0.02
REFINE_RHO_HALF = 2          # ± 2 steps
REFINE_ALPHA_STEP = 0.001
REFINE_ALPHA_HALF = 2
REFINE_FLOOR_STEP = 0.02
REFINE_FLOOR_HALF = 1

# Calibration thresholds
MIN_CALIBRATION_MATCHES = 50
CALIBRATION_WINDOW_DAYS = 365
BRIER_DEGRADATION_PCT = 10
MIN_IMPROVEMENT_PCT = 2
BRIER_HISTORY_MAX = 52
EVAL_WINDOW_DAYS = 90

# L2 regularization weights
REG_WEIGHT_RHO = 0.5
REG_WEIGHT_ALPHA = 10.0
REG_WEIGHT_FLOOR = 2.0

async def _get_calibrated_league_ids() -> list[int]:
    """Return active league ids for calibration/evaluation."""
    docs = await _db.db.league_registry_v3.find(
        {"is_active": True, "features.tipping": True},
        {"league_id": 1},
    ).to_list(length=500)
    return [doc["league_id"] for doc in docs if isinstance(doc.get("league_id"), int)]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class MatchCalibrationData(TypedDict):
    actual_result: str
    odds_h2h: dict[str, float]
    home_scored: list[float]
    home_conceded: list[float]
    home_dates: list[datetime]
    away_scored: list[float]
    away_conceded: list[float]
    away_dates: list[datetime]
    league_avg_home: float
    league_avg_away: float
    h2h_lambdas: dict | None
    home_rest: int
    away_rest: int
    match_date: datetime


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def _fetch_calibration_matches(
    league_id: int, window_days: int = CALIBRATION_WINDOW_DAYS,
    before_date: datetime | None = None,
) -> list[dict]:
    """Fetch resolved matches with odds + scores for calibration.

    Uses exact league_id (not related_keys) so each league is calibrated
    on its own population — different leagues have different dynamics.

    When *before_date* is set, only matches played before that date are
    considered (point-in-time calibration for the time machine).
    """
    reference = before_date or utcnow()
    cutoff = reference - timedelta(days=window_days)

    matches = await _db.db.matches_v3.find(
        {
            "league_id": league_id,
            "status": "FINISHED",
            "start_at": {"$gte": cutoff, "$lt": reference},
            "result.home_score": {"$exists": True},
            "result.away_score": {"$exists": True},
            "odds_meta.markets.h2h.current.1": {"$gt": 0},
            "odds_meta.markets.h2h.current.X": {"$gt": 0},
            "odds_meta.markets.h2h.current.2": {"$gt": 0},
        },
        {
            "result": 1, "odds_meta.markets.h2h.current": 1, "start_at": 1,
            "home_team_id": 1, "away_team_id": 1, "league_id": 1,
        },
    ).sort("start_at", 1).to_list(length=500)

    return matches


def _determine_actual_result(match: dict) -> str | None:
    """Determine 1/X/2 from match result scores."""
    result = match.get("result", {})
    hs = result.get("home_score")
    as_ = result.get("away_score")
    if hs is None or as_ is None:
        return None
    if hs > as_:
        return "1"
    elif hs < as_:
        return "2"
    return "X"


async def _prefetch_match_data(
    matches: list[dict], league_id: int,
    before_date: datetime | None = None,
) -> list[MatchCalibrationData]:
    """Fetch-once, slice-per-match strategy for temporal-leak-free calibration data.

    1. Collect unique teams → bulk-fetch all their matches (one query per team/venue)
    2. For each calibration match: bisect the pre-fetched arrays at match_date
       to get only prior entries (temporal leakage boundary)
    3. League averages computed once (stable across calibration window)

    When *before_date* is set, team histories and league averages are capped
    at that date (point-in-time calibration).
    """
    # --- 1. Collect unique team ids ---
    team_ids: set = set()
    for m in matches:
        hk = m.get("home_team_id")
        ak = m.get("away_team_id")
        if not hk or not ak:
            # Greenfield rule: skip legacy matches missing canonical ids.
            continue
        if hk:
            team_ids.add(hk)
        if ak:
            team_ids.add(ak)

    # --- 2. Bulk-fetch team match histories ---
    # Per-team: home matches (goals scored at home), away matches (goals scored away),
    # and all matches for rest-day lookup.
    # Each sorted chronologically for bisect slicing.
    team_home: dict[str, list[dict]] = {}
    team_away: dict[str, list[dict]] = {}
    team_all: dict[str, list[dict]] = {}  # For rest-day lookup

    projection = {"result.home_score": 1, "result.away_score": 1,
                  "result.home_xg": 1, "result.away_xg": 1,
                  "start_at": 1, "home_team_id": 1, "away_team_id": 1}

    # When before_date is set, cap team history to avoid loading future data
    date_cap: dict = {}
    if before_date:
        date_cap = {"start_at": {"$lt": before_date}}

    total_teams = len(team_ids)
    for ti, tk in enumerate(team_ids, 1):
        # Home matches
        home_docs = await _db.db.matches_v3.find(
            {"home_team_id": tk, "league_id": league_id, "status": "FINISHED",
             **date_cap},
            projection,
        ).sort("start_at", 1).to_list(length=200)
        team_home[tk] = home_docs

        # Away matches
        away_docs = await _db.db.matches_v3.find(
            {"away_team_id": tk, "league_id": league_id, "status": "FINISHED",
             **date_cap},
            projection,
        ).sort("start_at", 1).to_list(length=200)
        team_away[tk] = away_docs

        # All matches for rest days (cross-competition but within soccer)
        all_docs = await _db.db.matches_v3.find(
            {"$or": [{"home_team_id": tk}, {"away_team_id": tk}],
             "league_id": league_id, "status": "FINISHED",
             **date_cap},
            {"start_at": 1},
        ).sort("start_at", 1).to_list(length=500)
        team_all[tk] = all_docs

        if ti % 10 == 0 or ti == total_teams:
            logger.info("  Prefetch: %d/%d teams fetched", ti, total_teams)

    # --- 3. League averages (exact league_id — each league has different scoring patterns) ---
    reference = before_date or utcnow()
    cutoff = reference - timedelta(days=CALIBRATION_WINDOW_DAYS)
    avg_pipeline = [
        {"$match": {"league_id": league_id, "status": "FINISHED",
                     "start_at": {"$gte": cutoff, "$lt": reference}}},
        {"$sort": {"start_at": -1}},
        {"$limit": 1000},
        {"$group": {
            "_id": None,
            "total_home": {"$sum": "$result.home_score"},
            "total_away": {"$sum": "$result.away_score"},
            "count": {"$sum": 1},
        }},
    ]
    avg_results = await _db.db.matches_v3.aggregate(avg_pipeline).to_list(length=1)
    if not avg_results or avg_results[0]["count"] < 50:
        return []

    avg_home = avg_results[0]["total_home"] / avg_results[0]["count"]
    avg_away = avg_results[0]["total_away"] / avg_results[0]["count"]
    if avg_home <= 0 or avg_away <= 0:
        return []

    # --- Helper: extract dates from a list of match docs for bisect ---
    def _extract_dates(docs: list[dict]) -> list[datetime]:
        return [ensure_utc(d["start_at"]) for d in docs]

    # --- 4. For each calibration match: slice at match_date ---
    result: list[MatchCalibrationData] = []

    for m in matches:
        hk = m.get("home_team_id")
        ak = m.get("away_team_id")
        if not hk or not ak:
            continue

        actual = _determine_actual_result(m)
        if not actual:
            continue

        odds_h2h = (((m.get("odds_meta") or {}).get("markets") or {}).get("h2h") or {}).get("current", {})
        if not all(odds_h2h.get(k, 0) > 0 for k in ("1", "X", "2")):
            continue

        match_dt = ensure_utc(m["start_at"])

        # Slice home matches for home team (before match_date)
        h_home_docs = team_home.get(hk, [])
        h_home_dates = _extract_dates(h_home_docs)
        cut_idx = bisect_left(h_home_dates, match_dt)
        h_home_slice = h_home_docs[max(0, cut_idx - N_TEAM_MATCHES):cut_idx]

        # Slice away matches for away team (before match_date)
        a_away_docs = team_away.get(ak, [])
        a_away_dates = _extract_dates(a_away_docs)
        cut_idx = bisect_left(a_away_dates, match_dt)
        a_away_slice = a_away_docs[max(0, cut_idx - N_TEAM_MATCHES):cut_idx]

        if len(h_home_slice) < MIN_MATCHES_REQUIRED or len(a_away_slice) < MIN_MATCHES_REQUIRED:
            continue

        # Extract goals + dates for time-weighted averaging (xG-blended when available)
        home_scored = [blend_goals(d["result"]["home_score"], d["result"].get("home_xg")) for d in h_home_slice]
        home_conceded = [blend_goals(d["result"]["away_score"], d["result"].get("away_xg")) for d in h_home_slice]
        home_dates = [d["start_at"] for d in h_home_slice]

        away_scored = [blend_goals(d["result"]["away_score"], d["result"].get("away_xg")) for d in a_away_slice]
        away_conceded = [blend_goals(d["result"]["home_score"], d["result"].get("home_xg")) for d in a_away_slice]
        away_dates = [d["start_at"] for d in a_away_slice]

        # Rest days: find last final match before match_date (cross-competition)
        def _rest_from_all(tk_: str) -> int:
            all_docs_ = team_all.get(tk_, [])
            all_dates_ = _extract_dates(all_docs_)
            idx = bisect_left(all_dates_, match_dt)
            if idx > 0:
                delta = (match_dt - all_dates_[idx - 1]).days
                return min(max(delta, 0), 14)  # Cap at 14d — longer gaps are data issues
            return 7  # REST_DEFAULT_DAYS

        home_rest = _rest_from_all(hk)
        away_rest = _rest_from_all(ak)

        # H2H lambdas (filter home team's matches for away team id, and vice versa)
        h2h_matches_raw: list[dict] = []
        for doc in h_home_docs[:cut_idx]:
            if doc.get("away_team_id") == ak and ensure_utc(doc["start_at"]) < match_dt:
                h2h_matches_raw.append(doc)
        for doc in team_home.get(ak, []):
            if doc.get("away_team_id") == hk and ensure_utc(doc["start_at"]) < match_dt:
                h2h_matches_raw.append(doc)
        # Sort by date descending (most recent first) for _compute_h2h_lambdas
        h2h_matches_raw.sort(key=lambda d: d["start_at"], reverse=True)
        h2h_lambdas = _compute_h2h_lambdas(h2h_matches_raw, hk, ak) if h2h_matches_raw else None

        result.append(MatchCalibrationData(
            actual_result=actual,
            odds_h2h=odds_h2h,
            home_scored=home_scored,
            home_conceded=home_conceded,
            home_dates=home_dates,
            away_scored=away_scored,
            away_conceded=away_conceded,
            away_dates=away_dates,
            league_avg_home=avg_home,
            league_avg_away=avg_away,
            h2h_lambdas=h2h_lambdas,
            home_rest=home_rest,
            away_rest=away_rest,
            match_date=m["start_at"],
        ))

    logger.info("Prefetched %d calibration data points for %s (%d teams)",
                len(result), league_id, len(team_ids))
    return result


# ---------------------------------------------------------------------------
# Pure-math probability computation (no DB calls)
# ---------------------------------------------------------------------------

def _compute_probs_for_params(
    md: MatchCalibrationData, rho: float, alpha: float, floor: float,
) -> dict[str, float] | None:
    """Compute 1/X/2 probabilities for one match using candidate parameters.

    Pure computation — no DB calls. Uses the same Dixon-Coles pipeline as
    compute_poisson_probabilities but with explicit parameter inputs.
    """
    avg_h = md["league_avg_home"]
    avg_a = md["league_avg_away"]
    ref_date = md["start_at"]

    # Time-weighted attack/defense strengths
    home_attack = _time_weighted_average(
        md["home_scored"], md["home_dates"], ref_date, alpha=alpha, floor=floor) / avg_h
    home_defense = _time_weighted_average(
        md["home_conceded"], md["home_dates"], ref_date, alpha=alpha, floor=floor) / avg_a

    away_attack = _time_weighted_average(
        md["away_scored"], md["away_dates"], ref_date, alpha=alpha, floor=floor) / avg_a
    away_defense = _time_weighted_average(
        md["away_conceded"], md["away_dates"], ref_date, alpha=alpha, floor=floor) / avg_h

    # Base lambdas
    lambda_h = home_attack * away_defense * avg_h
    lambda_a = away_attack * home_defense * avg_a

    # H2H blend
    if md["h2h_lambdas"]:
        h2h = md["h2h_lambdas"]
        h2h_w = min(h2h["count"] / H2H_WEIGHT_SCALE, H2H_WEIGHT_MAX)
        gw = 1.0 - h2h_w
        lambda_h = gw * lambda_h + h2h_w * h2h["lambda_home"]
        lambda_a = gw * lambda_a + h2h_w * h2h["lambda_away"]

    # Rest penalty
    home_mod, away_mod = _calculate_fatigue_penalty(md["home_rest"], md["away_rest"])
    lambda_h *= home_mod
    lambda_a *= away_mod

    # Lambda cap
    lambda_h = min(lambda_h, LAMBDA_CAP)
    lambda_a = min(lambda_a, LAMBDA_CAP)

    # Dixon-Coles corrected matrix
    matrix = [[0.0] * SCORELINE_MAX for _ in range(SCORELINE_MAX)]
    for i in range(SCORELINE_MAX):
        for j in range(SCORELINE_MAX):
            prob = _poisson_pmf(i, lambda_h) * _poisson_pmf(j, lambda_a)
            adj = _dixon_coles_adjustment(i, j, lambda_h, lambda_a, rho)
            matrix[i][j] = max(prob * adj, 0.0)

    # Renormalize
    total = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX))
    if total <= 0:
        return None
    for i in range(SCORELINE_MAX):
        for j in range(SCORELINE_MAX):
            matrix[i][j] /= total

    prob_home = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX) if i > j)
    prob_draw = sum(matrix[i][i] for i in range(SCORELINE_MAX))
    prob_away = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX) if i < j)

    return {"1": prob_home, "X": prob_draw, "2": prob_away}


# ---------------------------------------------------------------------------
# Evaluation (Brier Score + Calibration Error + Regularization)
# ---------------------------------------------------------------------------

def _get_defaults(league_id: int) -> dict:
    """Return hardcoded default parameters for a league (regularization anchor)."""
    return {
        "rho": LEAGUE_RHO.get(league_id, DIXON_COLES_RHO_DEFAULT),
        "alpha": ALPHA_TIME_DECAY,
        "floor": ALPHA_WEIGHT_FLOOR,
    }


def _evaluate_params(
    all_data: list[MatchCalibrationData],
    rho: float, alpha: float, floor: float,
    defaults: dict,
) -> dict:
    """Evaluate a parameter triple on all calibration data.

    Returns pure_brier, regularized_brier, log_likelihood,
    per-outcome calibration_error, and evaluated count.
    """
    brier_sum = 0.0
    ll_sum = 0.0
    p_sums = {"1": 0.0, "X": 0.0, "2": 0.0}
    o_sums = {"1": 0.0, "X": 0.0, "2": 0.0}
    n = 0

    for md in all_data:
        probs = _compute_probs_for_params(md, rho, alpha, floor)
        if not probs:
            continue

        actual = md["actual_result"]

        # Multi-class Brier score: Σ_j (p_j - o_j)²
        for outcome in ("1", "X", "2"):
            o = 1.0 if outcome == actual else 0.0
            brier_sum += (probs[outcome] - o) ** 2
            p_sums[outcome] += probs[outcome]
            o_sums[outcome] += o

        # Log-likelihood
        ll_sum += math.log(max(probs[actual], 1e-10))
        n += 1

    if n == 0:
        return {
            "pure_brier": 1.0, "regularized_brier": 1.0,
            "log_likelihood": -10.0,
            "calibration_error": {"home": 0.0, "draw": 0.0, "away": 0.0},
            "evaluated": 0,
        }

    pure_brier = brier_sum / n
    log_likelihood = ll_sum / n

    # Per-outcome calibration error: CE_j = mean(p_j) - mean(o_j)
    cal_error = {
        "home": round((p_sums["1"] - o_sums["1"]) / n, 4),
        "draw": round((p_sums["X"] - o_sums["X"]) / n, 4),
        "away": round((p_sums["2"] - o_sums["2"]) / n, 4),
    }

    # L2 regularization: λ_reg = 1/√N
    lambda_reg = 1.0 / math.sqrt(n)
    penalty = (
        REG_WEIGHT_RHO * (rho - defaults["rho"]) ** 2
        + REG_WEIGHT_ALPHA * (alpha - defaults["alpha"]) ** 2
        + REG_WEIGHT_FLOOR * (floor - defaults["floor"]) ** 2
    )
    regularized_brier = pure_brier + lambda_reg * penalty

    return {
        "pure_brier": round(pure_brier, 6),
        "regularized_brier": round(regularized_brier, 6),
        "log_likelihood": round(log_likelihood, 6),
        "calibration_error": cal_error,
        "evaluated": n,
    }


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------

def _build_grid(
    mode: str, current: dict,
) -> list[tuple[float, float, float]]:
    """Build parameter grid for calibration.

    Exploration: full grid (770 combos).
    Refinement: narrow ± around current values (~75 combos).
    """
    if mode == "exploration":
        return [
            (r, a, f)
            for r in RHO_GRID_FULL
            for a in ALPHA_GRID_FULL
            for f in FLOOR_GRID_FULL
        ]

    # Refinement: narrow grid around current values
    c_rho = current.get("rho", DIXON_COLES_RHO_DEFAULT)
    c_alpha = current.get("alpha", ALPHA_TIME_DECAY)
    c_floor = current.get("floor", ALPHA_WEIGHT_FLOOR)

    def _range(center: float, step: float, half: int, lo: float, hi: float) -> list[float]:
        vals = [round(center + i * step, 4) for i in range(-half, half + 1)]
        return [v for v in vals if lo <= v <= hi]

    rhos = _range(c_rho, REFINE_RHO_STEP, REFINE_RHO_HALF, -0.20, 0.00)
    alphas = _range(c_alpha, REFINE_ALPHA_STEP, REFINE_ALPHA_HALF, 0.002, 0.015)
    floors = _range(c_floor, REFINE_FLOOR_STEP, REFINE_FLOOR_HALF, 0.01, 0.15)

    return [(r, a, f) for r in rhos for a in alphas for f in floors]


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

async def calibrate_league(
    league_id: int, mode: str = "refinement",
    before_date: datetime | None = None,
) -> dict:
    """Run grid search calibration for a single league.

    Returns summary dict with best params, scores, and landscape analysis.

    When *before_date* is set the calibration is retroactive (point-in-time)
    and the live ``engine_config`` document is **not** modified — the caller
    is responsible for storing results (e.g. in ``engine_config_history``).
    """
    # Load current config (or defaults)
    existing = await _db.db.engine_config.find_one({"_id": league_id})
    if not existing:
        # Auto-promote to exploration on first run
        mode = "exploration"
        logger.info("No engine_config for %s — auto-promoting to exploration", league_id)

    current = {
        "rho": existing["rho"] if existing else LEAGUE_RHO.get(league_id, DIXON_COLES_RHO_DEFAULT),
        "alpha": existing["alpha_time_decay"] if existing else ALPHA_TIME_DECAY,
        "floor": existing["alpha_weight_floor"] if existing else ALPHA_WEIGHT_FLOOR,
    }
    defaults = _get_defaults(league_id)

    # Fetch and prefetch calibration data
    matches = await _fetch_calibration_matches(league_id, before_date=before_date)
    if len(matches) < MIN_CALIBRATION_MATCHES:
        logger.warning("Skipping %s: only %d matches (need %d)",
                       league_id, len(matches), MIN_CALIBRATION_MATCHES)
        return {"league_id": league_id, "status": "skipped", "matches": len(matches)}

    all_data = await _prefetch_match_data(matches, league_id, before_date=before_date)
    if len(all_data) < MIN_CALIBRATION_MATCHES:
        logger.warning("Skipping %s: only %d usable data points after prefetch",
                       league_id, len(all_data))
        return {"league_id": league_id, "status": "skipped", "data_points": len(all_data)}

    # Build grid
    grid = _build_grid(mode, current)
    logger.info("Calibrating %s (%s mode): %d grid points, %d matches",
                league_id, mode, len(grid), len(all_data))

    # Grid search
    best_result: dict | None = None
    best_params: tuple[float, float, float] | None = None
    best_rbs = float("inf")
    worst_rbs = float("-inf")
    grid_size = len(grid)
    grid_t0 = _time.monotonic()

    for gi, (rho, alpha, floor) in enumerate(grid, 1):
        result = _evaluate_params(all_data, rho, alpha, floor, defaults)
        rbs = result["regularized_brier"]

        if rbs < best_rbs:
            best_rbs = rbs
            best_result = result
            best_params = (rho, alpha, floor)
        if rbs > worst_rbs:
            worst_rbs = rbs

        if gi % 100 == 0 or gi == grid_size:
            elapsed = _time.monotonic() - grid_t0
            logger.info("  Grid search: %d/%d combos evaluated (%.1fs)", gi, grid_size, elapsed)

    if not best_params or not best_result:
        return {"league_id": league_id, "status": "error", "reason": "no valid grid point"}

    # Minimum improvement check
    old_rbs = existing["regularized_brier"] if existing and "regularized_brier" in existing else None
    if old_rbs is not None:
        improvement_pct = (old_rbs - best_rbs) / old_rbs * 100
        if improvement_pct < MIN_IMPROVEMENT_PCT:
            logger.info(
                "Keeping current params for %s (improvement %.2f%% < %d%% threshold)",
                league_id, improvement_pct, MIN_IMPROVEMENT_PCT,
            )
            return {
                "league_id": league_id, "status": "kept",
                "improvement_pct": round(improvement_pct, 2),
                "current_rbs": old_rbs, "candidate_rbs": best_rbs,
            }

    rho_best, alpha_best, floor_best = best_params

    # Extract league baselines from prefetched data
    baselines = None
    if all_data:
        baselines = {
            "avg_home": round(all_data[0]["league_avg_home"], 3),
            "avg_away": round(all_data[0]["league_avg_away"], 3),
        }

    # Upsert to engine_config (only for live calibration, not retroactive)
    if before_date is None:
        now = utcnow()
        history_entry = {
            "date": now,
            "pure_brier": best_result["pure_brier"],
            "regularized_brier": best_result["regularized_brier"],
            "calibration_error": best_result["calibration_error"],
            "matches": best_result["evaluated"],
            "rho": rho_best,
            "alpha": alpha_best,
            "floor": floor_best,
            "mode": mode,
        }

        await _db.db.engine_config.update_one(
            {"_id": league_id},
            {
                "$set": {
                    "rho": rho_best,
                    "alpha_time_decay": alpha_best,
                    "alpha_weight_floor": floor_best,
                    "last_calibrated": now,
                    "calibration_mode": mode,
                    "calibration_matches": best_result["evaluated"],
                    "pure_brier": best_result["pure_brier"],
                    "regularized_brier": best_result["regularized_brier"],
                    "calibration_error": best_result["calibration_error"],
                    "log_likelihood": best_result["log_likelihood"],
                    "grid_landscape": {
                        "best_rbs": round(best_rbs, 6),
                        "worst_rbs": round(worst_rbs, 6),
                        "range": round(worst_rbs - best_rbs, 6),
                    },
                },
                "$push": {
                    "brier_history": {
                        "$each": [history_entry],
                        "$slice": -BRIER_HISTORY_MAX,
                    },
                },
            },
            upsert=True,
        )

        # Append snapshot to engine_config_history for future backfills
        await _db.db.engine_config_history.update_one(
            {"league_id": league_id, "snapshot_date": now},
            {"$set": {
                "league_id": league_id,
                "snapshot_date": now,
                "params": {
                    "rho": rho_best,
                    "alpha": alpha_best,
                    "floor": floor_best,
                },
                "scores": {
                    "pure_brier": best_result["pure_brier"],
                    "regularized_brier": best_result["regularized_brier"],
                    "calibration_error": best_result["calibration_error"],
                },
                "baselines": baselines,
                "reliability": None,
                "meta": {
                    "source": "live_worker",
                    "matches_analyzed": best_result["evaluated"],
                    "mode": mode,
                    "is_retroactive": False,
                    "landscape_range": round(worst_rbs - best_rbs, 6),
                },
            }},
            upsert=True,
        )

    logger.info(
        "Calibrated %s (%s%s): ρ=%.2f, α=%.3f, floor=%.2f, "
        "BS=%.4f, RBS=%.4f, CE=%s, landscape=[%.4f..%.4f] (N=%d)",
        league_id, mode, f", as-of {before_date:%Y-%m-%d}" if before_date else "",
        rho_best, alpha_best, floor_best,
        best_result["pure_brier"], best_result["regularized_brier"],
        best_result["calibration_error"],
        best_rbs, worst_rbs, best_result["evaluated"],
    )

    return {
        "league_id": league_id, "status": "calibrated", "mode": mode,
        "rho": rho_best, "alpha": alpha_best, "floor": floor_best,
        "pure_brier": best_result["pure_brier"],
        "regularized_brier": best_result["regularized_brier"],
        "calibration_error": best_result["calibration_error"],
        "evaluated": best_result["evaluated"],
        "landscape_range": round(worst_rbs - best_rbs, 6),
        "baselines": baselines,
    }


# ---------------------------------------------------------------------------
# Daily evaluation
# ---------------------------------------------------------------------------

async def evaluate_engine_performance() -> dict:
    """Evaluate current model performance using the same match population as calibration.

    Uses matches collection (not quotico_tips) for population consistency.
    Compares rolling Brier against average of last 4 brier_history entries.
    """
    results: dict = {}

    for league_id in await _get_calibrated_league_ids():
        config = await _db.db.engine_config.find_one({"_id": league_id})
        if not config:
            continue

        # Fetch recent resolved matches (same criteria as calibration — exact league_id)
        cutoff = utcnow() - timedelta(days=EVAL_WINDOW_DAYS)

        eval_matches = await _db.db.matches_v3.find(
            {
                "league_id": league_id,
                "status": "FINISHED",
                "start_at": {"$gte": cutoff},
                "result.home_score": {"$exists": True},
                "result.away_score": {"$exists": True},
                "odds_meta.markets.h2h.current.1": {"$gt": 0},
                "odds_meta.markets.h2h.current.X": {"$gt": 0},
                "odds_meta.markets.h2h.current.2": {"$gt": 0},
            },
        ).sort("start_at", 1).to_list(length=200)

        if len(eval_matches) < 20:
            continue

        # Prefetch and evaluate with current calibrated params
        eval_data = await _prefetch_match_data(eval_matches, league_id)
        if len(eval_data) < 20:
            continue

        rho = config["rho"]
        alpha = config["alpha_time_decay"]
        floor = config["alpha_weight_floor"]
        defaults = _get_defaults(league_id)

        eval_result = _evaluate_params(eval_data, rho, alpha, floor, defaults)

        # Trend comparison: average of last 4 history entries
        history = config.get("brier_history", [])
        if len(history) >= 2:
            recent_entries = history[-4:]
            history_avg = sum(e["pure_brier"] for e in recent_entries) / len(recent_entries)
        else:
            history_avg = config.get("pure_brier", eval_result["pure_brier"])

        current_brier = eval_result["pure_brier"]
        degradation_pct = ((current_brier - history_avg) / history_avg * 100) if history_avg > 0 else 0
        needs_recal = degradation_pct > BRIER_DEGRADATION_PCT

        if needs_recal:
            logger.warning(
                "%s: Brier degraded %.1f%% (rolling %.4f vs history avg %.4f) — flagged for recalibration",
                league_id, degradation_pct, current_brier, history_avg,
            )

        results[league_id] = {
            "brier": current_brier,
            "calibration_error": eval_result["calibration_error"],
            "matches": eval_result["evaluated"],
            "history_avg_brier": round(history_avg, 6),
            "degradation_pct": round(degradation_pct, 2),
            "needs_recalibration": needs_recal,
        }

    return results


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------

async def run_calibration(force_mode: str | None = None) -> dict:
    """Main entry point for the calibration worker.

    force_mode="exploration": monthly full grid for all leagues.
    force_mode="refinement": weekly narrow grid for all leagues.
    force_mode=None (daily): evaluate, only recalibrate flagged leagues.
    """
    if force_mode in ("exploration", "refinement"):
        results = {}
        for league_id in await _get_calibrated_league_ids():
            try:
                results[league_id] = await calibrate_league(league_id, mode=force_mode)
            except Exception:
                logger.exception("Calibration failed for %s", league_id)
                results[league_id] = {"status": "error"}
        return {"mode": force_mode, "leagues": results}

    # Daily: evaluate, then recalibrate flagged leagues
    eval_results = await evaluate_engine_performance()
    recalibrated = {}

    for league_id, eval_data in eval_results.items():
        if eval_data.get("needs_recalibration"):
            try:
                recalibrated[league_id] = await calibrate_league(league_id, mode="refinement")
            except Exception:
                logger.exception("Recalibration failed for %s", league_id)
                recalibrated[league_id] = {"status": "error"}

    return {
        "mode": "daily_eval",
        "evaluation": eval_results,
        "recalibrated": recalibrated,
    }
