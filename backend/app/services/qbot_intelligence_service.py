"""
backend/app/services/qbot_intelligence_service.py

Purpose:
    Qbot intelligence enrichment for tips: Bayesian cluster confidence,
    archetype reasoning, and Kelly-based staking metadata.

Dependencies:
    - app.database
    - app.utils
"""

import logging
import time as _time

from bson import ObjectId

import app.database as _db
from app.utils import utcnow

logger = logging.getLogger("quotico.qbot_intelligence")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRIOR_WIN_RATE = 0.333   # 3-way baseline (same as reliability_service)
PRIOR_STRENGTH = 10      # Lower than reliability's 20 — clusters have less data

# Cluster dimensions: each tip is binned along these axes
# Results in keys like "soccer_epl|sharp|high|strong"

EDGE_BAND_HIGH = 10.0
EDGE_BAND_MID = 5.0
MOMENTUM_GAP_STRONG = 0.3

# Archetype definitions: (key, i18n_key)
ARCHETYPES = [
    ("value_oracle",    "qbot.reasoning.valueOracle"),
    ("sharp_hunter",    "qbot.reasoning.sharpHunter"),
    ("momentum_rider",  "qbot.reasoning.momentumRider"),
    ("kings_prophet",   "qbot.reasoning.kingsProphet"),
    ("steam_snatcher",  "qbot.reasoning.steamSnatcher"),  # CLV awareness
    ("contrarian",      "qbot.reasoning.contrarian"),
    ("night_owl",       "qbot.reasoning.nightOwl"),      # Temporal bias
    ("steady_hand",     "qbot.reasoning.steadyHand"),
    ("the_strategist",  "qbot.reasoning.strategist"),  # Player Mode only
]

# DNA gene names (must match arena DNA_GENES order for ensemble indexing)
_DNA_GENES = [
    "min_edge", "min_confidence", "sharp_weight", "momentum_weight",
    "rest_weight", "kelly_fraction", "max_stake",
    "home_bias", "away_bias", "h2h_weight", "draw_threshold",
    "volatility_buffer", "bayes_trust_factor",
]

# League display names for reasoning params
LEAGUE_DISPLAY = {
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_germany_bundesliga2": "2. Bundesliga",
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_portugal_primeira_liga": "Primeira Liga",
}

# Qbot Intelligence 2.0 constants
CLUSTER_VERSION = 2
CLUSTER_MIN_SAMPLES = 5  # Hierarchical fallback threshold

# Volatility binning thresholds
VOLATILITY_STABLE_PCT = 0.03   # < 3%
VOLATILITY_EXTREME_PCT = 0.07  # > 7%

# Market trust factors (multiplicative)
VOLATILITY_DISCOUNT = {
    "stable": 1.0,
    "volatile": 0.95,
    "extreme": 0.85,
    "NA": 0.90,
}

# Stats anomaly thresholds (post-match)
SHOTS_RATIO_EXTREME = 0.65  # > 65% of total shots = dominant
XG_EFFICIENCY_THRESHOLD = 1.3
XG_BETRAYAL_DELTA_THRESHOLD = 0.8

# Qbot Intelligence 2.3 constants
# Temporal binning thresholds (UTC hours)
TEMPORAL_DAY_MAX = 18    # day: < 18:00
TEMPORAL_PRIME_MAX = 22  # prime: 18:00 - 22:00, late: > 22:00

# Market synergy thresholds
SYNERGY_POSITIVE_LINE = 2.5    # totals_line >= 2.5 for positive synergy
SYNERGY_POSITIVE_OVER_PRICE = 1.70  # over_price < 1.70
SYNERGY_NEGATIVE_LINE = 2.0    # totals_line <= 2.0 for negative synergy (low-scoring)
SYNERGY_MULTIPLIER_BOUNDS = (0.90, 1.10)  # min, max multiplier bounds
SYNERGY_POSITIVE_MULTIPLIER = 1.05
SYNERGY_NEGATIVE_MULTIPLIER = 0.95


# ---------------------------------------------------------------------------
# Bayesian smoothing
# ---------------------------------------------------------------------------

def bayesian_win_rate(wins: int, total: int) -> float:
    """Laplace-smoothed win rate with configurable prior."""
    smoothed_wins = wins + PRIOR_STRENGTH * PRIOR_WIN_RATE
    smoothed_total = total + PRIOR_STRENGTH
    return smoothed_wins / smoothed_total


# ---------------------------------------------------------------------------
# Qbot Intelligence 2.3 Helper Functions
# ---------------------------------------------------------------------------

def _get_temporal_dim(match_hour: int | None) -> str:
    """Convert UTC hour to temporal dimension (day/prime/late)."""
    if match_hour is None:
        return "day"  # default
    
    if match_hour < TEMPORAL_DAY_MAX:
        return "day"
    elif match_hour < TEMPORAL_PRIME_MAX:
        return "prime"
    else:
        return "late"


def _get_market_synergy(pick: str, market_ctx: dict) -> float:
    """Calculate market synergy multiplier based on totals correlation.
    
    Positive synergy (1.05): H2H pick + totals_line >= 2.5 + over_price < 1.70
    Negative synergy (0.95): H2H pick + totals_line <= 2.0 (low-scoring environment)
    Default: 1.0 (no synergy)
    
    Returns multiplier bounded between 0.90 and 1.10.
    """
    # Check provider count for totals market (ghost protection)
    totals_provider_count = market_ctx.get("totals_provider_count", 0)
    if totals_provider_count < 1:
        return 1.0  # No data, no synergy
    
    totals_line = market_ctx.get("totals_line")
    totals_over = market_ctx.get("totals_over")
    
    if totals_line is None or totals_over is None:
        return 1.0
    
    try:
        line_val = float(totals_line)
        over_val = float(totals_over)
    except (ValueError, TypeError):
        return 1.0
    
    # Positive synergy: high-scoring environment supports win pick
    if pick in ["1", "2"]:
        if line_val >= SYNERGY_POSITIVE_LINE and over_val < SYNERGY_POSITIVE_OVER_PRICE:
            return SYNERGY_POSITIVE_MULTIPLIER
        
        # Negative synergy: low-scoring environment increases draw risk
        if line_val <= SYNERGY_NEGATIVE_LINE:
            return SYNERGY_NEGATIVE_MULTIPLIER
    
    return 1.0


# ---------------------------------------------------------------------------
# Market context extraction (Qbot Intelligence 2.0)
# ---------------------------------------------------------------------------

def _extract_market_context(match: dict | None, pick: str) -> dict:
    """Extract odds_meta market data + stats from a match document.
    
    Fully null-safe: returns neutral defaults when data is unavailable.
    """
    default = {
        "provider_count": 0,
        "spread_pct": None,
        "volatility_dim": "NA",
        "h2h_current": {},
        "h2h_max": {},
        "h2h_min": {},
        "market_trust_factor": VOLATILITY_DISCOUNT["NA"],
        "stats": None,
    }
    if not match or not isinstance(match, dict):
        return default

    odds_meta = match.get("odds_meta") or {}
    markets = odds_meta.get("markets") or {}
    h2h_node = markets.get("h2h") or {}
    
    # Totals market data
    totals_node = markets.get("totals") or {}
    totals_current = totals_node.get("current") or {}
    totals_provider_count = int(totals_node.get("provider_count") or 0)

    h2h_current = h2h_node.get("current") or {}
    h2h_max = h2h_node.get("max") or {}
    h2h_min = h2h_node.get("min") or {}
    provider_count = int(h2h_node.get("provider_count") or 0)

    # Compute spread for the pick's outcome
    spread_pct: float | None = None
    volatility_dim = "NA"

    current_val = h2h_current.get(pick)
    max_val = h2h_max.get(pick)
    min_val = h2h_min.get(pick)

    if (
        current_val is not None
        and max_val is not None
        and min_val is not None
        and float(current_val) > 0
    ):
        spread_pct = (float(max_val) - float(min_val)) / float(current_val)
        if spread_pct < VOLATILITY_STABLE_PCT:
            volatility_dim = "stable"
        elif spread_pct < VOLATILITY_EXTREME_PCT:
            volatility_dim = "volatile"
        else:
            volatility_dim = "extreme"

    # Market trust factor: provider depth × volatility discount
    provider_factor = min(1.0, provider_count / 2.0) if provider_count > 0 else 0.5
    vol_discount = VOLATILITY_DISCOUNT.get(volatility_dim, 0.90)
    market_trust_factor = round(provider_factor * vol_discount, 4)

    # Extract stats from match
    stats = match.get("stats") if isinstance(match.get("stats"), dict) else None
    result_node = match.get("result") if isinstance(match.get("result"), dict) else {}

    def _safe_float(value: object, fallback: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return fallback
    
    # Temporal context extraction
    match_date_hour = match.get("match_date_hour")
    match_hour = None
    is_weekend = True  # default to True (conservative)
    is_midweek = False
    temporal_dim = "day"  # default
    
    if match_date_hour:
        # Ensure UTC handling (match_date_hour should already be UTC)
        try:
            # match_date_hour could be datetime or string, we'll assume it's a datetime
            # with hour attribute, or we can parse it
            if hasattr(match_date_hour, 'hour'):
                match_hour = match_date_hour.hour
                weekday = match_date_hour.weekday()  # Monday = 0, Sunday = 6
                is_weekend = weekday >= 5  # Saturday (5) or Sunday (6)
                is_midweek = weekday in [1, 2]  # Tuesday (1) or Wednesday (2)
                temporal_dim = _get_temporal_dim(match_hour)
        except (AttributeError, TypeError):
            # Fallback if match_date_hour is not a datetime
            pass

    return {
        "provider_count": provider_count,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "volatility_dim": volatility_dim,
        "h2h_current": h2h_current,
        "h2h_max": h2h_max,
        "h2h_min": h2h_min,
        "market_trust_factor": market_trust_factor,
        "xg_home": _safe_float(result_node.get("home_xg")),
        "xg_away": _safe_float(result_node.get("away_xg")),
        "stats": {
            "shots_home": stats.get("shots_home") if stats else None,
            "shots_away": stats.get("shots_away") if stats else None,
            "cards_yellow_home": int(stats.get("cards_yellow_home", 0) or 0) if stats else 0,
            "cards_yellow_away": int(stats.get("cards_yellow_away", 0) or 0) if stats else 0,
            "cards_red_home": int(stats.get("cards_red_home", 0) or 0) if stats else 0,
            "cards_red_away": int(stats.get("cards_red_away", 0) or 0) if stats else 0,
            "red_card_minute": stats.get("red_card_minute") if stats else None,  # Future timestamp support
            "corners_home": int(stats.get("corners_home", 0) or 0) if stats else 0,
            "corners_away": int(stats.get("corners_away", 0) or 0) if stats else 0,
            "fouls_home": int(stats.get("fouls_home", 0) or 0) if stats else 0,
            "fouls_away": int(stats.get("fouls_away", 0) or 0) if stats else 0,
            "xg_home": _safe_float(result_node.get("home_xg")),
            "xg_away": _safe_float(result_node.get("away_xg")),
        },
        "market_move": {
            "opening": h2h_node.get("opening", {}).get(pick),
            "current": h2h_node.get("current", {}).get(pick),
        },
        # Qbot Intelligence 2.3 additions
        "totals_line": totals_current.get("line"),
        "totals_over": totals_current.get("over"),
        "totals_provider_count": totals_provider_count,
        "match_hour": match_hour,
        "temporal_dim": temporal_dim,
        "is_weekend": is_weekend,
        "is_midweek": is_midweek,
    }


# ---------------------------------------------------------------------------
# Cluster key computation
# ---------------------------------------------------------------------------

def compute_cluster_key(tip: dict, market_ctx: dict | None = None) -> str:
    """Compute the signal cluster key for a tip document.
    
    v3 format: {sport_key}|{sharp_dim}|{edge_dim}|{momentum_dim}|{volatility_dim}|{temporal_dim}
    """
    sport_key = tip.get("sport_key", "unknown")
    signals = tip.get("tier_signals", {})

    # Sharp dimension
    sharp = signals.get("sharp_movement", {})
    sharp_dim = "sharp" if sharp.get("has_sharp_movement") else "no_sharp"

    # Edge band dimension
    edge = tip.get("edge_pct", 0.0)
    if edge > EDGE_BAND_HIGH:
        edge_dim = "high"
    elif edge > EDGE_BAND_MID:
        edge_dim = "mid"
    else:
        edge_dim = "low"

    # Momentum dimension
    momentum = signals.get("momentum", {})
    gap = momentum.get("gap", 0.0)
    momentum_dim = "strong" if gap > MOMENTUM_GAP_STRONG else "weak"

    # Volatility dimension (from market context)
    volatility_dim = "NA"
    if market_ctx:
        volatility_dim = market_ctx.get("volatility_dim", "NA")

    # Temporal dimension (Qbot Intelligence 2.3)
    temporal_dim = "day"  # default
    if market_ctx:
        temporal_dim = market_ctx.get("temporal_dim", "day")

    return f"{sport_key}|{sharp_dim}|{edge_dim}|{momentum_dim}|{volatility_dim}|{temporal_dim}"


def _lookup_cluster_with_fallback(
    cluster_key: str, cluster_stats: dict[str, dict],
) -> tuple[float, int, str]:
    """Look up Bayesian confidence with hierarchical fallback.
    
    v3 cluster key format: {sport}|{sharp}|{edge}|{momentum}|{volatility}|{temporal}
    
    Fallback hierarchy:
    1. Try full 6-dim key
    2. Strip temporal (6th dimension) -> 5-dim key
    3. Strip volatility (5th dimension) -> 4-dim key  
    4. Ultimate fallback: pure prior
    
    Returns (bayesian_confidence, sample_size, used_key).
    """
    cluster = cluster_stats.get(cluster_key, {})
    total = cluster.get("total", 0)

    if total >= CLUSTER_MIN_SAMPLES:
        return (
            bayesian_win_rate(cluster.get("wins", 0), total),
            total,
            cluster_key,
        )

    # Hierarchical fallback levels
    fallback_levels = []
    
    # Level 1: Strip temporal dimension (6th dimension)
    if "|" in cluster_key:
        parts = cluster_key.rsplit("|", 1)  # Strip last dimension (temporal)
        if len(parts) == 2:
            fallback_levels.append(parts[0])  # 5-dim key without temporal
    
    # Level 2: Strip volatility dimension (5th dimension, now last)
    for parent_key in fallback_levels:
        if "|" in parent_key:
            grandparent_parts = parent_key.rsplit("|", 1)
            if len(grandparent_parts) == 2:
                fallback_levels.append(grandparent_parts[0])  # 4-dim key without volatility
    
    # Try each fallback level in order (most specific to least)
    for fallback_key in fallback_levels:
        # Aggregate all clusters that start with this prefix
        agg_wins = 0
        agg_total = 0
        for k, v in cluster_stats.items():
            if k.startswith(fallback_key + "|"):
                agg_wins += v.get("wins", 0)
                agg_total += v.get("total", 0)
        if agg_total >= CLUSTER_MIN_SAMPLES:
            return (
                bayesian_win_rate(agg_wins, agg_total),
                agg_total,
                f"{fallback_key}|*",
            )

    # Ultimate fallback: pure prior
    return bayesian_win_rate(0, 0), 0, cluster_key


# ---------------------------------------------------------------------------
# Strategy + cluster cache (in-memory, 1h TTL)
# ---------------------------------------------------------------------------

_strategy_cache: dict[str, dict] = {}   # keyed by sport_key
_strategy_expires: float = 0.0

_cluster_cache: dict[str, dict] = {}
_cluster_expires: float = 0.0

_CACHE_TTL = 3600  # 1 hour


async def _get_active_strategy(sport_key: str = "all") -> dict | None:
    """Load the active evolved strategy from DB (cached per sport_key)."""
    global _strategy_cache, _strategy_expires

    now = _time.time()
    if now >= _strategy_expires:
        try:
            docs = await _db.db.qbot_strategies.find(
                {"is_active": True},
            ).sort("created_at", -1).to_list(100)
            _strategy_cache = {}
            for doc in docs:
                sk = doc.get("sport_key", "all")
                if sk not in _strategy_cache:  # first = most recent
                    _strategy_cache[sk] = doc
        except Exception:
            logger.warning("Failed to load qbot_strategies", exc_info=True)
        _strategy_expires = now + _CACHE_TTL

    # Sport-specific first, fallback to "all"
    return _strategy_cache.get(sport_key) or _strategy_cache.get("all")


async def _get_cluster_stats() -> dict[str, dict]:
    """Load all pre-computed cluster stats (cached)."""
    global _cluster_cache, _cluster_expires

    now = _time.time()
    if now < _cluster_expires and _cluster_cache:
        return _cluster_cache

    try:
        docs = await _db.db.qbot_cluster_stats.find({}).to_list(length=5000)
        _cluster_cache = {d["_id"]: d for d in docs}
    except Exception:
        logger.warning("Failed to load qbot_cluster_stats", exc_info=True)

    _cluster_expires = now + _CACHE_TTL
    return _cluster_cache


# ---------------------------------------------------------------------------
# Archetype selection
# ---------------------------------------------------------------------------

def _select_archetype(tip: dict, market_ctx: dict | None = None) -> tuple[str, str]:
    """Determine the dominant archetype from signal patterns.

    Returns (archetype_key, i18n_reasoning_key).
    """
    signals = tip.get("tier_signals", {})
    pick = tip.get("recommended_selection")
    edge = tip.get("edge_pct", 0.0)
    confidence = tip.get("confidence", 0.0)

    sharp = signals.get("sharp_movement", {})
    momentum = signals.get("momentum", {})
    kings = signals.get("kings_choice", {})

    # Priority-ordered archetype checks

    # 1. Value Oracle: high edge + confidence + market ceiling check
    if edge > 12.0 and confidence > 0.65:
        is_near_ceiling = True  # default: pass if no market data
        if market_ctx and market_ctx.get("h2h_current") and market_ctx.get("h2h_max"):
            current_val = market_ctx["h2h_current"].get(pick)
            max_val = market_ctx["h2h_max"].get(pick)
            provider_count = market_ctx.get("provider_count", 0)
            if current_val and max_val and provider_count >= 2:
                is_near_ceiling = float(current_val) >= float(max_val) * 0.98
            elif current_val and max_val:
                is_near_ceiling = True  # low providers → don't block, just can't confirm
        if is_near_ceiling:
            return "value_oracle", "qbot.reasoning.valueOracle"

    # 2. Sharp Hunter: sharp movement agrees with pick
    if sharp.get("has_sharp_movement") and sharp.get("direction") == pick:
        return "sharp_hunter", "qbot.reasoning.sharpHunter"

    # 3. Momentum Rider: strong form gap agrees with pick
    gap = momentum.get("gap", 0.0)
    if gap > 0.3:
        home_m = momentum.get("home", {}).get("momentum_score", 0.5)
        away_m = momentum.get("away", {}).get("momentum_score", 0.5)
        if (pick == "1" and home_m > away_m) or (pick == "2" and away_m > home_m):
            return "momentum_rider", "qbot.reasoning.momentumRider"

    # 4. King's Prophet: community consensus
    if kings.get("has_kings_choice") and kings.get("kings_pick") == pick:
        return "kings_prophet", "qbot.reasoning.kingsProphet"

    # 5. Steam Snatcher: CLV awareness - significant market movement
    if market_ctx and market_ctx.get("market_move"):
        opening = market_ctx["market_move"].get("opening")
        current = market_ctx["market_move"].get("current")
        if opening and current and float(current) < float(opening) * 0.90:
            # Quote ist um mehr als 10% gefallen -> Markt-Vertrauen
            return "steam_snatcher", "qbot.reasoning.steamSnatcher"

    # 6. Contrarian: pick disagrees with market favorite
    imp = tip.get("implied_probability", 0.33)
    if imp < 0.50:  # our pick is the underdog (implied < 50%)
        return "contrarian", "qbot.reasoning.contrarian"

    # 7. Night Owl: temporal bias for late games (>= 22:00 UTC)
    if market_ctx:
        match_hour = market_ctx.get("match_hour")
        if match_hour is not None and match_hour >= 22:
            return "night_owl", "qbot.reasoning.nightOwl"

    # 8. Steady Hand: fallback — balanced multi-signal
    return "steady_hand", "qbot.reasoning.steadyHand"


def _compute_post_match_reasoning(
    tip: dict, stats: dict | None,
) -> dict | None:
    """Compute post-match anomaly reasoning with discipline validation and pressure/flow analytics."""
    if not stats or tip.get("was_correct") is None:
        return None

    pick = tip.get("recommended_selection", "-")
    was_correct = tip.get("was_correct", False)
    
    red_home = stats.get("cards_red_home", 0)
    red_away = stats.get("cards_red_away", 0)

    # 1. Check for Discipline Collapse (Single Team)
    if not was_correct:
        if (pick == "1" and red_home > 0 and red_away == 0) or (pick == "2" and red_away > 0 and red_home == 0):
            return {
                "type": "discipline_collapse",
                "red_cards": red_home if pick == "1" else red_away,
                "interpretation": "A red card invalidated the pre-match statistical edge. Performance was impaired by discipline issues."
            }

    # 2. Check for Total Collapse (Both Teams)
    if red_home > 0 and red_away > 0:
        return {
            "type": "total_collapse", 
            "red_cards_home": red_home,
            "red_cards_away": red_away,
            "interpretation": "Both teams received red cards, neutralizing numerical advantage. Falling back to statistical analysis."
        }

    # 3. xG-first post-match checks (Qbot Intelligence 2.4)
    try:
        xg_home = float(stats.get("xg_home", 0) or 0)
    except (TypeError, ValueError):
        xg_home = 0.0
    try:
        xg_away = float(stats.get("xg_away", 0) or 0)
    except (TypeError, ValueError):
        xg_away = 0.0

    goals_home = None
    goals_away = None
    actual_result = tip.get("actual_result")
    if isinstance(actual_result, str) and ":" in actual_result:
        parts = actual_result.split(":", 1)
        try:
            goals_home = int(parts[0].strip())
            goals_away = int(parts[1].strip())
        except (TypeError, ValueError):
            goals_home = None
            goals_away = None

    if goals_home is None or goals_away is None:
        try:
            goals_home = int(stats.get("goals_home")) if stats.get("goals_home") is not None else None
            goals_away = int(stats.get("goals_away")) if stats.get("goals_away") is not None else None
        except (TypeError, ValueError):
            goals_home = None
            goals_away = None

    if (xg_home > 0 or xg_away > 0) and goals_home is not None and goals_away is not None:
        efficiency_home = goals_home / max(xg_home, 0.01)
        efficiency_away = goals_away / max(xg_away, 0.01)
        max_eff = max(efficiency_home, efficiency_away)
        if max_eff > XG_EFFICIENCY_THRESHOLD:
            return {
                "type": "clinical_efficiency",
                "xg_home": round(xg_home, 3),
                "xg_away": round(xg_away, 3),
                "goals_home": goals_home,
                "goals_away": goals_away,
                "efficiency_home": round(efficiency_home, 3),
                "efficiency_away": round(efficiency_away, 3),
                "efficient_team": "home" if efficiency_home >= efficiency_away else "away",
                "interpretation": "Clinical finishing exceeded chance quality expectations.",
            }

        xg_delta = abs(xg_home - xg_away)
        if xg_delta > XG_BETRAYAL_DELTA_THRESHOLD:
            expected_winner = "home" if xg_home > xg_away else "away"
            if goals_home > goals_away:
                actual_outcome = "home"
            elif goals_away > goals_home:
                actual_outcome = "away"
            else:
                actual_outcome = "draw"
            if expected_winner != actual_outcome:
                return {
                    "type": "xg_betrayal",
                    "xg_home": round(xg_home, 3),
                    "xg_away": round(xg_away, 3),
                    "xg_delta": round(xg_delta, 3),
                    "goals_home": goals_home,
                    "goals_away": goals_away,
                    "expected_winner": expected_winner,
                    "actual_outcome": actual_outcome,
                    "interpretation": "Superior chance quality did not convert into the expected result.",
                }

    # 4. Pressure & Flow Analytics (Qbot Intelligence 2.2)
    shots_home = stats.get("shots_home", 0)
    shots_away = stats.get("shots_away", 0)
    corners_home = stats.get("corners_home", 0)
    corners_away = stats.get("corners_away", 0)
    fouls_home = stats.get("fouls_home", 0)
    fouls_away = stats.get("fouls_away", 0)
    
    # 4a. Pressure Index Check
    pressure_home = (shots_home or 0) + (corners_home * 1.5)
    pressure_away = (shots_away or 0) + (corners_away * 1.5)
    
    if not was_correct:
        # Siege Failure: High pressure but no win
        if pick == "1" and pressure_home > pressure_away * 1.8:
            return {
                "type": "siege_failure",
                "pressure_home": pressure_home,
                "pressure_away": pressure_away,
                "interpretation": "High pressure (shots + corners) didn't convert into goals. Dominant performance, poor finishing."
            }
        elif pick == "2" and pressure_away > pressure_home * 1.8:
            return {
                "type": "siege_failure",
                "pressure_home": pressure_home,
                "pressure_away": pressure_away,
                "interpretation": "High pressure (shots + corners) didn't convert into goals. Dominant performance, poor finishing."
            }
    
    # 4b. Game Flow Check (Fouls)
    total_fouls = (fouls_home or 0) + (fouls_away or 0)
    if total_fouls > 30 and not was_correct:
        return {
            "type": "disrupted_flow",
            "fouls_home": fouls_home,
            "fouls_away": fouls_away,
            "interpretation": "Extremely high foul count (30+) disrupted the game flow, penalizing the technical advantage."
        }

    # 5. Statistical Anomaly Detection (Shots) - Only if no discipline/xG reason exists
    if shots_home is not None and shots_away is not None:
        total_shots = shots_home + shots_away
        if total_shots >= 5:
            home_ratio = shots_home / total_shots
            
            # Dominant team lost (but check for red cards first)
            if pick == "1" and home_ratio > SHOTS_RATIO_EXTREME and not was_correct:
                return {"type": "home_dominant_lost", "interpretation": "Statistically correct tip — result was an outlier (shots dominance)."}
            elif pick == "2" and (1 - home_ratio) > SHOTS_RATIO_EXTREME and not was_correct:
                return {"type": "away_dominant_lost", "interpretation": "Statistically correct tip — result was an outlier (shots dominance)."}
            
            # Underdog won (Lucky result)
            elif pick == "1" and home_ratio < (1 - SHOTS_RATIO_EXTREME) and was_correct:
                return {"type": "home_underdog_won", "interpretation": "Lucky result — stats didn't support the pick."}
            elif pick == "2" and (1 - home_ratio) < (1 - SHOTS_RATIO_EXTREME) and was_correct:
                return {"type": "away_underdog_won", "interpretation": "Lucky result — stats didn't support the pick."}

    return None


# ---------------------------------------------------------------------------
# Kelly stake calculation
# ---------------------------------------------------------------------------

def _compute_kelly_stake_single(
    tip: dict, dna: dict, market_ctx: dict | None = None,
) -> tuple[float, float]:
    """Compute Kelly-based stake from a single DNA dict.

    Returns (stake_units, kelly_raw).
    """
    kelly_f = dna.get("kelly_fraction", 0.25)
    max_stake = dna.get("max_stake", 50.0)
    vol_buffer = dna.get("volatility_buffer", 0.0)
    bayes_trust = dna.get("bayes_trust_factor", 0.0)
    home_bias = dna.get("home_bias", 1.0)
    away_bias = dna.get("away_bias", 1.0)
    draw_thresh = dna.get("draw_threshold", 0.0)

    confidence = tip.get("confidence", 0.0)
    pick = tip.get("recommended_selection")

    # Venue bias
    if pick == "1":
        confidence *= home_bias
    elif pick == "2":
        confidence *= away_bias

    # Draw gate
    if pick == "X" and confidence < draw_thresh:
        return 0.0, 0.0

    # Bayesian blending
    bayes_conf = (tip.get("qbot_logic") or {}).get("bayesian_confidence")
    if bayes_conf is not None and bayes_trust > 0:
        blend = min(bayes_trust * 0.5, 0.75)
        confidence = (1.0 - blend) * confidence + blend * bayes_conf

    confidence = min(confidence, 0.99)
    implied_prob = tip.get("implied_probability", 0.33)
    odds = (1.0 / implied_prob) if implied_prob > 0.01 else 10.0

    # Kelly with volatility buffer (DNA-evolved)
    edge = confidence - implied_prob - vol_buffer
    denom = max(odds - 1.0, 0.01)
    kelly_raw = kelly_f * max(edge, 0.0) / denom

    # Apply multiplicative market trust factor
    if market_ctx:
        kelly_raw *= market_ctx.get("market_trust_factor", 1.0)

    # Qbot Intelligence 2.3: Apply market synergy multiplier
    if market_ctx:
        synergy_multiplier = _get_market_synergy(pick, market_ctx)
        # Bound the multiplier to prevent extreme values
        min_bound, max_bound = SYNERGY_MULTIPLIER_BOUNDS
        synergy_multiplier = max(min_bound, min(max_bound, synergy_multiplier))
        kelly_raw *= synergy_multiplier

    stake = min(kelly_raw, max_stake)
    return round(stake, 2), round(kelly_raw, 4)


def _compute_kelly_stake(
    tip: dict, strategy: dict, market_ctx: dict | None = None,
) -> tuple[float, float]:
    """Compute Kelly-based stake, supporting ensemble mode.

    Returns (stake_units, kelly_raw).
    """
    ensemble_dna = strategy.get("ensemble_dna")

    if ensemble_dna and len(ensemble_dna) > 1:
        stakes = []
        raws = []
        for dna_list in ensemble_dna:
            dna = {_DNA_GENES[i]: v for i, v in enumerate(dna_list) if i < len(_DNA_GENES)}
            s, r = _compute_kelly_stake_single(tip, dna, market_ctx)
            stakes.append(s)
            raws.append(r)
        stakes.sort()
        raws.sort()
        mid = len(stakes) // 2
        median_stake = stakes[mid] if len(stakes) % 2 else (stakes[mid - 1] + stakes[mid]) / 2
        median_raw = raws[mid] if len(raws) % 2 else (raws[mid - 1] + raws[mid]) / 2
        return round(median_stake, 2), round(median_raw, 4)
    else:
        dna = strategy.get("dna", {})
        return _compute_kelly_stake_single(tip, dna, market_ctx)


def _effective_dna_from_strategy(strategy: dict) -> tuple[dict, str]:
    """Return effective DNA dict used for trace calculations."""
    ensemble_dna = strategy.get("ensemble_dna")
    if ensemble_dna and len(ensemble_dna) > 0:
        matrix = []
        for dna_list in ensemble_dna:
            row = [float(dna_list[i]) if i < len(dna_list) else 0.0 for i in range(len(_DNA_GENES))]
            matrix.append(row)
        if matrix:
            medians = []
            for i in range(len(_DNA_GENES)):
                col = sorted(row[i] for row in matrix)
                mid = len(col) // 2
                if len(col) % 2:
                    med = col[mid]
                else:
                    med = (col[mid - 1] + col[mid]) / 2
                medians.append(med)
            return ({_DNA_GENES[i]: round(float(medians[i]), 6) for i in range(len(_DNA_GENES))}, "ensemble_median")
    return (strategy.get("dna", {}) or {}, "single_dna")


def _build_decision_trace(
    tip: dict,
    strategy: dict | None,
    *,
    bayes_conf: float | None = None,
    stake_units: float | None = None,
    kelly_raw: float | None = None,
    market_ctx: dict | None = None,
) -> dict:
    """Build a forensic decision trace for one tip."""
    edge_pct = float(tip.get("edge_pct", 0.0))
    raw_prob = float(tip.get("true_probability", 0.0))
    raw_conf = float(tip.get("confidence", 0.0))
    implied_prob = float(tip.get("implied_probability", 0.33))
    pick = tip.get("recommended_selection", "-")
    status = tip.get("status")
    skip_reason = tip.get("skip_reason")

    kill_point = None

    if strategy is None:
        if skip_reason:
            kill_point = {
                "stage": 1,
                "code": "engine_skip",
                "reason": skip_reason,
            }
        else:
            kill_point = {
                "stage": 2,
                "code": "no_active_strategy",
                "reason": "No active strategy found for sport/all fallback.",
            }
        return {
            "version": 1,
            "stage_1_engine": {
                "raw_edge_pct": round(edge_pct, 4),
                "raw_probability": round(raw_prob, 4),
                "raw_confidence": round(raw_conf, 4),
                "implied_probability": round(implied_prob, 4),
                "recommended_selection": pick,
                "status": status,
                "skip_reason": skip_reason,
            },
            "stage_2_dna_match": {
                "matched": False,
                "strategy_id": None,
                "strategy_label": None,
                "source": "none",
            },
            "stage_3_filters": None,
            "stage_4_risk": None,
            "kill_point": kill_point,
        }

    dna, dna_source = _effective_dna_from_strategy(strategy)
    min_edge = float(dna.get("min_edge", 0.0))
    min_conf = float(dna.get("min_confidence", 0.0))
    home_bias = float(dna.get("home_bias", 1.0))
    away_bias = float(dna.get("away_bias", 1.0))
    draw_thresh = float(dna.get("draw_threshold", 0.0))
    bayes_trust = float(dna.get("bayes_trust_factor", 0.0))
    vol_buffer = float(dna.get("volatility_buffer", 0.0))
    kelly_fraction = float(dna.get("kelly_fraction", 0.0))
    max_stake = float(dna.get("max_stake", 0.0))

    # Confidence pipeline (mirror of strategy layer)
    conf_adj = raw_conf
    if pick == "1":
        conf_adj *= home_bias
    elif pick == "2":
        conf_adj *= away_bias

    conf_blended = min(conf_adj, 0.99)
    if bayes_conf is not None and bayes_trust > 0:
        blend = min(bayes_trust * 0.5, 0.75)
        conf_blended = (1.0 - blend) * conf_blended + blend * float(bayes_conf)
        conf_blended = min(conf_blended, 0.99)

    min_edge_passed = edge_pct >= min_edge
    min_conf_passed = raw_conf >= min_conf
    is_draw = pick == "X"
    draw_blocked = bool(is_draw and conf_blended < draw_thresh)
    draw_gate_passed = not draw_blocked
    filters_passed = min_edge_passed and min_conf_passed and draw_gate_passed

    denom = max((1.0 / implied_prob) - 1.0, 0.01) if implied_prob > 0.01 else 9.0
    buffered_edge = max(conf_blended - implied_prob - vol_buffer, 0.0)
    calc_kelly_raw = kelly_fraction * buffered_edge / denom
    calc_kelly_raw = max(calc_kelly_raw, 0.0)
    calc_final_stake = min(calc_kelly_raw, max_stake)
    stake_capped = calc_kelly_raw > max_stake

    final_kelly = float(kelly_raw) if kelly_raw is not None else calc_kelly_raw
    final_stake = float(stake_units) if stake_units is not None else calc_final_stake

    stage_info = (strategy.get("optimization_notes", {}) or {}).get("stage_info", {}) or {}
    stage_used = stage_info.get("stage_used")
    if stage_used == 2:
        stage_label = "Stage 2 (Relaxed)"
    elif stage_used == 1:
        stage_label = "Stage 1 (Ideal)"
    else:
        stage_label = f"Stage {stage_used}" if stage_used is not None else "Stage n/a"

    if status == "no_signal" and skip_reason:
        kill_point = {
            "stage": 1,
            "code": "engine_skip",
            "reason": skip_reason,
        }
    elif not min_edge_passed:
        kill_point = {
            "stage": 3,
            "code": "min_edge_failed",
            "reason": f"edge {edge_pct:.2f}% < min_edge {min_edge:.2f}%",
        }
    elif not min_conf_passed:
        kill_point = {
            "stage": 3,
            "code": "min_confidence_failed",
            "reason": f"confidence {raw_conf:.3f} < min_confidence {min_conf:.3f}",
        }
    elif draw_blocked:
        kill_point = {
            "stage": 3,
            "code": "draw_gate_blocked",
            "reason": f"draw confidence {conf_blended:.3f} < draw_threshold {draw_thresh:.3f}",
        }
    elif final_stake <= 0:
        kill_point = {
            "stage": 4,
            "code": "risk_zero_stake",
            "reason": "kelly_raw produced zero stake after risk controls.",
        }

    strategy_id = strategy.get("_id")
    strategy_id_str = str(strategy_id) if strategy_id is not None else None
    strategy_sport = strategy.get("sport_key", "all")
    source = "sport_key" if strategy_sport == tip.get("sport_key") else "fallback_all"

    return {
        "version": 1,
        "stage_1_engine": {
            "raw_edge_pct": round(edge_pct, 4),
            "raw_probability": round(raw_prob, 4),
            "raw_confidence": round(raw_conf, 4),
            "implied_probability": round(implied_prob, 4),
            "recommended_selection": pick,
            "status": status,
            "skip_reason": skip_reason,
        },
        "stage_2_dna_match": {
            "matched": True,
            "strategy_id": strategy_id_str,
            "strategy_label": f"{strategy_sport} {strategy.get('version', 'v1')} - {stage_label}",
            "strategy_version": strategy.get("version", "v1"),
            "strategy_generation": strategy.get("generation", 0),
            "strategy_sport_key": strategy_sport,
            "strategy_state": "active" if strategy.get("is_active", False) else ("shadow" if strategy.get("is_shadow", False) else "inactive"),
            "source": source,
            "stage_used": stage_used,
            "dna_source": dna_source,
        },
        "stage_3_filters": {
            "min_edge": {"required": round(min_edge, 4), "actual": round(edge_pct, 4), "passed": min_edge_passed},
            "min_confidence": {"required": round(min_conf, 4), "actual": round(raw_conf, 4), "passed": min_conf_passed},
            "draw_gate": {
                "required": bool(is_draw),
                "actual_confidence": round(conf_blended, 4),
                "draw_threshold": round(draw_thresh, 4),
                "blocked": draw_blocked,
                "passed": draw_gate_passed,
            },
            "overall_passed": filters_passed,
        },
        "stage_4_risk": {
            "kelly_fraction": round(kelly_fraction, 4),
            "max_stake": round(max_stake, 4),
            "volatility_buffer": round(vol_buffer, 4),
            "bayes_trust_factor": round(bayes_trust, 4),
            "confidence_adjusted": round(conf_blended, 4),
            "buffered_edge": round(buffered_edge, 4),
            "kelly_raw": round(final_kelly, 4),
            "final_stake": round(final_stake, 4),
            "stake_capped": stake_capped,
        },
        "market_context": {
            "provider_count": (market_ctx or {}).get("provider_count", 0),
            "spread_pct": (market_ctx or {}).get("spread_pct"),
            "volatility_dim": (market_ctx or {}).get("volatility_dim", "NA"),
            "market_trust_factor": (market_ctx or {}).get("market_trust_factor", 1.0),
            "xg_home": (market_ctx or {}).get("xg_home", 0.0),
            "xg_away": (market_ctx or {}).get("xg_away", 0.0),
            # Qbot Intelligence 2.3 additions
            "totals_line": (market_ctx or {}).get("totals_line"),
            "totals_over": (market_ctx or {}).get("totals_over"),
            "totals_provider_count": (market_ctx or {}).get("totals_provider_count", 0),
            "match_hour": (market_ctx or {}).get("match_hour"),
            "temporal_dim": (market_ctx or {}).get("temporal_dim", "day"),
            "is_weekend": (market_ctx or {}).get("is_weekend", True),
            "is_midweek": (market_ctx or {}).get("is_midweek", False),
            "synergy_factor": _get_market_synergy(pick, market_ctx) if market_ctx else 1.0,
        } if market_ctx else None,
        "kill_point": kill_point,
    }


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

async def enrich_tip(tip: dict, match: dict | None = None) -> dict:
    """Enrich a tip document with qbot_logic field (dual-mode: investor + player).

    Active tips get both investor enrichment and player prediction.
    No-signal tips get only player prediction (investor fields are empty).
    Gracefully returns the tip unchanged if no active strategy exists.
    
    Args:
        tip: The tip document to enrich.
        match: The associated match document (odds_meta + stats).
               If not provided, lazy-loads via match_id (with warning).
    """
    # Lazy-load match if not passed (callers should pass it)
    if match is None:
        match_id_str = tip.get("match_id")
        if match_id_str:
            try:
                match = await _db.db.matches.find_one(
                    {"_id": ObjectId(match_id_str)},
                    {"odds_meta": 1, "stats": 1, "result": 1},
                )
                logger.warning(
                    "enrich_tip lazy-load for match=%s — caller should pass match",
                    match_id_str,
                )
            except Exception:
                logger.warning("Failed to lazy-load match %s", match_id_str, exc_info=True)

    strategy = await _get_active_strategy(tip.get("sport_key", "all"))
    if not strategy:
        # Still consume transient field
        tip.pop("player_prediction", None)
        tip["decision_trace"] = _build_decision_trace(tip, None)
        return tip

    is_no_signal = tip.get("status") == "no_signal"
    league_display = LEAGUE_DISPLAY.get(
        tip.get("sport_key", ""), tip.get("sport_key", "")
    )

    # Extract market context (null-safe)
    pick = tip.get("recommended_selection", "-")
    market_ctx = _extract_market_context(match, pick)

    # --- Investor enrichment (active tips only) ---
    investor_data = {}
    bayes_conf: float | None = None
    stake_units: float | None = None
    kelly_raw: float | None = None
    if not is_no_signal:
        cluster_stats = await _get_cluster_stats()
        cluster_key = compute_cluster_key(tip, market_ctx)
        bayes_conf, cluster_total, used_cluster_key = _lookup_cluster_with_fallback(
            cluster_key, cluster_stats,
        )

        archetype, reasoning_key = _select_archetype(tip, market_ctx)
        stake_units, kelly_raw = _compute_kelly_stake(tip, strategy, market_ctx)

        signal_name = {
            "value_oracle": "Edge",
            "sharp_hunter": "Sharp Money",
            "momentum_rider": "Momentum",
            "kings_prophet": "King's Choice",
            "contrarian": "Contrarian",
            "steady_hand": "Multi-Signal",
        }.get(archetype, "Multi-Signal")

        investor_data = {
            "archetype": archetype,
            "reasoning_key": reasoning_key,
            "reasoning_params": {
                "generation": strategy.get("generation", 0),
                "cluster_win_rate": round(bayes_conf * 100, 1),
                "league": league_display,
                "signal": signal_name,
                "edge": round(tip.get("edge_pct", 0.0), 1),
                "confidence": round(tip.get("confidence", 0.0) * 100, 1),
            },
            "stake_units": stake_units,
            "kelly_raw": kelly_raw,
            "bayesian_confidence": round(bayes_conf, 4),
            "cluster_key": cluster_key,
            "cluster_key_used": used_cluster_key,
            "cluster_sample_size": cluster_total,
        }

    # --- Player mode enrichment (unchanged) ---
    player_data = None
    player_pred = tip.pop("player_prediction", None)
    if player_pred:
        score = player_pred.get("predicted_score", {})
        score_str = f"{score.get('home', 0)}:{score.get('away', 0)}"
        player_data = {
            "archetype": "the_strategist",
            "reasoning_key": "qbot.reasoning.strategist",
            "reasoning_params": {
                "generation": strategy.get("generation", 0),
                "league": league_display,
                "score": score_str,
                "probability": round(player_pred.get("score_probability", 0.0) * 100, 1),
            },
            "predicted_outcome": player_pred.get("predicted_outcome"),
            "predicted_score": player_pred.get("predicted_score"),
            "score_probability": player_pred.get("score_probability", 0.0),
            "outcome_probability": player_pred.get("outcome_probability", 0.0),
            "is_mandatory_tip": True,
        }

    # --- Post-match reasoning (stats anomaly) ---
    post_match = _compute_post_match_reasoning(tip, market_ctx.get("stats"))

    # --- Build combined qbot_logic ---
    synergy_factor = _get_market_synergy(pick, market_ctx)
    qbot_logic: dict = {
        "strategy_version": strategy.get("version", "v1"),
        **investor_data,
        "market_synergy_factor": round(synergy_factor, 4),
        "market_trust_factor": round(float(market_ctx.get("market_trust_factor", 1.0)), 4),
        "market_context": {
            "volatility_dim": market_ctx.get("volatility_dim", "NA"),
        },
        "is_weekend": bool(market_ctx.get("is_weekend", True)),
        "is_midweek": bool(market_ctx.get("is_midweek", False)),
        "applied_at": utcnow(),
    }
    if player_data:
        qbot_logic["player"] = player_data
    if post_match:
        qbot_logic["post_match_reasoning"] = post_match

    tip["qbot_logic"] = qbot_logic
    tip["decision_trace"] = _build_decision_trace(
        tip, strategy,
        bayes_conf=bayes_conf,
        stake_units=stake_units,
        kelly_raw=kelly_raw,
        market_ctx=market_ctx,
    )
    return tip


# ---------------------------------------------------------------------------
# Daily cluster stats update (called by calibration_worker)
# ---------------------------------------------------------------------------

async def update_cluster_stats() -> dict:
    """Recompute all Bayesian cluster stats from resolved tips.

    Called daily by calibration_worker.  Groups all resolved tips by
    cluster key, computes Bayesian win rate, and upserts to
    qbot_cluster_stats collection.
    """
    from pymongo import UpdateOne

    query = {"status": "resolved", "was_correct": {"$ne": None}}
    projection = {
        "match_id": 1, "sport_key": 1, "edge_pct": 1, "was_correct": 1, "tier_signals": 1,
    }

    tips = await _db.db.quotico_tips.find(query, projection).to_list(length=100_000)
    logger.info("Computing cluster stats from %d resolved tips", len(tips))

    # Batch pre-fetch matches for market context (match_id -> match)
    match_ids = [ObjectId(t["match_id"]) for t in tips if t.get("match_id")]
    matches_by_id: dict[str, dict] = {}
    if match_ids:
        matches = await _db.db.matches.find(
            {"_id": {"$in": match_ids}},
            {"odds_meta": 1, "stats": 1},
        ).to_list(length=len(match_ids))
        matches_by_id = {str(m["_id"]): m for m in matches}

    # Tally wins/total per cluster
    clusters: dict[str, dict] = {}
    for tip in tips:
        pick = tip.get("recommended_selection", "-")
        match = matches_by_id.get(tip.get("match_id"))
        market_ctx = _extract_market_context(match, pick)
        key = compute_cluster_key(tip, market_ctx)
        if key not in clusters:
            clusters[key] = {"wins": 0, "total": 0, "sport_key": tip.get("sport_key")}
        clusters[key]["total"] += 1
        if tip.get("was_correct"):
            clusters[key]["wins"] += 1

    # Build upsert operations
    now = utcnow()
    ops = []
    for key, stats in clusters.items():
        bwr = bayesian_win_rate(stats["wins"], stats["total"])
        ops.append(UpdateOne(
            {"_id": key},
            {"$set": {
                "wins": stats["wins"],
                "total": stats["total"],
                "bayesian_win_rate": round(bwr, 4),
                "sport_key": stats["sport_key"],
                "last_updated": now,
            }},
            upsert=True,
        ))

    if ops:
        result = await _db.db.qbot_cluster_stats.bulk_write(ops)
        logger.info(
            "Cluster stats updated: %d clusters (%d upserted, %d modified)",
            len(ops), result.upserted_count, result.modified_count,
        )

    # Invalidate cache
    global _cluster_cache, _cluster_expires
    _cluster_cache = {}
    _cluster_expires = 0.0

    return {"clusters_updated": len(ops), "total_tips_analyzed": len(tips)}


# ---------------------------------------------------------------------------
# Temporal-safe backfill enrichment (running tally, no future leakage)
# ---------------------------------------------------------------------------

async def backfill_enrich_tips(
    sport_key: str | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Enrich historical tips with qbot_logic using a running tally.

    Iterates tips chronologically.  For each tip, the Bayesian cluster
    stats are computed from ONLY the tips that came before it (temporal
    safety).  This avoids data leakage that would inflate historical
    cluster win rates.
    """
    from pymongo import UpdateOne

    strategy = await _get_active_strategy("all")
    if not strategy:
        logger.error("No active strategy found. Run evolution arena first.")
        return {"error": "no_strategy"}

    query: dict = {"status": "resolved", "was_correct": {"$ne": None}}
    if sport_key:
        query["sport_key"] = sport_key

    projection = {
        "match_id": 1, "sport_key": 1, "edge_pct": 1, "confidence": 1,
        "implied_probability": 1, "recommended_selection": 1,
        "was_correct": 1, "tier_signals": 1, "status": 1, "match_date": 1,
    }

    tips = await _db.db.quotico_tips.find(
        query, projection,
    ).sort("match_date", 1).to_list(length=limit or 100_000)

    logger.info("Backfill enriching %d tips (temporal-safe running tally)", len(tips))

    # Running tally: cluster_key → {wins, total}
    running_tally: dict[str, dict] = {}

    # Batch pre-fetch matches for market context (match_id -> match)
    match_ids = [ObjectId(t["match_id"]) for t in tips if t.get("match_id")]
    matches_by_id: dict[str, dict] = {}
    if match_ids:
        matches = await _db.db.matches.find(
            {"_id": {"$in": match_ids}},
            {"odds_meta": 1, "stats": 1},
        ).to_list(length=len(match_ids))
        matches_by_id = {str(m["_id"]): m for m in matches}

    ops = []
    enriched = 0

    for tip in tips:
        cluster_key = compute_cluster_key(tip)

        # Look up cluster stats from running tally (only past data)
        tally = running_tally.get(cluster_key, {"wins": 0, "total": 0})
        bayes_conf = bayesian_win_rate(tally["wins"], tally["total"])

        # Select archetype
        archetype, reasoning_key = _select_archetype(tip)
        stake_units, kelly_raw = _compute_kelly_stake(tip, strategy)

        league_display = LEAGUE_DISPLAY.get(
            tip.get("sport_key", ""), tip.get("sport_key", "")
        )
        signal_name = {
            "value_oracle": "Edge", "sharp_hunter": "Sharp Money",
            "momentum_rider": "Momentum", "kings_prophet": "King's Choice",
            "contrarian": "Contrarian", "steady_hand": "Multi-Signal",
        }.get(archetype, "Multi-Signal")

        # Extract market context for this tip (null-safe)
        pick = tip.get("recommended_selection", "-")
        match = matches_by_id.get(tip.get("match_id"))
        market_ctx = _extract_market_context(match, pick)

        synergy_factor = _get_market_synergy(pick, market_ctx)
        qbot_logic = {
            "strategy_version": strategy.get("version", "v1"),
            "archetype": archetype,
            "reasoning_key": reasoning_key,
            "reasoning_params": {
                "generation": strategy.get("generation", 0),
                "cluster_win_rate": round(bayes_conf * 100, 1),
                "league": league_display,
                "signal": signal_name,
                "edge": round(tip.get("edge_pct", 0.0), 1),
                "confidence": round(tip.get("confidence", 0.0) * 100, 1),
            },
            "stake_units": stake_units,
            "kelly_raw": kelly_raw,
            "bayesian_confidence": round(bayes_conf, 4),
            "cluster_key": cluster_key,
            "cluster_key_used": cluster_key,
            "cluster_sample_size": tally["total"],
            "market_synergy_factor": round(synergy_factor, 4),
            "market_trust_factor": round(float(market_ctx.get("market_trust_factor", 1.0)), 4),
            "market_context": {
                "volatility_dim": market_ctx.get("volatility_dim", "NA"),
            },
            "is_weekend": bool(market_ctx.get("is_weekend", True)),
            "is_midweek": bool(market_ctx.get("is_midweek", False)),
            "applied_at": utcnow(),
        }

        ops.append(UpdateOne(
            {"_id": tip["_id"]},
            {"$set": {"qbot_logic": qbot_logic}},
        ))
        enriched += 1

        # NOW update the running tally (after using it — temporal safety)
        if cluster_key not in running_tally:
            running_tally[cluster_key] = {"wins": 0, "total": 0}
        running_tally[cluster_key]["total"] += 1
        if tip.get("was_correct"):
            running_tally[cluster_key]["wins"] += 1

        # Batch write every 500
        if len(ops) >= 500 and not dry_run:
            await _db.db.quotico_tips.bulk_write(ops)
            logger.info("  Backfill batch: %d/%d enriched", enriched, len(tips))
            ops = []

    # Final batch
    if ops and not dry_run:
        await _db.db.quotico_tips.bulk_write(ops)

    logger.info(
        "Backfill complete: %d tips enriched, %d clusters tracked",
        enriched, len(running_tally),
    )

    return {
        "enriched": enriched,
        "clusters": len(running_tally),
        "dry_run": dry_run,
    }
