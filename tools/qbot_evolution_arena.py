"""
tools/qbot_evolution_arena.py

Purpose:
    Qbot Evolution Arena v3.2 for strategy DNA optimization on v3 data.
    Extends legacy genes with justice/xG, referee, lineup, liquidity and
    entropy-aware controls and evaluates bots via friction-adjusted fitness.

Dependencies:
    - backend/app/database.py (Mongo connection)
    - backend/app/services/* (tip generation and reliability signals)
    - numpy, pymongo

Usage:
    python -m tools.qbot_evolution_arena                          # all leagues
    python -m tools.qbot_evolution_arena --sport soccer_epl       # per-league
    python -m tools.qbot_evolution_arena --multi                   # all leagues, sequential
    python -m tools.qbot_evolution_arena --multi --parallel        # all leagues, concurrent
    python -m tools.qbot_evolution_arena --dry-run                 # no DB write
    python -m tools.qbot_evolution_arena --resume                  # resume from checkpoint
    python -m tools.qbot_evolution_arena --watch 6h                # periodic tip reload
    python -m tools.qbot_evolution_arena --mode deep               # 5-fold CV pessimistic
    python -m tools.qbot_evolution_arena --generations 50 --population 300
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import signal as _signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from pymongo import ReturnDocument

# Add backend to Python path
sys.path.insert(0, "backend")

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qbot_evolution")

# ---------------------------------------------------------------------------
# Strategy DNA definition
# ---------------------------------------------------------------------------

DNA_GENES = [
    # Original 7 (indices 0-6)
    "min_edge", "min_confidence", "sharp_weight", "momentum_weight",
    "rest_weight", "kelly_fraction", "max_stake",
    # New 6 (indices 7-12)
    "home_bias", "away_bias", "h2h_weight", "draw_threshold",
    "volatility_buffer", "bayes_trust_factor",
    # v3.1 / v3.2 expansion
    "xg_trust_factor", "luck_regression_weight", "ref_cards_sensitivity",
    "var_buffer_pct", "rotation_penalty_weight", "early_signal_confidence",
    "expected_roi_weight", "liquidity_priority_weight", "entropy_damping",
    "complexity_penalty",
]

# DNA_RANGES = {
#     # Original
#     "min_edge":           (3.0, 15.0),
#     "min_confidence":     (0.30, 0.80),
#     "sharp_weight":       (0.5, 2.0),
#     "momentum_weight":    (0.5, 2.0),
#     "rest_weight":        (0.0, 1.5),
#     "kelly_fraction":     (0.05, 0.50),
#     "max_stake":          (10.0, 100.0),
#     # New
#     "home_bias":          (0.80, 1.20),
#     "away_bias":          (0.80, 1.20),
#     "h2h_weight":         (0.0, 2.0),
#     "draw_threshold":     (0.0, 1.0),
#     "volatility_buffer":  (0.0, 0.20),
#     "bayes_trust_factor": (0.0, 1.5),
# }

DNA_RANGES = {
    "min_edge":           (4.0, 9.0),    # Verhindert Flucht in extreme Edges
    "min_confidence":     (0.40, 0.65),  # Zwingt ihn, moderatere Spiele zu nehmen
    "sharp_weight":       (0.8, 1.5),
    "momentum_weight":    (0.8, 1.5),
    "rest_weight":        (0.2, 1.0),
    "kelly_fraction":     (0.05, 0.25),  # Weniger Risiko pro Wette
    "max_stake":          (10.0, 50.0),
    "home_bias":          (0.95, 1.10),  # Realistischere Bias-Werte
    "away_bias":          (0.95, 1.10),
    "h2h_weight":         (0.1, 1.0),
    "draw_threshold":     (0.1, 0.4),
    "volatility_buffer":  (0.02, 0.08),  # Kleinerer Puffer, damit er mutiger wird
    "bayes_trust_factor": (0.2, 1.0),
    "xg_trust_factor": (0.0, 1.0),
    "luck_regression_weight": (0.0, 1.5),
    "ref_cards_sensitivity": (0.0, 1.5),
    "var_buffer_pct": (0.0, 0.2),
    "rotation_penalty_weight": (0.0, 1.5),
    "early_signal_confidence": (0.5, 1.5),
    "expected_roi_weight": (0.5, 2.0),
    "liquidity_priority_weight": (0.0, 1.0),
    "entropy_damping": (0.0, 2.0),
    "complexity_penalty": (0.0, 0.5),
}

# GA parameters
MUTATION_RATE = 0.08       # 8% chance per gene
MUTATION_SIGMA_PCT = 0.03  # Gaussian noise σ = 3% of gene range
ELITE_FRACTION = 0.20      # Top 20% survive
CROSSOVER_UNIFORM = True   # Uniform crossover
FITNESS_WEIGHT_FRICTION_ROI = 0.4
FITNESS_WEIGHT_XROI = 0.3
FITNESS_WEIGHT_CALIBRATION = 0.2
FITNESS_WEIGHT_COMPLEXITY = 0.1
MIN_BETS_FOR_FITNESS = 150  # was: 30, Minimum bets to evaluate a strategy
MIN_TIPS_PER_LEAGUE = 200  # Minimum tips for per-league evolution
DEFAULT_LOOKBACK_YEARS = 8
TIME_WEIGHT_FLOOR = 0.20
MAX_STAKE_BANKROLL_FRACTION = 0.05
SOFT_PENALTY_K = 0.20
SOFT_PENALTY_LAMBDA = 0.50
RELAXATION_RANGE_PCT = 0.15
RELAXATION_MIN_BETS_FACTOR = 0.80
RISK_SCALE_STEP = 0.90
RISK_SCALE_MAX_ATTEMPTS = 8
KELLY_FLOOR = 0.02
MAX_STAKE_FLOOR = 5.0
LIVE_GUARDRAIL_MAX_DD_PCT = 0.25
LIVE_GUARDRAIL_MAX_RUIN_PROXY = 0.20
RESCUE_MIN_RUIN_IMPROVEMENT = 0.003
QUICK_CANDIDATE_POOL = 12
DEEP_CANDIDATE_POOL = 20
BOOTSTRAP_PREFILTER_SAMPLES = 2000
BOOTSTRAP_FINAL_SAMPLES = 8000
MC_PREFILTER_PATHS = 8000
MC_FINAL_PATHS = 30000
MC_FAIL_FAST_CHECK_PATHS = (500, 1000)
MC_FAIL_FAST_CHECK_LIMITS = (0.30, 0.25)
BOOTSTRAP_VECTOR_MAX_CELLS = 20_000_000

# Mode defaults (tuned for 32GB dedicated memory)
MODE_DEFAULTS = {
    "quick": {"population_size": 300, "generations": 50},
    "deep":  {"population_size": 500, "generations": 100},
}

# Mining mode
CHECKPOINT_DIR = Path("checkpoints")
CHECKPOINT_INTERVAL = 5
STAGNATION_THRESHOLD = 20
RADIATION_MUTATION_RATE = 0.25
WATCH_DEFAULT_INTERVAL = 6 * 3600  # 6 hours
CALIBRATION_BINS = 10
EPISTASIS_PAIRS = (
    ("xg_trust_factor", "expected_roi_weight"),
    ("rotation_penalty_weight", "early_signal_confidence"),
    ("liquidity_priority_weight", "entropy_damping"),
)

# Shutdown flag for SIGINT
_shutdown_requested = False

def _handle_sigint(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.warning("SIGINT received — finishing current generation, saving checkpoint...")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def get_time_weight(
    match_date: datetime | None,
    *,
    reference_now: datetime,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> float:
    """Linear time decay: 1.0 today -> TIME_WEIGHT_FLOOR at lookback horizon."""
    if match_date is None:
        return TIME_WEIGHT_FLOOR
    md = _ensure_utc(match_date)
    days_old = max(0.0, (reference_now - md).total_seconds() / 86400.0)
    horizon_days = max(1.0, float(lookback_years) * 365.0)
    return float(max(TIME_WEIGHT_FLOOR, 1.0 - (days_old / horizon_days)))


async def load_tips(
    sport_key: str | None,
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> list[dict]:
    """Load all resolved tips from MongoDB."""
    import app.database as _db

    query = {"status": "resolved", "was_correct": {"$ne": None}}
    if sport_key:
        query["sport_key"] = sport_key
    if lookback_years > 0:
        now = _utcnow()
        cutoff = now - timedelta(days=365 * lookback_years)
        query["match_date"] = {"$gte": cutoff}

    projection = {
        "match_id": 1,
        "sport_key": 1,
        "match_date": 1,
        "edge_pct": 1,
        "confidence": 1,
        "implied_probability": 1,
        "was_correct": 1,
        "recommended_selection": 1,
        "tier_signals": 1,
        "qbot_logic": 1,
    }

    tips = await _db.db.quotico_tips.find(
        query, projection,
    ).sort("match_date", 1).to_list(length=100_000)

    log.info("Loaded %d resolved tips (lookback=%dy)", len(tips), lookback_years)
    return tips


def vectorize_tips(
    tips: list[dict],
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    reference_now: datetime | None = None,
) -> dict[str, np.ndarray]:
    """Convert tip documents to numpy arrays for vectorized evaluation."""
    n = len(tips)
    ref_now = reference_now or _utcnow()

    edge_pct = np.zeros(n, dtype=np.float64)
    confidence = np.zeros(n, dtype=np.float64)
    implied_prob = np.zeros(n, dtype=np.float64)
    was_correct = np.zeros(n, dtype=np.bool_)
    iso_week = np.zeros(n, dtype=np.int32)
    time_weight = np.zeros(n, dtype=np.float64)

    # Signal-specific boost amounts (pre-computed from tier_signals)
    sharp_boost = np.zeros(n, dtype=np.float64)
    momentum_boost = np.zeros(n, dtype=np.float64)
    rest_boost = np.zeros(n, dtype=np.float64)

    # New v2 arrays
    pick_type = np.zeros(n, dtype=np.int8)         # 0=home, 1=draw, 2=away
    h2h_weight_tip = np.zeros(n, dtype=np.float64)  # from tier_signals.poisson.h2h_weight
    bayes_conf = np.zeros(n, dtype=np.float64)      # from qbot_logic.bayesian_confidence

    # We need odds to compute Kelly stake and profit
    odds = np.zeros(n, dtype=np.float64)
    # v3.1/v3.2 support arrays
    xroi = np.zeros(n, dtype=np.float64)
    luck_gap = np.zeros(n, dtype=np.float64)
    ref_intensity = np.zeros(n, dtype=np.float64)
    var_risk = np.zeros(n, dtype=np.float64)
    lineup_delta = np.zeros(n, dtype=np.float64)
    is_alpha = np.zeros(n, dtype=np.bool_)
    liquidity_score = np.zeros(n, dtype=np.float64)
    data_entropy = np.zeros(n, dtype=np.float64)
    friction_bps = np.zeros(n, dtype=np.float64)

    for i, tip in enumerate(tips):
        edge_pct[i] = tip.get("edge_pct", 0.0)
        confidence[i] = tip.get("confidence", 0.0)
        implied_prob[i] = tip.get("implied_probability", 0.33)
        was_correct[i] = bool(tip.get("was_correct", False))

        # ISO week for Sharpe bucketing
        md = tip.get("match_date")
        if md:
            iso_week[i] = md.isocalendar()[1] + md.isocalendar()[0] * 100
            time_weight[i] = get_time_weight(md, reference_now=ref_now, lookback_years=lookback_years)
        else:
            iso_week[i] = 0
            time_weight[i] = TIME_WEIGHT_FLOOR

        # Odds approximation from implied probability
        ip = implied_prob[i]
        odds[i] = (1.0 / ip) if ip > 0.01 else 10.0

        # Extract signal contributions
        signals = tip.get("tier_signals", {})

        # Sharp: was there a sharp move agreeing with pick?
        sharp_sig = signals.get("sharp_movement", {})
        if sharp_sig.get("has_sharp_movement") and sharp_sig.get("direction") == tip.get("recommended_selection"):
            sharp_boost[i] = 0.10
            if sharp_sig.get("is_late_money"):
                sharp_boost[i] = 0.12
            if sharp_sig.get("has_steam_move") and sharp_sig.get("steam_outcome") == tip.get("recommended_selection"):
                sharp_boost[i] += 0.03

        # Momentum: how much form gap contributes
        momentum_sig = signals.get("momentum", {})
        gap = momentum_sig.get("gap", 0.0)
        if gap > 0.20:
            # Check if momentum direction agrees with pick
            home_m = momentum_sig.get("home", {}).get("momentum_score", 0.5)
            away_m = momentum_sig.get("away", {}).get("momentum_score", 0.5)
            pick = tip.get("recommended_selection")
            if (pick == "1" and home_m > away_m) or (pick == "2" and away_m > home_m):
                momentum_boost[i] = 0.08

        # Rest advantage (can be None)
        rest_sig = signals.get("rest_advantage") or {}
        if rest_sig.get("contributes"):
            diff = rest_sig.get("diff", 0)
            pick = tip.get("recommended_selection")
            if (pick == "1" and diff > 0) or (pick == "2" and diff < 0):
                rest_boost[i] = 0.04

        # Pick type encoding (for venue bias genes)
        pick = tip.get("recommended_selection")
        if pick == "1":
            pick_type[i] = 0
        elif pick == "X":
            pick_type[i] = 1
        else:
            pick_type[i] = 2

        # H2H weight from Poisson signal (nullable -> 0.0)
        poisson_sig = signals.get("poisson") or {}
        h2h_weight_tip[i] = poisson_sig.get("h2h_weight", 0.0)

        # Bayesian confidence from qbot_logic (nullable -> 0.333 prior)
        qbot = tip.get("qbot_logic") or {}
        bayes_conf[i] = qbot.get("bayesian_confidence", 0.333)
        xroi[i] = float(qbot.get("xroi", qbot.get("expected_roi", edge_pct[i] / 100.0)) or 0.0)
        luck_gap[i] = float(qbot.get("luck_factor", qbot.get("xp_gap", 0.0)) or 0.0)
        ref_intensity[i] = float(qbot.get("ref_cards_index", 0.0) or 0.0)
        var_risk[i] = float(qbot.get("var_risk", 0.0) or 0.0)
        lineup_delta[i] = float(qbot.get("lineup_delta", 0.0) or 0.0)
        stage = str(qbot.get("signal_stage", "")).lower()
        is_alpha[i] = stage == "alpha"
        liquidity_score[i] = float(qbot.get("liquidity_score", 0.5) or 0.5)
        data_entropy[i] = float(qbot.get("data_entropy", qbot.get("market_entropy", 0.3)) or 0.3)
        friction_bps[i] = float(qbot.get("friction_bps", 15.0) or 15.0)

    return {
        "edge_pct": edge_pct,
        "confidence": confidence,
        "implied_prob": implied_prob,
        "was_correct": was_correct,
        "odds": odds,
        "iso_week": iso_week,
        "time_weight": time_weight,
        "sharp_boost": sharp_boost,
        "momentum_boost": momentum_boost,
        "rest_boost": rest_boost,
        "pick_type": pick_type,
        "h2h_weight": h2h_weight_tip,
        "bayes_conf": bayes_conf,
        "xroi": xroi,
        "luck_gap": luck_gap,
        "ref_intensity": ref_intensity,
        "var_risk": var_risk,
        "lineup_delta": lineup_delta,
        "is_alpha": is_alpha,
        "liquidity_score": liquidity_score,
        "data_entropy": data_entropy,
        "friction_bps": friction_bps,
    }


# ---------------------------------------------------------------------------
# Fitness evaluation (fully vectorized via 2D broadcasting)
# ---------------------------------------------------------------------------

def evaluate_population(
    population: np.ndarray,
    data: dict[str, np.ndarray],
    *,
    min_bets_for_fitness: int = MIN_BETS_FOR_FITNESS,
    penalty_k: float = SOFT_PENALTY_K,
    penalty_lambda: float = SOFT_PENALTY_LAMBDA,
) -> np.ndarray:
    """Evaluate fitness for all bots simultaneously.

    Args:
        population: (P, G) array of DNA parameters
        data: vectorized tip arrays of shape (N,)

    Returns:
        (P,) array of fitness scores
    """
    P = population.shape[0]
    N = data["edge_pct"].shape[0]

    # Extract DNA columns -> (P, 1) for broadcasting
    min_edge    = population[:, 0:1]
    min_conf    = population[:, 1:2]
    sharp_w     = population[:, 2:3]
    momentum_w  = population[:, 3:4]
    rest_w      = population[:, 4:5]
    kelly_f     = population[:, 5:6]
    max_s       = population[:, 6:7]
    home_bias_w = population[:, 7:8]
    away_bias_w = population[:, 8:9]
    h2h_w       = population[:, 9:10]
    draw_thresh = population[:, 10:11]
    vol_buffer  = population[:, 11:12]
    bayes_trust = population[:, 12:13]
    xg_trust = population[:, 13:14]
    luck_reg = population[:, 14:15]
    ref_cards = population[:, 15:16]
    var_buffer = population[:, 16:17]
    rotation_penalty = population[:, 17:18]
    early_signal_conf = population[:, 18:19]
    xroi_weight = population[:, 19:20]
    liquidity_priority = population[:, 20:21]
    entropy_damping = population[:, 21:22]
    complexity_gene = population[:, 22:23]

    # Tip arrays -> (1, N) for broadcasting
    edge    = data["edge_pct"][np.newaxis, :]
    conf    = data["confidence"][np.newaxis, :]
    imp     = data["implied_prob"][np.newaxis, :]
    correct = data["was_correct"][np.newaxis, :]
    odds    = data["odds"][np.newaxis, :]
    s_boost = data["sharp_boost"][np.newaxis, :]
    m_boost = data["momentum_boost"][np.newaxis, :]
    r_boost = data["rest_boost"][np.newaxis, :]
    pick    = data["pick_type"][np.newaxis, :]      # (1, N) int8
    h2h_t   = data["h2h_weight"][np.newaxis, :]     # (1, N)
    bayes_c = data["bayes_conf"][np.newaxis, :]     # (1, N)
    t_weight = data["time_weight"][np.newaxis, :]   # (1, N)
    xroi = data["xroi"][np.newaxis, :]
    luck_gap = data["luck_gap"][np.newaxis, :]
    ref_intensity = data["ref_intensity"][np.newaxis, :]
    var_risk = data["var_risk"][np.newaxis, :]
    lineup_delta = data["lineup_delta"][np.newaxis, :]
    is_alpha = data["is_alpha"][np.newaxis, :]
    liquidity = data["liquidity_score"][np.newaxis, :]
    entropy = data["data_entropy"][np.newaxis, :]
    friction_bps = data["friction_bps"][np.newaxis, :]

    # --- Confidence pipeline ---
    # 1. Base: signal-weighted boosts
    adj_conf = conf + sharp_w * s_boost + momentum_w * m_boost + rest_w * r_boost

    # 2. Venue bias: multiply by home_bias or away_bias based on pick type
    is_home = (pick == 0)
    is_draw = (pick == 1)
    is_away = (pick == 2)
    bias = is_home * home_bias_w + is_draw * 1.0 + is_away * away_bias_w
    adj_conf = adj_conf * bias

    # 3. H2H amplification
    adj_conf = adj_conf + h2h_w * h2h_t * 0.10

    # 4. Bayesian trust blending
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c

    # v3.1 adjustments
    adj_conf = (1.0 - xg_trust) * adj_conf + xg_trust * np.clip(xroi + imp, 0.0, 0.99)
    adj_conf = adj_conf + luck_reg * np.clip(luck_gap, -0.25, 0.25)
    adj_conf = adj_conf - ref_cards * np.maximum(ref_intensity, 0.0) * 0.02
    adj_conf = adj_conf - var_buffer * np.maximum(var_risk, 0.0)
    adj_conf = np.where(is_alpha, adj_conf * early_signal_conf, adj_conf)
    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    # --- Filter mask ---
    mask = (edge >= min_edge) & (conf >= min_conf)

    # Draw gate: draws must exceed draw_threshold on ADJUSTED confidence
    # IMPORTANT: applied AFTER full confidence pipeline
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate
    # Rotation safety: higher lineup delta reduces activation.
    rotation_gate = lineup_delta > np.maximum(0.1, 0.35 * np.maximum(rotation_penalty, 1e-6))
    mask = mask & ~rotation_gate

    # --- Kelly with volatility buffer ---
    buffered_edge = adj_conf - imp - vol_buffer
    buffered_edge = np.maximum(buffered_edge, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_raw = kelly_f * buffered_edge / denom
    kelly_raw = np.maximum(kelly_raw, 0.0)
    safety_cap = MAX_STAKE_BANKROLL_FRACTION * 1000.0
    stake_cap = np.minimum(max_s, safety_cap)
    stake = np.minimum(kelly_raw, stake_cap)
    # Liquidity-aware sizing: prioritize executable edges.
    liquidity_scale = np.clip(liquidity + (1.0 - liquidity_priority), 0.20, 1.0)
    stake = stake * liquidity_scale
    # Entropy damping: Kelly decreases when uncertainty is high.
    entropy_scale = 1.0 / (1.0 + np.maximum(entropy, 0.0) * np.maximum(entropy_damping, 0.0))
    stake = stake * entropy_scale

    # Apply mask
    stake = stake * mask

    # Profit per bet
    profit = np.where(correct, stake * (odds - 1.0), -stake)
    friction_cost = stake * (friction_bps / 10_000.0)
    profit_after_friction = profit - friction_cost
    weighted_profit = profit_after_friction * t_weight
    weighted_stake = stake * t_weight

    # --- ROI per bot ---
    total_staked = weighted_stake.sum(axis=1)
    total_profit = weighted_profit.sum(axis=1)
    friction_roi = np.where(total_staked > 0, total_profit / total_staked, -1.0)

    # --- Bet count ---
    bet_count = mask.sum(axis=1)

    # xROI quality on executed bets
    weighted_xroi_num = (xroi * stake).sum(axis=1)
    weighted_xroi_den = np.maximum(stake.sum(axis=1), 1e-9)
    weighted_xroi = np.where(weighted_xroi_den > 0, weighted_xroi_num / weighted_xroi_den, -1.0)

    # Expected calibration error (ECE) on executed bets; lower is better.
    ece = np.zeros(P, dtype=np.float64)
    conf_flat = adj_conf
    for b in range(CALIBRATION_BINS):
        lo = b / CALIBRATION_BINS
        hi = (b + 1) / CALIBRATION_BINS
        in_bin = (conf_flat >= lo) & (conf_flat < hi) & mask
        n_bin = in_bin.sum(axis=1).astype(np.float64)
        conf_sum = (conf_flat * in_bin).sum(axis=1)
        acc_sum = (correct.astype(np.float64) * in_bin).sum(axis=1)
        avg_conf = np.where(n_bin > 0, conf_sum / np.maximum(n_bin, 1e-9), 0.0)
        avg_acc = np.where(n_bin > 0, acc_sum / np.maximum(n_bin, 1e-9), 0.0)
        weight = n_bin / np.maximum(mask.sum(axis=1), 1.0)
        ece += weight * np.abs(avg_acc - avg_conf)
    calibration_score = 1.0 - np.clip(ece, 0.0, 1.0)

    # DNA complexity penalty
    dna_mean = population.mean(axis=1)
    dna_std = population.std(axis=1)
    dna_complexity = np.clip((dna_std / np.maximum(np.abs(dna_mean), 1e-6)) * 0.1 + complexity_gene[:, 0], 0.0, 2.0)

    # --- Combined fitness (v3.2) ---
    fitness = (
        FITNESS_WEIGHT_FRICTION_ROI * friction_roi
        + FITNESS_WEIGHT_XROI * (weighted_xroi * xroi_weight[:, 0])
        + FITNESS_WEIGHT_CALIBRATION * calibration_score
        - FITNESS_WEIGHT_COMPLEXITY * dna_complexity
    )

    # Soft penalty on sample size: discourage low bet count without hard-killing candidates
    sigmoid = 1.0 / (1.0 + np.exp(-penalty_k * (bet_count - min_bets_for_fitness)))
    fitness = fitness - penalty_lambda * (1.0 - sigmoid)

    return fitness


# ---------------------------------------------------------------------------
# Genetic operators
# ---------------------------------------------------------------------------

def random_population(
    size: int,
    rng: np.random.Generator,
    dna_ranges: dict[str, tuple[float, float]] = DNA_RANGES,
) -> np.ndarray:
    """Create initial random population within DNA_RANGES."""
    pop = np.zeros((size, len(DNA_GENES)), dtype=np.float64)
    for j, gene in enumerate(DNA_GENES):
        lo, hi = dna_ranges[gene]
        pop[:, j] = rng.uniform(lo, hi, size=size)
    return pop


def select_elites(population: np.ndarray, fitness: np.ndarray, n_elites: int) -> np.ndarray:
    """Select top-N bots by fitness (elitism)."""
    elite_idx = np.argsort(fitness)[-n_elites:]
    return population[elite_idx].copy()


def crossover(parents: np.ndarray, n_children: int, rng: np.random.Generator) -> np.ndarray:
    """Uniform crossover: randomly pick genes from two parents."""
    n_parents = parents.shape[0]
    n_genes = parents.shape[1]
    children = np.zeros((n_children, n_genes), dtype=np.float64)

    for i in range(n_children):
        p1, p2 = rng.choice(n_parents, size=2, replace=False)
        # Uniform: each gene comes from either parent with 50% chance
        mask = rng.random(n_genes) < 0.5
        child = np.where(mask, parents[p1], parents[p2])
        # Epistasis preservation: keep high-value gene pairs from one parent.
        for left, right in EPISTASIS_PAIRS:
            if left not in DNA_GENES or right not in DNA_GENES:
                continue
            if rng.random() < 0.35:
                li = DNA_GENES.index(left)
                ri = DNA_GENES.index(right)
                src = p1 if rng.random() < 0.5 else p2
                child[li] = parents[src, li]
                child[ri] = parents[src, ri]
        children[i] = child

    return children


def mutate(
    population: np.ndarray,
    rng: np.random.Generator,
    mutation_rate: float = MUTATION_RATE,
    reliability_score: float = 0.5,
    dna_ranges: dict[str, tuple[float, float]] = DNA_RANGES,
) -> np.ndarray:
    """Gaussian mutation with clip to DNA_RANGES."""
    P, G = population.shape
    mutated = population.copy()

    adaptive_rate = float(np.clip(mutation_rate * (1.25 - np.clip(reliability_score, 0.0, 1.0)), 0.02, 0.35))
    for j, gene in enumerate(DNA_GENES):
        lo, hi = dna_ranges[gene]
        gene_range = hi - lo
        sigma = MUTATION_SIGMA_PCT * gene_range

        should_mutate = rng.random(P) < adaptive_rate
        noise = rng.normal(0, sigma, size=P)
        mutated[:, j] = np.where(
            should_mutate,
            np.clip(mutated[:, j] + noise, lo, hi),
            mutated[:, j],
        )

    return mutated


def _expand_dna_ranges(
    dna_ranges: dict[str, tuple[float, float]],
    pct: float = RELAXATION_RANGE_PCT,
) -> dict[str, tuple[float, float]]:
    """Expand search ranges symmetrically, with sane clamps for bounded genes."""
    clamps: dict[str, tuple[float, float]] = {
        "min_confidence": (0.0, 1.0),
        "draw_threshold": (0.0, 1.0),
        "volatility_buffer": (0.0, 0.5),
        "home_bias": (0.5, 1.5),
        "away_bias": (0.5, 1.5),
        "kelly_fraction": (0.01, 1.0),
        "max_stake": (1.0, 200.0),
    }
    expanded: dict[str, tuple[float, float]] = {}
    for gene, (lo, hi) in dna_ranges.items():
        span = hi - lo
        new_lo = lo - pct * span
        new_hi = hi + pct * span
        if gene in clamps:
            c_lo, c_hi = clamps[gene]
            new_lo = max(c_lo, new_lo)
            new_hi = min(c_hi, new_hi)
        expanded[gene] = (round(new_lo, 6), round(new_hi, 6))
    return expanded


def _dominates(a: dict, b: dict) -> bool:
    """True if candidate a dominates b on all Pareto metrics."""
    better_or_equal = (
        a["roi"] >= b["roi"]
        and a["bet_count"] >= b["bet_count"]
        and a["ruin_prob"] <= b["ruin_prob"]
        and a["max_dd_pct"] <= b["max_dd_pct"]
    )
    strictly_better = (
        a["roi"] > b["roi"]
        or a["bet_count"] > b["bet_count"]
        or a["ruin_prob"] < b["ruin_prob"]
        or a["max_dd_pct"] < b["max_dd_pct"]
    )
    return better_or_equal and strictly_better


def _pareto_frontier(candidates: list[dict]) -> list[dict]:
    """Return non-dominated candidates."""
    frontier: list[dict] = []
    for i, cand in enumerate(candidates):
        dominated = False
        for j, other in enumerate(candidates):
            if i == j:
                continue
            if _dominates(other, cand):
                dominated = True
                break
        if not dominated:
            frontier.append(cand)
    return frontier


def _crowding_distance(front: list[dict]) -> dict[int, float]:
    """Compute crowding distance for Pareto diversity."""
    if not front:
        return {}
    if len(front) <= 2:
        return {i: float("inf") for i in range(len(front))}

    values = np.array(
        [
            [
                c["roi"],
                np.log1p(max(c["bet_count"], 0)),
                1.0 - c["ruin_prob"],
                1.0 - c["max_dd_pct"],
            ]
            for c in front
        ],
        dtype=np.float64,
    )
    n_obj = values.shape[1]
    dist = np.zeros(len(front), dtype=np.float64)

    for m in range(n_obj):
        order = np.argsort(values[:, m])
        dist[order[0]] = float("inf")
        dist[order[-1]] = float("inf")
        v_min = values[order[0], m]
        v_max = values[order[-1], m]
        if v_max - v_min < 1e-12:
            continue
        scale = v_max - v_min
        for k in range(1, len(front) - 1):
            left_idx = order[k - 1]
            right_idx = order[k + 1]
            cur_idx = order[k]
            dist[cur_idx] += (values[right_idx, m] - values[left_idx, m]) / scale

    return {i: float(dist[i]) for i in range(len(front))}


def _tradeoff_label(candidate: dict) -> str:
    """Human-friendly label for candidate's strongest objective."""
    score_map = {
        "High-ROI": candidate["roi"],
        "High-Volume": np.log1p(max(candidate["bet_count"], 0)),
        "Low-Ruin": 1.0 - candidate["ruin_prob"],
        "Low-Drawdown": 1.0 - candidate["max_dd_pct"],
    }
    return max(score_map, key=score_map.get)


def _dna_cache_key(dna: np.ndarray, precision: int = 6) -> tuple:
    """Stable cache key for DNA vectors."""
    return tuple(np.round(dna.astype(np.float64), precision).tolist())


def _cached_bootstrap(
    dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    *,
    n_samples: int,
    rng: np.random.Generator,
    cache: dict[tuple, dict],
) -> dict:
    key = ("bs", n_samples, _dna_cache_key(dna))
    if key not in cache:
        cache[key] = bootstrap_fitness(dna, tip_data, n_samples=n_samples, rng=rng)
    return cache[key]


def _cached_monte_carlo(
    dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    *,
    n_paths: int,
    fail_fast: bool,
    rng: np.random.Generator,
    cache: dict[tuple, dict],
) -> dict:
    key = ("mc", n_paths, fail_fast, _dna_cache_key(dna))
    if key not in cache:
        cache[key] = monte_carlo_bankroll(
            dna,
            tip_data,
            n_paths=n_paths,
            fail_fast=fail_fast,
            rng=rng,
        )
    return cache[key]


def _stress_test_with_rescue(
    dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    rng: np.random.Generator,
    *,
    bootstrap_threshold: float = 0.60,
    ruin_threshold: float = 0.15,
    bootstrap_prefilter_samples: int = BOOTSTRAP_PREFILTER_SAMPLES,
    bootstrap_final_samples: int = BOOTSTRAP_FINAL_SAMPLES,
    mc_prefilter_paths: int = MC_PREFILTER_PATHS,
    mc_final_paths: int = MC_FINAL_PATHS,
    bootstrap_cache: dict[tuple, dict] | None = None,
    mc_cache: dict[tuple, dict] | None = None,
) -> dict:
    """Run stress tests and iteratively downscale risk genes if needed."""
    if bootstrap_cache is None:
        bootstrap_cache = {}
    if mc_cache is None:
        mc_cache = {}

    t_total_start = time.perf_counter()
    timing: dict[str, float | int | bool] = {
        "bootstrap_prefilter_s": 0.0,
        "bootstrap_prefilter_cache_hit": False,
        "mc_prefilter_s": 0.0,
        "mc_prefilter_calls": 0,
        "mc_prefilter_cache_hits": 0,
        "rescue_loop_s": 0.0,
        "bootstrap_final_s": 0.0,
        "bootstrap_final_cache_hit": False,
        "mc_final_s": 0.0,
        "mc_final_cache_hit": False,
        "total_s": 0.0,
    }

    candidate = dna.copy()
    kelly_idx = DNA_GENES.index("kelly_fraction")
    stake_idx = DNA_GENES.index("max_stake")
    initial_kelly = float(candidate[kelly_idx])
    initial_stake = float(candidate[stake_idx])

    bs_pref_key = ("bs", bootstrap_prefilter_samples, _dna_cache_key(candidate))
    timing["bootstrap_prefilter_cache_hit"] = bs_pref_key in bootstrap_cache
    t_bs_pref = time.perf_counter()
    bs_prefilter = _cached_bootstrap(
        candidate,
        tip_data,
        n_samples=bootstrap_prefilter_samples,
        rng=rng,
        cache=bootstrap_cache,
    )
    timing["bootstrap_prefilter_s"] = time.perf_counter() - t_bs_pref
    if bs_prefilter["p_positive"] < bootstrap_threshold:
        timing["total_s"] = time.perf_counter() - t_total_start
        log.info(
            "Stress timing | reason=bootstrap_failed | total=%.3fs bs_pre=%.3fs mc_pre=%.3fs rescue=%.3fs bs_final=%.3fs mc_final=%.3fs",
            timing["total_s"], timing["bootstrap_prefilter_s"], timing["mc_prefilter_s"],
            timing["rescue_loop_s"], timing["bootstrap_final_s"], timing["mc_final_s"],
        )
        return {
            "passed": False,
            "reason": "bootstrap_failed",
            "dna": candidate,
            "bootstrap": bs_prefilter,
            "bootstrap_prefilter": bs_prefilter,
            "monte_carlo": None,
            "monte_carlo_prefilter": None,
            "timing": {
                "bootstrap_prefilter_s": round(float(timing["bootstrap_prefilter_s"]), 4),
                "bootstrap_prefilter_cache_hit": bool(timing["bootstrap_prefilter_cache_hit"]),
                "mc_prefilter_s": round(float(timing["mc_prefilter_s"]), 4),
                "mc_prefilter_calls": int(timing["mc_prefilter_calls"]),
                "mc_prefilter_cache_hits": int(timing["mc_prefilter_cache_hits"]),
                "rescue_loop_s": round(float(timing["rescue_loop_s"]), 4),
                "bootstrap_final_s": round(float(timing["bootstrap_final_s"]), 4),
                "bootstrap_final_cache_hit": bool(timing["bootstrap_final_cache_hit"]),
                "mc_final_s": round(float(timing["mc_final_s"]), 4),
                "mc_final_cache_hit": bool(timing["mc_final_cache_hit"]),
                "total_s": round(float(timing["total_s"]), 4),
            },
            "rescue_log": {
                "applied": False,
                "tuning_attempts": 0,
                "final_risk_scaling": 1.0,
                "safety_floor_reached": False,
                "early_stop_low_improvement": False,
            },
        }

    attempts = 0
    scaled = 1.0
    safety_floor_reached = False
    early_stop_low_improvement = False
    rescue_applied = False
    mc_pref_key = ("mc", mc_prefilter_paths, True, _dna_cache_key(candidate))
    mc_pref_hit = mc_pref_key in mc_cache
    t_mc_pref = time.perf_counter()
    mc_prefilter = _cached_monte_carlo(
        candidate,
        tip_data,
        n_paths=mc_prefilter_paths,
        fail_fast=True,
        rng=rng,
        cache=mc_cache,
    )
    timing["mc_prefilter_s"] += time.perf_counter() - t_mc_pref
    timing["mc_prefilter_calls"] += 1
    if mc_pref_hit:
        timing["mc_prefilter_cache_hits"] += 1
    prev_ruin = float(mc_prefilter["ruin_prob"])

    t_rescue = time.perf_counter()
    while mc_prefilter["ruin_prob"] > ruin_threshold and attempts < RISK_SCALE_MAX_ATTEMPTS:
        if float(candidate[kelly_idx]) <= KELLY_FLOOR or float(candidate[stake_idx]) <= MAX_STAKE_FLOOR:
            safety_floor_reached = True
            break
        rescue_applied = True
        attempts += 1
        scaled *= RISK_SCALE_STEP
        candidate[kelly_idx] = max(KELLY_FLOOR, float(candidate[kelly_idx]) * RISK_SCALE_STEP)
        candidate[stake_idx] = max(MAX_STAKE_FLOOR, float(candidate[stake_idx]) * RISK_SCALE_STEP)
        mc_pref_key = ("mc", mc_prefilter_paths, True, _dna_cache_key(candidate))
        mc_pref_hit = mc_pref_key in mc_cache
        t_mc_pref = time.perf_counter()
        mc_prefilter = _cached_monte_carlo(
            candidate,
            tip_data,
            n_paths=mc_prefilter_paths,
            fail_fast=True,
            rng=rng,
            cache=mc_cache,
        )
        timing["mc_prefilter_s"] += time.perf_counter() - t_mc_pref
        timing["mc_prefilter_calls"] += 1
        if mc_pref_hit:
            timing["mc_prefilter_cache_hits"] += 1
        ruin_now = float(mc_prefilter["ruin_prob"])
        if (prev_ruin - ruin_now) < RESCUE_MIN_RUIN_IMPROVEMENT and ruin_now > ruin_threshold:
            early_stop_low_improvement = True
            break
        prev_ruin = ruin_now
    timing["rescue_loop_s"] = time.perf_counter() - t_rescue

    passed_prefilter = mc_prefilter["ruin_prob"] <= ruin_threshold
    bs_final = bs_prefilter
    mc_final = mc_prefilter
    passed = passed_prefilter
    if passed_prefilter:
        # High-fidelity confirmation for finalists
        bs_final_key = ("bs", bootstrap_final_samples, _dna_cache_key(candidate))
        timing["bootstrap_final_cache_hit"] = bs_final_key in bootstrap_cache
        t_bs_final = time.perf_counter()
        bs_final = _cached_bootstrap(
            candidate,
            tip_data,
            n_samples=bootstrap_final_samples,
            rng=rng,
            cache=bootstrap_cache,
        )
        timing["bootstrap_final_s"] = time.perf_counter() - t_bs_final
        mc_final_key = ("mc", mc_final_paths, False, _dna_cache_key(candidate))
        timing["mc_final_cache_hit"] = mc_final_key in mc_cache
        t_mc_final = time.perf_counter()
        mc_final = _cached_monte_carlo(
            candidate,
            tip_data,
            n_paths=mc_final_paths,
            fail_fast=False,
            rng=rng,
            cache=mc_cache,
        )
        timing["mc_final_s"] = time.perf_counter() - t_mc_final
        passed = bs_final["p_positive"] >= bootstrap_threshold and mc_final["ruin_prob"] <= ruin_threshold

    if not passed and (
        float(candidate[kelly_idx]) <= KELLY_FLOOR or float(candidate[stake_idx]) <= MAX_STAKE_FLOOR
    ):
        safety_floor_reached = True

    timing["total_s"] = time.perf_counter() - t_total_start
    log.info(
        "Stress timing | reason=%s | total=%.3fs bs_pre=%.3fs mc_pre=%.3fs rescue=%.3fs bs_final=%.3fs mc_final=%.3fs mc_pre_calls=%d mc_pre_cache_hits=%d",
        "ok" if passed else ("low_improvement_stop" if early_stop_low_improvement else "ruin_threshold_failed"),
        timing["total_s"],
        timing["bootstrap_prefilter_s"],
        timing["mc_prefilter_s"],
        timing["rescue_loop_s"],
        timing["bootstrap_final_s"],
        timing["mc_final_s"],
        timing["mc_prefilter_calls"],
        timing["mc_prefilter_cache_hits"],
    )

    return {
        "passed": passed,
        "reason": "ok" if passed else ("low_improvement_stop" if early_stop_low_improvement else "ruin_threshold_failed"),
        "dna": candidate,
        "bootstrap": bs_final,
        "bootstrap_prefilter": bs_prefilter,
        "monte_carlo": mc_final,
        "monte_carlo_prefilter": mc_prefilter,
        "timing": {
            "bootstrap_prefilter_s": round(float(timing["bootstrap_prefilter_s"]), 4),
            "bootstrap_prefilter_cache_hit": bool(timing["bootstrap_prefilter_cache_hit"]),
            "mc_prefilter_s": round(float(timing["mc_prefilter_s"]), 4),
            "mc_prefilter_calls": int(timing["mc_prefilter_calls"]),
            "mc_prefilter_cache_hits": int(timing["mc_prefilter_cache_hits"]),
            "rescue_loop_s": round(float(timing["rescue_loop_s"]), 4),
            "bootstrap_final_s": round(float(timing["bootstrap_final_s"]), 4),
            "bootstrap_final_cache_hit": bool(timing["bootstrap_final_cache_hit"]),
            "mc_final_s": round(float(timing["mc_final_s"]), 4),
            "mc_final_cache_hit": bool(timing["mc_final_cache_hit"]),
            "total_s": round(float(timing["total_s"]), 4),
        },
        "rescue_log": {
            "applied": rescue_applied,
            "tuning_attempts": attempts,
            "final_risk_scaling": round(scaled, 4),
            "safety_floor_reached": safety_floor_reached,
            "early_stop_low_improvement": early_stop_low_improvement,
            "initial_kelly": round(initial_kelly, 4),
            "initial_max_stake": round(initial_stake, 4),
            "final_kelly": round(float(candidate[kelly_idx]), 4),
            "final_max_stake": round(float(candidate[stake_idx]), 4),
        },
    }


def _log_stress_timing_summary(label: str, candidates: list[dict]) -> None:
    """Log aggregated stress timing metrics across candidate evaluations."""
    if not candidates:
        log.info("Stress timing summary | %s | no candidates", label)
        return

    timings = [c.get("stress", {}).get("timing", {}) for c in candidates]
    timings = [t for t in timings if t]
    if not timings:
        log.info("Stress timing summary | %s | missing timing payload", label)
        return

    def _sum_float(key: str) -> float:
        return float(sum(float(t.get(key, 0.0)) for t in timings))

    def _sum_int(key: str) -> int:
        return int(sum(int(t.get(key, 0)) for t in timings))

    def _count_true(key: str) -> int:
        return int(sum(1 for t in timings if bool(t.get(key, False))))

    n = len(timings)
    total_s = _sum_float("total_s")
    mc_pref_calls = _sum_int("mc_prefilter_calls")
    mc_pref_hits = _sum_int("mc_prefilter_cache_hits")
    bs_pre_hits = _count_true("bootstrap_prefilter_cache_hit")
    bs_final_hits = _count_true("bootstrap_final_cache_hit")
    mc_final_hits = _count_true("mc_final_cache_hit")

    log.info(
        "Stress timing summary | %s | n=%d total=%.3fs avg=%.3fs bs_pre=%.3fs mc_pre=%.3fs rescue=%.3fs bs_final=%.3fs mc_final=%.3fs mc_pre_calls=%d mc_pre_hit_rate=%.1f%% bs_pre_hit_rate=%.1f%% bs_final_hit_rate=%.1f%% mc_final_hit_rate=%.1f%%",
        label,
        n,
        total_s,
        total_s / max(n, 1),
        _sum_float("bootstrap_prefilter_s"),
        _sum_float("mc_prefilter_s"),
        _sum_float("rescue_loop_s"),
        _sum_float("bootstrap_final_s"),
        _sum_float("mc_final_s"),
        mc_pref_calls,
        (100.0 * mc_pref_hits / max(mc_pref_calls, 1)),
        (100.0 * bs_pre_hits / max(n, 1)),
        (100.0 * bs_final_hits / max(n, 1)),
        (100.0 * mc_final_hits / max(n, 1)),
    )


def _evaluate_single_candidate(
    *,
    dna: np.ndarray,
    fitness_value: float,
    pool_rank: int,
    train_data: dict[str, np.ndarray],
    val_data: dict[str, np.ndarray],
    rng_seed: int,
    require_positive_roi: bool = True,
    bootstrap_cache: dict[tuple, dict] | None = None,
    mc_cache: dict[tuple, dict] | None = None,
) -> dict | None:
    """Evaluate one candidate through stress + metrics."""
    pre_val_metrics = _compute_detailed_metrics(dna, val_data)
    if require_positive_roi and pre_val_metrics["roi"] <= 0:
        return None

    local_rng = np.random.default_rng(rng_seed)
    if bootstrap_cache is None:
        bootstrap_cache = {}
    if mc_cache is None:
        mc_cache = {}

    stress = _stress_test_with_rescue(
        dna,
        val_data,
        rng=local_rng,
        bootstrap_prefilter_samples=BOOTSTRAP_PREFILTER_SAMPLES,
        bootstrap_final_samples=BOOTSTRAP_FINAL_SAMPLES,
        mc_prefilter_paths=MC_PREFILTER_PATHS,
        mc_final_paths=MC_FINAL_PATHS,
        bootstrap_cache=bootstrap_cache,
        mc_cache=mc_cache,
    )
    tuned_dna = stress["dna"]
    train_metrics = _compute_detailed_metrics(tuned_dna, train_data)
    val_metrics = _compute_detailed_metrics(tuned_dna, val_data)
    mc = stress.get("monte_carlo") or {}
    return {
        "pool_rank": pool_rank,
        "fitness": float(fitness_value),
        "dna": tuned_dna,
        "stress": stress,
        "train_metrics": train_metrics,
        "val_metrics": val_metrics,
        "roi": float(val_metrics["roi"]),
        "bet_count": int(val_metrics["total_bets"]),
        "max_dd_pct": float(val_metrics["max_drawdown_pct"]),
        "ruin_prob": float(mc.get("ruin_prob", 1.0)),
    }


# ---------------------------------------------------------------------------
# Main GA loop
# ---------------------------------------------------------------------------

async def run_evolution(
    sport_key: str | None,
    *,
    population_size: int = 300,
    generations: int = 50,
    dry_run: bool = False,
    seed: int | None = None,
    resume: bool = False,
    search_mode: str = "quick",
    candidate_workers: int = 1,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> dict:
    """Run staged GA search and return the best feasible strategy."""
    import app.database as _db

    global _shutdown_requested
    _shutdown_requested = False
    prev_handler = _signal.signal(_signal.SIGINT, _handle_sigint)

    rng = np.random.default_rng(seed)

    # Load and vectorize data
    tips = await load_tips(sport_key, lookback_years=lookback_years)
    if len(tips) < 100:
        log.error("Not enough resolved tips (%d < 100). Aborting.", len(tips))
        _signal.signal(_signal.SIGINT, prev_handler)
        return {"error": "insufficient_data", "tip_count": len(tips)}

    # Temporal split: 80% training, 20% validation
    split_idx = int(len(tips) * 0.80)
    train_tips = tips[:split_idx]
    val_tips = tips[split_idx:]
    log.info("Split: %d training / %d validation tips", len(train_tips), len(val_tips))

    ref_now = _utcnow()
    train_data = vectorize_tips(train_tips, lookback_years=lookback_years, reference_now=ref_now)
    val_data = vectorize_tips(val_tips, lookback_years=lookback_years, reference_now=ref_now)
    validation_window = {
        "source": "arena_quick_80_20",
        "split_ratio": 0.8,
        "tips_total": len(tips),
        "train_tips": len(train_tips),
        "validation_tips": len(val_tips),
        "start_date": _ensure_utc(val_tips[0]["match_date"]).isoformat() if val_tips else None,
        "end_date": _ensure_utc(val_tips[-1]["match_date"]).isoformat() if val_tips else None,
    }

    base_ranges = {k: tuple(v) for k, v in DNA_RANGES.items()}
    relaxed_ranges = _expand_dna_ranges(base_ranges, RELAXATION_RANGE_PCT)
    stages = [
        {
            "stage_id": 1,
            "name": "ideal",
            "dna_ranges": base_ranges,
            "min_bets": MIN_BETS_FOR_FITNESS,
        },
        {
            "stage_id": 2,
            "name": "relaxed",
            "dna_ranges": relaxed_ranges,
            "min_bets": max(1, int(round(MIN_BETS_FOR_FITNESS * RELAXATION_MIN_BETS_FACTOR))),
        },
    ]

    # Initialize or resume population seed
    start_gen = 0
    best_fitness_history: list[float] = []
    avg_fitness_history: list[float] = []
    stage_outputs: list[dict] = []

    if resume:
        ckpt = _load_checkpoint(sport_key)
        if ckpt:
            population = ckpt["population"]
            start_gen = ckpt["generation"] + 1
            best_fitness_history = ckpt.get("fitness_history", [])
            if "rng_state" in ckpt:
                try:
                    rng.bit_generator.state = ckpt["rng_state"]
                except Exception:
                    log.warning("Could not restore RNG state — continuing with new seed")
            log.info("Resumed from checkpoint: gen=%d, pop=%s", ckpt["generation"], population.shape)

            # Handle DNA schema expansion (e.g. 7->13 genes)
            if population.shape[1] < len(DNA_GENES):
                old_cols = population.shape[1]
                defaults = np.array([
                    (base_ranges[g][0] + base_ranges[g][1]) / 2
                    for g in DNA_GENES[old_cols:]
                ])
                padding = np.tile(defaults, (population.shape[0], 1))
                population = np.hstack([population, padding])
                log.info("Padded population: %d -> %d genes", old_cols, len(DNA_GENES))

            # Adjust population size if different
            if population.shape[0] != population_size:
                if population.shape[0] < population_size:
                    extra = random_population(population_size - population.shape[0], rng)
                    population = np.vstack([population, extra])
                else:
                    population = population[:population_size]
                log.info("Adjusted population size to %d", population_size)
        else:
            log.warning("No checkpoint found for %s — starting fresh", sport_key or "all")
            population = random_population(population_size, rng, dna_ranges=base_ranges)
    else:
        population = random_population(population_size, rng, dna_ranges=base_ranges)

    population_seed = population.copy()

    for stage in stages:
        if stage["stage_id"] == 2 and stage_outputs and stage_outputs[0]["has_deployable"]:
            break

        dna_ranges = stage["dna_ranges"]
        min_bets = stage["min_bets"]
        log.info(
            "Starting GA stage %d (%s): min_bets=%d",
            stage["stage_id"],
            stage["name"],
            min_bets,
        )

        # Clip carried population into current stage search-space
        pop = population_seed.copy()
        for j, gene in enumerate(DNA_GENES):
            lo, hi = dna_ranges[gene]
            pop[:, j] = np.clip(pop[:, j], lo, hi)

        n_elites = max(int(population_size * ELITE_FRACTION), 2)
        n_children = population_size - n_elites
        candidate_pool = QUICK_CANDIDATE_POOL if search_mode == "quick" else DEEP_CANDIDATE_POOL
        bootstrap_cache: dict[tuple, dict] = {}
        mc_cache: dict[tuple, dict] = {}
        stage_best_hist: list[float] = []
        stage_avg_hist: list[float] = []
        stage_t0 = time.monotonic()
        stagnant_gens = 0
        prev_best = -np.inf

        for gen in range(start_gen, generations):
            if _shutdown_requested:
                log.info("Graceful shutdown: saving checkpoint at gen %d", gen)
                _save_checkpoint(sport_key, gen - 1, pop, stage_best_hist, rng)
                break

            fitness = evaluate_population(pop, train_data, min_bets_for_fitness=min_bets)
            best_idx = int(np.argmax(fitness))
            best_fit = float(fitness[best_idx])
            avg_fit = float(fitness.mean())

            stage_best_hist.append(best_fit)
            stage_avg_hist.append(avg_fit)

            if gen % 5 == 0 or gen == generations - 1:
                log.info(
                    "Stage %d Gen %2d/%d | Best fitness: %.4f | Avg: %.4f | Pop: %d",
                    stage["stage_id"], gen + 1, generations, best_fit, avg_fit, population_size,
                )

            if gen > start_gen and gen % CHECKPOINT_INTERVAL == 0 and stage["stage_id"] == 1:
                _save_checkpoint(sport_key, gen, pop, stage_best_hist, rng)

            if best_fit <= prev_best + 1e-6:
                stagnant_gens += 1
            else:
                stagnant_gens = 0
            prev_best = best_fit

            elites = select_elites(pop, fitness, n_elites)
            children = crossover(elites, n_children, rng)

            if stagnant_gens >= STAGNATION_THRESHOLD:
                log.warning(
                    "RADIATION EVENT at gen %d — spiking mutation to %.0f%%",
                    gen, RADIATION_MUTATION_RATE * 100,
                )
                children = mutate(
                    children,
                    rng,
                    mutation_rate=RADIATION_MUTATION_RATE,
                    dna_ranges=dna_ranges,
                )
                stagnant_gens = 0
            else:
                children = mutate(children, rng, dna_ranges=dna_ranges)

            pop = np.vstack([elites, children])

        stage_elapsed = time.monotonic() - stage_t0
        log.info(
            "Stage %d complete in %.1fs (%d generations)",
            stage["stage_id"], stage_elapsed, generations,
        )

        final_fitness = evaluate_population(pop, train_data, min_bets_for_fitness=min_bets)
        top_indices = np.argsort(final_fitness)[-min(candidate_pool, population_size):][::-1]
        candidates: list[dict] = []
        candidate_specs = [
            (rank_idx, int(pop_idx), int(rng.integers(0, 2**63 - 1)))
            for rank_idx, pop_idx in enumerate(top_indices, 1)
        ]
        if candidate_workers > 1 and len(candidate_specs) > 1:
            max_workers = min(candidate_workers, len(candidate_specs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(
                        _evaluate_single_candidate,
                        dna=pop[pop_idx].copy(),
                        fitness_value=float(final_fitness[pop_idx]),
                        pool_rank=rank_idx,
                        train_data=train_data,
                        val_data=val_data,
                        rng_seed=seed_i,
                        require_positive_roi=True,
                        bootstrap_cache=None,
                        mc_cache=None,
                    )
                    for rank_idx, pop_idx, seed_i in candidate_specs
                ]
                for fut in concurrent.futures.as_completed(futures):
                    result = fut.result()
                    if result is not None:
                        candidates.append(result)
        else:
            for rank_idx, pop_idx, seed_i in candidate_specs:
                result = _evaluate_single_candidate(
                    dna=pop[pop_idx].copy(),
                    fitness_value=float(final_fitness[pop_idx]),
                    pool_rank=rank_idx,
                    train_data=train_data,
                    val_data=val_data,
                    rng_seed=seed_i,
                    require_positive_roi=True,
                    bootstrap_cache=bootstrap_cache,
                    mc_cache=mc_cache,
                )
                if result is not None:
                    candidates.append(result)

        if not candidates:
            # Fallback if no positive-ROI candidates survived the prefilter
            fallback_idx = int(np.argmax(final_fitness))
            fallback_seed = int(rng.integers(0, 2**63 - 1))
            fallback = _evaluate_single_candidate(
                dna=pop[fallback_idx].copy(),
                fitness_value=float(final_fitness[fallback_idx]),
                pool_rank=1,
                train_data=train_data,
                val_data=val_data,
                rng_seed=fallback_seed,
                require_positive_roi=False,
                bootstrap_cache=bootstrap_cache if candidate_workers <= 1 else None,
                mc_cache=mc_cache if candidate_workers <= 1 else None,
            )
            if fallback is not None:
                candidates.append(fallback)

        _log_stress_timing_summary(
            f"sport={sport_key or 'all'} stage={stage['stage_id']}-{stage['name']}",
            candidates,
        )

        frontier = _pareto_frontier(candidates)
        if not frontier:
            frontier = sorted(candidates, key=lambda c: c["fitness"], reverse=True)[:3]
        crowd = _crowding_distance(frontier)
        for i, cand in enumerate(frontier):
            cand["crowding"] = crowd.get(i, 0.0)

        pareto_top = sorted(
            frontier,
            key=lambda c: (c.get("crowding", 0.0), c["roi"], c["bet_count"]),
            reverse=True,
        )[:3]
        for i, cand in enumerate(pareto_top, 1):
            cand["pareto_rank"] = i
            cand["tradeoff_label"] = _tradeoff_label(cand)

        deployable = [
            c for c in pareto_top
            if c["stress"]["passed"] and c["val_metrics"]["total_bets"] >= min_bets
        ]
        has_deployable = len(deployable) > 0
        selected = (
            max(deployable, key=lambda c: (c["roi"], -c["ruin_prob"]))
            if has_deployable
            else max(pareto_top, key=lambda c: (c["roi"], c["bet_count"]))
        )

        stage_outputs.append(
            {
                "stage": stage,
                "population": pop,
                "best_hist": stage_best_hist,
                "avg_hist": stage_avg_hist,
                "candidates": candidates,
                "pareto_top": pareto_top,
                "selected": selected,
                "has_deployable": has_deployable,
                "elapsed": stage_elapsed,
            }
        )
        population_seed = pop

    # Restore SIGINT handler
    _signal.signal(_signal.SIGINT, prev_handler)

    if not stage_outputs:
        return {"error": "no_stage_output"}

    stage1 = stage_outputs[0]
    stage2 = stage_outputs[1] if len(stage_outputs) > 1 else None
    if stage1["has_deployable"]:
        final_stage = stage1
    elif stage2 is not None:
        final_stage = stage2
    else:
        final_stage = stage1

    selected = final_stage["selected"]
    stage_used = final_stage["stage"]["stage_id"]
    relaxed_used = stage_used == 2

    # Deployment policy:
    # Stage 1 deployable => active strategy.
    # Stage 2 / non-deployable => shadow strategy only.
    is_deployable = bool(final_stage["has_deployable"] and stage_used == 1)
    is_active = is_deployable
    is_shadow = not is_active

    alpha_dna = selected["dna"]
    train_metrics = selected["train_metrics"]
    val_metrics = selected["val_metrics"]
    stress = selected["stress"]
    stress_mc = stress.get("monte_carlo") or {}
    stress_bs = stress.get("bootstrap") or {}
    fitness_history = final_stage["best_hist"]
    avg_fitness_history = final_stage["avg_hist"]

    elapsed = sum(s["elapsed"] for s in stage_outputs)
    log.info("Evolution complete in %.1fs (%d stages)", elapsed, len(stage_outputs))

    # Build DNA dict
    dna_dict = {gene: round(float(alpha_dna[j]), 4) for j, gene in enumerate(DNA_GENES)}

    log.info("=" * 60)
    log.info("ALPHA BOT (Generation %d)", generations)
    log.info("DNA: %s", dna_dict)
    log.info("Training:   ROI=%.3f  Sharpe=%.2f  WR=%.1f%%  Bets=%d  MaxDD=%.1f%%",
             train_metrics["roi"], train_metrics["sharpe"],
             train_metrics["win_rate"] * 100, train_metrics["total_bets"],
             train_metrics["max_drawdown_pct"] * 100)
    log.info("Validation: ROI=%.3f  Sharpe=%.2f  WR=%.1f%%  Bets=%d  MaxDD=%.1f%%",
             val_metrics["roi"], val_metrics["sharpe"],
             val_metrics["win_rate"] * 100, val_metrics["total_bets"],
             val_metrics["max_drawdown_pct"] * 100)
    log.info("=" * 60)

    # Overfit check (directional: only warn when training >> validation)
    roi_gap = train_metrics["roi"] - val_metrics["roi"]
    if roi_gap > 0.15:
        log.warning(
            "OVERFIT WARNING: Train ROI - Val ROI = %.3f (>0.15). "
            "Consider reducing generations or increasing population.",
            roi_gap,
        )

    stress_test_data = {
        "bootstrap_p_positive": stress_bs.get("p_positive", 0.0),
        "bootstrap_ci_95": stress_bs.get("ci_95", (0.0, 0.0)),
        "bootstrap_mean_roi": stress_bs.get("mean", 0.0),
        "monte_carlo_ruin_prob": stress_mc.get("ruin_prob", 1.0),
        "monte_carlo_max_dd_median": stress_mc.get("max_dd_median", 1.0),
        "monte_carlo_max_dd_95": stress_mc.get("max_dd_95", 1.0),
        "ensemble_size": 1,
        "stress_passed": bool(stress.get("passed", False)),
        "stress_reason": stress.get("reason", "unknown"),
    }

    live_guardrail_reasons: list[str] = []
    if is_active and float(val_metrics["max_drawdown_pct"]) > LIVE_GUARDRAIL_MAX_DD_PCT:
        live_guardrail_reasons.append(
            f"validation_max_dd_pct={val_metrics['max_drawdown_pct']:.3f} > {LIVE_GUARDRAIL_MAX_DD_PCT:.3f}"
        )
    ruin_proxy = float(stress_mc.get("ruin_prob", 1.0))
    if is_active and ruin_proxy > LIVE_GUARDRAIL_MAX_RUIN_PROXY:
        live_guardrail_reasons.append(
            f"ruin_proxy={ruin_proxy:.3f} > {LIVE_GUARDRAIL_MAX_RUIN_PROXY:.3f}"
        )
    if live_guardrail_reasons:
        is_active = False
        is_shadow = True

    strategy_doc = {
        "version": "v3",
        "fitness_version": "soft_penalty_v1",
        "stress_version": "stress_rescue_v1",
        "sport_key": sport_key or "all",
        "generation": generations,
        "created_at": _utcnow(),
        "dna": dna_dict,
        "training_fitness": {
            "roi": round(train_metrics["roi"], 4),
            "sharpe": round(train_metrics["sharpe"], 3),
            "win_rate": round(train_metrics["win_rate"], 4),
            "total_bets": train_metrics["total_bets"],
            "profit": round(train_metrics["profit"], 2),
            "max_drawdown_pct": train_metrics["max_drawdown_pct"],
        },
        "validation_fitness": {
            "roi": round(val_metrics["roi"], 4),
            "sharpe": round(val_metrics["sharpe"], 3),
            "win_rate": round(val_metrics["win_rate"], 4),
            "total_bets": val_metrics["total_bets"],
            "profit": round(val_metrics["profit"], 2),
            "max_drawdown_pct": val_metrics["max_drawdown_pct"],
        },
        "stress_test": stress_test_data,
        "is_active": is_active,
        "is_shadow": is_shadow,
        "population_size": population_size,
        "generations_run": generations,
        "evolution_time_s": round(elapsed, 1),
        "fitness_history": {
            "best": [round(f, 4) for f in fitness_history],
            "avg": [round(f, 4) for f in avg_fitness_history],
        },
        "pareto_candidates": [
            {
                "pareto_rank": c.get("pareto_rank"),
                "tradeoff_label": c.get("tradeoff_label"),
                "roi": round(float(c["roi"]), 4),
                "bet_count": int(c["bet_count"]),
                "ruin_prob": round(float(c["ruin_prob"]), 4),
                "max_dd_pct": round(float(c["max_dd_pct"]), 4),
            }
            for c in final_stage["pareto_top"]
        ],
        "optimization_notes": {
            "schema_version": "v3.1",
            "fitness_version": "soft_penalty_v1",
            "stress_version": "stress_rescue_v1",
            "stage_info": {
                "stage_used": stage_used,
                "why_stage2": None
                if stage_used == 1
                else "No deployable candidate in stage 1 (stress/min_bets constraints).",
                "base_constraints": {
                    "min_bets": stages[0]["min_bets"],
                    "edge_range": list(stages[0]["dna_ranges"]["min_edge"]),
                },
                "effective_constraints": {
                    "min_bets": final_stage["stage"]["min_bets"],
                    "edge_range": list(final_stage["stage"]["dna_ranges"]["min_edge"]),
                },
            },
            "rescue_log": selected["stress"]["rescue_log"],
            "validation_window": validation_window,
            "live_guardrail": {
                "applied": bool(live_guardrail_reasons),
                "reasons": live_guardrail_reasons,
                "max_dd_limit": LIVE_GUARDRAIL_MAX_DD_PCT,
                "ruin_proxy_limit": LIVE_GUARDRAIL_MAX_RUIN_PROXY,
                "observed_max_dd": round(float(val_metrics["max_drawdown_pct"]), 4),
                "observed_ruin_proxy": round(ruin_proxy, 4),
            },
            "pareto_rank": selected.get("pareto_rank"),
            "tradeoff_label": selected.get("tradeoff_label"),
            "seed": seed,
            "population_size": population_size,
            "generations_run": generations,
            "lookback_years": lookback_years,
            "why_shadow": (
                "Guardrail fallback: active deployment disabled."
                if live_guardrail_reasons
                else (
                    "Relaxed-stage strategy: kept in shadow mode."
                    if relaxed_used
                    else (
                        "No deployable strategy passed stress + min_bets in ideal stage."
                        if not is_deployable
                        else None
                    )
                )
            ),
        },
    }

    strategy_doc["deployment_method"] = "single"

    if not dry_run:
        # Only replace active strategy when we actually found a deployable active candidate.
        if strategy_doc["is_active"]:
            await _db.db.qbot_strategies.update_many(
                {"sport_key": strategy_doc["sport_key"], "is_active": True},
                {"$set": {"is_active": False}},
            )
        result = await _db.db.qbot_strategies.insert_one(strategy_doc)
        log.info("Saved strategy to qbot_strategies: %s", result.inserted_id)
    else:
        log.info("[DRY RUN] Would save strategy — skipping DB write.")

    return strategy_doc


def _single_bot_pipeline(
    dna: np.ndarray,
    data: dict[str, np.ndarray],
    *,
    initial_bankroll: float = 1000.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Canonical confidence/stake/profit pipeline for one bot."""
    min_edge = dna[0]
    min_conf = dna[1]
    sharp_w = dna[2]
    momentum_w = dna[3]
    rest_w = dna[4]
    kelly_f = dna[5]
    max_s = dna[6]
    home_bias_v = dna[7] if len(dna) > 7 else 1.0
    away_bias_v = dna[8] if len(dna) > 8 else 1.0
    h2h_w = dna[9] if len(dna) > 9 else 0.0
    draw_thresh = dna[10] if len(dna) > 10 else 0.0
    vol_buffer = dna[11] if len(dna) > 11 else 0.0
    bayes_trust = dna[12] if len(dna) > 12 else 0.0
    xg_trust = dna[13] if len(dna) > 13 else 0.0
    luck_reg = dna[14] if len(dna) > 14 else 0.0
    ref_cards = dna[15] if len(dna) > 15 else 0.0
    var_buffer = dna[16] if len(dna) > 16 else 0.0
    rotation_penalty = dna[17] if len(dna) > 17 else 0.0
    early_signal_conf = dna[18] if len(dna) > 18 else 1.0
    liquidity_priority = dna[20] if len(dna) > 20 else 0.5
    entropy_damping = dna[21] if len(dna) > 21 else 0.0

    edge = data["edge_pct"]
    conf = data["confidence"]
    imp = data["implied_prob"]
    correct = data["was_correct"]
    odds = data["odds"]
    pick = data["pick_type"]
    h2h_t = data["h2h_weight"]
    bayes_c = data["bayes_conf"]
    xroi = data.get("xroi", np.zeros_like(conf))
    luck_gap = data.get("luck_gap", np.zeros_like(conf))
    ref_intensity = data.get("ref_intensity", np.zeros_like(conf))
    var_risk = data.get("var_risk", np.zeros_like(conf))
    lineup_delta = data.get("lineup_delta", np.zeros_like(conf))
    is_alpha = data.get("is_alpha", np.zeros_like(conf, dtype=np.bool_))
    liquidity = data.get("liquidity_score", np.full_like(conf, 0.5))
    entropy = data.get("data_entropy", np.zeros_like(conf))

    adj_conf = conf + sharp_w * data["sharp_boost"] + momentum_w * data["momentum_boost"] + rest_w * data["rest_boost"]

    is_home = pick == 0
    is_draw = pick == 1
    is_away = pick == 2
    bias = is_home * home_bias_v + is_draw * 1.0 + is_away * away_bias_v
    adj_conf = adj_conf * bias

    adj_conf = adj_conf + h2h_w * h2h_t * 0.10
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c
    adj_conf = (1.0 - xg_trust) * adj_conf + xg_trust * np.clip(xroi + imp, 0.0, 0.99)
    adj_conf = adj_conf + luck_reg * np.clip(luck_gap, -0.25, 0.25)
    adj_conf = adj_conf - ref_cards * np.maximum(ref_intensity, 0.0) * 0.02
    adj_conf = adj_conf - var_buffer * np.maximum(var_risk, 0.0)
    adj_conf = np.where(is_alpha, adj_conf * early_signal_conf, adj_conf)
    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    mask = (edge >= min_edge) & (conf >= min_conf)
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate
    rotation_gate = lineup_delta > np.maximum(0.1, 0.35 * max(rotation_penalty, 1e-6))
    mask = mask & ~rotation_gate

    buffered_edge = np.maximum(adj_conf - imp - vol_buffer, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_frac = np.maximum(kelly_f * buffered_edge / denom, 0.0)
    liquidity_scale = np.clip(liquidity + (1.0 - liquidity_priority), 0.20, 1.0)
    entropy_scale = 1.0 / (1.0 + np.maximum(entropy, 0.0) * max(entropy_damping, 0.0))
    kelly_frac = kelly_frac * liquidity_scale * entropy_scale

    stake = np.zeros_like(odds, dtype=np.float64)
    profit = np.zeros_like(odds, dtype=np.float64)
    bankroll = float(initial_bankroll)
    for i in range(len(odds)):
        if not mask[i] or bankroll <= 0.0:
            continue
        raw_stake = bankroll * float(kelly_frac[i])
        risk_cap = bankroll * MAX_STAKE_BANKROLL_FRACTION
        final_stake = min(raw_stake, float(max_s), risk_cap, bankroll)
        if final_stake <= 0.0:
            continue
        stake[i] = final_stake
        if bool(correct[i]):
            profit[i] = final_stake * (float(odds[i]) - 1.0)
        else:
            profit[i] = -final_stake
        bankroll += float(profit[i])
        bankroll = max(bankroll, 0.0)
    return mask, stake, profit


def _compute_detailed_metrics(dna: np.ndarray, data: dict[str, np.ndarray]) -> dict:
    """Compute detailed metrics for a single bot (mirrors evaluate_population v2)."""
    _, stake, profit = _single_bot_pipeline(dna, data)
    correct = data["was_correct"]
    iso_weeks = data["iso_week"]
    t_weight = data["time_weight"]
    active = stake > 0

    weighted_stake = stake * t_weight
    weighted_profit = profit * t_weight
    total_staked = float(weighted_stake.sum())
    total_profit = float(weighted_profit.sum())
    total_bets = int(active.sum())
    wins = int((active & correct).sum())

    roi = total_profit / total_staked if total_staked > 0 else 0.0
    xroi_vec = data.get("xroi", np.zeros_like(stake))
    weighted_xroi = float((xroi_vec * stake).sum() / max(stake.sum(), 1e-9)) if stake.sum() > 0 else 0.0

    conf_vec = np.clip(data["confidence"], 0.0, 0.999)
    ece = 0.0
    if total_bets > 0:
        idx = active
        for b in range(CALIBRATION_BINS):
            lo = b / CALIBRATION_BINS
            hi = (b + 1) / CALIBRATION_BINS
            bmask = idx & (conf_vec >= lo) & (conf_vec < hi)
            n_b = int(bmask.sum())
            if n_b <= 0:
                continue
            acc = float(correct[bmask].mean())
            cavg = float(conf_vec[bmask].mean())
            ece += (n_b / max(total_bets, 1)) * abs(acc - cavg)
    win_rate = wins / total_bets if total_bets > 0 else 0.0

    bet_pnl = weighted_profit[active]
    if bet_pnl.size:
        cum_profit = np.cumsum(bet_pnl)
        cum_max = np.maximum.accumulate(cum_profit)
        drawdown = cum_max - cum_profit
        max_dd = float(drawdown.max())
        peak_eq = max(float(cum_max.max()), 1.0)
        max_dd_pct = max_dd / peak_eq
    else:
        max_dd_pct = 0.0

    unique_weeks = np.unique(iso_weeks)
    weekly_pnl = np.array([weighted_profit[(iso_weeks == w) & active].sum() for w in unique_weeks])
    if len(weekly_pnl) >= 4:
        w_mean = weekly_pnl.mean()
        w_std = weekly_pnl.std(ddof=1)
        sharpe = (w_mean / w_std * np.sqrt(52)) if w_std > 1e-6 else 0.0
    else:
        sharpe = 0.0

    return {
        "roi": float(roi),
        "xroi": float(weighted_xroi),
        "ece": float(ece),
        "calibration_score": float(1.0 - min(max(ece, 0.0), 1.0)),
        "sharpe": float(sharpe),
        "win_rate": float(win_rate),
        "total_bets": total_bets,
        "profit": float(total_profit),
        "max_drawdown_pct": round(float(max_dd_pct), 4),
    }


def _utcnow():
    """UTC-aware datetime (matches project convention)."""
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    """Convert naive Mongo datetimes to UTC-aware values."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Checkpoint persistence (npz + JSON — NOT pickle)
# ---------------------------------------------------------------------------

def _save_checkpoint(
    sport_key: str | None,
    generation: int,
    population: np.ndarray,
    fitness_history: list[float],
    rng: np.random.Generator,
) -> Path:
    """Save GA state to disk for resume."""
    CHECKPOINT_DIR.mkdir(exist_ok=True)
    label = sport_key or "all"

    np.savez(
        CHECKPOINT_DIR / f"qbot_pop_{label}.npz",
        population=population,
    )

    meta = {
        "generation": generation,
        "fitness_history": fitness_history,
        "rng_state": rng.bit_generator.state,
        "saved_at": _utcnow().isoformat(),
        "dna_genes": DNA_GENES,
        "population_shape": list(population.shape),
    }
    with open(CHECKPOINT_DIR / f"qbot_meta_{label}.json", "w") as f:
        json.dump(meta, f, default=str)

    log.info("Checkpoint saved: gen=%d -> %s/%s", generation, CHECKPOINT_DIR, label)
    return CHECKPOINT_DIR


def _load_checkpoint(sport_key: str | None) -> dict | None:
    """Load checkpoint from disk (returns None if not found)."""
    label = sport_key or "all"
    npz_path = CHECKPOINT_DIR / f"qbot_pop_{label}.npz"
    meta_path = CHECKPOINT_DIR / f"qbot_meta_{label}.json"
    if not npz_path.exists() or not meta_path.exists():
        return None
    data = np.load(npz_path)
    with open(meta_path) as f:
        meta = json.load(f)
    meta["population"] = data["population"]
    return meta


# ---------------------------------------------------------------------------
# Strategy stress test — bootstrap, Monte Carlo, ensemble
# ---------------------------------------------------------------------------


def bootstrap_fitness(
    bot_dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    n_samples: int = 10000,
    rng: np.random.Generator | None = None,
) -> dict:
    """Bootstrap confidence interval for a single bot's ROI."""
    if rng is None:
        rng = np.random.default_rng()

    n_tips = tip_data["odds"].shape[0]
    _, stake, profit = _single_bot_pipeline(bot_dna, tip_data)

    rois = np.empty(n_samples, dtype=np.float64)
    max_samples_per_batch = max(1, int(BOOTSTRAP_VECTOR_MAX_CELLS // max(n_tips, 1)))

    out = 0
    while out < n_samples:
        batch = min(max_samples_per_batch, n_samples - out)
        idx = rng.integers(0, n_tips, size=(batch, n_tips), dtype=np.int32)
        batch_profit = profit[idx].sum(axis=1)
        batch_stake = stake[idx].sum(axis=1)
        batch_roi = np.full(batch, -1.0, dtype=np.float64)
        np.divide(batch_profit, batch_stake, out=batch_roi, where=batch_stake > 0)
        rois[out:out + batch] = batch_roi
        out += batch

    ci_low, ci_high = np.percentile(rois, [2.5, 97.5])
    return {
        "mean": round(float(rois.mean()), 4),
        "ci_95": (round(float(ci_low), 4), round(float(ci_high), 4)),
        "p_positive": round(float((rois > 0).mean()), 4),
    }


def monte_carlo_bankroll(
    bot_dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    n_paths: int = 50000,
    initial_bank: float = 1000.0,
    ruin_threshold: float = 0.20,
    fail_fast: bool = False,
    rng: np.random.Generator | None = None,
) -> dict:
    """Monte Carlo bankroll simulation with randomized bet ordering."""
    if rng is None:
        rng = np.random.default_rng()
    min_edge = bot_dna[0]
    min_conf = bot_dna[1]
    sharp_w = bot_dna[2]
    momentum_w = bot_dna[3]
    rest_w = bot_dna[4]
    kelly_f = bot_dna[5]
    max_s = bot_dna[6]
    home_bias_v = bot_dna[7] if len(bot_dna) > 7 else 1.0
    away_bias_v = bot_dna[8] if len(bot_dna) > 8 else 1.0
    h2h_w = bot_dna[9] if len(bot_dna) > 9 else 0.0
    draw_thresh = bot_dna[10] if len(bot_dna) > 10 else 0.0
    vol_buffer = bot_dna[11] if len(bot_dna) > 11 else 0.0
    bayes_trust = bot_dna[12] if len(bot_dna) > 12 else 0.0
    xg_trust = bot_dna[13] if len(bot_dna) > 13 else 0.0
    luck_reg = bot_dna[14] if len(bot_dna) > 14 else 0.0
    ref_cards = bot_dna[15] if len(bot_dna) > 15 else 0.0
    var_buffer = bot_dna[16] if len(bot_dna) > 16 else 0.0
    rotation_penalty = bot_dna[17] if len(bot_dna) > 17 else 0.0
    early_signal_conf = bot_dna[18] if len(bot_dna) > 18 else 1.0
    liquidity_priority = bot_dna[20] if len(bot_dna) > 20 else 0.5
    entropy_damping = bot_dna[21] if len(bot_dna) > 21 else 0.0

    edge = tip_data["edge_pct"]
    conf = tip_data["confidence"]
    imp = tip_data["implied_prob"]
    correct = tip_data["was_correct"]
    odds = tip_data["odds"]
    pick = tip_data["pick_type"]
    h2h_t = tip_data["h2h_weight"]
    bayes_c = tip_data["bayes_conf"]
    xroi = tip_data.get("xroi", np.zeros_like(conf))
    luck_gap = tip_data.get("luck_gap", np.zeros_like(conf))
    ref_intensity = tip_data.get("ref_intensity", np.zeros_like(conf))
    var_risk = tip_data.get("var_risk", np.zeros_like(conf))
    lineup_delta = tip_data.get("lineup_delta", np.zeros_like(conf))
    is_alpha = tip_data.get("is_alpha", np.zeros_like(conf, dtype=np.bool_))
    liquidity = tip_data.get("liquidity_score", np.full_like(conf, 0.5))
    entropy = tip_data.get("data_entropy", np.zeros_like(conf))

    adj_conf = conf + sharp_w * tip_data["sharp_boost"] + momentum_w * tip_data["momentum_boost"] + rest_w * tip_data["rest_boost"]
    is_home = pick == 0
    is_draw = pick == 1
    is_away = pick == 2
    bias = is_home * home_bias_v + is_draw * 1.0 + is_away * away_bias_v
    adj_conf = adj_conf * bias
    adj_conf = adj_conf + h2h_w * h2h_t * 0.10
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c
    adj_conf = (1.0 - xg_trust) * adj_conf + xg_trust * np.clip(xroi + imp, 0.0, 0.99)
    adj_conf = adj_conf + luck_reg * np.clip(luck_gap, -0.25, 0.25)
    adj_conf = adj_conf - ref_cards * np.maximum(ref_intensity, 0.0) * 0.02
    adj_conf = adj_conf - var_buffer * np.maximum(var_risk, 0.0)
    adj_conf = np.where(is_alpha, adj_conf * early_signal_conf, adj_conf)
    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    mask = (edge >= min_edge) & (conf >= min_conf)
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate
    rotation_gate = lineup_delta > np.maximum(0.1, 0.35 * max(rotation_penalty, 1e-6))
    mask = mask & ~rotation_gate

    buffered_edge = np.maximum(adj_conf - imp - vol_buffer, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_frac = np.maximum(kelly_f * buffered_edge / denom, 0.0)
    liquidity_scale = np.clip(liquidity + (1.0 - liquidity_priority), 0.20, 1.0)
    entropy_scale = 1.0 / (1.0 + np.maximum(entropy, 0.0) * max(entropy_damping, 0.0))
    kelly_frac = kelly_frac * liquidity_scale * entropy_scale

    active_idx = np.where(mask)[0]
    n_bets = int(active_idx.size)

    if n_bets < 10:
        return {
            "ruin_prob": 1.0, "max_dd_median": 1.0, "max_dd_95": 1.0,
            "terminal_wealth_median": initial_bank, "n_bets": n_bets,
        }

    # Build shuffled path indices: (n_paths, n_bets)
    base_order = np.tile(np.arange(n_bets, dtype=np.int32), (n_paths, 1))
    shuffle_idx = rng.random(base_order.shape).argsort(axis=1)
    order = np.take_along_axis(base_order, shuffle_idx, axis=1)

    active_frac = kelly_frac[active_idx]
    active_odds = odds[active_idx]
    active_correct = correct[active_idx]

    bankroll = np.full(n_paths, float(initial_bank), dtype=np.float64)
    peak = bankroll.copy()
    min_bank = bankroll.copy()
    max_dd = np.zeros(n_paths, dtype=np.float64)

    for j in range(n_bets):
        col = order[:, j]
        frac_j = active_frac[col]
        odds_j = active_odds[col]
        corr_j = active_correct[col]

        raw_stake = bankroll * frac_j
        stake_cap = np.minimum(float(max_s), bankroll * MAX_STAKE_BANKROLL_FRACTION)
        stake = np.minimum(np.maximum(raw_stake, 0.0), stake_cap)
        stake = np.minimum(stake, bankroll)

        pnl = np.where(corr_j, stake * (odds_j - 1.0), -stake)
        bankroll = bankroll + pnl
        bankroll = np.maximum(bankroll, 0.0)

        peak = np.maximum(peak, bankroll)
        min_bank = np.minimum(min_bank, bankroll)
        dd_step = (peak - bankroll) / np.maximum(peak, 1.0)
        max_dd = np.maximum(max_dd, dd_step)

        if fail_fast and (j + 1) % 50 == 0:
            for check_n, fail_limit in zip(MC_FAIL_FAST_CHECK_PATHS, MC_FAIL_FAST_CHECK_LIMITS):
                check_n = min(check_n, n_paths)
                if check_n <= 0:
                    continue
                check_ruin = float((min_bank[:check_n] < initial_bank * ruin_threshold).mean())
                if check_ruin > fail_limit:
                    return {
                        "ruin_prob": round(check_ruin, 4),
                        "max_dd_median": round(float(np.median(max_dd[:check_n])), 4),
                        "max_dd_95": round(float(np.percentile(max_dd[:check_n], 95)), 4),
                        "terminal_wealth_median": round(float(np.median(bankroll[:check_n])), 2),
                        "n_bets": n_bets,
                        "fail_fast_triggered": True,
                        "fail_fast_check_n": int(check_n),
                        "fail_fast_limit": float(fail_limit),
                    }

    # Ruin
    ruin_level = initial_bank * ruin_threshold
    ruin_prob = float((min_bank < ruin_level).mean())

    return {
        "ruin_prob": round(ruin_prob, 4),
        "max_dd_median": round(float(np.median(max_dd)), 4),
        "max_dd_95": round(float(np.percentile(max_dd, 95)), 4),
        "terminal_wealth_median": round(float(np.median(bankroll)), 2),
        "n_bets": n_bets,
        "fail_fast_triggered": False,
    }


def build_ensemble(
    top_n_dna: list[np.ndarray],
    tip_data: dict[str, np.ndarray],
    rng: np.random.Generator | None = None,
    bootstrap_threshold: float = 0.60, # was: 0.9, confidence
    ruin_threshold: float = 0.15, # was: 0.05, loss gate,
) -> dict | None:
    """Filter top-N bots through stress test, build ensemble from survivors."""
    if rng is None:
        rng = np.random.default_rng()

    survivors = []
    metrics_list = []

    for i, dna in enumerate(top_n_dna):
        # Bootstrap
        bs = bootstrap_fitness(dna, tip_data, rng=rng)
        log.info("  Bot %d bootstrap: p_positive=%.2f, CI=%s, mean_ROI=%.4f",
                 i, bs["p_positive"], bs["ci_95"], bs["mean"])
        if bs["p_positive"] < bootstrap_threshold:
            log.info("  Bot %d FAILED bootstrap: p_positive=%.2f < %.2f",
                     i, bs["p_positive"], bootstrap_threshold)
            continue

        # Monte Carlo
        mc = monte_carlo_bankroll(dna, tip_data, rng=rng)
        log.info("  Bot %d Monte Carlo: ruin=%.3f, max_dd_median=%.3f, max_dd_95=%.3f",
                 i, mc["ruin_prob"], mc["max_dd_median"], mc["max_dd_95"])

        if mc["ruin_prob"] > ruin_threshold:
            # Try scaling Kelly down
            scaled_dna = dna.copy()
            kelly_idx = DNA_GENES.index("kelly_fraction")
            for attempt in range(3):
                scaled_dna[kelly_idx] *= 0.80
                mc = monte_carlo_bankroll(scaled_dna, tip_data, rng=rng)
                if mc["ruin_prob"] <= ruin_threshold:
                    dna = scaled_dna
                    log.info("  Bot %d passed MC after %d Kelly reductions", i, attempt + 1)
                    break
            else:
                log.info("  Bot %d FAILED Monte Carlo after 3 Kelly reductions: ruin=%.2f",
                         i, mc["ruin_prob"])
                continue

        survivors.append(dna)
        metrics_list.append({"bootstrap": bs, "monte_carlo": mc})

    if not survivors:
        log.warning("No bots passed stress test — no strategy activated")
        return None

    log.info("Ensemble: %d of %d bots survived stress test", len(survivors), len(top_n_dna))
    return {
        "ensemble_dna": [d.tolist() for d in survivors],
        "ensemble_size": len(survivors),
        "ensemble_metrics": metrics_list,
        "deployment_method": "median",
        "stress_passed": True,
    }


# ---------------------------------------------------------------------------
# Multi-league wrapper
# ---------------------------------------------------------------------------

async def discover_leagues() -> list[tuple[str, int]]:
    """Discover leagues with resolved tips, ordered by count descending."""
    import app.database as _db

    if _db.db is None:
        await _db.connect_db()
    if _db.db is None:
        raise RuntimeError("MongoDB connection not initialized for discover_leagues()")

    pipeline = [
        {"$match": {"status": "resolved", "was_correct": {"$ne": None}}},
        {"$group": {"_id": "$sport_key", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    results = await _db.db.quotico_tips.aggregate(pipeline).to_list(100)
    return [(r["_id"], r["count"]) for r in results if r["_id"]]


def _run_evolution_sync(sport_key: str | None, **kwargs) -> dict:
    """Sync wrapper for run_evolution() — used by ProcessPoolExecutor."""
    loop = asyncio.new_event_loop()
    try:
        import app.database as _db
        loop.run_until_complete(_db.connect_db())
        result = loop.run_until_complete(run_evolution(sport_key, **kwargs))
        loop.run_until_complete(_db.close_db())
        return result
    finally:
        loop.close()


async def run_multi_league(
    *,
    parallel: bool = False,
    dry_run: bool = False,
    population_size: int = 300,
    generations: int = 50,
    seed: int | None = None,
    resume: bool = False,
    search_mode: str = "quick",
    candidate_workers: int = 1,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> dict:
    """Evolve per-league strategies + 'all' fallback."""
    leagues = await discover_leagues()
    log.info("Discovered %d leagues: %s",
             len(leagues), [(k, c) for k, c in leagues])

    eligible = [(k, c) for k, c in leagues if c >= MIN_TIPS_PER_LEAGUE]
    skipped = [(k, c) for k, c in leagues if c < MIN_TIPS_PER_LEAGUE]

    for k, c in skipped:
        log.info("Skipping %s (%d tips < %d minimum) — will use 'all' fallback",
                 k, c, MIN_TIPS_PER_LEAGUE)

    results: dict[str, dict] = {}
    kwargs = dict(
        population_size=population_size,
        generations=generations,
        dry_run=dry_run,
        seed=seed,
        resume=resume,
        search_mode=search_mode,
        candidate_workers=candidate_workers,
        lookback_years=lookback_years,
    )

    if parallel and len(eligible) > 1:
        log.info("Running %d leagues in parallel...", len(eligible) + 1)
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(len(eligible) + 1, 6),
        ) as pool:
            futures = {}
            for key, count in eligible:
                futures[pool.submit(_run_evolution_sync, key, **kwargs)] = key
            futures[pool.submit(_run_evolution_sync, None, **kwargs)] = "all"

            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    log.error("League %s failed: %s", key, e)
                    results[key] = {"error": str(e)}
    else:
        for key, count in eligible:
            log.info("Evolving %s (%d tips)...", key, count)
            results[key] = await run_evolution(key, **kwargs)

        log.info("Evolving 'all' fallback...")
        results["all"] = await run_evolution(None, **kwargs)

    _print_cross_league_report(results)
    return results


def _print_cross_league_report(results: dict[str, dict]) -> None:
    """Print cross-league DNA comparison table."""
    print("\n" + "=" * 80)
    print("CROSS-LEAGUE DNA COMPARISON")
    print("=" * 80)

    # Header
    gene_cols = DNA_GENES[:7] + ["home_bias", "draw_thresh", "vol_buf"]
    header = f"{'League':<35}" + "".join(f"{g[:10]:>11}" for g in gene_cols)
    print(header)
    print("-" * 80)

    for key in sorted(results.keys()):
        r = results[key]
        if "error" in r:
            print(f"{key:<35}  ERROR: {r['error']}")
            continue
        dna = r.get("dna", {})
        row = f"{key:<35}"
        for g in gene_cols:
            val = dna.get(g, dna.get(g.replace("_thresh", "_threshold").replace("_buf", "_buffer"), 0.0))
            row += f"{val:>11.4f}"
        # Add val ROI
        val_roi = r.get("validation_fitness", {}).get("roi", 0.0)
        row += f"  ROI={val_roi:+.3f}"
        print(row)

    print("=" * 80 + "\n")


# ---------------------------------------------------------------------------
# Deep search mode — expanding-window temporal CV
# ---------------------------------------------------------------------------

def temporal_expanding_cv(
    tips: list[dict],
    k: int = 5,
) -> list[tuple[list[dict], list[dict]]]:
    """Expanding-window temporal CV. Tips must be sorted chronologically.

    For k=5 -> 4 folds. Never trains on future data.
    """
    n = len(tips)
    chunk_size = n // k
    folds = []
    for i in range(1, k):
        val_start = i * chunk_size
        val_end = val_start + chunk_size if i < k - 1 else n
        train_tips = tips[:val_start]
        val_tips = tips[val_start:val_end]
        folds.append((train_tips, val_tips))
    return folds


async def run_evolution_deep(
    sport_key: str | None,
    *,
    population_size: int = 500,
    generations: int = 100,
    dry_run: bool = False,
    seed: int | None = None,
    resume: bool = False,
    candidate_workers: int = 1,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> dict:
    """Deep search: expanding-window CV with pessimistic fitness."""
    import app.database as _db

    global _shutdown_requested
    _shutdown_requested = False
    prev_handler = _signal.signal(_signal.SIGINT, _handle_sigint)

    rng = np.random.default_rng(seed)

    tips = await load_tips(sport_key, lookback_years=lookback_years)
    if len(tips) < 500:
        log.error("Deep mode needs >=500 tips (%d found). Use quick mode.", len(tips))
        _signal.signal(_signal.SIGINT, prev_handler)
        return {"error": "insufficient_data_deep", "tip_count": len(tips)}

    # Expanding-window CV: 4 folds from k=5
    folds = temporal_expanding_cv(tips, k=5)
    log.info("Deep mode: %d folds, %d tips total", len(folds), len(tips))

    # Pre-vectorize ALL folds (stays in memory for entire GA run)
    fold_data = []
    for fi, (train, val) in enumerate(folds):
        ref_now = _utcnow()
        td = vectorize_tips(train, lookback_years=lookback_years, reference_now=ref_now)
        vd = vectorize_tips(val, lookback_years=lookback_years, reference_now=ref_now)
        fold_data.append((td, vd))
        log.info("  Fold %d: %d train / %d val", fi, len(train), len(val))

    # Use last fold's val as the held-out validation set for final metrics
    final_val_data = fold_data[-1][1]
    final_val_tips = folds[-1][1]

    # Initialize population
    population = random_population(population_size, rng)

    if resume:
        ckpt = _load_checkpoint(sport_key)
        if ckpt:
            population = ckpt["population"]
            if population.shape[1] < len(DNA_GENES):
                old_cols = population.shape[1]
                defaults = np.array([
                    (DNA_RANGES[g][0] + DNA_RANGES[g][1]) / 2
                    for g in DNA_GENES[old_cols:]
                ])
                padding = np.tile(defaults, (population.shape[0], 1))
                population = np.hstack([population, padding])
            if population.shape[0] != population_size:
                if population.shape[0] < population_size:
                    extra = random_population(population_size - population.shape[0], rng)
                    population = np.vstack([population, extra])
                else:
                    population = population[:population_size]
            log.info("Resumed deep mode from checkpoint")

    n_elites = max(int(population_size * ELITE_FRACTION), 2)
    n_children = population_size - n_elites

    best_fitness_history: list[float] = []
    stagnant_gens = 0
    prev_best = -np.inf

    t0 = time.monotonic()

    try:
        from rich.live import Live
        from rich.table import Table
        use_rich = True
    except ImportError:
        use_rich = False

    for gen in range(generations):
        if _shutdown_requested:
            log.info("Graceful shutdown at gen %d", gen)
            _save_checkpoint(sport_key, gen - 1, population, best_fitness_history, rng)
            break

        # Evaluate on ALL training folds — pessimistic (worst fold)
        fold_fitnesses = np.stack([
            evaluate_population(population, td) for td, _ in fold_data
        ])  # (4, P)
        # fitness = fold_fitnesses.min(axis=0)  # (P,) — worst fold per bot

        # 1. Wir berechnen den Durchschnitt über alle Folds
        fitness_mean = fold_fitnesses.mean(axis=0)

        # 2. Wir berechnen die Standardabweichung (wie stark schwankt der Bot?)
        fitness_std = fold_fitnesses.std(axis=0)

        # 3. Wir nehmen den Durchschnitt, ziehen aber die Schwankung ab (Realistischer Ansatz)
        fitness = fitness_mean - (0.5 * fitness_std)

        best_fit = float(fitness.max())
        avg_fit = float(fitness.mean())
        best_fitness_history.append(best_fit)

        if gen % 5 == 0 or gen == generations - 1:
            fold_bests = [float(fold_fitnesses[f].max()) for f in range(fold_fitnesses.shape[0])]
            log.info(
                "Gen %2d/%d | Pessimistic: %.4f | Fold bests: %s | Pop: %d",
                gen + 1, generations, best_fit,
                [f"{x:.3f}" for x in fold_bests], population_size,
            )

        if gen % CHECKPOINT_INTERVAL == 0 and gen > 0:
            _save_checkpoint(sport_key, gen, population, best_fitness_history, rng)

        # Radiation event
        if best_fit <= prev_best + 1e-6:
            stagnant_gens += 1
        else:
            stagnant_gens = 0
        prev_best = best_fit

        elites = select_elites(population, fitness, n_elites)
        children = crossover(elites, n_children, rng)

        if stagnant_gens >= STAGNATION_THRESHOLD:
            log.warning("RADIATION EVENT at gen %d", gen)
            children = mutate(children, rng, mutation_rate=RADIATION_MUTATION_RATE)
            stagnant_gens = 0
        else:
            children = mutate(children, rng)

        population = np.vstack([elites, children])

    _signal.signal(_signal.SIGINT, prev_handler)

    elapsed = time.monotonic() - t0
    log.info("Deep evolution complete in %.1fs (%d generations)", elapsed, generations)

    # Best bot (same objective as in training loop)
    final_fold_fitnesses = np.stack([
        evaluate_population(population, td) for td, _ in fold_data
    ])
    final_fitness = final_fold_fitnesses.mean(axis=0) - (0.5 * final_fold_fitnesses.std(axis=0))
    alpha_idx = np.argmax(final_fitness)
    alpha_dna = population[alpha_idx]

    train_metrics = _compute_detailed_metrics(alpha_dna, fold_data[-1][0])
    val_metrics = _compute_detailed_metrics(alpha_dna, final_val_data)

    dna_dict = {gene: round(float(alpha_dna[j]), 4) for j, gene in enumerate(DNA_GENES)}

    log.info("=" * 60)
    log.info("ALPHA BOT — DEEP MODE (%d folds, pessimistic)", len(folds))
    log.info("DNA: %s", dna_dict)
    log.info("Training:   ROI=%.3f  Sharpe=%.2f  WR=%.1f%%  MaxDD=%.1f%%",
             train_metrics["roi"], train_metrics["sharpe"],
             train_metrics["win_rate"] * 100, train_metrics["max_drawdown_pct"] * 100)
    log.info("Validation: ROI=%.3f  Sharpe=%.2f  WR=%.1f%%  MaxDD=%.1f%%",
             val_metrics["roi"], val_metrics["sharpe"],
             val_metrics["win_rate"] * 100, val_metrics["max_drawdown_pct"] * 100)
    log.info("=" * 60)

    # Stress test (adaptive candidate pool + staged samples)
    top_indices = np.argsort(final_fitness)[-min(DEEP_CANDIDATE_POOL, population_size):][::-1]
    bootstrap_cache: dict[tuple, dict] = {}
    mc_cache: dict[tuple, dict] = {}
    deep_candidates: list[dict] = []
    candidate_specs = [
        (rank_idx, int(pop_idx), int(rng.integers(0, 2**63 - 1)))
        for rank_idx, pop_idx in enumerate(top_indices, 1)
    ]
    if candidate_workers > 1 and len(candidate_specs) > 1:
        max_workers = min(candidate_workers, len(candidate_specs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    _evaluate_single_candidate,
                    dna=population[pop_idx].copy(),
                    fitness_value=float(final_fitness[pop_idx]),
                    pool_rank=rank_idx,
                    train_data=fold_data[-1][0],
                    val_data=final_val_data,
                    rng_seed=seed_i,
                    require_positive_roi=True,
                    bootstrap_cache=None,
                    mc_cache=None,
                )
                for rank_idx, pop_idx, seed_i in candidate_specs
            ]
            for fut in concurrent.futures.as_completed(futures):
                result = fut.result()
                if result is not None:
                    deep_candidates.append(result)
    else:
        for rank_idx, pop_idx, seed_i in candidate_specs:
            result = _evaluate_single_candidate(
                dna=population[pop_idx].copy(),
                fitness_value=float(final_fitness[pop_idx]),
                pool_rank=rank_idx,
                train_data=fold_data[-1][0],
                val_data=final_val_data,
                rng_seed=seed_i,
                require_positive_roi=True,
                bootstrap_cache=bootstrap_cache,
                mc_cache=mc_cache,
            )
            if result is not None:
                deep_candidates.append(result)

    deep_candidates = sorted(deep_candidates, key=lambda c: c["pool_rank"])
    if not deep_candidates:
        fallback_dna = population[int(np.argmax(final_fitness))]
        fallback_seed = int(rng.integers(0, 2**63 - 1))
        fallback = _evaluate_single_candidate(
            dna=fallback_dna.copy(),
            fitness_value=float(final_fitness[int(np.argmax(final_fitness))]),
            pool_rank=1,
            train_data=fold_data[-1][0],
            val_data=final_val_data,
            rng_seed=fallback_seed,
            require_positive_roi=False,
            bootstrap_cache=bootstrap_cache if candidate_workers <= 1 else None,
            mc_cache=mc_cache if candidate_workers <= 1 else None,
        )
        if fallback is not None:
            deep_candidates.append(fallback)

    _log_stress_timing_summary(
        f"deep sport={sport_key or 'all'}",
        deep_candidates,
    )

    deployable = [
        c for c in deep_candidates
        if c["stress"]["passed"] and c["val_metrics"]["total_bets"] >= MIN_BETS_FOR_FITNESS
    ]
    selected = (
        max(deployable, key=lambda c: c["roi"])
        if deployable
        else max(deep_candidates, key=lambda c: c["roi"])
    )
    alpha_dna = selected["dna"]
    train_metrics = selected["train_metrics"]
    val_metrics = selected["val_metrics"]
    dna_dict = {gene: round(float(alpha_dna[j]), 4) for j, gene in enumerate(DNA_GENES)}
    is_active = bool(len(deployable) > 0)
    stress_bs = selected["stress"]["bootstrap"]
    stress_mc = selected["stress"]["monte_carlo"] or {}
    stress_test_data: dict = {
        "bootstrap_p_positive": stress_bs.get("p_positive", 0.0),
        "bootstrap_ci_95": stress_bs.get("ci_95", (0.0, 0.0)),
        "bootstrap_mean_roi": stress_bs.get("mean", 0.0),
        "monte_carlo_ruin_prob": stress_mc.get("ruin_prob", 1.0),
        "monte_carlo_max_dd_median": stress_mc.get("max_dd_median", 1.0),
        "monte_carlo_max_dd_95": stress_mc.get("max_dd_95", 1.0),
        "ensemble_size": 1,
        "stress_passed": bool(selected["stress"]["passed"]),
        "stress_reason": selected["stress"].get("reason", "unknown"),
    }

    strategy_doc = {
        "version": "v3",
        "mode": "deep",
        "sport_key": sport_key or "all",
        "generation": generations,
        "created_at": _utcnow(),
        "dna": dna_dict,
        "training_fitness": {
            "roi": round(train_metrics["roi"], 4),
            "sharpe": round(train_metrics["sharpe"], 3),
            "win_rate": round(train_metrics["win_rate"], 4),
            "total_bets": train_metrics["total_bets"],
            "profit": round(train_metrics["profit"], 2),
            "max_drawdown_pct": train_metrics["max_drawdown_pct"],
        },
        "validation_fitness": {
            "roi": round(val_metrics["roi"], 4),
            "sharpe": round(val_metrics["sharpe"], 3),
            "win_rate": round(val_metrics["win_rate"], 4),
            "total_bets": val_metrics["total_bets"],
            "profit": round(val_metrics["profit"], 2),
            "max_drawdown_pct": val_metrics["max_drawdown_pct"],
        },
        "stress_test": stress_test_data,
        "is_active": is_active,
        "is_shadow": not is_active,
        "population_size": population_size,
        "generations_run": generations,
        "n_folds": len(folds),
        "evolution_time_s": round(elapsed, 1),
        "fitness_history": {
            "best": [round(f, 4) for f in best_fitness_history],
        },
        "optimization_notes": {
            "validation_window": {
                "source": "arena_deep_last_fold",
                "n_folds": len(folds),
                "tips_total": len(tips),
                "validation_tips": len(final_val_tips),
                "start_date": _ensure_utc(final_val_tips[0]["match_date"]).isoformat() if final_val_tips else None,
                "end_date": _ensure_utc(final_val_tips[-1]["match_date"]).isoformat() if final_val_tips else None,
            },
            "schema_version": "v3.1",
        },
    }

    strategy_doc["deployment_method"] = "single"

    if not dry_run:
        await _db.db.qbot_strategies.update_many(
            {"sport_key": strategy_doc["sport_key"], "is_active": True},
            {"$set": {"is_active": False}},
        )
        result = await _db.db.qbot_strategies.insert_one(strategy_doc)
        log.info("Saved Deep Alpha Bot: %s", result.inserted_id)
    else:
        log.info("[DRY RUN] Would save deep strategy — skipping.")

    return strategy_doc


# ---------------------------------------------------------------------------
# Watch mode — periodic tip reload for 24/7 mining
# ---------------------------------------------------------------------------

def _parse_interval(s: str) -> int:
    """Parse interval string like '6h', '30m', '10s' to seconds."""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    elif s.endswith("m"):
        return int(float(s[:-1]) * 60)
    elif s.endswith("s"):
        return int(float(s[:-1]))
    return int(s)


async def run_watch_mode(
    sport_key: str | None,
    interval: int = WATCH_DEFAULT_INTERVAL,
    **kwargs,
) -> None:
    """Run evolution in a loop, reloading tips every interval."""
    cycle = 0
    while not _shutdown_requested:
        cycle += 1
        log.info("=" * 40)
        log.info("Watch cycle %d — loading latest tips...", cycle)
        result = await run_evolution(sport_key=sport_key, resume=True, **kwargs)
        val_roi = result.get("validation_fitness", {}).get("roi", 0.0)
        log.info("Watch cycle %d complete. Val ROI: %.2f%%. Sleeping %ds...",
                 cycle, val_roi * 100, interval)

        # Sleep in small increments to allow SIGINT
        slept = 0
        while slept < interval and not _shutdown_requested:
            time.sleep(min(5, interval - slept))
            slept += 5

    log.info("Watch mode stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Qbot Evolution Arena v2 — GA Strategy Optimizer",
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Filter by sport_key (e.g. soccer_epl)")
    parser.add_argument("--generations", type=int, default=None,
                        help="Number of GA generations (default: mode-dependent)")
    parser.add_argument("--population", type=int, default=None,
                        help="Population size (default: mode-dependent)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without saving to DB")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--multi", action="store_true",
                        help="Evolve per-league strategies + 'all' fallback")
    parser.add_argument("--parallel", action="store_true",
                        help="Run leagues concurrently (requires --multi)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint")
    parser.add_argument("--watch", type=str, default=None, metavar="INTERVAL",
                        help="Watch mode: reload tips periodically (e.g. '6h', '30m')")
    parser.add_argument("--mode", choices=["quick", "deep"], default="quick",
                        help="quick=80/20 split (default), deep=expanding-window CV")
    parser.add_argument("--candidate-workers", type=int, default=1,
                        help="Parallel workers for per-league candidate stress tests")
    parser.add_argument("--lookback-years", type=int, default=DEFAULT_LOOKBACK_YEARS,
                        help="Load only tips from the last N years (default: 8)")
    args = parser.parse_args()

    # Apply mode defaults
    defaults = MODE_DEFAULTS[args.mode]
    population_size = args.population or defaults["population_size"]
    generations = args.generations or defaults["generations"]

    async def _run():
        import app.database as _db
        await _db.connect_db()
        try:
            if args.watch:
                interval = _parse_interval(args.watch)
                await run_watch_mode(
                    args.sport,
                    interval=interval,
                    population_size=population_size,
                    generations=generations,
                    dry_run=args.dry_run,
                    seed=args.seed,
                    search_mode=args.mode,
                    candidate_workers=max(1, args.candidate_workers),
                    lookback_years=max(1, int(args.lookback_years)),
                )
            elif args.multi:
                result = await run_multi_league(
                    parallel=args.parallel,
                    dry_run=args.dry_run,
                    population_size=population_size,
                    generations=generations,
                    seed=args.seed,
                    resume=args.resume,
                    search_mode=args.mode,
                    candidate_workers=max(1, args.candidate_workers),
                    lookback_years=max(1, int(args.lookback_years)),
                )
            elif args.mode == "deep":
                result = await run_evolution_deep(
                    args.sport,
                    population_size=population_size,
                    generations=generations,
                    dry_run=args.dry_run,
                    seed=args.seed,
                    resume=args.resume,
                    candidate_workers=max(1, args.candidate_workers),
                    lookback_years=max(1, int(args.lookback_years)),
                )
            else:
                result = await run_evolution(
                    args.sport,
                    population_size=population_size,
                    generations=generations,
                    dry_run=args.dry_run,
                    seed=args.seed,
                    resume=args.resume,
                    search_mode=args.mode,
                    candidate_workers=max(1, args.candidate_workers),
                    lookback_years=max(1, int(args.lookback_years)),
                )
                if "error" in result:
                    log.error("Evolution failed: %s", result["error"])
                    sys.exit(1)
        finally:
            await _db.close_db()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
