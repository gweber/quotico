"""QuoticoTip EV Engine — 3-tier hybrid scoring model.

Combines Poisson-based true probabilities, form/momentum scoring,
sharp line-movement detection, and community consensus ("King's Choice")
to identify value bets where bookmaker odds are mispriced.
"""

import logging
import math
import time as _time
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel

import app.database as _db
from app.services.historical_service import (
    build_match_context,
    sport_keys_for,
)
from app.services.team_mapping_service import resolve_team_key
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.quotico_tip")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

N_TEAM_MATCHES = 10          # Last N home/away matches for attack/defense strength
RECENCY_DECAY = 0.9          # Exponential decay for Poisson: weight_i = 0.9^i
MOMENTUM_DECAY = 0.85        # Decay for momentum scoring
SCORELINE_MAX = 7            # Compute 0-0 through 6-6
LAMBDA_CAP = 5.0             # Cap expected goals to prevent unrealistic predictions
EDGE_THRESHOLD_PCT = 5.0     # Minimum edge to flag as a value bet
SHARP_DROP_PCT = 10.0        # Minimum % odds drop for sharp movement
LATE_MONEY_HOURS = 6         # Hours before kickoff for late-money classification
MIN_MATCHES_REQUIRED = 5     # Minimum matches for Poisson to produce a result
MIN_KINGS_TIPPED = 5         # Minimum kings who bet for King's Choice
KINGS_AGREEMENT_PCT = 0.80   # 80% agreement threshold
UNDERDOG_ODDS_THRESHOLD = 2.5

# H2H Poisson blend
H2H_WEIGHT_MAX = 0.30       # Maximum H2H influence on lambda
H2H_MIN_MATCHES = 3         # Minimum H2H meetings to activate blend
H2H_WEIGHT_SCALE = 10       # h2h_weight = min(h2h_total / scale, max)

# Odds timeline enrichment
STEAM_VELOCITY_PCT = 5.0     # % drop in a single snapshot interval = steam move
REVERSAL_THRESHOLD_PCT = 5.0 # Minimum swing size to flag a reversal

# EVD (Expected Value Differential) — "Beat the Books" metric
EVD_BOOST_THRESHOLD = 0.10   # Minimum EVD for +confidence
EVD_DAMPEN_THRESHOLD = -0.10 # EVD below this → -confidence
EVD_CONFIDENCE_BOOST = 0.05  # Boost amount
EVD_CONFIDENCE_DAMPEN = 0.03 # Dampener amount
EVD_MIN_MATCHES = 5          # Minimum matches with odds to produce a signal

# Dixon-Coles model parameters
ALPHA_TIME_DECAY = 0.005     # Time decay constant (~0.5% information loss per day)
ALPHA_WEIGHT_FLOOR = 0.05    # Minimum weight — prevents blind start after summer break
DIXON_COLES_RHO_DEFAULT = -0.08  # Fallback rho for unlisted leagues

# Per-league rho: defensive/tactical leagues need stronger (more negative) correction
LEAGUE_RHO: dict[str, float] = {
    "soccer_italy_serie_a":       -0.13,  # Tactical, many low-scoring draws
    "soccer_spain_la_liga":       -0.10,  # Moderate defensive tendency
    "soccer_epl":                 -0.07,  # Attacking, fewer 0-0s
    "soccer_germany_bundesliga":  -0.08,  # Balanced
    "soccer_germany_bundesliga2": -0.09,  # Slightly more defensive than BL1
    "soccer_uefa_champs_league":  -0.06,  # Open, attacking (knockout bias)
}

# Dixon-Coles stability — hard safety floor (not optimized)
DIXON_COLES_ADJ_FLOOR = 0.10    # Correction factor never drops below this

# Rest advantage parameters
REST_ADVANTAGE_DAYS = 2      # Minimum rest gap for mild advantage
REST_EXTREME_DAYS = 4        # Rest gap for extreme advantage
REST_MILD_PENALTY = 0.95     # -5% lambda for mildly fatigued team
REST_HEAVY_PENALTY = 0.90    # -10% lambda for heavily fatigued team
REST_DEFAULT_DAYS = 7        # Default when no prior match found (season start)
REST_CONFIDENCE_BOOST = 0.04 # Confidence boost when rest aligns with pick

# Margin model for 2-way sports (NBA/NFL)
MARGIN_N_MATCHES = 10           # Last N home/away matches for margin average
MARGIN_MIN_MATCHES = 5          # Minimum matches to produce a signal
MARGIN_FATIGUE_MILD = 1.5       # Point reduction for mild fatigue
MARGIN_FATIGUE_HEAVY = 3.0      # Point reduction for heavy fatigue

# Historical league standard deviation of game margins
LEAGUE_MARGIN_SIGMA: dict[str, float] = {
    "americanfootball_nfl": 13.5,   # NFL game margins σ ≈ 13-14 points
    "basketball_nba": 12.0,         # NBA game margins σ ≈ 11-13 points
}
MARGIN_SIGMA_DEFAULT = 13.0         # Fallback for unknown 2-way sports


# ---------------------------------------------------------------------------
# Engine config cache — calibrated params from optimizer (DB-backed)
# ---------------------------------------------------------------------------

_engine_config_cache: dict[str, dict] = {}
_engine_config_expires: float = 0.0
_ENGINE_CONFIG_TTL = 3600  # 1 hour


async def _get_engine_params(sport_key: str) -> dict:
    """Load calibrated parameters from DB, with in-memory cache + fallback.

    Returns dict with keys: rho, alpha_time_decay, alpha_weight_floor.
    Falls back to hardcoded defaults when no engine_config doc exists.
    """
    global _engine_config_cache, _engine_config_expires

    now = _time.time()
    if now >= _engine_config_expires:
        try:
            docs = await _db.db.engine_config.find({}, {
                "rho": 1, "alpha_time_decay": 1, "alpha_weight_floor": 1,
            }).to_list(length=20)
            _engine_config_cache = {d["_id"]: d for d in docs}
        except Exception:
            logger.warning("Failed to refresh engine_config cache", exc_info=True)
            # Keep stale cache — connection errors are recoverable
        _engine_config_expires = now + _ENGINE_CONFIG_TTL

    cfg = _engine_config_cache.get(sport_key, {})
    return {
        "rho": cfg.get("rho", LEAGUE_RHO.get(sport_key, DIXON_COLES_RHO_DEFAULT)),
        "alpha_time_decay": cfg.get("alpha_time_decay", ALPHA_TIME_DECAY),
        "alpha_weight_floor": cfg.get("alpha_weight_floor", ALPHA_WEIGHT_FLOOR),
    }


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class QuoticoTipResponse(BaseModel):
    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    match_date: datetime
    recommended_selection: str
    confidence: float
    edge_pct: float
    true_probability: float
    implied_probability: float
    expected_goals_home: float
    expected_goals_away: float
    tier_signals: dict
    justification: str
    skip_reason: str | None = None
    generated_at: datetime


def _no_signal_bet(match: dict, reason: str) -> dict:
    """Build a stored tip with status 'no_signal' and skip_reason."""
    return {
        "match_id": str(match["_id"]),
        "sport_key": match["sport_key"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "match_date": match["match_date"],
        "recommended_selection": "-",
        "confidence": 0.0,
        "edge_pct": 0.0,
        "true_probability": 0.0,
        "implied_probability": 0.0,
        "expected_goals_home": 0.0,
        "expected_goals_away": 0.0,
        "tier_signals": {},
        "justification": "",
        "skip_reason": reason,
        "status": "no_signal",
        "actual_result": None,
        "was_correct": None,
        "generated_at": utcnow(),
    }


# ---------------------------------------------------------------------------
# Tier 1: Poisson & Gaussian Models
# ---------------------------------------------------------------------------

def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (λ^k * e^(-λ)) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k * math.exp(-lam)) / math.factorial(k)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF: P(Z ≤ x). Uses erfc for numerical stability."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _weighted_average(values: list[float], decay: float) -> float:
    """Compute exponentially-weighted average (index 0 = most recent)."""
    if not values:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for i, v in enumerate(values):
        w = decay ** i
        weighted_sum += v * w
        total_weight += w
    return weighted_sum / total_weight if total_weight > 0 else 0.0


def _time_based_weight(
    match_date: datetime, reference_date: datetime,
    *, alpha: float | None = None, floor: float | None = None,
) -> float:
    """Compute weight based on days since match (exponential time decay)."""
    a = alpha if alpha is not None else ALPHA_TIME_DECAY
    f = floor if floor is not None else ALPHA_WEIGHT_FLOOR
    delta_days = (ensure_utc(reference_date) - ensure_utc(match_date)).days
    return max(math.exp(-a * max(0, delta_days)), f)


def _time_weighted_average(
    values: list[float], match_dates: list[datetime], reference_date: datetime,
    *, alpha: float | None = None, floor: float | None = None,
) -> float:
    """Compute time-decay weighted average (more recent matches weigh more)."""
    if not values:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for v, d in zip(values, match_dates):
        w = _time_based_weight(d, reference_date, alpha=alpha, floor=floor)
        weighted_sum += v * w
        total_weight += w
    return weighted_sum / total_weight if total_weight > 0 else 0.0


def _dixon_coles_adjustment(
    home_goals: int, away_goals: int, lambda_h: float, lambda_a: float, rho: float,
) -> float:
    """Dixon-Coles correction factor for low scorelines (0-0, 1-0, 0-1, 1-1)."""
    if home_goals == 0 and away_goals == 0:
        adj = 1 - lambda_h * lambda_a * rho
    elif home_goals == 1 and away_goals == 0:
        adj = 1 + lambda_a * rho
    elif home_goals == 0 and away_goals == 1:
        adj = 1 + lambda_h * rho
    elif home_goals == 1 and away_goals == 1:
        adj = 1 - rho
    else:
        return 1.0
    return max(adj, DIXON_COLES_ADJ_FLOOR)


async def _get_rest_days(team_key: str, match_date: datetime) -> int:
    """Compute days since last competitive match (across ALL competitions)."""
    last_match = await _db.db.matches.find_one(
        {
            "$or": [{"home_team_key": team_key}, {"away_team_key": team_key}],
            "status": "final",
            "match_date": {"$lt": ensure_utc(match_date)},
        },
        {"match_date": 1},
        sort=[("match_date", -1)],
    )

    if not last_match:
        return REST_DEFAULT_DAYS

    delta = ensure_utc(match_date) - ensure_utc(last_match["match_date"])
    return max(delta.days, 0)


def _calculate_fatigue_penalty(home_rest: int, away_rest: int) -> tuple[float, float]:
    """Return lambda multipliers based on rest day differential."""
    home_mod, away_mod = 1.0, 1.0
    diff = home_rest - away_rest

    # Home is more fatigued
    if diff <= -REST_EXTREME_DAYS:
        home_mod = REST_HEAVY_PENALTY
    elif diff <= -REST_ADVANTAGE_DAYS:
        home_mod = REST_MILD_PENALTY

    # Away is more fatigued
    if diff >= REST_EXTREME_DAYS:
        away_mod = REST_HEAVY_PENALTY
    elif diff >= REST_ADVANTAGE_DAYS:
        away_mod = REST_MILD_PENALTY

    return home_mod, away_mod


async def _get_league_averages(
    related_keys: list[str],
    *,
    before_date: datetime | None = None,
) -> tuple[float, float] | None:
    """Compute league average home/away goals from last 2 seasons of historical data."""
    match_filter: dict = {"sport_key": {"$in": related_keys}, "status": "final"}
    if before_date:
        match_filter["match_date"] = {"$lt": before_date}
    pipeline = [
        {"$match": match_filter},
        {"$sort": {"match_date": -1}},
        {"$limit": 1000},  # ~2 seasons of top-flight football
        {"$group": {
            "_id": None,
            "total_home": {"$sum": "$result.home_score"},
            "total_away": {"$sum": "$result.away_score"},
            "count": {"$sum": 1},
        }},
    ]
    results = await _db.db.matches.aggregate(pipeline).to_list(length=1)
    if not results or results[0]["count"] < 50:
        return None

    count = results[0]["count"]
    return (
        results[0]["total_home"] / count,
        results[0]["total_away"] / count,
    )


async def _get_team_home_matches(
    team_key: str, related_keys: list[str], limit: int = N_TEAM_MATCHES,
    *, before_date: datetime | None = None,
) -> list[dict]:
    """Fetch team's last N home matches (goals scored/conceded)."""
    query: dict = {"home_team_key": team_key, "sport_key": {"$in": related_keys}, "status": "final"}
    if before_date:
        query["match_date"] = {"$lt": before_date}
    return await _db.db.matches.find(
        query,
        {"_id": 0, "result.home_score": 1, "result.away_score": 1, "match_date": 1},
    ).sort("match_date", -1).to_list(length=limit)


async def _get_team_away_matches(
    team_key: str, related_keys: list[str], limit: int = N_TEAM_MATCHES,
    *, before_date: datetime | None = None,
) -> list[dict]:
    """Fetch team's last N away matches (goals scored/conceded)."""
    query: dict = {"away_team_key": team_key, "sport_key": {"$in": related_keys}, "status": "final"}
    if before_date:
        query["match_date"] = {"$lt": before_date}
    return await _db.db.matches.find(
        query,
        {"_id": 0, "result.home_score": 1, "result.away_score": 1, "match_date": 1},
    ).sort("match_date", -1).to_list(length=limit)


async def compute_poisson_probabilities(
    home_team_key: str,
    away_team_key: str,
    sport_key: str,
    related_keys: list[str],
    *,
    match_date: datetime,
    h2h_lambdas: dict | None = None,
    before_date: datetime | None = None,
) -> Optional[dict]:
    """Compute Dixon-Coles-adjusted Poisson probabilities for 1/X/2 outcomes.

    Returns dict with lambda_home, lambda_away, prob_home, prob_draw, prob_away,
    rest days, and rho used — or None if insufficient data.
    """
    # Load calibrated (or default) parameters for this league
    params = await _get_engine_params(sport_key)

    league_avgs = await _get_league_averages(related_keys, before_date=before_date)
    if not league_avgs:
        return None

    avg_home_goals, avg_away_goals = league_avgs

    if avg_home_goals <= 0 or avg_away_goals <= 0:
        return None

    # Fetch team-specific data
    home_home_matches = await _get_team_home_matches(home_team_key, related_keys, before_date=before_date)
    away_away_matches = await _get_team_away_matches(away_team_key, related_keys, before_date=before_date)

    if len(home_home_matches) < MIN_MATCHES_REQUIRED or len(away_away_matches) < MIN_MATCHES_REQUIRED:
        return None

    # Reference date for time-based weighting
    reference_date = before_date or utcnow()
    cal_alpha = params["alpha_time_decay"]
    cal_floor = params["alpha_weight_floor"]

    # Home team attack/defense (from their home matches) — time-weighted
    home_goals_scored = [m["result"]["home_score"] for m in home_home_matches]
    home_goals_conceded = [m["result"]["away_score"] for m in home_home_matches]
    home_match_dates = [m["match_date"] for m in home_home_matches]

    # Away team attack/defense (from their away matches) — time-weighted
    away_goals_scored = [m["result"]["away_score"] for m in away_away_matches]
    away_goals_conceded = [m["result"]["home_score"] for m in away_away_matches]
    away_match_dates = [m["match_date"] for m in away_away_matches]

    home_attack = _time_weighted_average(
        home_goals_scored, home_match_dates, reference_date,
        alpha=cal_alpha, floor=cal_floor) / avg_home_goals
    home_defense = _time_weighted_average(
        home_goals_conceded, home_match_dates, reference_date,
        alpha=cal_alpha, floor=cal_floor) / avg_away_goals

    away_attack = _time_weighted_average(
        away_goals_scored, away_match_dates, reference_date,
        alpha=cal_alpha, floor=cal_floor) / avg_away_goals
    away_defense = _time_weighted_average(
        away_goals_conceded, away_match_dates, reference_date,
        alpha=cal_alpha, floor=cal_floor) / avg_home_goals

    # Expected goals (global model)
    lambda_home = home_attack * away_defense * avg_home_goals
    lambda_away = away_attack * home_defense * avg_away_goals

    # H2H blend: scale weight by sample size, cap at H2H_WEIGHT_MAX
    h2h_weight_used = 0.0
    if h2h_lambdas:
        h2h_weight_used = min(h2h_lambdas["count"] / H2H_WEIGHT_SCALE, H2H_WEIGHT_MAX)
        global_weight = 1.0 - h2h_weight_used
        lambda_home = global_weight * lambda_home + h2h_weight_used * h2h_lambdas["lambda_home"]
        lambda_away = global_weight * lambda_away + h2h_weight_used * h2h_lambdas["lambda_away"]

    # Rest advantage: reduce expected goals for fatigued team
    home_rest = await _get_rest_days(home_team_key, ensure_utc(match_date))
    away_rest = await _get_rest_days(away_team_key, ensure_utc(match_date))
    home_mod, away_mod = _calculate_fatigue_penalty(home_rest, away_rest)
    lambda_home *= home_mod
    lambda_away *= away_mod

    lambda_home = min(lambda_home, LAMBDA_CAP)
    lambda_away = min(lambda_away, LAMBDA_CAP)

    # Build Dixon-Coles corrected scoreline probability matrix
    rho = params["rho"]
    matrix = [[0.0] * SCORELINE_MAX for _ in range(SCORELINE_MAX)]
    for i in range(SCORELINE_MAX):
        for j in range(SCORELINE_MAX):
            prob = _poisson_pmf(i, lambda_home) * _poisson_pmf(j, lambda_away)
            adj = _dixon_coles_adjustment(i, j, lambda_home, lambda_away, rho)
            matrix[i][j] = max(prob * adj, 0.0)

    # Renormalize — Dixon-Coles correction shifts probability mass
    total_prob = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX))
    if total_prob > 0:
        for i in range(SCORELINE_MAX):
            for j in range(SCORELINE_MAX):
                matrix[i][j] /= total_prob

    prob_home = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX) if i > j)
    prob_draw = sum(matrix[i][i] for i in range(SCORELINE_MAX))
    prob_away = sum(matrix[i][j] for i in range(SCORELINE_MAX) for j in range(SCORELINE_MAX) if i < j)

    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "h2h_weight": h2h_weight_used,
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
        "rho": rho,
    }


async def compute_margin_probabilities(
    home_team_key: str,
    away_team_key: str,
    sport_key: str,
    related_keys: list[str],
    *,
    match_date: datetime,
    before_date: datetime | None = None,
) -> dict | None:
    """Compute win probabilities for 2-way sports using a Gaussian margin model.

    Computes moving average of point differentials for each team,
    derives an expected margin, and uses a normal distribution with
    league-specific historical variance to estimate P(home_win).
    """
    params = await _get_engine_params(sport_key)

    # Fetch team-specific data (last MARGIN_N_MATCHES)
    home_matches = await _get_team_home_matches(
        home_team_key, related_keys, limit=MARGIN_N_MATCHES, before_date=before_date
    )
    away_matches = await _get_team_away_matches(
        away_team_key, related_keys, limit=MARGIN_N_MATCHES, before_date=before_date
    )

    if len(home_matches) < MARGIN_MIN_MATCHES or len(away_matches) < MARGIN_MIN_MATCHES:
        return None

    reference_date = before_date or utcnow()
    cal_alpha = params["alpha_time_decay"]
    cal_floor = params["alpha_weight_floor"]

    # Calculate time-weighted average margin
    # Home team at home: home_score - away_score
    home_margins = [
        m["result"]["home_score"] - m["result"]["away_score"]
        for m in home_matches if m.get("result", {}).get("home_score") is not None
    ]
    home_dates = [m["match_date"] for m in home_matches]

    # Away team on road: away_score - home_score
    away_margins = [
        m["result"]["away_score"] - m["result"]["home_score"]
        for m in away_matches if m.get("result", {}).get("away_score") is not None
    ]
    away_dates = [m["match_date"] for m in away_matches]

    home_avg_margin = _time_weighted_average(
        home_margins, home_dates, reference_date, alpha=cal_alpha, floor=cal_floor
    )
    away_avg_margin = _time_weighted_average(
        away_margins, away_dates, reference_date, alpha=cal_alpha, floor=cal_floor
    )

    # Expected margin: Home Advantage + Relative Strength
    # home_avg_margin includes HCA implicitly (data from home games)
    # away_avg_margin includes "Road Ability" implicitly
    # Expected margin = Home's Home Strength - (-Away's Road Strength)
    # Logic: If Home usually wins by +5, and Away usually loses by -3 (wins by -3),
    # then Expected = 5 - (-3) is wrong.
    # Let's align:
    # Home's exp perf = +5. Away's exp perf = -3.
    # Differential = 5 - (-3) = +8? No.
    # Standard logic: Expected Home Margin = (HomeAvg + AwayAvgInv) / 2?
    # Simple relative strength: HomeAvgMargin - AwayAvgMargin?
    # HomeAvg (+5) means they are +5 better than average opponent at home.
    # AwayAvg (-3) means they are 3 pts worse than average opponent on road.
    # Diff = +5 - (-3) = +8. Yes.
    expected_margin = home_avg_margin - away_avg_margin

    # Rest advantage correction (Fatigue Penalty in POINTS)
    home_rest = await _get_rest_days(home_team_key, ensure_utc(match_date))
    away_rest = await _get_rest_days(away_team_key, ensure_utc(match_date))
    rest_diff = home_rest - away_rest

    # Apply penalty to expected margin (subtract if home tired, add if away tired)
    # Home tired (negative diff): reduce margin
    if rest_diff <= -REST_EXTREME_DAYS:
        expected_margin -= MARGIN_FATIGUE_HEAVY
    elif rest_diff <= -REST_ADVANTAGE_DAYS:
        expected_margin -= MARGIN_FATIGUE_MILD

    # Away tired (positive diff): increase margin
    if rest_diff >= REST_EXTREME_DAYS:
        expected_margin += MARGIN_FATIGUE_HEAVY
    elif rest_diff >= REST_ADVANTAGE_DAYS:
        expected_margin += MARGIN_FATIGUE_MILD

    # Probability Calculation via Gaussian
    sigma = LEAGUE_MARGIN_SIGMA.get(sport_key, MARGIN_SIGMA_DEFAULT)
    prob_home = _normal_cdf(expected_margin / sigma)
    prob_away = 1.0 - prob_home

    return {
        "expected_margin": expected_margin,
        "sigma": sigma,
        "prob_home": prob_home,
        "prob_away": prob_away,
        "home_avg_margin": home_avg_margin,
        "away_avg_margin": away_avg_margin,
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
    }


def _compute_h2h_lambdas(
    h2h_matches: list[dict], home_key: str, away_key: str,
) -> dict | None:
    """Compute H2H-specific expected goals from direct meetings."""
    if len(h2h_matches) < H2H_MIN_MATCHES:
        return None

    sum_home_goals = 0.0
    sum_away_goals = 0.0
    total_weight = 0.0

    for i, m in enumerate(h2h_matches):
        weight = RECENCY_DECAY ** i

        # Adjust perspective: match home_team may be current away_team
        m_result = m.get("result", {})
        if m.get("home_team_key") == home_key:
            sum_home_goals += m_result.get("home_score", 0) * weight
            sum_away_goals += m_result.get("away_score", 0) * weight
        else:
            sum_home_goals += m_result.get("away_score", 0) * weight
            sum_away_goals += m_result.get("home_score", 0) * weight

        total_weight += weight

    if total_weight <= 0:
        return None

    return {
        "lambda_home": sum_home_goals / total_weight,
        "lambda_away": sum_away_goals / total_weight,
        "count": len(h2h_matches),
    }


# ---------------------------------------------------------------------------
# Edge calculation
# ---------------------------------------------------------------------------

def normalize_implied_probabilities(odds: dict[str, float]) -> dict[str, float]:
    """Remove bookmaker vig by normalizing implied probabilities to sum to 1.0."""
    implied = {}
    for k, v in odds.items():
        if v > 0:
            implied[k] = 1.0 / v
        else:
            implied[k] = 0.0
    total = sum(implied.values())
    if total <= 0:
        return implied
    return {k: v / total for k, v in implied.items()}


def compute_edge(true_prob: float, implied_prob: float) -> float:
    """Return edge as percentage points (positive = value bet)."""
    return (true_prob - implied_prob) * 100


# ---------------------------------------------------------------------------
# Tier 2: Form & Momentum score
# ---------------------------------------------------------------------------

async def compute_momentum_score(
    team_key: str,
    form_matches: list[dict],
    related_keys: list[str],
    *,
    before_date: datetime | None = None,
) -> dict:
    """Compute weighted form/momentum score for a team."""
    if not form_matches:
        return {"momentum_score": 0.5, "form_points": 0, "weighted_form": 0.0}

    max_possible = 0.0
    weighted_sum = 0.0

    for i, m in enumerate(form_matches):
        m_result = m.get("result", {})
        hg = m_result.get("home_score", 0)
        ag = m_result.get("away_score", 0)
        h_key = m.get("home_team_key", "")
        is_home = h_key == team_key

        # Determine result for this team
        if is_home:
            points = 3 if hg > ag else (1 if hg == ag else 0)
        else:
            points = 3 if ag > hg else (1 if ag == hg else 0)

        # Opponent strength weight from historical odds
        opponent_weight = await _get_opponent_strength_weight(m, team_key, related_keys, before_date=before_date)

        recency_w = MOMENTUM_DECAY ** i
        combined_w = recency_w * opponent_weight
        weighted_sum += points * combined_w
        max_possible += 3 * combined_w

    momentum_score = weighted_sum / max_possible if max_possible > 0 else 0.5
    form_points = int(weighted_sum)

    return {
        "momentum_score": round(momentum_score, 3),
        "form_points": form_points,
        "weighted_form": round(weighted_sum, 2),
    }


async def _get_opponent_strength_weight(
    match: dict, team_key: str, related_keys: list[str],
    *, before_date: datetime | None = None,
) -> float:
    """Estimate opponent strength from their historical odds."""
    h_key = match.get("home_team_key", "")
    opponent_key = match.get("away_team_key", "") if h_key == team_key else h_key

    if not opponent_key:
        return 1.0

    # Look up the opponent's recent average odds (as home favorite indicator)
    opp_query: dict = {
        "sport_key": {"$in": related_keys},
        "status": "final",
        "$or": [{"home_team_key": opponent_key}, {"away_team_key": opponent_key}],
        "odds.bookmakers": {"$ne": None},
    }
    if before_date:
        opp_query["match_date"] = {"$lt": before_date}
    recent = await _db.db.matches.find_one(
        opp_query,
        {"odds.bookmakers": 1},
        sort=[("match_date", -1)],
    )

    if not recent or not recent.get("odds", {}).get("bookmakers"):
        return 1.0

    # Use first bookmaker's odds to gauge opponent strength
    for _bk, entry in recent["odds"]["bookmakers"].items():
        if isinstance(entry, dict):
            # Strong team → low home odds → higher weight for beating them
            home_odds = entry.get("home", 2.0)
            if home_odds < 1.5:
                return 1.5  # Beat a strong favorite
            elif home_odds < 2.0:
                return 1.2
            elif home_odds > 3.0:
                return 0.7  # Beat a weak underdog
            return 1.0
    return 1.0


# ---------------------------------------------------------------------------
# Tier 3: Sharp movement detection
# ---------------------------------------------------------------------------

async def detect_sharp_movement(
    match_id: str,
    commence_time: Optional[datetime] = None,
    *,
    before_date: datetime | None = None,
) -> dict:
    """Analyze odds snapshots for significant line movement."""
    default = {
        "has_sharp_movement": False,
        "direction": None,
        "opening_odds": None,
        "current_odds": None,
        "max_drop_pct": 0.0,
        "is_late_money": False,
        "has_steam_move": False,
        "steam_outcome": None,
        "steam_drop_pct": 0.0,
        "has_reversal": False,
        "reversal_outcome": None,
        "snapshot_count": 0,
    }

    snap_query: dict = {"match_id": match_id}
    if before_date:
        snap_query["snapshot_at"] = {"$lt": before_date}
    snapshots = await _db.db.odds_snapshots.find(
        snap_query,
    ).sort("snapshot_at", 1).to_list(length=200)

    if len(snapshots) < 3:
        default["snapshot_count"] = len(snapshots)
        return default

    opening = snapshots[0]["odds"]
    current = snapshots[-1]["odds"]

    # --- Signal 1: Sharp movement (opening vs current) ---
    max_drop = 0.0
    direction = None

    for outcome in opening:
        open_val = opening[outcome]
        curr_val = current.get(outcome, open_val)
        if open_val <= 0:
            continue
        drop_pct = ((open_val - curr_val) / open_val) * 100
        if drop_pct > max_drop:
            max_drop = drop_pct
            direction = outcome

    has_sharp = max_drop >= SHARP_DROP_PCT

    # Late money check
    is_late = False
    if has_sharp and commence_time:
        commence_time = ensure_utc(commence_time)
        late_cutoff = commence_time - timedelta(hours=LATE_MONEY_HOURS)
        late_snapshots = [s for s in snapshots if ensure_utc(s["snapshot_at"]) >= late_cutoff]
        if len(late_snapshots) >= 2 and direction:
            late_open = late_snapshots[0]["odds"].get(direction, 0)
            late_curr = late_snapshots[-1]["odds"].get(direction, 0)
            if late_open > 0:
                late_drop = ((late_open - late_curr) / late_open) * 100
                is_late = late_drop > SHARP_DROP_PCT * 0.5

    # --- Signal 2: Steam velocity (single-interval whale moves) ---
    has_steam = False
    steam_outcome = None
    steam_drop_pct = 0.0

    for i in range(len(snapshots) - 1):
        s_curr = snapshots[i]["odds"]
        s_next = snapshots[i + 1]["odds"]
        for outcome in s_curr:
            val_curr = s_curr[outcome]
            val_next = s_next.get(outcome, val_curr)
            if val_curr <= 0:
                continue
            interval_drop = ((val_curr - val_next) / val_curr) * 100
            if interval_drop > steam_drop_pct:
                steam_drop_pct = interval_drop
                steam_outcome = outcome
        if steam_drop_pct >= STEAM_VELOCITY_PCT:
            has_steam = True

    if steam_drop_pct < STEAM_VELOCITY_PCT:
        steam_outcome = None
        steam_drop_pct = 0.0

    # --- Signal 3: Reversal detection (direction flip) ---
    has_reversal = False
    reversal_outcome = None

    for i in range(1, len(snapshots) - 1):
        s_prev = snapshots[i - 1]["odds"]
        s_curr = snapshots[i]["odds"]
        s_next = snapshots[i + 1]["odds"]
        for outcome in s_curr:
            prev_val = s_prev.get(outcome, 0)
            curr_val = s_curr.get(outcome, 0)
            next_val = s_next.get(outcome, 0)
            if prev_val <= 0 or curr_val <= 0:
                continue
            move_1 = curr_val - prev_val
            move_2 = next_val - curr_val
            # Sharp sign flip: moved one way then snapped back
            if (move_1 > 0 and move_2 < 0) or (move_1 < 0 and move_2 > 0):
                swing_pct = abs(move_2 / curr_val) * 100
                if swing_pct >= REVERSAL_THRESHOLD_PCT:
                    has_reversal = True
                    reversal_outcome = outcome
                    break
        if has_reversal:
            break

    return {
        "has_sharp_movement": has_sharp,
        "direction": direction if has_sharp else None,
        "opening_odds": opening,
        "current_odds": current,
        "max_drop_pct": round(max_drop, 1),
        "is_late_money": is_late,
        "has_steam_move": has_steam,
        "steam_outcome": steam_outcome,
        "steam_drop_pct": round(steam_drop_pct, 1),
        "has_reversal": has_reversal,
        "reversal_outcome": reversal_outcome,
        "snapshot_count": len(snapshots),
    }


# ---------------------------------------------------------------------------
# Bonus Tier: King's Choice (squad consensus)
# ---------------------------------------------------------------------------

async def compute_kings_choice(match_id: str, *, before_date: datetime | None = None) -> dict:
    """Query bets from top-10% leaderboard users for this match."""
    default = {
        "has_kings_choice": False,
        "kings_pick": None,
        "kings_pct": 0.0,
        "total_kings": 0,
        "kings_who_bet": 0,
        "is_underdog_pick": False,
    }

    # Cannot reconstruct historical leaderboard state for backfill
    if before_date:
        return default

    # Get total leaderboard size
    total_users = await _db.db.leaderboard.count_documents({})
    if total_users < 10:
        return default

    # Top 10% cutoff
    top_n = max(int(total_users * 0.10), 3)
    kings = await _db.db.leaderboard.find(
        {}, {"user_id": 1},
    ).sort("points", -1).limit(top_n).to_list(length=top_n)

    king_ids = [k["user_id"] for k in kings]
    if not king_ids:
        return default

    # Get their single bets for this match from unified collection
    king_slips = await _db.db.betting_slips.find(
        {
            "selections.match_id": match_id,
            "user_id": {"$in": king_ids},
            "type": "single",
        },
        {"selections": 1, "user_id": 1},
    ).to_list(length=len(king_ids))

    if len(king_slips) < MIN_KINGS_TIPPED:
        return default

    # Extract picks from selections
    king_picks: list[dict] = []
    for slip in king_slips:
        for sel in slip.get("selections", []):
            if sel.get("match_id") == match_id:
                king_picks.append({
                    "pick": sel["pick"],
                    "locked_odds": sel.get("locked_odds", 0),
                })
                break

    if len(king_picks) < MIN_KINGS_TIPPED:
        return default

    # Count selections
    counts: dict[str, int] = {}
    for kp in king_picks:
        counts[kp["pick"]] = counts.get(kp["pick"], 0) + 1

    # Find dominant pick
    best_pick = max(counts, key=lambda k: counts[k])
    best_count = counts[best_pick]
    agreement = best_count / len(king_picks)

    # Check if it's an underdog pick
    avg_locked = sum(
        kp["locked_odds"] for kp in king_picks if kp["pick"] == best_pick
    ) / best_count if best_count > 0 else 0
    is_underdog = avg_locked > UNDERDOG_ODDS_THRESHOLD

    if agreement < KINGS_AGREEMENT_PCT:
        return default

    return {
        "has_kings_choice": True,
        "kings_pick": best_pick,
        "kings_pct": round(agreement, 2),
        "total_kings": len(king_ids),
        "kings_who_bet": len(king_slips),
        "is_underdog_pick": is_underdog,
    }


# ---------------------------------------------------------------------------
# EVD: Expected Value Differential ("Beat the Books")
# ---------------------------------------------------------------------------

async def compute_team_evd(
    team_key: str,
    related_keys: list[str],
    n: int = N_TEAM_MATCHES,
    *,
    before_date: datetime | None = None,
) -> dict:
    """Compute how much a team outperforms market expectations."""
    default = {
        "evd": 0.0,
        "matches_analyzed": 0,
        "btb_count": 0,
        "btb_ratio": 0.0,
        "contributes": False,
    }

    # Fetch last n matches (home + away) that have odds data
    query: dict = {
        "$or": [
            {"home_team_key": team_key},
            {"away_team_key": team_key},
        ],
        "sport_key": {"$in": related_keys},
        "status": "final",
        "odds.bookmakers": {"$exists": True, "$ne": None},
    }
    if before_date:
        query["match_date"] = {"$lt": before_date}

    matches = await _db.db.matches.find(
        query,
        {
            "_id": 0, "home_team_key": 1, "away_team_key": 1,
            "result.home_score": 1, "result.away_score": 1, "result.outcome": 1, "odds.bookmakers": 1,
        },
    ).sort("match_date", -1).to_list(length=n * 2)  # fetch extra, filter below

    edges: list[float] = []
    btb_count = 0

    for m in matches:
        if len(edges) >= n:
            break

        # Determine perspective and extract team odds
        is_home = m.get("home_team_key") == team_key
        odds_dict = m.get("odds", {}).get("bookmakers", {})

        # Find the first bookmaker entry with usable odds
        team_odds: float | None = None
        for _bk, entry in odds_dict.items():
            if not isinstance(entry, dict):
                continue
            if is_home:
                team_odds = entry.get("home")
            else:
                team_odds = entry.get("away")
            if team_odds and team_odds > 1.0:
                break
            team_odds = None

        if not team_odds:
            continue

        implied_prob = 1.0 / team_odds

        # Actual outcome for this team
        m_result = m.get("result", {})
        hg = m_result.get("home_score", 0)
        ag = m_result.get("away_score", 0)
        if is_home:
            actual = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        else:
            actual = 1.0 if ag > hg else (0.5 if ag == hg else 0.0)

        edge = actual - implied_prob
        edges.append(edge)
        if edge > 0:
            btb_count += 1

    analyzed = len(edges)
    if analyzed < EVD_MIN_MATCHES:
        default["matches_analyzed"] = analyzed
        return default

    evd = sum(edges) / analyzed
    return {
        "evd": round(evd, 4),
        "matches_analyzed": analyzed,
        "btb_count": btb_count,
        "btb_ratio": round(btb_count / analyzed, 3),
        "contributes": True,
    }


# ---------------------------------------------------------------------------
# Confidence calculation
# ---------------------------------------------------------------------------

def _calculate_confidence(
    edge_pct: float,
    momentum_gap: float,
    home_momentum: dict,
    away_momentum: dict,
    sharp: dict,
    kings: dict,
    best_outcome: str,
    *,
    h2h_summary: dict | None = None,
    evd_home: dict | None = None,
    evd_away: dict | None = None,
    rest_advantage: dict | None = None,
) -> float:
    """Combine tier signals into a single confidence score [0.0, 1.0]."""
    # Base confidence from edge (sigmoid centered at 5%)
    base = 1.0 / (1.0 + math.exp(-0.15 * (edge_pct - 5)))

    # Tier 2: momentum agrees with Poisson pick
    momentum_boost = 0.0
    if momentum_gap > 0.20:
        home_m = home_momentum["momentum_score"]
        away_m = away_momentum["momentum_score"]
        if best_outcome == "1" and home_m > away_m:
            momentum_boost = 0.08
        elif best_outcome == "2" and away_m > home_m:
            momentum_boost = 0.08

    # Tier 3: sharp money agrees
    sharp_boost = 0.0
    if sharp["has_sharp_movement"] and sharp["direction"] == best_outcome:
        sharp_boost = 0.10
        if sharp["is_late_money"]:
            sharp_boost = 0.12

    # Steam move agrees with pick: additional boost on top of sharp
    steam_boost = 0.0
    if sharp.get("has_steam_move") and sharp.get("steam_outcome") == best_outcome:
        steam_boost = 0.03

    # Reversal detected on pick's outcome: market indecision → dampen
    reversal_penalty = 0.0
    if sharp.get("has_reversal") and sharp.get("reversal_outcome") == best_outcome:
        reversal_penalty = -0.05

    # Bonus: King's Choice agrees
    kings_boost = 0.0
    if kings["has_kings_choice"] and kings["kings_pick"] == best_outcome:
        kings_boost = 0.05

    # H2H record strongly favors the pick (+0.05)
    h2h_boost = 0.0
    if h2h_summary and h2h_summary["total"] >= H2H_MIN_MATCHES:
        total = h2h_summary["total"]
        if best_outcome == "1" and h2h_summary["home_wins"] / total >= 0.60:
            h2h_boost = 0.05
        elif best_outcome == "2" and h2h_summary["away_wins"] / total >= 0.60:
            h2h_boost = 0.05
        elif best_outcome == "X" and h2h_summary["draws"] / total >= 0.40:
            h2h_boost = 0.05

    # EVD: team systematically beats / disappoints the books
    evd_boost = 0.0
    picked_evd = evd_home if best_outcome == "1" else (evd_away if best_outcome == "2" else None)
    if picked_evd and picked_evd.get("contributes"):
        if picked_evd["evd"] > EVD_BOOST_THRESHOLD:
            evd_boost = EVD_CONFIDENCE_BOOST
        elif picked_evd["evd"] < EVD_DAMPEN_THRESHOLD:
            evd_boost = -EVD_CONFIDENCE_DAMPEN

    # Rest advantage: picked team is better rested
    rest_boost = 0.0
    if rest_advantage and rest_advantage.get("contributes"):
        diff = rest_advantage["diff"]
        if best_outcome == "1" and diff > 0:
            rest_boost = REST_CONFIDENCE_BOOST
        elif best_outcome == "2" and diff < 0:
            rest_boost = REST_CONFIDENCE_BOOST

    total = base + momentum_boost + sharp_boost + steam_boost + reversal_penalty + kings_boost + h2h_boost + evd_boost + rest_boost
    confidence = min(total, 0.95)
    return max(confidence, 0.10)


# ---------------------------------------------------------------------------
# Justification builder
# ---------------------------------------------------------------------------

def _build_justification(
    best_outcome: str,
    edge_pct: float,
    poisson: dict,
    implied: dict,
    momentum_gap: float,
    sharp: dict,
    kings: dict,
    match: dict,
    *,
    h2h_summary: dict | None = None,
    evd_home: dict | None = None,
    evd_away: dict | None = None,
    rest_advantage: dict | None = None,
) -> str:
    """Build a human-readable English explanation of the recommendation."""
    team_labels = {
        "1": match["home_team"],
        "X": "Draw",
        "2": match["away_team"],
    }
    pick_label = team_labels[best_outcome]

    prob_key = {"1": "prob_home", "X": "prob_draw", "2": "prob_away"}[best_outcome]
    true_pct = poisson[prob_key] * 100
    implied_pct = implied.get(best_outcome, 0) * 100

    parts = [f"Recommendation: {pick_label} ({best_outcome})."]
    parts.append(
        f"Model sees {true_pct:.0f}% probability vs. "
        f"{implied_pct:.0f}% from bookmakers = {edge_pct:.1f}% edge."
    )
    parts.append(
        f"Expected goals: {poisson['lambda_home']:.1f} - {poisson['lambda_away']:.1f}."
    )

    # H2H context
    if h2h_summary and h2h_summary["total"] >= H2H_MIN_MATCHES:
        parts.append(
            f"Head-to-head: {h2h_summary['total']} meetings "
            f"({h2h_summary['home_wins']}W/{h2h_summary['draws']}D/{h2h_summary['away_wins']}L, "
            f"avg {h2h_summary['avg_goals']} goals)."
        )

    if momentum_gap > 0.20:
        parts.append("Form strength supports this recommendation.")
    if sharp["has_sharp_movement"] and sharp["direction"] == best_outcome:
        if sharp["is_late_money"]:
            parts.append("Late professional money is moving the odds in this direction.")
        else:
            parts.append("Professional bettors are moving the odds in this direction.")
    if sharp.get("has_steam_move") and sharp.get("steam_outcome") == best_outcome:
        parts.append("Strong short-term odds movement detected (sharp signal).")
    if sharp.get("has_reversal") and sharp.get("reversal_outcome") == best_outcome:
        parts.append("Odds reversal detected — market uncertain about this outcome.")
    if kings["has_kings_choice"] and kings["kings_pick"] == best_outcome:
        parts.append(
            f"King's Choice: {kings['kings_pct']*100:.0f}% of top players agree."
        )

    # EVD / Beat the Books
    picked_evd = evd_home if best_outcome == "1" else (evd_away if best_outcome == "2" else None)
    if picked_evd and picked_evd.get("contributes"):
        evd_val = picked_evd["evd"]
        ratio_pct = picked_evd["btb_ratio"] * 100
        if evd_val > EVD_BOOST_THRESHOLD:
            parts.append(
                f"Market edge: {pick_label} outperforms bookmaker expectations "
                f"in {ratio_pct:.0f}% of recent matches (EVD: {evd_val:+.1%}). "
                f"Systematically undervalued — value factor."
            )
        elif evd_val < EVD_DAMPEN_THRESHOLD:
            parts.append(
                f"Market risk: {pick_label} regularly disappoints against the odds "
                f"(EVD: {evd_val:+.1%}). The market tends to overvalue this team."
            )

    # Rest advantage
    if rest_advantage and rest_advantage.get("contributes"):
        h_rest = rest_advantage["home_rest_days"]
        a_rest = rest_advantage["away_rest_days"]
        diff = rest_advantage["diff"]
        if diff > 0:
            rested_team = match["home_team"]
            fatigued_mod = REST_HEAVY_PENALTY if diff >= REST_EXTREME_DAYS else REST_MILD_PENALTY
        else:
            rested_team = match["away_team"]
            fatigued_mod = REST_HEAVY_PENALTY if abs(diff) >= REST_EXTREME_DAYS else REST_MILD_PENALTY
        penalty_pct = round((1 - fatigued_mod) * 100)
        parts.append(
            f"Rest advantage: {rested_team} ({h_rest}d vs {a_rest}d). "
            f"Fatigued opponent's attacking output reduced by {penalty_pct}%."
        )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def generate_quotico_tip(match: dict, *, before_date: datetime | None = None) -> dict:
    """Generate a QuoticoTip for a single match."""
    sport_key = match["sport_key"]
    current_odds = match.get("odds", {}).get("h2h", {})
    is_three_way = "X" in current_odds

    if not is_three_way:
        # 2-way sports (NBA/NFL): Try Gaussian Margin Model first
        return await _generate_two_way_bet(match, before_date=before_date)

    related_keys = sport_keys_for(sport_key)
    home_team = match["home_team"]
    away_team = match["away_team"]
    match_id = str(match["_id"])

    # Resolve team keys
    home_key = await resolve_team_key(home_team, related_keys)
    away_key = await resolve_team_key(away_team, related_keys)
    if not home_key or not away_key:
        missing = []
        if not home_key:
            missing.append(home_team)
        if not away_key:
            missing.append(away_team)
        return _no_signal_bet(match, f"Team not resolved: {', '.join(missing)}")

    # Fetch H2H + form data (needed for both Poisson blend and momentum)
    context = await build_match_context(home_team, away_team, sport_key, h2h_limit=10, form_limit=5, before_date=before_date)
    h2h_data = context.get("h2h")
    h2h_summary = h2h_data["summary"] if h2h_data else None
    h2h_matches = h2h_data["matches"] if h2h_data else []

    # Compute H2H lambdas for Poisson blend
    h2h_lambdas = _compute_h2h_lambdas(h2h_matches, home_key, away_key) if h2h_matches else None

    # Tier 1: Dixon-Coles Poisson (with H2H blend + rest advantage)
    poisson = await compute_poisson_probabilities(
        home_key, away_key, sport_key, related_keys,
        match_date=match["match_date"], h2h_lambdas=h2h_lambdas, before_date=before_date,
    )
    if not poisson:
        return _no_signal_bet(match, "Insufficient historical data for Poisson model")

    # Normalize bookmaker implied probabilities (remove vig)
    implied = normalize_implied_probabilities(current_odds)

    # Compute edges for each outcome
    prob_map = {"1": "prob_home", "X": "prob_draw", "2": "prob_away"}
    edges = {}
    for outcome in ["1", "X", "2"]:
        true_prob = poisson[prob_map[outcome]]
        imp_prob = implied.get(outcome, 0)
        edges[outcome] = compute_edge(true_prob, imp_prob)

    # Find best edge
    best_outcome = max(edges, key=lambda k: edges[k])
    best_edge = edges[best_outcome]

    if best_edge < EDGE_THRESHOLD_PCT:
        return _no_signal_bet(match, f"No value bet found (best edge: {best_edge:.1f}% < {EDGE_THRESHOLD_PCT}%)")

    # Tier 2: Form & Momentum
    home_form = context.get("home_form") or []
    away_form = context.get("away_form") or []

    home_momentum = await compute_momentum_score(home_key, home_form, related_keys, before_date=before_date)
    away_momentum = await compute_momentum_score(away_key, away_form, related_keys, before_date=before_date)
    momentum_gap = abs(home_momentum["momentum_score"] - away_momentum["momentum_score"])

    # Tier 3: Sharp Movement
    commence_time = match.get("match_date")
    sharp = await detect_sharp_movement(match_id, commence_time, before_date=before_date)

    # Bonus: King's Choice
    kings = await compute_kings_choice(match_id, before_date=before_date)

    # EVD: Beat the Books
    evd_home = await compute_team_evd(home_key, related_keys, before_date=before_date)
    evd_away = await compute_team_evd(away_key, related_keys, before_date=before_date)

    # Rest advantage signal
    rest_diff = poisson["home_rest_days"] - poisson["away_rest_days"]
    rest_advantage = {
        "home_rest_days": poisson["home_rest_days"],
        "away_rest_days": poisson["away_rest_days"],
        "diff": rest_diff,
        "contributes": abs(rest_diff) >= REST_ADVANTAGE_DAYS,
    }

    # Confidence
    confidence = _calculate_confidence(
        best_edge, momentum_gap, home_momentum, away_momentum, sharp, kings, best_outcome,
        h2h_summary=h2h_summary, evd_home=evd_home, evd_away=evd_away,
        rest_advantage=rest_advantage,
    )

    # Justification
    justification = _build_justification(
        best_outcome, best_edge, poisson, implied, momentum_gap, sharp, kings, match,
        h2h_summary=h2h_summary, evd_home=evd_home, evd_away=evd_away,
        rest_advantage=rest_advantage,
    )

    true_prob = poisson[prob_map[best_outcome]]
    imp_prob = implied.get(best_outcome, 0)

    return {
        "match_id": match_id,
        "sport_key": sport_key,
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "match_date": match["match_date"],
        "recommended_selection": best_outcome,
        "confidence": round(confidence, 3),
        "edge_pct": round(best_edge, 2),
        "true_probability": round(true_prob, 4),
        "implied_probability": round(imp_prob, 4),
        "expected_goals_home": round(poisson["lambda_home"], 2),
        "expected_goals_away": round(poisson["lambda_away"], 2),
        "tier_signals": {
            "poisson": {
                "lambda_home": round(poisson["lambda_home"], 2),
                "lambda_away": round(poisson["lambda_away"], 2),
                "h2h_weight": round(poisson["h2h_weight"], 2),
                "true_probs": {
                    "1": round(poisson["prob_home"], 4),
                    "X": round(poisson["prob_draw"], 4),
                    "2": round(poisson["prob_away"], 4),
                },
                "edges": {k: round(v, 2) for k, v in edges.items()},
            },
            "h2h": {
                "total_meetings": h2h_summary["total"] if h2h_summary else 0,
                "home_wins": h2h_summary["home_wins"] if h2h_summary else 0,
                "away_wins": h2h_summary["away_wins"] if h2h_summary else 0,
                "draws": h2h_summary["draws"] if h2h_summary else 0,
                "avg_goals": h2h_summary["avg_goals"] if h2h_summary else 0,
                "contributes": h2h_lambdas is not None,
            },
            "momentum": {
                "home": home_momentum,
                "away": away_momentum,
                "gap": round(momentum_gap, 3),
                "contributes": momentum_gap > 0.20,
            },
            "sharp_movement": sharp,
            "kings_choice": kings,
            "btb": {
                "home": evd_home,
                "away": evd_away,
            },
            "rest_advantage": rest_advantage,
        },
        "justification": justification,
        "status": "active",
        "actual_result": None,
        "was_correct": None,
        "generated_at": utcnow(),
    }


async def _generate_two_way_bet(match: dict, *, before_date: datetime | None = None) -> dict:
    """Generate a tip for 2-way sports (NFL, NBA).

    Prioritizes the Gaussian Margin Model. If insufficient data, falls back to
    Tier 2+3 (Momentum/Sharp) only logic.
    """
    sport_key = match["sport_key"]
    current_odds = match.get("odds", {}).get("h2h", {})
    match_id = str(match["_id"])
    related_keys = sport_keys_for(sport_key)
    home_team = match["home_team"]
    away_team = match["away_team"]

    home_key = await resolve_team_key(home_team, related_keys)
    away_key = await resolve_team_key(away_team, related_keys)
    if not home_key or not away_key:
        missing = []
        if not home_key:
            missing.append(home_team)
        if not away_key:
            missing.append(away_team)
        return _no_signal_bet(match, f"Team not resolved: {', '.join(missing)}")

    # Tier 2: Momentum (Always needed for confidence/form check)
    context = await build_match_context(home_team, away_team, sport_key, h2h_limit=5, form_limit=5, before_date=before_date)
    home_form = context.get("home_form") or []
    away_form = context.get("away_form") or []

    home_momentum = await compute_momentum_score(home_key, home_form, related_keys, before_date=before_date)
    away_momentum = await compute_momentum_score(away_key, away_form, related_keys, before_date=before_date)
    momentum_gap = abs(home_momentum["momentum_score"] - away_momentum["momentum_score"])

    # Tier 3: Sharp Movement
    sharp = await detect_sharp_movement(match_id, match.get("match_date"), before_date=before_date)

    # EVD: Beat the Books
    evd_home = await compute_team_evd(home_key, related_keys, before_date=before_date)
    evd_away = await compute_team_evd(away_key, related_keys, before_date=before_date)

    # King's Choice
    kings = await compute_kings_choice(match_id, before_date=before_date)

    # --- Attempt Margin Model (Tier 1 for 2-way) ---
    margin = await compute_margin_probabilities(
        home_key, away_key, sport_key, related_keys,
        match_date=match["match_date"], before_date=before_date
    )

    best_outcome = None
    true_prob = 0.0
    edge_pct = 0.0
    confidence = 0.0
    margin_signals = None
    rest_signals = None
    justification = ""

    implied = normalize_implied_probabilities(current_odds)

    if margin:
        # Gaussian Margin Model Succeeded
        prob_map = {"1": "prob_home", "2": "prob_away"}
        edges = {}
        for outcome in ["1", "2"]:
            tp = margin[prob_map[outcome]]
            ip = implied.get(outcome, 0)
            edges[outcome] = compute_edge(tp, ip)

        best_outcome = max(edges, key=lambda k: edges[k])
        edge_pct = edges[best_outcome]
        true_prob = margin[prob_map[best_outcome]]

        if edge_pct < EDGE_THRESHOLD_PCT:
             return _no_signal_bet(match, f"No value bet found (best edge: {edge_pct:.1f}% < {EDGE_THRESHOLD_PCT}%)")

        # Rest signal from margin model
        rest_diff = margin["home_rest_days"] - margin["away_rest_days"]
        rest_signals = {
            "home_rest_days": margin["home_rest_days"],
            "away_rest_days": margin["away_rest_days"],
            "diff": rest_diff,
            "contributes": abs(rest_diff) >= REST_ADVANTAGE_DAYS,
        }

        # Confidence using full suite
        confidence = _calculate_confidence(
            edge_pct, momentum_gap, home_momentum, away_momentum, sharp, kings, best_outcome,
            evd_home=evd_home, evd_away=evd_away, rest_advantage=rest_signals,
        )

        margin_signals = {
            "expected_margin": round(margin["expected_margin"], 1),
            "sigma": margin["sigma"],
            "home_avg_margin": round(margin["home_avg_margin"], 1),
            "away_avg_margin": round(margin["away_avg_margin"], 1),
            "prob_home": round(margin["prob_home"], 4),
            "prob_away": round(margin["prob_away"], 4),
        }

        # Build specific justification for margin model
        pick_team = match["home_team"] if best_outcome == "1" else match["away_team"]
        imp_prob = implied.get(best_outcome, 0)
        exp_margin_abs = abs(margin["expected_margin"])
        favored_team = match["home_team"] if margin["expected_margin"] > 0 else match["away_team"]

        justification = (
            f"Recommendation: {pick_team} ({best_outcome}). "
            f"Model sees {true_prob*100:.0f}% probability vs {imp_prob*100:.0f}% implied ({edge_pct:.1f}% edge). "
            f"Expected margin: {favored_team} by {exp_margin_abs:.1f} points (σ={margin['sigma']}). "
        )
        if abs(rest_diff) >= REST_ADVANTAGE_DAYS:
            rested = match["home_team"] if rest_diff > 0 else match["away_team"]
            fatigued = match["away_team"] if rest_diff > 0 else match["home_team"]
            justification += f"Rest advantage: {rested} ({margin['home_rest_days']}d vs {margin['away_rest_days']}d). "

    else:
        # Fallback: Momentum + Sharp Only (Legacy Logic)
        if momentum_gap < 0.15:
            return _no_signal_bet(match, f"Insufficient form gap ({momentum_gap:.0%} < 15%)")

        if home_momentum["momentum_score"] > away_momentum["momentum_score"]:
            best_outcome = "1"
        else:
            best_outcome = "2"

        # Sharp disagreement flip
        if sharp["has_sharp_movement"] and sharp["direction"] and sharp["direction"] != best_outcome:
            if sharp["max_drop_pct"] > 15:
                best_outcome = sharp["direction"]

        # Confidence ceiling 0.70
        base = 0.40 + (momentum_gap * 0.5)
        sharp_boost = 0.10 if (sharp["has_sharp_movement"] and sharp["direction"] == best_outcome) else 0.0
        confidence = min(base + sharp_boost, 0.70)
        justification = (
            f"Recommendation: {match.get('home_team' if best_outcome == '1' else 'away_team', '?')} ({best_outcome}). "
            f"Based on form analysis (momentum gap: {momentum_gap:.0%})."
        )
        # No edge/true_prob for fallback

    imp_prob = implied.get(best_outcome, 0.5)

    return {
        "match_id": match_id,
        "sport_key": sport_key,
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "match_date": match["match_date"],
        "recommended_selection": best_outcome,
        "confidence": round(confidence, 3),
        "edge_pct": round(edge_pct, 2),
        "true_probability": round(true_prob, 4),
        "implied_probability": round(imp_prob, 4),
        "expected_goals_home": 0.0,
        "expected_goals_away": 0.0,
        "tier_signals": {
            "poisson": None,
            "margin_model": margin_signals,  # New signal key
            "momentum": {
                "home": home_momentum,
                "away": away_momentum,
                "gap": round(momentum_gap, 3),
                "contributes": momentum_gap > 0.20,
            },
            "sharp_movement": sharp,
            "kings_choice": kings,
            "btb": {
                "home": evd_home,
                "away": evd_away,
            },
            "rest_advantage": rest_signals,
        },
        "justification": justification,
        "status": "active",
        "actual_result": None,
        "was_correct": None,
        "generated_at": utcnow(),
    }


# ---------------------------------------------------------------------------
# Resolution helper
# ---------------------------------------------------------------------------

_OUTCOME_TO_1X2 = {"H": "1", "D": "X", "A": "2"}


def resolve_tip(tip: dict, match: dict) -> dict:
    """Resolve a tip against the match result."""
    outcome = match.get("result", {}).get("outcome")
    if not outcome or tip.get("status") == "no_signal":
        tip["status"] = "no_signal"
        return tip

    # Normalize H/D/A → 1/X/2 for comparison
    normalized = _OUTCOME_TO_1X2.get(outcome, outcome)

    tip["actual_result"] = normalized
    tip["was_correct"] = tip["recommended_selection"] == normalized
    tip["status"] = "resolved"
    tip["resolved_at"] = utcnow()
    return tip