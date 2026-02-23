"""QuoticoTip EV Engine — 3-tier hybrid scoring model.

Combines Poisson-based true probabilities, form/momentum scoring,
sharp line-movement detection, and community consensus ("King's Choice")
to identify value bets where bookmaker odds are mispriced.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel

import app.database as _db
from app.services.historical_service import (
    build_match_context,
    resolve_team_key,
    sport_keys_for,
)
from app.utils import utcnow

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
MIN_KINGS_TIPPED = 5         # Minimum kings who tipped for King's Choice
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


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class QuoticoTipResponse(BaseModel):
    match_id: str
    sport_key: str
    teams: dict[str, str]
    commence_time: datetime
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


def _no_signal_tip(match: dict, reason: str) -> dict:
    """Build a stored tip with status 'no_signal' and skip_reason."""
    return {
        "match_id": str(match["_id"]),
        "sport_key": match["sport_key"],
        "teams": match["teams"],
        "match_commence_time": match["commence_time"],
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
# Tier 1: Poisson-based true probability
# ---------------------------------------------------------------------------

def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (λ^k * e^(-λ)) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k * math.exp(-lam)) / math.factorial(k)


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


async def _get_league_averages(
    related_keys: list[str],
) -> tuple[float, float] | None:
    """Compute league average home/away goals from last 2 seasons of historical data.

    Returns (avg_home_goals, avg_away_goals) or None if insufficient data.
    """
    pipeline = [
        {"$match": {"sport_key": {"$in": related_keys}}},
        {"$sort": {"match_date": -1}},
        {"$limit": 1000},  # ~2 seasons of top-flight football
        {"$group": {
            "_id": None,
            "total_home": {"$sum": "$home_goals"},
            "total_away": {"$sum": "$away_goals"},
            "count": {"$sum": 1},
        }},
    ]
    results = await _db.db.historical_matches.aggregate(pipeline).to_list(length=1)
    if not results or results[0]["count"] < 50:
        return None

    count = results[0]["count"]
    return (
        results[0]["total_home"] / count,
        results[0]["total_away"] / count,
    )


async def _get_team_home_matches(
    team_key: str, related_keys: list[str], limit: int = N_TEAM_MATCHES,
) -> list[dict]:
    """Fetch team's last N home matches (goals scored/conceded)."""
    return await _db.db.historical_matches.find(
        {"home_team_key": team_key, "sport_key": {"$in": related_keys}},
        {"_id": 0, "home_goals": 1, "away_goals": 1, "match_date": 1},
    ).sort("match_date", -1).to_list(length=limit)


async def _get_team_away_matches(
    team_key: str, related_keys: list[str], limit: int = N_TEAM_MATCHES,
) -> list[dict]:
    """Fetch team's last N away matches (goals scored/conceded)."""
    return await _db.db.historical_matches.find(
        {"away_team_key": team_key, "sport_key": {"$in": related_keys}},
        {"_id": 0, "home_goals": 1, "away_goals": 1, "match_date": 1},
    ).sort("match_date", -1).to_list(length=limit)


async def compute_poisson_probabilities(
    home_team_key: str,
    away_team_key: str,
    sport_key: str,
    related_keys: list[str],
    *,
    h2h_lambdas: dict | None = None,
) -> Optional[dict]:
    """Compute Poisson-based true probabilities for 1/X/2 outcomes.

    If h2h_lambdas is provided, blends global lambdas with H2H-specific
    lambdas using a sample-size-scaled weight (up to H2H_WEIGHT_MAX).

    Returns dict with lambda_home, lambda_away, prob_home, prob_draw, prob_away
    or None if insufficient data.
    """
    league_avgs = await _get_league_averages(related_keys)
    if not league_avgs:
        return None

    avg_home_goals, avg_away_goals = league_avgs

    if avg_home_goals <= 0 or avg_away_goals <= 0:
        return None

    # Fetch team-specific data
    home_home_matches = await _get_team_home_matches(home_team_key, related_keys)
    away_away_matches = await _get_team_away_matches(away_team_key, related_keys)

    if len(home_home_matches) < MIN_MATCHES_REQUIRED or len(away_away_matches) < MIN_MATCHES_REQUIRED:
        return None

    # Home team attack/defense (from their home matches)
    home_goals_scored = [m["home_goals"] for m in home_home_matches]
    home_goals_conceded = [m["away_goals"] for m in home_home_matches]

    # Away team attack/defense (from their away matches)
    away_goals_scored = [m["away_goals"] for m in away_away_matches]
    away_goals_conceded = [m["home_goals"] for m in away_away_matches]

    home_attack = _weighted_average(home_goals_scored, RECENCY_DECAY) / avg_home_goals
    home_defense = _weighted_average(home_goals_conceded, RECENCY_DECAY) / avg_away_goals

    away_attack = _weighted_average(away_goals_scored, RECENCY_DECAY) / avg_away_goals
    away_defense = _weighted_average(away_goals_conceded, RECENCY_DECAY) / avg_home_goals

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

    lambda_home = min(lambda_home, LAMBDA_CAP)
    lambda_away = min(lambda_away, LAMBDA_CAP)

    # Build scoreline probability matrix
    matrix = [[0.0] * SCORELINE_MAX for _ in range(SCORELINE_MAX)]
    for i in range(SCORELINE_MAX):
        for j in range(SCORELINE_MAX):
            matrix[i][j] = _poisson_pmf(i, lambda_home) * _poisson_pmf(j, lambda_away)

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
    }


def _compute_h2h_lambdas(
    h2h_matches: list[dict], home_key: str, away_key: str,
) -> dict | None:
    """Compute H2H-specific expected goals from direct meetings.

    Applies exponential decay (most recent H2H = highest weight).
    Returns {"lambda_home": float, "lambda_away": float, "count": int}
    or None if fewer than H2H_MIN_MATCHES meetings.
    """
    if len(h2h_matches) < H2H_MIN_MATCHES:
        return None

    sum_home_goals = 0.0
    sum_away_goals = 0.0
    total_weight = 0.0

    for i, m in enumerate(h2h_matches):
        weight = RECENCY_DECAY ** i

        # Adjust perspective: match home_team may be current away_team
        if m.get("home_team_key") == home_key:
            sum_home_goals += m["home_goals"] * weight
            sum_away_goals += m["away_goals"] * weight
        else:
            sum_home_goals += m["away_goals"] * weight
            sum_away_goals += m["home_goals"] * weight

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
) -> dict:
    """Compute weighted form/momentum score for a team.

    Win=3, Draw=1, Loss=0. Weighted by recency and opponent strength.
    Returns dict with momentum_score (0.0 - 1.0), form_points, weighted_form.
    """
    if not form_matches:
        return {"momentum_score": 0.5, "form_points": 0, "weighted_form": 0.0}

    max_possible = 0.0
    weighted_sum = 0.0

    for i, m in enumerate(form_matches):
        hg = m.get("home_goals", 0)
        ag = m.get("away_goals", 0)
        h_key = m.get("home_team_key", "")
        is_home = h_key == team_key

        # Determine result for this team
        if is_home:
            points = 3 if hg > ag else (1 if hg == ag else 0)
        else:
            points = 3 if ag > hg else (1 if ag == hg else 0)

        # Opponent strength weight from historical odds
        opponent_weight = await _get_opponent_strength_weight(m, team_key, related_keys)

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
) -> float:
    """Estimate opponent strength from their historical odds.

    Win vs strong opponent (low odds) → higher weight (~1.5).
    Win vs weak opponent (high odds) → lower weight (~0.7).
    Default 1.0 if no odds data available.
    """
    h_key = match.get("home_team_key", "")
    opponent_key = match.get("away_team_key", "") if h_key == team_key else h_key

    if not opponent_key:
        return 1.0

    # Look up the opponent's recent average odds (as home favorite indicator)
    recent = await _db.db.historical_matches.find_one(
        {
            "sport_key": {"$in": related_keys},
            "$or": [{"home_team_key": opponent_key}, {"away_team_key": opponent_key}],
            "odds": {"$ne": None},
        },
        {"odds": 1},
        sort=[("match_date", -1)],
    )

    if not recent or not recent.get("odds"):
        return 1.0

    # Use first bookmaker's odds to gauge opponent strength
    for _bk, entry in recent["odds"].items():
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
) -> dict:
    """Analyze odds snapshots for significant line movement.

    Detects three signal types from the full snapshot timeline:
    1. Sharp movement — opening vs current odds drop > SHARP_DROP_PCT
    2. Steam move — single-interval drop > STEAM_VELOCITY_PCT (whale action)
    3. Reversal — odds swing direction then snap back (market indecision)
    """
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

    snapshots = await _db.db.odds_snapshots.find(
        {"match_id": match_id},
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
        late_cutoff = commence_time - timedelta(hours=LATE_MONEY_HOURS)
        late_snapshots = [s for s in snapshots if s["snapshot_at"] >= late_cutoff]
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

async def compute_kings_choice(match_id: str) -> dict:
    """Query tips from top-10% leaderboard users for this match."""
    default = {
        "has_kings_choice": False,
        "kings_pick": None,
        "kings_pct": 0.0,
        "total_kings": 0,
        "kings_who_tipped": 0,
        "is_underdog_pick": False,
    }

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

    # Get their tips for this match
    king_tips = await _db.db.tips.find(
        {"match_id": match_id, "user_id": {"$in": king_ids}},
        {"selection": 1, "locked_odds": 1},
    ).to_list(length=len(king_ids))

    if len(king_tips) < MIN_KINGS_TIPPED:
        return default

    # Count selections
    counts: dict[str, int] = {}
    for tip in king_tips:
        sel = tip["selection"]["value"]
        counts[sel] = counts.get(sel, 0) + 1

    # Find dominant pick
    best_pick = max(counts, key=lambda k: counts[k])
    best_count = counts[best_pick]
    agreement = best_count / len(king_tips)

    # Check if it's an underdog pick
    match_doc = await _db.db.matches.find_one(
        {"_id": {"$regex": ""}},  # placeholder — we query by string match_id
    )
    # More direct approach: look at locked_odds of these tips
    avg_locked = sum(
        t["locked_odds"] for t in king_tips if t["selection"]["value"] == best_pick
    ) / best_count if best_count > 0 else 0
    is_underdog = avg_locked > UNDERDOG_ODDS_THRESHOLD

    if agreement < KINGS_AGREEMENT_PCT:
        return default

    return {
        "has_kings_choice": True,
        "kings_pick": best_pick,
        "kings_pct": round(agreement, 2),
        "total_kings": len(king_ids),
        "kings_who_tipped": len(king_tips),
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
    """Compute how much a team outperforms market expectations.

    For each of the last *n* matches with odds data:
      implied_prob = 1 / decimal_odds (for this team's outcome)
      actual_value = 1.0 (win) | 0.5 (draw) | 0.0 (loss)
      edge = actual_value - implied_prob

    EVD = mean(edge).  Positive → team consistently beats the books.

    If *before_date* is set, only matches before that date are considered
    (used for backtesting to avoid temporal data leakage).
    """
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
        "odds": {"$exists": True, "$ne": None},
    }
    if before_date:
        query["match_date"] = {"$lt": before_date}

    matches = await _db.db.historical_matches.find(
        query,
        {
            "_id": 0, "home_team_key": 1, "away_team_key": 1,
            "home_goals": 1, "away_goals": 1, "result": 1, "odds": 1,
        },
    ).sort("match_date", -1).to_list(length=n * 2)  # fetch extra, filter below

    edges: list[float] = []
    btb_count = 0

    for m in matches:
        if len(edges) >= n:
            break

        # Determine perspective and extract team odds
        is_home = m.get("home_team_key") == team_key
        odds_dict = m.get("odds", {})

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
        hg = m.get("home_goals", 0)
        ag = m.get("away_goals", 0)
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
) -> float:
    """Combine tier signals into a single confidence score [0.0, 1.0].

    Base: sigmoid of Poisson edge.
    Boosters: momentum, sharp/steam, King's Choice, H2H record, EVD.
    Dampener: reversal detection, negative EVD.
    """
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

    total = base + momentum_boost + sharp_boost + steam_boost + reversal_penalty + kings_boost + h2h_boost + evd_boost
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
) -> str:
    """Build a human-readable German explanation of the tip."""
    team_labels = {
        "1": match["teams"]["home"],
        "X": "Unentschieden",
        "2": match["teams"]["away"],
    }
    pick_label = team_labels[best_outcome]

    prob_key = {"1": "prob_home", "X": "prob_draw", "2": "prob_away"}[best_outcome]
    true_pct = poisson[prob_key] * 100
    implied_pct = implied.get(best_outcome, 0) * 100

    parts = [f"Empfehlung: {pick_label} ({best_outcome})."]
    parts.append(
        f"Modell sieht {true_pct:.0f}% Wahrscheinlichkeit vs. "
        f"{implied_pct:.0f}% der Buchmacher = {edge_pct:.1f}% Kante."
    )
    parts.append(
        f"Erwartete Tore: {poisson['lambda_home']:.1f} – {poisson['lambda_away']:.1f}."
    )

    # H2H context
    if h2h_summary and h2h_summary["total"] >= H2H_MIN_MATCHES:
        parts.append(
            f"Direktvergleich: {h2h_summary['total']} Begegnungen "
            f"({h2h_summary['home_wins']}S/{h2h_summary['draws']}U/{h2h_summary['away_wins']}N, "
            f"Ø {h2h_summary['avg_goals']} Tore)."
        )

    if momentum_gap > 0.20:
        parts.append("Formstärke unterstützt diese Empfehlung.")
    if sharp["has_sharp_movement"] and sharp["direction"] == best_outcome:
        if sharp["is_late_money"]:
            parts.append("Späte Profiwetten bewegen die Quoten in diese Richtung.")
        else:
            parts.append("Professionelle Wetter bewegen die Quoten in diese Richtung.")
    if sharp.get("has_steam_move") and sharp.get("steam_outcome") == best_outcome:
        parts.append("Starke Kurzbewegung der Quoten erkannt (Profisignal).")
    if sharp.get("has_reversal") and sharp.get("reversal_outcome") == best_outcome:
        parts.append("Quotenumkehr erkannt — Markt unsicher über dieses Ergebnis.")
    if kings["has_kings_choice"] and kings["kings_pick"] == best_outcome:
        parts.append(
            f"King's Choice: {kings['kings_pct']*100:.0f}% der Top-Spieler stimmen zu."
        )

    # EVD / Beat the Books
    picked_evd = evd_home if best_outcome == "1" else (evd_away if best_outcome == "2" else None)
    if picked_evd and picked_evd.get("contributes"):
        evd_val = picked_evd["evd"]
        ratio_pct = picked_evd["btb_ratio"] * 100
        if evd_val > EVD_BOOST_THRESHOLD:
            parts.append(
                f"Markt-Kante: {pick_label} übertrifft die Buchmacher-Erwartung "
                f"in {ratio_pct:.0f}% der letzten Spiele (EVD: {evd_val:+.1%}). "
                f"Systematisch unterschätzt — Value-Faktor."
            )
        elif evd_val < EVD_DAMPEN_THRESHOLD:
            parts.append(
                f"Markt-Risiko: {pick_label} enttäuscht regelmäßig gegen die Quoten "
                f"(EVD: {evd_val:+.1%}). Der Markt neigt dazu, dieses Team zu überschätzen."
            )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def generate_quotico_tip(match: dict) -> dict:
    """Generate a QuoticoTip for a single match.

    Always returns a dict ready for DB storage. When no recommendation can
    be made the dict has status='no_signal' and skip_reason explaining why.
    """
    sport_key = match["sport_key"]
    current_odds = match.get("current_odds", {})
    is_three_way = "X" in current_odds

    if not is_three_way:
        # 2-way sports: skip Poisson, use only Tier 2 + 3
        return await _generate_two_way_tip(match)

    related_keys = sport_keys_for(sport_key)
    home_team = match["teams"]["home"]
    away_team = match["teams"]["away"]
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
        return _no_signal_tip(match, f"Team nicht aufgelöst: {', '.join(missing)}")

    # Fetch H2H + form data (needed for both Poisson blend and momentum)
    context = await build_match_context(home_team, away_team, sport_key, h2h_limit=10, form_limit=5)
    h2h_data = context.get("h2h")
    h2h_summary = h2h_data["summary"] if h2h_data else None
    h2h_matches = h2h_data["matches"] if h2h_data else []

    # Compute H2H lambdas for Poisson blend
    h2h_lambdas = _compute_h2h_lambdas(h2h_matches, home_key, away_key) if h2h_matches else None

    # Tier 1: Poisson (with H2H blend when available)
    poisson = await compute_poisson_probabilities(
        home_key, away_key, sport_key, related_keys, h2h_lambdas=h2h_lambdas,
    )
    if not poisson:
        return _no_signal_tip(match, "Zu wenig historische Daten für Poisson-Modell")

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
        return _no_signal_tip(match, f"Kein Value-Bet gefunden (bester Edge: {best_edge:.1f}% < {EDGE_THRESHOLD_PCT}%)")

    # Tier 2: Form & Momentum
    home_form = context.get("home_form") or []
    away_form = context.get("away_form") or []

    home_momentum = await compute_momentum_score(home_key, home_form, related_keys)
    away_momentum = await compute_momentum_score(away_key, away_form, related_keys)
    momentum_gap = abs(home_momentum["momentum_score"] - away_momentum["momentum_score"])

    # Tier 3: Sharp Movement
    commence_time = match.get("commence_time")
    sharp = await detect_sharp_movement(match_id, commence_time)

    # Bonus: King's Choice
    kings = await compute_kings_choice(match_id)

    # EVD: Beat the Books
    evd_home = await compute_team_evd(home_key, related_keys)
    evd_away = await compute_team_evd(away_key, related_keys)

    # Confidence
    confidence = _calculate_confidence(
        best_edge, momentum_gap, home_momentum, away_momentum, sharp, kings, best_outcome,
        h2h_summary=h2h_summary, evd_home=evd_home, evd_away=evd_away,
    )

    # Justification
    justification = _build_justification(
        best_outcome, best_edge, poisson, implied, momentum_gap, sharp, kings, match,
        h2h_summary=h2h_summary, evd_home=evd_home, evd_away=evd_away,
    )

    true_prob = poisson[prob_map[best_outcome]]
    imp_prob = implied.get(best_outcome, 0)

    return {
        "match_id": match_id,
        "sport_key": sport_key,
        "teams": match["teams"],
        "match_commence_time": match["commence_time"],
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
        },
        "justification": justification,
        "status": "active",
        "actual_result": None,
        "was_correct": None,
        "generated_at": utcnow(),
    }


async def _generate_two_way_tip(match: dict) -> dict:
    """Generate a tip for 2-way sports (NFL, NBA) using only Tier 2 + 3.

    No Poisson model — confidence ceiling is lower (0.70).
    """
    sport_key = match["sport_key"]
    current_odds = match.get("current_odds", {})
    match_id = str(match["_id"])
    related_keys = sport_keys_for(sport_key)
    home_team = match["teams"]["home"]
    away_team = match["teams"]["away"]

    home_key = await resolve_team_key(home_team, related_keys)
    away_key = await resolve_team_key(away_team, related_keys)
    if not home_key or not away_key:
        missing = []
        if not home_key:
            missing.append(home_team)
        if not away_key:
            missing.append(away_team)
        return _no_signal_tip(match, f"Team nicht aufgelöst: {', '.join(missing)}")

    # Tier 2: Momentum only
    context = await build_match_context(home_team, away_team, sport_key, h2h_limit=5, form_limit=5)
    home_form = context.get("home_form") or []
    away_form = context.get("away_form") or []

    home_momentum = await compute_momentum_score(home_key, home_form, related_keys)
    away_momentum = await compute_momentum_score(away_key, away_form, related_keys)
    momentum_gap = abs(home_momentum["momentum_score"] - away_momentum["momentum_score"])

    if momentum_gap < 0.15:
        return _no_signal_tip(match, f"Zu geringer Formunterschied ({momentum_gap:.0%} < 15%)")

    # Tier 3: Sharp Movement
    sharp = await detect_sharp_movement(match_id, match.get("commence_time"))

    # EVD: Beat the Books (will mostly be contributes=False for US sports)
    evd_home = await compute_team_evd(home_key, related_keys)
    evd_away = await compute_team_evd(away_key, related_keys)

    # Determine pick from momentum
    if home_momentum["momentum_score"] > away_momentum["momentum_score"]:
        best_outcome = "1"
    else:
        best_outcome = "2"

    # If sharp money disagrees strongly, flip
    if sharp["has_sharp_movement"] and sharp["direction"] and sharp["direction"] != best_outcome:
        if sharp["max_drop_pct"] > 15:
            best_outcome = sharp["direction"]

    implied = normalize_implied_probabilities(current_odds)
    imp_prob = implied.get(best_outcome, 0.5)

    # Confidence: lower ceiling for 2-way (no Poisson backbone)
    base = 0.40 + (momentum_gap * 0.5)  # 0.15 gap → 0.475, 0.30 gap → 0.55
    sharp_boost = 0.10 if (sharp["has_sharp_movement"] and sharp["direction"] == best_outcome) else 0.0

    # EVD boost/dampen for 2-way
    evd_adj = 0.0
    picked_evd = evd_home if best_outcome == "1" else evd_away
    if picked_evd.get("contributes"):
        if picked_evd["evd"] > EVD_BOOST_THRESHOLD:
            evd_adj = EVD_CONFIDENCE_BOOST
        elif picked_evd["evd"] < EVD_DAMPEN_THRESHOLD:
            evd_adj = -EVD_CONFIDENCE_DAMPEN

    confidence = min(base + sharp_boost + evd_adj, 0.70)

    return {
        "match_id": match_id,
        "sport_key": sport_key,
        "teams": match["teams"],
        "match_commence_time": match["commence_time"],
        "recommended_selection": best_outcome,
        "confidence": round(confidence, 3),
        "edge_pct": 0.0,  # No Poisson edge for 2-way
        "true_probability": 0.0,
        "implied_probability": round(imp_prob, 4),
        "expected_goals_home": 0.0,
        "expected_goals_away": 0.0,
        "tier_signals": {
            "poisson": None,
            "momentum": {
                "home": home_momentum,
                "away": away_momentum,
                "gap": round(momentum_gap, 3),
                "contributes": True,
            },
            "sharp_movement": sharp,
            "kings_choice": {"has_kings_choice": False},
            "btb": {
                "home": evd_home,
                "away": evd_away,
            },
        },
        "justification": (
            f"Empfehlung: {match['teams'].get('home' if best_outcome == '1' else 'away', '?')} ({best_outcome}). "
            f"Basiert auf Formanalyse (Momentum-Vorsprung: {momentum_gap:.0%})."
            + (" Profiwetten unterstützen diese Richtung." if sharp_boost else "")
        ),
        "status": "active",
        "actual_result": None,
        "was_correct": None,
        "generated_at": utcnow(),
    }
