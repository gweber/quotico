"""Qbot Intelligence Service — Bayesian signal clustering + reasoning engine.

Enriches QuoticoTips with:
- Bayesian confidence from historical signal clusters
- Kelly-based stake recommendation from evolved strategy DNA
- Archetypal reasoning strings (i18n key + params)

Cluster stats are pre-computed daily by the calibration worker and cached
in-memory with a 1-hour TTL.  Live tip enrichment is a pure lookup — no
DB queries on the hot path.
"""

import logging
import time as _time
from datetime import datetime, timezone

import app.database as _db

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
    ("contrarian",      "qbot.reasoning.contrarian"),
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


# ---------------------------------------------------------------------------
# Bayesian smoothing
# ---------------------------------------------------------------------------

def bayesian_win_rate(wins: int, total: int) -> float:
    """Laplace-smoothed win rate with configurable prior."""
    smoothed_wins = wins + PRIOR_STRENGTH * PRIOR_WIN_RATE
    smoothed_total = total + PRIOR_STRENGTH
    return smoothed_wins / smoothed_total


# ---------------------------------------------------------------------------
# Cluster key computation
# ---------------------------------------------------------------------------

def compute_cluster_key(tip: dict) -> str:
    """Compute the signal cluster key for a tip document."""
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

    return f"{sport_key}|{sharp_dim}|{edge_dim}|{momentum_dim}"


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

def _select_archetype(tip: dict) -> tuple[str, str]:
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

    # 1. Value Oracle: high edge + high confidence
    if edge > 12.0 and confidence > 0.65:
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

    # 5. Contrarian: pick disagrees with market favorite
    imp = tip.get("implied_probability", 0.33)
    if imp < 0.50:  # our pick is the underdog (implied < 50%)
        return "contrarian", "qbot.reasoning.contrarian"

    # 6. Steady Hand: fallback — balanced multi-signal
    return "steady_hand", "qbot.reasoning.steadyHand"


# ---------------------------------------------------------------------------
# Kelly stake calculation
# ---------------------------------------------------------------------------

def _compute_kelly_stake_single(tip: dict, dna: dict) -> tuple[float, float]:
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

    # Kelly with volatility buffer
    edge = confidence - implied_prob - vol_buffer
    denom = max(odds - 1.0, 0.01)
    kelly_raw = kelly_f * max(edge, 0.0) / denom
    stake = min(kelly_raw, max_stake)

    return round(stake, 2), round(kelly_raw, 4)


def _compute_kelly_stake(tip: dict, strategy: dict) -> tuple[float, float]:
    """Compute Kelly-based stake, supporting ensemble mode.

    Returns (stake_units, kelly_raw).
    """
    ensemble_dna = strategy.get("ensemble_dna")

    if ensemble_dna and len(ensemble_dna) > 1:
        # Ensemble mode: compute Kelly for each bot, take median
        stakes = []
        raws = []
        for dna_list in ensemble_dna:
            dna = {_DNA_GENES[i]: v for i, v in enumerate(dna_list) if i < len(_DNA_GENES)}
            s, r = _compute_kelly_stake_single(tip, dna)
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
        return _compute_kelly_stake_single(tip, dna)


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

async def enrich_tip(tip: dict) -> dict:
    """Enrich a tip document with qbot_logic field (dual-mode: investor + player).

    Active tips get both investor enrichment and player prediction.
    No-signal tips get only player prediction (investor fields are empty).
    Gracefully returns the tip unchanged if no active strategy exists.
    """
    strategy = await _get_active_strategy(tip.get("sport_key", "all"))
    if not strategy:
        # Still consume transient field
        tip.pop("player_prediction", None)
        return tip

    is_no_signal = tip.get("status") == "no_signal"
    league_display = LEAGUE_DISPLAY.get(
        tip.get("sport_key", ""), tip.get("sport_key", "")
    )

    # --- Investor enrichment (active tips only) ---
    investor_data = {}
    if not is_no_signal:
        cluster_stats = await _get_cluster_stats()
        cluster_key = compute_cluster_key(tip)
        cluster = cluster_stats.get(cluster_key, {})

        cluster_wins = cluster.get("wins", 0)
        cluster_total = cluster.get("total", 0)
        bayes_conf = bayesian_win_rate(cluster_wins, cluster_total)

        archetype, reasoning_key = _select_archetype(tip)
        stake_units, kelly_raw = _compute_kelly_stake(tip, strategy)

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
            "cluster_sample_size": cluster_total,
        }

    # --- Player mode enrichment (always, when player_prediction available) ---
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

    # --- Build combined qbot_logic ---
    qbot_logic: dict = {
        "strategy_version": strategy.get("version", "v1"),
        **investor_data,
        "applied_at": datetime.now(timezone.utc),
    }

    if player_data:
        qbot_logic["player"] = player_data

    tip["qbot_logic"] = qbot_logic
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
        "sport_key": 1, "edge_pct": 1, "was_correct": 1, "tier_signals": 1,
    }

    tips = await _db.db.quotico_tips.find(query, projection).to_list(length=100_000)
    logger.info("Computing cluster stats from %d resolved tips", len(tips))

    # Tally wins/total per cluster
    clusters: dict[str, dict] = {}
    for tip in tips:
        key = compute_cluster_key(tip)
        if key not in clusters:
            clusters[key] = {"wins": 0, "total": 0, "sport_key": tip.get("sport_key")}
        clusters[key]["total"] += 1
        if tip.get("was_correct"):
            clusters[key]["wins"] += 1

    # Build upsert operations
    now = datetime.now(timezone.utc)
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
            "cluster_sample_size": tally["total"],
            "applied_at": datetime.now(timezone.utc),
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
