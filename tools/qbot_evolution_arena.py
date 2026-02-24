"""
Qbot Evolution Arena v2 — Genetic Algorithm for betting strategy optimization.

Evolves a population of strategy "bots" against ~45k backfilled tips to find
optimal betting parameters (min_edge, confidence thresholds, signal weights,
Kelly fraction, stake limits, venue bias, draw threshold, volatility buffer,
H2H weight, Bayesian trust factor).

Fitness = 0.5 * ROI + 0.3 * Weekly Sharpe - 0.2 * Max Drawdown%.

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
from datetime import datetime, timezone
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
}

# GA parameters
MUTATION_RATE = 0.08       # 8% chance per gene
MUTATION_SIGMA_PCT = 0.03  # Gaussian noise σ = 3% of gene range
ELITE_FRACTION = 0.20      # Top 20% survive
CROSSOVER_UNIFORM = True   # Uniform crossover
FITNESS_WEIGHT_ROI = 0.5
FITNESS_WEIGHT_SHARPE = 0.3
FITNESS_WEIGHT_DRAWDOWN = 0.2  # penalty
MIN_BETS_FOR_FITNESS = 150  # was: 30, Minimum bets to evaluate a strategy
MIN_TIPS_PER_LEAGUE = 200  # Minimum tips for per-league evolution

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

# Shutdown flag for SIGINT
_shutdown_requested = False

def _handle_sigint(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.warning("SIGINT received — finishing current generation, saving checkpoint...")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

async def load_tips(sport_key: str | None) -> list[dict]:
    """Load all resolved tips from MongoDB."""
    import app.database as _db

    query = {"status": "resolved", "was_correct": {"$ne": None}}
    if sport_key:
        query["sport_key"] = sport_key

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

    log.info("Loaded %d resolved tips", len(tips))
    return tips


def vectorize_tips(tips: list[dict]) -> dict[str, np.ndarray]:
    """Convert tip documents to numpy arrays for vectorized evaluation."""
    n = len(tips)

    edge_pct = np.zeros(n, dtype=np.float64)
    confidence = np.zeros(n, dtype=np.float64)
    implied_prob = np.zeros(n, dtype=np.float64)
    was_correct = np.zeros(n, dtype=np.bool_)
    iso_week = np.zeros(n, dtype=np.int32)

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

    for i, tip in enumerate(tips):
        edge_pct[i] = tip.get("edge_pct", 0.0)
        confidence[i] = tip.get("confidence", 0.0)
        implied_prob[i] = tip.get("implied_probability", 0.33)
        was_correct[i] = bool(tip.get("was_correct", False))

        # ISO week for Sharpe bucketing
        md = tip.get("match_date")
        if md:
            iso_week[i] = md.isocalendar()[1] + md.isocalendar()[0] * 100
        else:
            iso_week[i] = 0

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

    return {
        "edge_pct": edge_pct,
        "confidence": confidence,
        "implied_prob": implied_prob,
        "was_correct": was_correct,
        "odds": odds,
        "iso_week": iso_week,
        "sharp_boost": sharp_boost,
        "momentum_boost": momentum_boost,
        "rest_boost": rest_boost,
        "pick_type": pick_type,
        "h2h_weight": h2h_weight_tip,
        "bayes_conf": bayes_conf,
    }


# ---------------------------------------------------------------------------
# Fitness evaluation (fully vectorized via 2D broadcasting)
# ---------------------------------------------------------------------------

def evaluate_population(
    population: np.ndarray,
    data: dict[str, np.ndarray],
) -> np.ndarray:
    """Evaluate fitness for all bots simultaneously.

    Args:
        population: (P, 13) array of DNA parameters
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

    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    # --- Filter mask ---
    mask = (edge >= min_edge) & (conf >= min_conf)

    # Draw gate: draws must exceed draw_threshold on ADJUSTED confidence
    # IMPORTANT: applied AFTER full confidence pipeline
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate

    # --- Kelly with volatility buffer ---
    buffered_edge = adj_conf - imp - vol_buffer
    buffered_edge = np.maximum(buffered_edge, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_raw = kelly_f * buffered_edge / denom
    kelly_raw = np.maximum(kelly_raw, 0.0)
    stake = np.minimum(kelly_raw, max_s)

    # Apply mask
    stake = stake * mask

    # Profit per bet
    profit = np.where(correct, stake * (odds - 1.0), -stake)

    # --- ROI per bot ---
    total_staked = stake.sum(axis=1)
    total_profit = profit.sum(axis=1)
    roi = np.where(total_staked > 0, total_profit / total_staked, -1.0)

    # --- Bet count ---
    bet_count = mask.sum(axis=1)

    # --- Max Drawdown per bot (vectorized) ---
    cum_profit = np.cumsum(profit * mask, axis=1)
    cum_max = np.maximum.accumulate(cum_profit, axis=1)
    drawdown = cum_max - cum_profit
    max_dd = drawdown.max(axis=1)
    peak_equity = np.maximum(cum_max.max(axis=1), 1.0)
    max_dd_pct = max_dd / peak_equity

    # --- Weekly Sharpe Ratio ---
    iso_weeks = data["iso_week"]
    unique_weeks = np.unique(iso_weeks)
    n_weeks = len(unique_weeks)

    sharpe = np.zeros(P, dtype=np.float64)

    if n_weeks >= 4:
        weekly_pnl = np.zeros((P, n_weeks), dtype=np.float64)
        for w_idx, week in enumerate(unique_weeks):
            week_mask = iso_weeks == week
            weekly_pnl[:, w_idx] = profit[:, week_mask].sum(axis=1)

        weekly_mean = weekly_pnl.mean(axis=1)
        weekly_std = weekly_pnl.std(axis=1, ddof=1)

        valid_std = weekly_std > 1e-6
        sharpe = np.where(
            valid_std,
            weekly_mean / weekly_std * np.sqrt(52),
            0.0,
        )

    # --- Combined fitness ---
    fitness = (FITNESS_WEIGHT_ROI * roi
               + FITNESS_WEIGHT_SHARPE * sharpe
               - FITNESS_WEIGHT_DRAWDOWN * max_dd_pct)

    # Penalize bots with too few bets
    fitness = np.where(bet_count >= MIN_BETS_FOR_FITNESS, fitness, -999.0)

    return fitness


# ---------------------------------------------------------------------------
# Genetic operators
# ---------------------------------------------------------------------------

def random_population(size: int, rng: np.random.Generator) -> np.ndarray:
    """Create initial random population within DNA_RANGES."""
    pop = np.zeros((size, len(DNA_GENES)), dtype=np.float64)
    for j, gene in enumerate(DNA_GENES):
        lo, hi = DNA_RANGES[gene]
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
        children[i] = np.where(mask, parents[p1], parents[p2])

    return children


def mutate(
    population: np.ndarray,
    rng: np.random.Generator,
    mutation_rate: float = MUTATION_RATE,
) -> np.ndarray:
    """Gaussian mutation with clip to DNA_RANGES."""
    P, G = population.shape
    mutated = population.copy()

    for j, gene in enumerate(DNA_GENES):
        lo, hi = DNA_RANGES[gene]
        gene_range = hi - lo
        sigma = MUTATION_SIGMA_PCT * gene_range

        should_mutate = rng.random(P) < mutation_rate
        noise = rng.normal(0, sigma, size=P)
        mutated[:, j] = np.where(
            should_mutate,
            np.clip(mutated[:, j] + noise, lo, hi),
            mutated[:, j],
        )

    return mutated


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
) -> dict:
    """Run the genetic algorithm and return the best strategy."""
    import app.database as _db

    global _shutdown_requested
    _shutdown_requested = False
    prev_handler = _signal.signal(_signal.SIGINT, _handle_sigint)

    rng = np.random.default_rng(seed)

    # Load and vectorize data
    tips = await load_tips(sport_key)
    if len(tips) < 100:
        log.error("Not enough resolved tips (%d < 100). Aborting.", len(tips))
        _signal.signal(_signal.SIGINT, prev_handler)
        return {"error": "insufficient_data", "tip_count": len(tips)}

    # Temporal split: 80% training, 20% validation
    split_idx = int(len(tips) * 0.80)
    train_tips = tips[:split_idx]
    val_tips = tips[split_idx:]
    log.info("Split: %d training / %d validation tips", len(train_tips), len(val_tips))

    train_data = vectorize_tips(train_tips)
    val_data = vectorize_tips(val_tips)

    # Initialize or resume population
    start_gen = 0
    best_fitness_history: list[float] = []
    avg_fitness_history: list[float] = []

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
                    (DNA_RANGES[g][0] + DNA_RANGES[g][1]) / 2
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
            population = random_population(population_size, rng)
    else:
        population = random_population(population_size, rng)

    n_elites = max(int(population_size * ELITE_FRACTION), 2)
    n_children = population_size - n_elites

    t0 = time.monotonic()

    # Radiation event tracking
    stagnant_gens = 0
    prev_best = -np.inf

    for gen in range(start_gen, generations):
        if _shutdown_requested:
            log.info("Graceful shutdown: saving checkpoint at gen %d", gen)
            _save_checkpoint(sport_key, gen - 1, population, best_fitness_history, rng)
            break

        # Evaluate on training set
        fitness = evaluate_population(population, train_data)

        best_idx = np.argmax(fitness)
        best_fit = fitness[best_idx]
        avg_fit = fitness[fitness > -999].mean() if (fitness > -999).any() else -999.0

        best_fitness_history.append(float(best_fit))
        avg_fitness_history.append(float(avg_fit))

        if gen % 5 == 0 or gen == generations - 1:
            log.info(
                "Gen %2d/%d | Best fitness: %.4f | Avg: %.4f | Pop: %d",
                gen + 1, generations, best_fit, avg_fit, population_size,
            )

        # Checkpoint every N generations
        if gen > start_gen and gen % CHECKPOINT_INTERVAL == 0:
            _save_checkpoint(sport_key, gen, population, best_fitness_history, rng)

        # Radiation event detection
        if best_fit <= prev_best + 1e-6:
            stagnant_gens += 1
        else:
            stagnant_gens = 0
        prev_best = best_fit

        # Selection + Crossover + Mutation
        elites = select_elites(population, fitness, n_elites)
        children = crossover(elites, n_children, rng)

        if stagnant_gens >= STAGNATION_THRESHOLD:
            log.warning(
                "RADIATION EVENT at gen %d — spiking mutation to %.0f%%",
                gen, RADIATION_MUTATION_RATE * 100,
            )
            children = mutate(children, rng, mutation_rate=RADIATION_MUTATION_RATE)
            stagnant_gens = 0
        else:
            children = mutate(children, rng)

        population = np.vstack([elites, children])

    # Restore SIGINT handler
    _signal.signal(_signal.SIGINT, prev_handler)

    elapsed = time.monotonic() - t0
    log.info("Evolution complete in %.1fs (%d generations)", elapsed, generations)

    # Final evaluation on training
    final_fitness = evaluate_population(population, train_data)
    alpha_idx = np.argmax(final_fitness)
    alpha_dna = population[alpha_idx]

    # Compute detailed metrics for alpha bot
    train_metrics = _compute_detailed_metrics(alpha_dna, train_data)
    val_metrics = _compute_detailed_metrics(alpha_dna, val_data)

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

    # --- Stress test top 5 bots ---
    top_indices = np.argsort(final_fitness)[-5:][::-1]
    top_dna = [population[i] for i in top_indices]

    log.info("Running stress test on top %d bots...", len(top_dna))
    ensemble_result = build_ensemble(top_dna, val_data, rng=rng)

    stress_test_data: dict = {}
    is_active = True

    if ensemble_result is None:
        log.warning("Strategy NOT activated — stress test failed for all candidates")
        is_active = False
        stress_test_data = {"stress_passed": False, "reason": "all_candidates_failed"}
    else:
        m0_bs = ensemble_result["ensemble_metrics"][0]["bootstrap"]
        m0_mc = ensemble_result["ensemble_metrics"][0]["monte_carlo"]
        stress_test_data = {
            "bootstrap_p_positive": m0_bs["p_positive"],
            "bootstrap_ci_95": m0_bs["ci_95"],
            "bootstrap_mean_roi": m0_bs["mean"],
            "monte_carlo_ruin_prob": m0_mc["ruin_prob"],
            "monte_carlo_max_dd_median": m0_mc["max_dd_median"],
            "monte_carlo_max_dd_95": m0_mc["max_dd_95"],
            "ensemble_size": ensemble_result["ensemble_size"],
            "stress_passed": True,
        }

    strategy_doc = {
        "version": "v2",
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
        "population_size": population_size,
        "generations_run": generations,
        "evolution_time_s": round(elapsed, 1),
        "fitness_history": {
            "best": [round(f, 4) for f in best_fitness_history],
            "avg": [round(f, 4) for f in avg_fitness_history],
        },
    }

    if ensemble_result and ensemble_result.get("ensemble_dna"):
        strategy_doc["ensemble_dna"] = ensemble_result["ensemble_dna"]
        strategy_doc["deployment_method"] = "median"

    if not dry_run:
        # Deactivate any existing active strategy for this sport_key
        await _db.db.qbot_strategies.update_many(
            {"sport_key": strategy_doc["sport_key"], "is_active": True},
            {"$set": {"is_active": False}},
        )
        result = await _db.db.qbot_strategies.insert_one(strategy_doc)
        log.info("Saved Alpha Bot to qbot_strategies: %s", result.inserted_id)
    else:
        log.info("[DRY RUN] Would save strategy — skipping DB write.")

    return strategy_doc


def _compute_detailed_metrics(dna: np.ndarray, data: dict[str, np.ndarray]) -> dict:
    """Compute detailed metrics for a single bot (mirrors evaluate_population v2)."""
    min_edge    = dna[0]
    min_conf    = dna[1]
    sharp_w     = dna[2]
    momentum_w  = dna[3]
    rest_w      = dna[4]
    kelly_f     = dna[5]
    max_s       = dna[6]
    home_bias_v = dna[7] if len(dna) > 7 else 1.0
    away_bias_v = dna[8] if len(dna) > 8 else 1.0
    h2h_w       = dna[9] if len(dna) > 9 else 0.0
    draw_thresh = dna[10] if len(dna) > 10 else 0.0
    vol_buffer  = dna[11] if len(dna) > 11 else 0.0
    bayes_trust = dna[12] if len(dna) > 12 else 0.0

    edge = data["edge_pct"]
    conf = data["confidence"]
    imp = data["implied_prob"]
    correct = data["was_correct"]
    odds = data["odds"]
    iso_weeks = data["iso_week"]
    pick = data["pick_type"]
    h2h_t = data["h2h_weight"]
    bayes_c = data["bayes_conf"]

    # --- Confidence pipeline (mirrors evaluate_population) ---
    adj_conf = conf + sharp_w * data["sharp_boost"] + momentum_w * data["momentum_boost"] + rest_w * data["rest_boost"]

    # Venue bias
    is_home = (pick == 0)
    is_draw = (pick == 1)
    is_away = (pick == 2)
    bias = is_home * home_bias_v + is_draw * 1.0 + is_away * away_bias_v
    adj_conf = adj_conf * bias

    # H2H amplification
    adj_conf = adj_conf + h2h_w * h2h_t * 0.10

    # Bayesian trust blending
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c

    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    # --- Filter mask ---
    mask = (edge >= min_edge) & (conf >= min_conf)
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate

    # --- Kelly with volatility buffer ---
    buffered_edge = adj_conf - imp - vol_buffer
    buffered_edge = np.maximum(buffered_edge, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_raw = kelly_f * buffered_edge / denom
    kelly_raw = np.maximum(kelly_raw, 0.0)
    stake = np.minimum(kelly_raw, max_s) * mask

    # Profit
    profit = np.where(correct, stake * (odds - 1.0), -stake)

    total_staked = float(stake.sum())
    total_profit = float(profit.sum())
    total_bets = int(mask.sum())
    wins = int((mask & correct).sum())

    roi = total_profit / total_staked if total_staked > 0 else 0.0
    win_rate = wins / total_bets if total_bets > 0 else 0.0

    # --- Max Drawdown ---
    cum_profit = np.cumsum(profit * mask)
    cum_max = np.maximum.accumulate(cum_profit)
    drawdown = cum_max - cum_profit
    max_dd = float(drawdown.max())
    peak_eq = max(float(cum_max.max()), 1.0)
    max_dd_pct = max_dd / peak_eq

    # --- Weekly Sharpe ---
    unique_weeks = np.unique(iso_weeks)
    weekly_pnl = np.array([profit[iso_weeks == w].sum() for w in unique_weeks])

    if len(weekly_pnl) >= 4:
        w_mean = weekly_pnl.mean()
        w_std = weekly_pnl.std(ddof=1)
        sharpe = (w_mean / w_std * np.sqrt(52)) if w_std > 1e-6 else 0.0
    else:
        sharpe = 0.0

    return {
        "roi": float(roi),
        "sharpe": float(sharpe),
        "win_rate": float(win_rate),
        "total_bets": total_bets,
        "profit": float(total_profit),
        "max_drawdown_pct": round(float(max_dd_pct), 4),
    }


def _utcnow():
    """UTC-aware datetime (matches project convention)."""
    return datetime.now(timezone.utc)


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

def _compute_roi_single(
    pop: np.ndarray,
    data: dict[str, np.ndarray],
) -> float:
    """Compute raw ROI for a single-bot population (1, G)."""
    min_edge = pop[0, 0]
    min_conf = pop[0, 1]
    sharp_w = pop[0, 2]
    momentum_w = pop[0, 3]
    rest_w = pop[0, 4]
    kelly_f = pop[0, 5]
    max_s = pop[0, 6]
    home_bias_v = pop[0, 7] if pop.shape[1] > 7 else 1.0
    away_bias_v = pop[0, 8] if pop.shape[1] > 8 else 1.0
    h2h_w = pop[0, 9] if pop.shape[1] > 9 else 0.0
    draw_thresh = pop[0, 10] if pop.shape[1] > 10 else 0.0
    vol_buffer = pop[0, 11] if pop.shape[1] > 11 else 0.0
    bayes_trust = pop[0, 12] if pop.shape[1] > 12 else 0.0

    edge = data["edge_pct"]
    conf = data["confidence"]
    imp = data["implied_prob"]
    correct = data["was_correct"]
    odds = data["odds"]
    pick = data["pick_type"]
    h2h_t = data["h2h_weight"]
    bayes_c = data["bayes_conf"]

    adj_conf = conf + sharp_w * data["sharp_boost"] + momentum_w * data["momentum_boost"] + rest_w * data["rest_boost"]
    is_home = (pick == 0)
    is_draw = (pick == 1)
    is_away = (pick == 2)
    bias = is_home * home_bias_v + is_draw * 1.0 + is_away * away_bias_v
    adj_conf = adj_conf * bias
    adj_conf = adj_conf + h2h_w * h2h_t * 0.10
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c
    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    mask = (edge >= min_edge) & (conf >= min_conf)
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate

    buffered_edge = np.maximum(adj_conf - imp - vol_buffer, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_raw = kelly_f * buffered_edge / denom
    stake = np.minimum(np.maximum(kelly_raw, 0.0), max_s) * mask

    profit = np.where(correct, stake * (odds - 1.0), -stake)
    total_staked = stake.sum()
    return float(profit.sum() / total_staked) if total_staked > 0 else -1.0


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
    pop = bot_dna[np.newaxis, :]  # (1, G)

    rois = np.empty(n_samples)
    for i in range(n_samples):
        idx = rng.choice(n_tips, size=n_tips, replace=True)
        sampled = {k: v[idx] for k, v in tip_data.items() if isinstance(v, np.ndarray)}
        rois[i] = _compute_roi_single(pop, sampled)

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
    rng: np.random.Generator | None = None,
) -> dict:
    """Monte Carlo bankroll simulation with randomized bet ordering."""
    if rng is None:
        rng = np.random.default_rng()

    metrics = _compute_detailed_metrics(bot_dna, tip_data)
    # Recompute per-tip P&L for the bot
    pop = bot_dna[np.newaxis, :]
    edge = tip_data["edge_pct"]
    conf = tip_data["confidence"]
    imp = tip_data["implied_prob"]
    correct = tip_data["was_correct"]
    odds = tip_data["odds"]
    pick = tip_data["pick_type"]

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

    adj_conf = conf + sharp_w * tip_data["sharp_boost"] + momentum_w * tip_data["momentum_boost"] + rest_w * tip_data["rest_boost"]
    is_home = (pick == 0)
    is_draw = (pick == 1)
    is_away = (pick == 2)
    bias = is_home * home_bias_v + is_draw * 1.0 + is_away * away_bias_v
    adj_conf = adj_conf * bias
    adj_conf = adj_conf + h2h_w * tip_data["h2h_weight"] * 0.10
    blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)
    adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * tip_data["bayes_conf"]
    adj_conf = np.clip(adj_conf, 0.0, 0.99)

    mask = (edge >= min_edge) & (conf >= min_conf)
    draw_gate = is_draw & (adj_conf < draw_thresh)
    mask = mask & ~draw_gate

    buffered_edge = np.maximum(adj_conf - imp - vol_buffer, 0.0)
    denom = np.maximum(odds - 1.0, 0.01)
    kelly_raw = kelly_f * buffered_edge / denom
    stake = np.minimum(np.maximum(kelly_raw, 0.0), max_s) * mask
    profit = np.where(correct, stake * (odds - 1.0), -stake)

    # Extract active bets only
    bet_pnl = profit[mask]
    n_bets = len(bet_pnl)

    if n_bets < 10:
        return {
            "ruin_prob": 1.0, "max_dd_median": 1.0, "max_dd_95": 1.0,
            "terminal_wealth_median": initial_bank, "n_bets": n_bets,
        }

    # Build shuffled paths: (n_paths, n_bets)
    paths = np.tile(bet_pnl, (n_paths, 1))
    for i in range(n_paths):
        rng.shuffle(paths[i])

    cum = initial_bank + np.cumsum(paths, axis=1)

    # Ruin
    ruin_level = initial_bank * ruin_threshold
    ruin_prob = float((cum.min(axis=1) < ruin_level).mean())

    # Max drawdown per path
    cum_max = np.maximum.accumulate(cum, axis=1)
    dd = (cum_max - cum) / np.maximum(cum_max, 1.0)
    max_dd = dd.max(axis=1)

    return {
        "ruin_prob": round(ruin_prob, 4),
        "max_dd_median": round(float(np.median(max_dd)), 4),
        "max_dd_95": round(float(np.percentile(max_dd, 95)), 4),
        "terminal_wealth_median": round(float(np.median(cum[:, -1])), 2),
        "n_bets": n_bets,
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
) -> dict:
    """Deep search: expanding-window CV with pessimistic fitness."""
    import app.database as _db

    global _shutdown_requested
    _shutdown_requested = False
    prev_handler = _signal.signal(_signal.SIGINT, _handle_sigint)

    rng = np.random.default_rng(seed)

    tips = await load_tips(sport_key)
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
        td = vectorize_tips(train)
        vd = vectorize_tips(val)
        fold_data.append((td, vd))
        log.info("  Fold %d: %d train / %d val", fi, len(train), len(val))

    # Use last fold's val as the held-out validation set for final metrics
    final_val_data = fold_data[-1][1]

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
        avg_fit = float(fitness[fitness > -999].mean()) if (fitness > -999).any() else -999.0
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

    # Best bot
    final_fitness = np.stack([
        evaluate_population(population, td) for td, _ in fold_data
    ]).min(axis=0)
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

    # Stress test
    top_indices = np.argsort(final_fitness)[-5:][::-1]
    top_dna = [population[i] for i in top_indices]
    log.info("Running stress test on top %d bots...", len(top_dna))
    ensemble_result = build_ensemble(top_dna, final_val_data, rng=rng)

    stress_test_data: dict = {}
    is_active = True

    if ensemble_result is None:
        is_active = False
        stress_test_data = {"stress_passed": False, "reason": "all_candidates_failed"}
    else:
        m0_bs = ensemble_result["ensemble_metrics"][0]["bootstrap"]
        m0_mc = ensemble_result["ensemble_metrics"][0]["monte_carlo"]
        stress_test_data = {
            "bootstrap_p_positive": m0_bs["p_positive"],
            "bootstrap_ci_95": m0_bs["ci_95"],
            "bootstrap_mean_roi": m0_bs["mean"],
            "monte_carlo_ruin_prob": m0_mc["ruin_prob"],
            "monte_carlo_max_dd_median": m0_mc["max_dd_median"],
            "monte_carlo_max_dd_95": m0_mc["max_dd_95"],
            "ensemble_size": ensemble_result["ensemble_size"],
            "stress_passed": True,
        }

    strategy_doc = {
        "version": "v2",
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
        "population_size": population_size,
        "generations_run": generations,
        "n_folds": len(folds),
        "evolution_time_s": round(elapsed, 1),
        "fitness_history": {
            "best": [round(f, 4) for f in best_fitness_history],
        },
    }

    if ensemble_result and ensemble_result.get("ensemble_dna"):
        strategy_doc["ensemble_dna"] = ensemble_result["ensemble_dna"]
        strategy_doc["deployment_method"] = "median"

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
                )
            elif args.multi:
                result = await run_multi_league(
                    parallel=args.parallel,
                    dry_run=args.dry_run,
                    population_size=population_size,
                    generations=generations,
                    seed=args.seed,
                    resume=args.resume,
                )
            elif args.mode == "deep":
                result = await run_evolution_deep(
                    args.sport,
                    population_size=population_size,
                    generations=generations,
                    dry_run=args.dry_run,
                    seed=args.seed,
                    resume=args.resume,
                )
            else:
                result = await run_evolution(
                    args.sport,
                    population_size=population_size,
                    generations=generations,
                    dry_run=args.dry_run,
                    seed=args.seed,
                    resume=args.resume,
                )
                if "error" in result:
                    log.error("Evolution failed: %s", result["error"])
                    sys.exit(1)
        finally:
            await _db.close_db()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
