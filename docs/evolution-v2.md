# Qbot Evolution Arena v2 — Multi-League Specialist, Deep DNA, Player Mode & Strategy Stress Test

## Context

The Qbot Evolution Arena v1 is fully implemented and working. It runs 30 generations × 50 bots on 30,931 tips in 1.8 seconds. The intelligence service, frontend integration, and backfill tooling are all deployed. This upgrade adds:

1. **League-specific DNA optimization** — each league gets its own evolved strategy
2. **Expanded DNA** — 6 new genes for venue bias, H2H weighting, draw handling, volatility buffer, and Bayesian feedback
3. **Risk-adjusted fitness** — max drawdown penalty
4. **Multi-league CLI wrapper** — auto-discover leagues, evolve per league, print cross-league DNA report, optional `--parallel` for concurrent league evolution
5. **Player Mode** — a "must-tip-every-game" mode using Poisson score matrix, complementing the selective Investor Mode
6. **Exact Score Matrix** — numpy-based Dixon-Coles corrected Poisson score matrix, persisted and served to frontend
7. **Mining Mode** — checkpoint/resume, graceful SIGINT, radiation events for stagnation escape, optional `--watch` for periodic tip reload
8. **Deep Search Mode** — expanding-window temporal cross-validation with pessimistic fitness, rich monitoring
9. **Strategy Stress Test** — bootstrap confidence intervals, Monte Carlo bankroll simulation, ensemble top-N deployment
10. **Admin Dashboard** — Qbot Lab with fitness metrics, DNA detail, stress test results

---

## TEAMS.md Review — Key Findings

### Source Truth
This review is based on **working code** (v1 arena, intelligence service) and **proposed intent** (v2 features). Findings against the proposed changes are evidence-grounded against the existing implementation.

**Domain:** New Feature / Greenfield extension. **Elevated roles:** Minimalist, Implementor, Baker, Database Engineer, Economic Realist, Midwife, Hummingbird, Tent, Success Catastrophe.

### Critical Feedback

| Role | Finding | Resolution |
|------|---------|------------|
| **Minimalist** | Going from 7→13 genes increases search space by ~5 orders of magnitude. 50 bots × 30 gens is thin for 13D. Sharpe already penalizes drawdown; extra fitness axes add noise. | Keep all 6 new genes but scale GA proportionally: 100 bots × 50 gens for multi-league (still <60s total). Drop the `consistency_penalty` — drawdown penalty is sufficient. |
| **Baker** | `home_bias`/`away_bias`/`draw_threshold` blur the boundary between the EV engine (tip generation) and the GA (filter/sizing). The GA should not re-decide outcomes. | These genes are **confidence modifiers**, not outcome selectors. The GA still trusts the EV engine's pick — it just weights the confidence differently for home/draw/away. Layer boundary is preserved. |
| **Implementor** | Max drawdown IS vectorizable: `np.cumsum` + `np.maximum.accumulate` along axis=1, ~6 lines. Consecutive losing weeks: cumsum sliding window trick, no Python loop. `draw_threshold` is architecturally inert unless simulation re-evaluates all outcomes (it doesn't). | `draw_threshold` acts as a **minimum confidence gate** for draw picks: if `adj_conf < draw_thresh` and `pick == "X"`, mask it out. This is a valid filter gene, not an outcome re-selector. |
| **Database Engineer** | No schema migration needed. The `(is_active, sport_key)` unique partial index already supports one active strategy per sport_key. New DNA fields are just extra keys in the `dna` subdocument. v1 strategies continue to work via `.get()` defaults. | Confirmed: no index changes. |
| **Midwife** | Before adding 6 genes, run the existing 7-gene arena per-league and measure whether per-league strategies outperform "all". | The `--multi` wrapper runs the `"all"` fallback alongside per-league runs. Cross-league DNA report makes comparison visible. First run will establish baselines. |
| **Success Catastrophe** | **BUG FOUND**: `_strategy_cache` in `qbot_intelligence_service.py:102` is a **single dict**, not keyed by sport_key. With per-league strategies, calling `_get_active_strategy("soccer_epl")` then `_get_active_strategy("soccer_germany_bundesliga")` returns the cached EPL strategy for both (1h TTL). Multi-league strategies will silently fail. | **Must fix before any multi-league deployment.** Change cache to `dict[str, dict]` keyed by sport_key. Load all active strategies in one query. |
| **Hummingbird** | Three quick wins hidden in the data: (1) `recommended_selection` is loaded but never used in GA — one boolean `is_home` enables `home_bias`. (2) `tier_signals.poisson.h2h_weight` is already a float — one vectorization line gives the GA per-tip H2H strength. (3) Pre-computed `qbot_logic.bayesian_confidence` is already stored after backfill — 2 lines of code for Bayesian feedback. | All three are implemented in this plan. |
| **Tent** | Must preserve: 1.8s runtime, 2D broadcasting architecture, 80/20 temporal split, strategy activation pattern. The 3D arrays proposal threatens memory (100 bots × 6 leagues × 30k tips × 12 columns = 17GB). | **No 3D arrays.** Iterate leagues sequentially, vectorize bots within each league. 6 × ~3s = ~18s total. Broadcasting architecture fully preserved. |
| **Economic Realist** | 6 leagues × 100 bots × 50 gens ≈ 18s total compute. The marginal cost of more genes per league is zero (bottleneck is tips, not DNA columns). The real cost is the 8h engineering time. | The strategy cache bugfix alone justifies the work (live correctness). The gene expansion is incremental atop working code. |

### Convergence Insight
All perspectives agree on three things:
1. **Fix the strategy cache bug first** — it breaks live multi-league correctness
2. **No 3D arrays** — iterate leagues, vectorize bots. Simple, fast, memory-safe
3. **All 6 new genes are implementable** within the existing broadcasting architecture

### Kill Decisions
- **KILLED: 3D arrays (Bots × Leagues × Tips)** — memory explosion, no benefit. Sequential league iteration is <60s total.
- **KILLED: Consistency bonus (>3 losing weeks penalty)** — creates discontinuous fitness landscape, hurts GA convergence. Max drawdown penalty is a sufficient risk measure.
- **KILLED: Apple Silicon / AMX tuning (Phase 0)** — premature optimization. numpy already uses vecLib/Accelerate via pip wheels on macOS. Arena runtime is seconds, not hours.
- **KILLED: DNA range narrowing around Alpha-Bot** — classic convergence trap. Contradicts radiation events (Phase 11) which exist to escape local optima.
- **KILLED: Pareto Fronts with manual gene flow** — objectives are contradictory (max ROI vs min drawdown). Real multi-objective optimization requires NSGA-II, not ad-hoc splicing. Replaced by ensemble approach (Phase 15) which achieves robustness without GA rewrite.
- **KILLED: Hardware thermal watchdog** — numpy operations on these data sizes don't cause thermal issues on Mac Studio M1.

### Salvaged from killed phases
- **`--parallel` flag** → integrated into Phase 4 (Multi-League Wrapper) using `concurrent.futures.ProcessPoolExecutor`
- **`--watch` flag for periodic tip reload** → integrated into Phase 11 (Mining Mode), ~15 lines

---

## Critical Success Factors

### 1. Strategy Cache Bugfix (PREREQUISITE)
`_strategy_cache` at `qbot_intelligence_service.py:102` must become a per-sport_key dict. Without this, live tip enrichment will serve stale/wrong strategies when multiple per-league strategies are active.

### 2. Backward Compatibility via `.get()` Defaults
All new DNA genes must have neutral defaults in `_compute_kelly_stake()`:
- `home_bias=1.0` (neutral multiplier), `away_bias=1.0`, `draw_threshold=0.0` (no gate), `volatility_buffer=0.0`, `bayes_trust_factor=0.0` (no blending), `h2h_weight=0.0` (no boost)
This ensures v1 strategies (7 genes) continue working without migration.

### 3. No 3D — League Iteration Pattern
`run_multi_league()` calls `run_evolution(sport_key=key)` per league sequentially (or in parallel with `--parallel`). The existing `--sport` filtering in `load_tips()` handles everything. Each league gets its own 2D `(P, N)` evaluation — no cross-league memory sharing needed.

### 4. Minimum Tips Threshold
Per-league minimum: **200 tips** (160 training / 40 validation after 80/20 split). Leagues below threshold use the `"all"` fallback strategy.

### 5. Draw Gate After Confidence Pipeline
The draw threshold gate must be applied AFTER the full confidence pipeline (venue bias, H2H amplification, Bayesian blending), not before. Otherwise draws are gated on raw confidence, defeating the purpose of the gene.

### 6. Temporal CV Must Not Look Ahead
Deep mode cross-validation uses expanding windows (train on folds 0..i-1, validate on fold i), NOT symmetric k-fold which trains on future data. This yields 4 folds, not 5.

### 7. Strategy Activation Quality Gate
No strategy goes live without passing stress test: bootstrap `p_positive ≥ 0.90` AND Monte Carlo `ruin_prob < 0.05`. This prevents deploying strategies whose edge is statistical noise.

---

## Implementation Plan

### Phase 1: Strategy Cache Bugfix

**File:** `backend/app/services/qbot_intelligence_service.py`

Replace `_strategy_cache` (line 102) and `_get_active_strategy()` (lines 111-135):

```python
# Line 102-103: replace
_strategy_cache: dict[str, dict] = {}   # keyed by sport_key
_strategy_expires: float = 0.0

# _get_active_strategy: replace body
async def _get_active_strategy(sport_key: str = "all") -> dict | None:
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
```

---

### Phase 2: DNA Expansion (7 → 13 genes)

**File:** `tools/qbot_evolution_arena.py`

#### New DNA Schema

```python
DNA_GENES = [
    # Original 7 (indices 0-6)
    "min_edge", "min_confidence", "sharp_weight", "momentum_weight",
    "rest_weight", "kelly_fraction", "max_stake",
    # New 6 (indices 7-12)
    "home_bias", "away_bias", "h2h_weight", "draw_threshold",
    "volatility_buffer", "bayes_trust_factor",
]

DNA_RANGES = {
    # Original
    "min_edge":           (3.0, 15.0),
    "min_confidence":     (0.30, 0.80),
    "sharp_weight":       (0.5, 2.0),
    "momentum_weight":    (0.5, 2.0),
    "rest_weight":        (0.0, 1.5),
    "kelly_fraction":     (0.05, 0.50),
    "max_stake":          (10.0, 100.0),
    # New
    "home_bias":          (0.80, 1.20),   # multiplier on confidence when pick is "1"
    "away_bias":          (0.80, 1.20),   # multiplier on confidence when pick is "2"
    "h2h_weight":         (0.0, 2.0),     # multiplier on per-tip h2h signal contribution
    "draw_threshold":     (0.0, 1.0),     # min adj_conf gate for draw picks (applied AFTER full confidence pipeline)
    "volatility_buffer":  (0.0, 0.20),    # subtracted from edge before Kelly
    "bayes_trust_factor": (0.0, 1.5),     # blending weight for Bayesian cluster confidence
}
```

#### Fitness Constants Update

```python
FITNESS_WEIGHT_ROI = 0.5         # was 0.6
FITNESS_WEIGHT_SHARPE = 0.3      # was 0.4
FITNESS_WEIGHT_DRAWDOWN = 0.2    # NEW (penalty)
```

#### `vectorize_tips()` — 3 New Arrays

Add after existing arrays (line 115):

```python
pick_type = np.zeros(n, dtype=np.int8)       # 0=home, 1=draw, 2=away
h2h_weight_tip = np.zeros(n, dtype=np.float64)  # from tier_signals.poisson.h2h_weight
bayes_conf = np.zeros(n, dtype=np.float64)    # from qbot_logic.bayesian_confidence
```

Inside the extraction loop (after line 168):

```python
# Pick type encoding
pick = tip.get("recommended_selection")
if pick == "1":
    pick_type[i] = 0
elif pick == "X":
    pick_type[i] = 1
else:
    pick_type[i] = 2

# H2H weight from Poisson signal (nullable → 0.0)
poisson_sig = signals.get("poisson") or {}
h2h_weight_tip[i] = poisson_sig.get("h2h_weight", 0.0)

# Bayesian confidence from qbot_logic (nullable → 0.333 prior)
qbot = tip.get("qbot_logic") or {}
bayes_conf[i] = qbot.get("bayesian_confidence", 0.333)
```

Return dict: add `"pick_type"`, `"h2h_weight"`, `"bayes_conf"`.

---

### Phase 3: Advanced Fitness Function

**File:** `tools/qbot_evolution_arena.py` — `evaluate_population()`

#### New DNA + Tip Array Extraction

After line 210 (existing DNA columns), add:

```python
home_bias_w = population[:, 7:8]      # (P, 1)
away_bias_w = population[:, 8:9]      # (P, 1)
h2h_w       = population[:, 9:10]     # (P, 1)
draw_thresh = population[:, 10:11]    # (P, 1)
vol_buffer  = population[:, 11:12]    # (P, 1)
bayes_trust = population[:, 12:13]    # (P, 1)
```

After line 220 (existing tip arrays), add:

```python
pick = data["pick_type"][np.newaxis, :]     # (1, N) int8
h2h_t = data["h2h_weight"][np.newaxis, :]   # (1, N)
bayes_c = data["bayes_conf"][np.newaxis, :] # (1, N)
```

#### Confidence Pipeline (CORRECTED ordering — draw gate AFTER full pipeline)

Replace lines 225-227 (current adj_conf block):

```python
# 1. Base: signal-weighted boosts (existing)
adj_conf = conf + sharp_w * s_boost + momentum_w * m_boost + rest_w * r_boost

# 2. Venue bias: multiply confidence by home_bias or away_bias based on pick type
is_home = (pick == 0)  # (1, N) bool
is_draw = (pick == 1)  # (1, N) bool
is_away = (pick == 2)  # (1, N) bool
bias = is_home * home_bias_w + is_draw * 1.0 + is_away * away_bias_w
adj_conf = adj_conf * bias

# 3. H2H amplification: boost when H2H data supports pick
adj_conf = adj_conf + h2h_w * h2h_t * 0.10  # max contribution: 2.0 * 0.30 * 0.10 = +0.006

# 4. Bayesian trust blending: blend model confidence with cluster win rate
blend_weight = np.clip(bayes_trust * 0.5, 0.0, 0.75)  # trust=1.0 → 50% blend
adj_conf = (1.0 - blend_weight) * adj_conf + blend_weight * bayes_c

adj_conf = np.clip(adj_conf, 0.0, 0.99)
```

#### Edge + Filter Mask (draw gate applied AFTER confidence pipeline)

```python
# Base filter: edge and confidence thresholds
mask = (edge >= min_edge) & (conf >= min_conf)

# Draw gate: draws must also exceed draw_threshold on ADJUSTED confidence
# IMPORTANT: This runs AFTER the full confidence pipeline above
draw_gate = is_draw & (adj_conf < draw_thresh)
mask = mask & ~draw_gate
```

#### Volatility Buffer on Kelly

Replace lines 231-235:

```python
buffered_edge = adj_conf - imp - vol_buffer  # buffer reduces perceived edge
buffered_edge = np.maximum(buffered_edge, 0.0)
denom = np.maximum(odds - 1.0, 0.01)
kelly_raw = kelly_f * buffered_edge / denom
kelly_raw = np.maximum(kelly_raw, 0.0)
stake = np.minimum(kelly_raw, max_s)
```

#### Max Drawdown (Vectorized)

Add after line 241 (after profit computation):

```python
# Max Drawdown per bot — fully vectorized
cum_profit = np.cumsum(profit * mask, axis=1)  # (P, N) — chronological
cum_max = np.maximum.accumulate(cum_profit, axis=1)  # (P, N) — running peak
drawdown = cum_max - cum_profit  # (P, N), always >= 0
max_dd = drawdown.max(axis=1)  # (P,) — worst trough
peak_equity = np.maximum(cum_max.max(axis=1), 1.0)  # avoid div/0
max_dd_pct = max_dd / peak_equity  # (P,) — 0.0 = no drawdown
```

#### Updated Fitness Formula

Replace line 279:

```python
fitness = 0.5 * roi + 0.3 * sharpe - 0.2 * max_dd_pct
```

---

### Phase 4: Multi-League Wrapper

**File:** `tools/qbot_evolution_arena.py`

#### New Constants

```python
MIN_TIPS_PER_LEAGUE = 200
```

#### `run_multi_league()` function (after `run_evolution()`)

1. `db.quotico_tips.aggregate([{$match: resolved}, {$group: {_id: "$sport_key", count: {$sum: 1}}}, {$sort: {count: -1}}])`
2. For each league with ≥200 tips → `await run_evolution(sport_key=key, population_size=100, generations=50, ...)`
3. For leagues with <200 tips → log skip, will use "all" fallback
4. Always run `await run_evolution(None, ...)` for the "all" fallback
5. Print cross-league DNA comparison report

#### Optional Parallel Execution (`--parallel` flag)

When `--parallel` is set, use `concurrent.futures.ProcessPoolExecutor` to evolve leagues concurrently:

```python
import concurrent.futures

async def run_multi_league(parallel: bool = False, **kwargs):
    leagues = await discover_leagues()  # returns [(sport_key, tip_count), ...]

    if parallel:
        with concurrent.futures.ProcessPoolExecutor(max_workers=min(len(leagues), 6)) as pool:
            futures = {
                pool.submit(run_evolution_sync, sport_key=key, **kwargs): key
                for key, count in leagues if count >= MIN_TIPS_PER_LEAGUE
            }
            # Always include "all" fallback
            futures[pool.submit(run_evolution_sync, sport_key=None, **kwargs)] = "all"
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                result = future.result()
                results[key] = result
    else:
        # Sequential (existing pattern)
        for key, count in leagues:
            if count >= MIN_TIPS_PER_LEAGUE:
                results[key] = await run_evolution(sport_key=key, **kwargs)
        results["all"] = await run_evolution(sport_key=None, **kwargs)

    print_cross_league_report(results)
```

Note: `run_evolution_sync` is a thin sync wrapper around `run_evolution()` that creates its own event loop, needed for ProcessPoolExecutor.

#### Cross-League DNA Report

```
============================================================
CROSS-LEAGUE DNA COMPARISON
============================================================
League                          min_edge  min_conf  home_bias  draw_thresh  ...
soccer_germany_bundesliga         5.2314    0.4521     1.0823       0.4200  ...
soccer_epl                        8.1452    0.6012     0.9512       0.1200  ...
soccer_spain_la_liga              6.8923    0.5234     1.1200       0.5800  ...
all (fallback)                    7.2341    0.5678     1.0012       0.2100  ...
============================================================
```

#### CLI Update

```bash
python -m tools.qbot_evolution_arena --multi                    # all leagues, sequential
python -m tools.qbot_evolution_arena --multi --parallel         # all leagues, concurrent
python -m tools.qbot_evolution_arena --multi --dry-run          # simulate all
python -m tools.qbot_evolution_arena --sport soccer_epl         # single league (unchanged)
```

---

### Phase 5: Intelligence Service — New Gene Integration

**File:** `backend/app/services/qbot_intelligence_service.py`

#### `_compute_kelly_stake()` Update (lines 209-231)

Use new DNA genes with backward-compatible defaults:

```python
def _compute_kelly_stake(tip: dict, strategy: dict) -> tuple[float, float]:
    dna = strategy.get("dna", {})
    kelly_f = dna.get("kelly_fraction", 0.25)
    max_stake = dna.get("max_stake", 50.0)
    vol_buffer = dna.get("volatility_buffer", 0.0)       # NEW — neutral default
    bayes_trust = dna.get("bayes_trust_factor", 0.0)      # NEW — neutral default
    home_bias = dna.get("home_bias", 1.0)                 # NEW — neutral default
    away_bias = dna.get("away_bias", 1.0)                 # NEW — neutral default
    draw_thresh = dna.get("draw_threshold", 0.0)          # NEW — neutral default

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
    bayes_conf = tip.get("qbot_logic", {}).get("bayesian_confidence")
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
```

---

### Phase 6: Strategy Schema v2

**File:** `tools/qbot_evolution_arena.py` — `run_evolution()` at line 452

Change `"version": "v1"` → `"version": "v2"`. Add new metrics to the strategy doc:

```python
strategy_doc = {
    "version": "v2",
    # ... existing fields ...
    "training_fitness": {
        # ... existing fields ...
        "max_drawdown_pct": round(float(train_max_dd_pct), 4),  # NEW
    },
    "validation_fitness": {
        # ... same ...
    },
}
```

#### `_compute_detailed_metrics()` Update (lines 495-562)

Mirror all changes from `evaluate_population()`:
- Extract 6 new DNA values (indices 7-12)
- Apply venue bias, H2H boost, Bayesian blending, draw gate, volatility buffer
- Compute max drawdown
- Return new metrics: `max_drawdown_pct`

---

### Phase 7: Score Matrix Generator + Poisson Data Preservation

**Files:** `backend/app/services/quotico_tip_service.py`

**Goal:** Create numpy-based score matrix function. Ensure Poisson data survives into `no_signal` tips so Player Mode always has data.

#### 7.1 — `generate_score_matrix()` (new public function)

Add after `_dixon_coles_adjustment()` (line 272). Uses numpy outer product + Dixon-Coles correction.

Pre-compute factorial lookup to avoid repeated computation (future-proof for max_goals > 6):

```python
import numpy as np  # add to imports

_FACTORIAL_LUT = np.array([math.factorial(k) for k in range(20)], dtype=np.float64)

def generate_score_matrix(
    lambda_home: float, lambda_away: float,
    rho: float = DIXON_COLES_RHO_DEFAULT, max_goals: int = 6,
) -> np.ndarray:
    """Dixon-Coles corrected Poisson score probability matrix (max_goals × max_goals)."""
    goals = np.arange(max_goals)
    home_pmf = np.exp(-lambda_home) * (lambda_home ** goals) / _FACTORIAL_LUT[:max_goals]
    away_pmf = np.exp(-lambda_away) * (lambda_away ** goals) / _FACTORIAL_LUT[:max_goals]
    matrix = np.outer(home_pmf, away_pmf)

    # Dixon-Coles correction on low scorelines
    matrix[0, 0] *= max(1 - lambda_home * lambda_away * rho, DIXON_COLES_ADJ_FLOOR)
    matrix[1, 0] *= max(1 + lambda_away * rho, DIXON_COLES_ADJ_FLOOR)
    matrix[0, 1] *= max(1 + lambda_home * rho, DIXON_COLES_ADJ_FLOOR)
    matrix[1, 1] *= max(1 - rho, DIXON_COLES_ADJ_FLOOR)

    matrix = np.maximum(matrix, 0.0)
    total = matrix.sum()
    if total > 0:
        matrix /= total
    return matrix
```

#### 7.2 — `compute_player_prediction()` (new public function)

```python
def compute_player_prediction(poisson: dict) -> dict:
    """Compute Player Mode prediction: highest-probability outcome + peak exact score."""
    matrix = generate_score_matrix(poisson["lambda_home"], poisson["lambda_away"], poisson["rho"])
    idx = np.argmax(matrix)
    h, a = np.unravel_index(idx, matrix.shape)

    true_probs = {"1": poisson["prob_home"], "X": poisson["prob_draw"], "2": poisson["prob_away"]}
    best_outcome = max(true_probs, key=true_probs.get)

    return {
        "predicted_outcome": best_outcome,
        "predicted_score": {"home": int(h), "away": int(a)},
        "score_probability": round(float(matrix[h, a]), 4),
        "outcome_probability": round(true_probs[best_outcome], 4),
        "is_mandatory_tip": True,
    }
```

Note: `poisson["rho"]` is already returned by `compute_poisson_probabilities()` (line 537).

#### 7.3 — Modify `_no_signal_bet()` (line 177)

Add optional kwargs to preserve Poisson data + player prediction for no_signal tips:

```python
def _no_signal_bet(
    match: dict, reason: str, *,
    poisson_data: dict | None = None,
    player_prediction: dict | None = None,
) -> dict:
```

When `poisson_data` is provided:
- Store `tier_signals.poisson` with lambdas, h2h_weight, true_probs (same shape as active tips)
- Set `expected_goals_home/away` from lambdas instead of 0.0
- Store `player_prediction` at top level (transient — consumed and removed by `enrich_tip()`)

#### 7.4 — Update `generate_quotico_tip()` (line 1394)

At the edge-threshold check, compute player prediction before returning no_signal:

```python
if best_edge < EDGE_THRESHOLD_PCT:
    player_pred = compute_player_prediction(poisson)
    return _no_signal_bet(
        match, f"No value bet found (best edge: {best_edge:.1f}% < {EDGE_THRESHOLD_PCT}%)",
        poisson_data=poisson, player_prediction=player_pred,
    )
```

Also store `player_prediction` on **active** tips (add to return dict at line 1443):

```python
"player_prediction": compute_player_prediction(poisson),
```

---

### Phase 8: Player Mode Enrichment + "The Strategist" Archetype

**File:** `backend/app/services/qbot_intelligence_service.py`

**Goal:** Extend `enrich_tip()` to produce dual-mode `qbot_logic` — investor data at top level (existing, backward compatible) + player data in `.player` sub-field.

#### IMPORTANT: `player_prediction` Transient Field Lifecycle

The `player_prediction` field is added to the tip dict in `generate_quotico_tip()` and must be consumed in `enrich_tip()` before the tip is written to MongoDB. **Verify the worker's write order:** if the tip worker saves to MongoDB before calling `enrich_tip()`, the transient field will persist as a dangling field in the DB.

**Preferred approach:** Pass `player_prediction` through the function call chain as a parameter rather than stuffing it on the document. If that requires too much refactoring, add a `$unset: {"player_prediction": 1}` to the MongoDB update in the tip worker as a safety net.

#### 8.1 — Remove no_signal early return

Remove line 250 (`if tip.get("status") == "no_signal": return tip`). Instead, branch:
- **Active tips**: compute both investor enrichment (existing) + player enrichment
- **No-signal tips**: skip investor, compute only player enrichment

#### 8.2 — Build dual-mode `qbot_logic`

```python
tip["qbot_logic"] = {
    "strategy_version": strategy.get("version", "v1"),
    **investor_data,  # top-level fields (empty dict for no_signal)
    "player": player_data,  # always present when player_prediction exists
    "applied_at": datetime.now(timezone.utc),
}
tip.pop("player_prediction", None)  # remove transient field
```

Player data structure:
```python
player_data = {
    "archetype": "the_strategist",
    "reasoning_key": "qbot.reasoning.strategist",
    "reasoning_params": {"score": "2:1", "probability": 14.2, ...},
    "predicted_outcome": "1",
    "predicted_score": {"home": 2, "away": 1},
    "score_probability": 0.142,
    "outcome_probability": 0.485,
    "is_mandatory_tip": True,
}
```

#### 8.3 — Add "the_strategist" to ARCHETYPES list

Not used in `_select_archetype()` priority chain (that's investor-only). Used directly for player mode enrichment.

---

### Phase 9: API + Auto-Tip + Worker Updates

**Goal:** Serve `qbot_logic` via API endpoints. Upgrade matchday auto-tip to use Player Mode exact scores.

#### 9.1 — `QuoticoTipResponse` (line 157)

Add field: `qbot_logic: dict | None = None`

Currently the Pydantic model does NOT include `qbot_logic` — it's silently dropped when constructing responses. This blocks all frontend access to both investor and player data.

#### 9.2 — API endpoints (`backend/app/routers/quotico_tips.py`)

Add `qbot_logic=tip.get("qbot_logic")` to both `list_tips()` (line 65) and `get_tip()` (line 254) `QuoticoTipResponse` constructors.

#### 9.3 — Matchday auto-tip (`backend/app/services/matchday_service.py`)

Update `_qbot_prediction()` (line 74) to prefer Player Mode exact score:

```python
def _qbot_prediction(quotico_tip: dict | None) -> tuple[int, int] | None:
    if not quotico_tip:
        return None
    # Prefer Player Mode exact score
    player = (quotico_tip.get("qbot_logic") or {}).get("player")
    if player and player.get("predicted_score"):
        s = player["predicted_score"]
        return (s.get("home", 1), s.get("away", 1))
    # Fallback to outcome-based mapping
    sel = quotico_tip.get("recommended_selection")
    if sel == "1": return (2, 1)
    elif sel == "X": return (1, 1)
    elif sel == "2": return (1, 2)
    return None
```

#### 9.4 — Matchday resolver projection (`backend/app/workers/matchday_resolver.py`, line 98)

Expand from `{"match_id": 1, "recommended_selection": 1, "confidence": 1}` to also include `"qbot_logic": 1`.

---

### Phase 10: Frontend — Player Mode Display + i18n

**Files:** `useQuoticoTip.ts`, `QuoticoTipBadge.vue`, `de.ts`, `en.ts`

#### 10.1 — TypeScript types (`useQuoticoTip.ts`)

Add `PlayerPrediction` interface:

```typescript
export interface PlayerPrediction {
  archetype: string;
  reasoning_key: string;
  reasoning_params: Record<string, string | number>;
  predicted_outcome: string;
  predicted_score: { home: number; away: number };
  score_probability: number;
  outcome_probability: number;
  is_mandatory_tip: boolean;
}
```

Add `player?: PlayerPrediction;` to existing `qbot_logic` interface.

#### 10.2 — i18n keys

**German (`de.ts`):**
```
qbot.reasoning.strategist: "Qbots Stratege (Gen. {generation}) analysiert die statistische Lage in der {league}. Wahrscheinlichstes Ergebnis: {score} ({probability}% Chance)."
qbot.archetypes.the_strategist: "Der Stratege"
qtip.playerMode: "Spieler-Modus"
qtip.investorMode: "Investor-Modus"
qtip.playerScore: "Tipp-Ergebnis"
qtip.playerNoSignal: "Kein Tipp"
qtip.playerMandatory: "Pflicht-Tipp"
```

**English (`en.ts`):** Same keys, English translations.

#### 10.3 — `QuoticoTipBadge.vue` display

**Compact badge row:** Add score pill `"2:1"` in indigo next to existing confidence bar.

**Dual-mode no_signal display:** `"Investor: No Bet | Player: 2:1"` when tip is no_signal but player data exists.

**Expanded panel:** New "Player Mode" section with:
- Predicted score in large mono font
- Most likely outcome label
- Score probability progress bar
- Strategist reasoning text

---

### Phase 11: Mining Mode — Checkpoint, Resume, Radiation Events & Watch

**File:** `tools/qbot_evolution_arena.py`

**Goal:** Enable long-running "night shift" evolution with graceful interrupt handling, automatic checkpoint persistence, stagnation escape via mutation spikes, and periodic tip reload.

#### 11.1 — SIGINT Signal Handler

Register a `signal.SIGINT` handler that sets a `_shutdown_requested` flag. The GA loop checks this flag each generation and exits cleanly:

```python
import signal
import json

_shutdown_requested = False

def _handle_sigint(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.warning("SIGINT received — finishing current generation, saving checkpoint...")

# Register in run_evolution():
signal.signal(signal.SIGINT, _handle_sigint)
```

Inside the GA loop, after each generation:
```python
if _shutdown_requested:
    log.info("Graceful shutdown: evaluating gen %d and saving final checkpoint", gen)
    _save_checkpoint(...)
    break
```

#### 11.2 — Checkpoint Persistence (every 5 generations) — npz + JSON, NOT pickle

Save state as numpy `.npz` for arrays and JSON for metadata. This is robust across numpy/Python version upgrades (unlike pickle):

```python
CHECKPOINT_DIR = Path("checkpoints")
CHECKPOINT_INTERVAL = 5  # save every 5 generations

def _save_checkpoint(
    sport_key: str | None, generation: int,
    population: np.ndarray, fitness_history: list[float],
    rng: np.random.Generator,
) -> Path:
    CHECKPOINT_DIR.mkdir(exist_ok=True)
    label = sport_key or "all"

    # Arrays → npz (version-stable)
    np.savez(
        CHECKPOINT_DIR / f"qbot_pop_{label}.npz",
        population=population,
    )

    # Metadata → JSON (human-readable, debuggable)
    meta = {
        "generation": generation,
        "fitness_history": fitness_history,
        "rng_state": rng.bit_generator.state,  # serializable dict
        "saved_at": datetime.utcnow().isoformat(),
        "dna_genes": DNA_GENES,  # track schema for compatibility check
        "population_shape": list(population.shape),
    }
    with open(CHECKPOINT_DIR / f"qbot_meta_{label}.json", "w") as f:
        json.dump(meta, f, default=str)

    log.info("Checkpoint saved: gen=%d → %s", generation, CHECKPOINT_DIR / label)
    return CHECKPOINT_DIR
```

Key design: `train_data` and `val_data` are NOT checkpointed — they're deterministically re-derived from MongoDB tips on resume (same sort order guarantees identical splits).

#### 11.3 — Resume from Checkpoint (`--resume` flag)

```python
def _load_checkpoint(sport_key: str | None) -> dict | None:
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
```

In `run_evolution()`, after loading tips and creating train/val splits:
```python
if resume:
    ckpt = _load_checkpoint(sport_key)
    if ckpt:
        population = ckpt["population"]
        start_gen = ckpt["generation"] + 1
        rng.bit_generator.state = ckpt["rng_state"]
        log.info("Resumed from checkpoint: gen=%d, pop=%s", ckpt["generation"], population.shape)

        # Handle DNA schema expansion (e.g., 7→13 genes between runs)
        if population.shape[1] < len(DNA_GENES):
            old_cols = population.shape[1]
            new_cols = len(DNA_GENES) - old_cols
            # Pad with neutral defaults (midpoint of range for new genes)
            defaults = np.array([
                (DNA_RANGES[g][0] + DNA_RANGES[g][1]) / 2
                for g in DNA_GENES[old_cols:]
            ])
            padding = np.tile(defaults, (population.shape[0], 1))
            population = np.hstack([population, padding])
            log.info("Padded population: %d → %d genes", old_cols, len(DNA_GENES))
    else:
        log.warning("No checkpoint found for %s — starting fresh", sport_key or "all")
```

#### 11.4 — Radiation Event (Stagnation Escape)

When best fitness hasn't improved for 20 consecutive generations, temporarily spike mutation rate:

```python
STAGNATION_THRESHOLD = 20
RADIATION_MUTATION_RATE = 0.25  # vs default ~0.10

# Inside GA loop:
stagnant_gens = 0
prev_best = -np.inf
for gen in range(start_gen, generations):
    ...
    current_best = fitness.max()
    if current_best <= prev_best + 1e-6:
        stagnant_gens += 1
    else:
        stagnant_gens = 0
    prev_best = current_best

    if stagnant_gens >= STAGNATION_THRESHOLD:
        log.warning("⚡ RADIATION EVENT at gen %d — spiking mutation to %.0f%%", gen, RADIATION_MUTATION_RATE * 100)
        children = mutate(children, rng, mutation_rate=RADIATION_MUTATION_RATE)
        stagnant_gens = 0  # reset counter
    else:
        children = mutate(children, rng)
```

The `mutate()` function needs a `mutation_rate` parameter (currently it uses a hardcoded rate). Add it as an optional kwarg with a default.

#### 11.5 — Watch Mode (`--watch` flag) — Periodic Tip Reload

For 24/7 mining, periodically reload tips from MongoDB to incorporate newly resolved results:

```python
import time as _time

WATCH_DEFAULT_INTERVAL = 6 * 3600  # 6 hours in seconds

async def run_watch_mode(sport_key, interval=WATCH_DEFAULT_INTERVAL, **kwargs):
    """Run evolution in a loop, reloading tips every `interval` seconds."""
    cycle = 0
    while True:
        cycle += 1
        log.info("Watch cycle %d — loading latest tips...", cycle)
        result = await run_evolution(sport_key=sport_key, resume=True, **kwargs)
        log.info("Watch cycle %d complete. Best ROI: %.2f%%. Sleeping %ds...",
                 cycle, result["validation_fitness"]["roi"] * 100, interval)
        _time.sleep(interval)
```

Key: uses `--resume` each cycle so GA continues from the last checkpoint with fresh data. Tips are re-derived from MongoDB each cycle (not from checkpoint), so new resolved tips are automatically included.

#### 11.6 — CLI additions

```python
parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
parser.add_argument("--watch", type=str, default=None, metavar="INTERVAL",
                    help="Run in watch mode, reloading tips every INTERVAL (e.g., '6h', '30m')")
```

---

### Phase 12: Deep Search Mode — Expanding-Window CV & Rich Monitoring

**File:** `tools/qbot_evolution_arena.py`

**Goal:** Add a `--mode deep` option with expanding-window temporal cross-validation and pessimistic fitness, plus real-time `rich.Table` progress monitoring.

**Dependency:** Add `rich>=13.0` to `backend/requirements.txt`.

#### 12.1 — Expanding-Window Temporal Cross-Validation (CORRECTED — no look-ahead)

Standard temporal CV with expanding training windows. This yields 4 folds (not 5) and never trains on future data:

```python
def temporal_expanding_cv(tips: list[dict], k: int = 5) -> list[tuple[list[dict], list[dict]]]:
    """
    Expanding-window temporal CV. Tips must be sorted chronologically.

    For k=5, creates 4 train/val pairs:
      Fold 1: train=[chunk 0],         val=[chunk 1]
      Fold 2: train=[chunk 0,1],       val=[chunk 2]
      Fold 3: train=[chunk 0,1,2],     val=[chunk 3]
      Fold 4: train=[chunk 0,1,2,3],   val=[chunk 4]

    Never trains on data from the future. Number of folds = k - 1.
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
```

#### 12.2 — Pessimistic Fitness (Worst Fold)

In deep mode, `evaluate_population()` runs on all folds. The bot's fitness is the **minimum** (worst) fold score:

```python
async def run_evolution_deep(sport_key, population_size=200, generations=100, ...):
    tips = await load_tips(sport_key)
    folds = temporal_expanding_cv(tips, k=5)  # 4 folds
    fold_data = [(vectorize_tips(train), vectorize_tips(val)) for train, val in folds]

    # GA loop
    for gen in range(generations):
        # Evaluate on ALL folds
        fold_fitnesses = []
        for train_d, val_d in fold_data:
            f = evaluate_population(population, train_d)
            fold_fitnesses.append(f)

        # Pessimistic: worst fold determines fitness
        all_folds = np.stack(fold_fitnesses)  # (K-1, P) = (4, P)
        fitness = all_folds.min(axis=0)        # (P,) — worst fold per bot

        # Selection, crossover, mutation as normal
        ...
```

The "worst fold" approach prevents overfitting to any particular time window. A bot must perform consistently across all 4 periods to score well.

#### 12.3 — Rich Progress Table

Real-time monitoring using `rich.live` and `rich.table`:

```python
from rich.live import Live
from rich.table import Table
from rich.console import Console

def _build_progress_table(results: list[dict]) -> Table:
    table = Table(title="Qbot Evolution Arena — Deep Search")
    table.add_column("League", style="cyan")
    table.add_column("Gen", justify="right")
    table.add_column("Best ROI", justify="right", style="green")
    table.add_column("Avg ROI", justify="right")
    table.add_column("Max DD", justify="right", style="red")
    table.add_column("Sharpe", justify="right")
    table.add_column("ETA", justify="right", style="dim")
    for r in results:
        table.add_row(
            r["league"], str(r["gen"]), f"{r['best_roi']:.1f}%",
            f"{r['avg_roi']:.1f}%", f"{r['max_dd']:.1f}%",
            f"{r['sharpe']:.2f}", r["eta"],
        )
    return table
```

Used inside the GA loop with `rich.Live` context manager to update the table in-place each generation.

#### 12.4 — CLI additions

```python
parser.add_argument("--mode", choices=["quick", "deep"], default="quick",
                    help="quick=80/20 split (default), deep=expanding-window CV pessimistic")
```

Mode defaults:
- `quick`: 50 pop, 30 gens, 80/20 split (current behavior)
- `deep`: 200 pop, 100 gens, 4-fold expanding-window temporal CV, pessimistic fitness, rich monitoring

---

### Phase 13: Qbot Lab — Admin Strategy Dashboard

**Files:** `backend/app/routers/admin.py`, `frontend/src/views/admin/QbotLabView.vue`, `frontend/src/composables/useQbotStrategies.ts`, `frontend/src/router/index.ts`, `de.ts`, `en.ts`

**Goal:** Admin dashboard showing active Qbot strategies per league, fitness metrics, stress test results, and DNA detail with progressive disclosure.

#### TEAMS.md + UX Review — Key Findings

| Role | Finding | Resolution |
|------|---------|------------|
| **UX Designer** | Radar chart with 13 spokes of incomparable ranges (0.05–100) is unreadable. On mobile 320px, 13 labels overlap severely. | **Normalize DNA to 0-100% of range** for display. Use **CSS bar chart** (Tailwind div width) instead of Chart.js radar — zero bundle cost, fully responsive, accessible. |
| **Minimalist** | "Strain comparison" is a phantom feature. Unique partial index enforces ONE active strategy per sport_key. Max comparison = 5 rows (4 leagues + "all"). | **Rename to "Active Strategies by League"** — simple table, not a comparison matrix. |
| **Information Architect** | Admin's real question: "Is the bot making money? Should I re-run evolution?" DNA gene values are not actionable. | **Prioritize fitness metrics + stress test results.** Hero cards → strategy table → DNA behind expandable detail per row. |
| **Baker** | Backend must not normalize DNA for charts (leaks UI concerns). Frontend needs gene ranges to normalize. | Backend returns raw strategies + `gene_ranges` metadata dict. Frontend normalizes for display. |
| **Tent** | All 7 existing admin views follow the same pattern: composable fetch, `max-w-4xl`, surface-1 cards, tabular-nums tables. | Follow the established pattern exactly. |

#### 13.1 — Backend: `GET /api/admin/qbot/strategies`

Add to `backend/app/routers/admin.py`. Response shape:

```python
{
    "strategies": [
        {
            "id": "...",
            "sport_key": "soccer_epl",
            "version": "v2",
            "generation": 47,
            "dna": { ... },  # raw gene values
            "training_fitness": { "roi": 0.082, "sharpe": 1.52, "win_rate": 0.457, "total_bets": 7461, "max_drawdown_pct": 0.12 },
            "validation_fitness": { ... },
            "stress_test": {                                    # NEW from Phase 15
                "bootstrap_p_positive": 0.94,
                "bootstrap_ci_95": [-0.02, 0.18],
                "bootstrap_mean_roi": 0.078,
                "monte_carlo_ruin_prob": 0.023,
                "monte_carlo_max_dd_median": 0.08,
                "monte_carlo_max_dd_95": 0.21,
                "ensemble_size": 4,
                "stress_passed": true,
            },
            "is_active": true,
            "created_at": "2026-02-24T...",
            "age_days": 5,              # computed: (utcnow() - created_at).days
            "overfit_warning": false,    # computed: train_roi - val_roi > 0.15 (DIRECTIONAL, not absolute)
        },
    ],
    "gene_ranges": {                     # from DNA_RANGES constant
        "min_edge": [3.0, 15.0],
        "min_confidence": [0.30, 0.80],
        ...
    },
    "summary": {
        "total_active": 3,
        "avg_val_roi": 0.074,
        "worst_league": "soccer_spain_la_liga",
        "worst_roi": 0.031,
        "oldest_strategy_days": 14,
        "all_stress_passed": true,       # NEW
    }
}
```

**Overfit warning** is DIRECTIONAL: `train_roi - val_roi > 0.15` (only warns when training significantly exceeds validation, not the reverse).

#### 13.2 — Composable: `useQbotStrategies.ts`

New file following `useQTipPerformance.ts` pattern:

```typescript
export interface StressTestResult {
  bootstrap_p_positive: number;
  bootstrap_ci_95: [number, number];
  bootstrap_mean_roi: number;
  monte_carlo_ruin_prob: number;
  monte_carlo_max_dd_median: number;
  monte_carlo_max_dd_95: number;
  ensemble_size: number;
  stress_passed: boolean;
}

export interface QbotStrategy {
  id: string;
  sport_key: string;
  version: string;
  generation: number;
  dna: Record<string, number>;
  training_fitness: { roi: number; sharpe: number; win_rate: number; total_bets: number; max_drawdown_pct: number };
  validation_fitness: { roi: number; sharpe: number; win_rate: number; total_bets: number; max_drawdown_pct: number };
  stress_test?: StressTestResult;
  is_active: boolean;
  created_at: string;
  age_days: number;
  overfit_warning: boolean;
}

export interface QbotStrategiesResponse {
  strategies: QbotStrategy[];
  gene_ranges: Record<string, [number, number]>;
  summary: {
    total_active: number;
    avg_val_roi: number;
    worst_league: string;
    worst_roi: number;
    oldest_strategy_days: number;
    all_stress_passed: boolean;
  };
}
```

#### 13.3 — View: `QbotLabView.vue`

Layout (follows AdminProviderStatus pattern):

```
Section 1: Hero Stats (grid-cols-2 md:grid-cols-4)
┌─────────────────┬─────────────────┬─────────────────┬─────────────────┐
│ Active          │ Avg ROI (val)   │ Worst League    │ Stress Test     │
│ 3 strategies    │ +7.4%           │ La Liga +3.1%   │ All Passed ✓    │
│                 │ emerald/red     │ amber if <5%    │ red if any fail │
└─────────────────┴─────────────────┴─────────────────┴─────────────────┘

Section 2: Active Strategies Table (bg-surface-1 rounded-card)
┌──────────┬─────┬──────┬────────┬──────────┬────────┬───────┬─────┬────────┐
│ League   │ Gen │ ROI  │ Sharpe │ Win Rate │ Bets   │ Ruin% │ Age │ Status │
├──────────┼─────┼──────┼────────┼──────────┼────────┼───────┼─────┼────────┤
│ ▸ EPL    │ 47  │ 8.1% │ 1.48  │ 44.5%    │ 2589   │ 2.3%  │ 5d  │  ✓    │
│ ▸ BuLi   │ 30  │ 6.1% │ 1.20  │ 44.2%    │ 2010   │ 3.1%  │ 12d │  ✓    │
│ ▸ La Liga│ 22  │ 3.1% │ 0.85  │ 41.0%    │ 1400   │ 4.8%  │ 14d │ ⚠ stale│
│ ▸ all    │ 35  │ 5.8% │ 1.10  │ 43.1%    │ 6187   │ 1.9%  │ 8d  │  ✓    │
└──────────┴─────┴──────┴────────┴──────────┴────────┴───────┴─────┴────────┘
  ^ Click row to expand DNA + stress test detail

Section 3: Expanded Detail (inline below clicked row)
┌─────────────────────────────────────────────────────────────────────┐
│ Strategy DNA — Premier League (Gen 47, v2)                          │
│                                                                     │
│ min_edge        8.14    ████████░░░░░░░░  43%   (range: 3–15)     │
│ min_confidence  0.60    ██████████░░░░░░  60%   (range: 0.3–0.8)  │
│ sharp_weight    1.45    ████████████░░░░  63%   (range: 0.5–2.0)  │
│ home_bias       0.95    █████████░░░░░░░  38%   (range: 0.8–1.2)  │
│ ...                                                                 │
│                                                                     │
│ Training:    ROI 8.2% | Sharpe 1.52 | WR 45.7% | 7461 bets       │
│ Validation:  ROI 8.1% | Sharpe 1.48 | WR 44.5% | 2589 bets  [OK] │
│                                                                     │
│ Stress Test:                                                        │
│ Bootstrap:   ROI 7.8% ± [−2.0%, +18.0%] | p(positive)=94%        │
│ Monte Carlo: Ruin risk 2.3% | Max DD median 8% | 95th pctl 21%   │
│ Ensemble:    4 of 5 bots passed → deployed as median-vote         │
└─────────────────────────────────────────────────────────────────────┘
```

DNA normalized bars are pure Tailwind `<div>` elements with `:style="{ width: pct + '%' }"` and `bg-indigo-500`. Overfit warning shown when `train_roi - val_roi > 0.15`.

Status indicators:
- Green checkmark: strategy is fresh, healthy, stress test passed
- Amber warning: age > 21 days (stale), overfit detected, or stress test marginal
- Red: validation ROI negative or stress test failed

#### 13.4 — Router + Admin Dashboard Link

Add to `router/index.ts` admin routes block:
```typescript
{ path: "/admin/qbot-lab", component: () => import("@/views/admin/QbotLabView.vue"), meta: { requiresAuth: true, requiresAdmin: true } }
```

Add a quick-link card to `AdminDashboardView.vue` (matching existing pattern).

#### 13.5 — i18n keys

**German:**
```
qbotLab.title: "Qbot Labor"
qbotLab.activeStrategies: "Aktive Strategien"
qbotLab.avgRoi: "Ø ROI (Validierung)"
qbotLab.worstLeague: "Schwächste Liga"
qbotLab.oldestStrategy: "Älteste Strategie"
qbotLab.stressTest: "Stresstest"
qbotLab.allPassed: "Alle bestanden"
qbotLab.dnaDetail: "DNA-Parameter"
qbotLab.overfit: "Überanpassung erkannt"
qbotLab.stale: "Veraltet"
qbotLab.training: "Training"
qbotLab.validation: "Validierung"
qbotLab.days: "{n} Tage"
qbotLab.generation: "Generation"
qbotLab.riskProfile: "Risikoprofil"
qbotLab.bootstrap: "Bootstrap-Konfidenz"
qbotLab.ruinRisk: "Ruin-Risiko"
qbotLab.ensembleSize: "Ensemble-Größe"
qbotLab.stressPassed: "Stresstest bestanden"
qbotLab.stressFailed: "Stresstest fehlgeschlagen"
```

**English:** Same keys, English translations.

---

### Phase 14: NOT IMPLEMENTED — Deferred Ideas

The following were evaluated and deliberately deferred. Documented here to prevent re-proposal:

| Idea | Reason Deferred |
|------|----------------|
| Apple Silicon / AMX tuning | numpy already uses Accelerate. Runtime is seconds, not hours. No measurable gain. |
| DNA range narrowing around Alpha-Bot | Convergence trap. Contradicts radiation events. |
| Pareto Fronts with manual gene flow | Requires NSGA-II rewrite, not compatible with single-fitness GA. Ensemble (Phase 15) achieves similar robustness. |
| Hardware thermal watchdog | Not a real problem at these compute levels. |
| 3D arrays (Bots × Leagues × Tips) | 17GB memory, no benefit. Sequential iteration is <60s. |

---

### Phase 15: Strategy Stress Test — Bootstrap, Monte Carlo & Ensemble

**File:** `tools/qbot_evolution_arena.py` (new section after `run_evolution()`)

**Goal:** After the GA finds candidate bots, validate that the edge is real (not noise) and the bankroll risk is acceptable. No strategy activates without passing stress test.

#### 15.1 — Bootstrap Fitness Confidence Intervals

Resamples the tip set 2,000× with replacement to estimate confidence interval on ROI:

```python
def bootstrap_fitness(
    bot_dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    n_samples: int = 2000,
    rng: np.random.Generator = None,
) -> dict:
    """
    Bootstrap confidence interval for a single bot's ROI.

    Returns:
        mean: mean ROI across bootstrap samples
        ci_95: (low, high) 95% confidence interval
        p_positive: fraction of samples where ROI > 0
    """
    n_tips = tip_data["odds"].shape[0]
    # Expand single bot DNA to (1, G) for evaluate_population compatibility
    pop = bot_dna[np.newaxis, :]  # (1, G)

    rois = np.empty(n_samples)
    for i in range(n_samples):
        idx = rng.choice(n_tips, size=n_tips, replace=True)
        sampled = {k: v[idx] for k, v in tip_data.items() if isinstance(v, np.ndarray)}
        # evaluate_population returns fitness, but we need raw ROI
        roi = _compute_roi_single(pop, sampled)  # returns scalar
        rois[i] = roi

    ci_low, ci_high = np.percentile(rois, [2.5, 97.5])
    return {
        "mean": float(rois.mean()),
        "ci_95": (round(float(ci_low), 4), round(float(ci_high), 4)),
        "p_positive": round(float((rois > 0).mean()), 4),
    }
```

`_compute_roi_single()` is a thin wrapper: runs the full confidence pipeline + Kelly + profit computation for a 1-bot population and returns scalar ROI. Reuses the existing `evaluate_population()` logic.

**Quality gate:** `p_positive < 0.90` → strategy is NOT activated.

#### 15.2 — Monte Carlo Bankroll Simulation

Simulates 10,000 bankroll paths to estimate ruin probability and drawdown distribution:

```python
def monte_carlo_bankroll(
    bot_dna: np.ndarray,
    tip_data: dict[str, np.ndarray],
    n_paths: int = 10000,
    initial_bank: float = 1000.0,
    ruin_threshold: float = 0.20,
    rng: np.random.Generator = None,
) -> dict:
    """
    Monte Carlo bankroll simulation with randomized bet ordering.

    Returns:
        ruin_prob: P(bankroll drops below ruin_threshold * initial)
        max_dd_median: median max drawdown across paths
        max_dd_95: 95th percentile max drawdown
        terminal_wealth_median: median final bankroll
    """
    # Pre-compute bet outcomes for this bot's DNA
    pop = bot_dna[np.newaxis, :]
    stakes, profits = _compute_bet_outcomes(pop, tip_data)  # (N,) arrays
    # Only include bets where stake > 0 (the bot chose to bet)
    active = stakes > 0
    bet_pnl = (profits * active).flatten()  # per-tip P&L
    bet_pnl = bet_pnl[bet_pnl != 0]        # remove non-bets
    n_bets = len(bet_pnl)

    if n_bets < 10:
        return {"ruin_prob": 1.0, "max_dd_median": 1.0, "max_dd_95": 1.0,
                "terminal_wealth_median": initial_bank, "n_bets": n_bets}

    # Build shuffled paths: (n_paths, n_bets)
    paths = np.tile(bet_pnl, (n_paths, 1))
    for i in range(n_paths):
        rng.shuffle(paths[i])

    # Cumulative bankroll
    cum = initial_bank + np.cumsum(paths, axis=1)  # (n_paths, n_bets)

    # Ruin: bankroll ever drops below threshold
    ruin_level = initial_bank * ruin_threshold
    ruin_prob = (cum.min(axis=1) < ruin_level).mean()

    # Max drawdown per path
    cum_max = np.maximum.accumulate(cum, axis=1)
    dd = (cum_max - cum) / np.maximum(cum_max, 1.0)
    max_dd = dd.max(axis=1)  # (n_paths,)

    return {
        "ruin_prob": round(float(ruin_prob), 4),
        "max_dd_median": round(float(np.median(max_dd)), 4),
        "max_dd_95": round(float(np.percentile(max_dd, 95)), 4),
        "terminal_wealth_median": round(float(np.median(cum[:, -1])), 2),
        "n_bets": n_bets,
    }
```

**Quality gate:** `ruin_prob > 0.05` → Kelly fraction scaled down by 20%, re-simulate. If still fails after 3 attempts → strategy is NOT activated.

#### 15.3 — Ensemble Top-N Deployment

Instead of deploying the single best bot, use median-vote of the top-N that pass stress test:

```python
def build_ensemble(
    top_n_dna: list[np.ndarray],
    tip_data: dict[str, np.ndarray],
    rng: np.random.Generator,
    bootstrap_threshold: float = 0.90,
    ruin_threshold: float = 0.05,
) -> dict:
    """
    Filter top-N bots through stress test, build ensemble from survivors.

    Returns:
        ensemble_dna: list of DNA arrays that passed stress test
        ensemble_metrics: per-bot stress test results
        deployment_method: "median" — median stake across ensemble
    """
    survivors = []
    metrics = []

    for i, dna in enumerate(top_n_dna):
        # Bootstrap
        bs = bootstrap_fitness(dna, tip_data, rng=rng)
        if bs["p_positive"] < bootstrap_threshold:
            log.info("Bot %d failed bootstrap: p_positive=%.2f < %.2f", i, bs["p_positive"], bootstrap_threshold)
            continue

        # Monte Carlo
        mc = monte_carlo_bankroll(dna, tip_data, rng=rng)
        if mc["ruin_prob"] > ruin_threshold:
            # Try scaling Kelly down
            scaled_dna = dna.copy()
            for attempt in range(3):
                kelly_idx = DNA_GENES.index("kelly_fraction")
                scaled_dna[kelly_idx] *= 0.80
                mc = monte_carlo_bankroll(scaled_dna, tip_data, rng=rng)
                if mc["ruin_prob"] <= ruin_threshold:
                    dna = scaled_dna
                    break
            else:
                log.info("Bot %d failed Monte Carlo after 3 Kelly reductions: ruin=%.2f", i, mc["ruin_prob"])
                continue

        survivors.append(dna)
        metrics.append({"bootstrap": bs, "monte_carlo": mc})

    if not survivors:
        log.warning("No bots passed stress test — no strategy activated")
        return None

    return {
        "ensemble_dna": [d.tolist() for d in survivors],
        "ensemble_size": len(survivors),
        "ensemble_metrics": metrics,
        "deployment_method": "median",
        "stress_passed": True,
    }
```

#### 15.4 — Integration into `run_evolution()`

After the GA loop completes and the best bot is identified:

```python
# After GA loop — extract top 5 bots
top_indices = np.argsort(fitness)[-5:][::-1]
top_dna = [population[i] for i in top_indices]

# Stress test
log.info("Running stress test on top %d bots...", len(top_dna))
ensemble_result = build_ensemble(top_dna, val_data, rng=rng)

if ensemble_result is None:
    log.warning("Strategy NOT activated — stress test failed for all candidates")
    if not dry_run:
        # Still save strategy doc but with is_active=False
        strategy_doc["is_active"] = False
        strategy_doc["stress_test"] = {"stress_passed": False, "reason": "all_candidates_failed"}
else:
    strategy_doc["stress_test"] = {
        "bootstrap_p_positive": ensemble_result["ensemble_metrics"][0]["bootstrap"]["p_positive"],
        "bootstrap_ci_95": ensemble_result["ensemble_metrics"][0]["bootstrap"]["ci_95"],
        "bootstrap_mean_roi": ensemble_result["ensemble_metrics"][0]["bootstrap"]["mean"],
        "monte_carlo_ruin_prob": ensemble_result["ensemble_metrics"][0]["monte_carlo"]["ruin_prob"],
        "monte_carlo_max_dd_median": ensemble_result["ensemble_metrics"][0]["monte_carlo"]["max_dd_median"],
        "monte_carlo_max_dd_95": ensemble_result["ensemble_metrics"][0]["monte_carlo"]["max_dd_95"],
        "ensemble_size": ensemble_result["ensemble_size"],
        "stress_passed": True,
    }
    strategy_doc["ensemble_dna"] = ensemble_result["ensemble_dna"]
    strategy_doc["deployment_method"] = "median"
```

#### 15.5 — Intelligence Service — Ensemble Kelly

**File:** `backend/app/services/qbot_intelligence_service.py`

Update `_compute_kelly_stake()` to support ensemble mode:

```python
def _compute_kelly_stake(tip: dict, strategy: dict) -> tuple[float, float]:
    ensemble_dna = strategy.get("ensemble_dna")

    if ensemble_dna and len(ensemble_dna) > 1:
        # Ensemble mode: compute Kelly for each bot, take median
        stakes = []
        raws = []
        for dna_list in ensemble_dna:
            dna = {DNA_GENES[i]: v for i, v in enumerate(dna_list)}
            s, r = _compute_kelly_stake_single(tip, dna)
            stakes.append(s)
            raws.append(r)
        median_stake = round(float(np.median(stakes)), 2)
        median_raw = round(float(np.median(raws)), 4)
        return median_stake, median_raw
    else:
        # Single bot mode (backward compatible)
        dna = strategy.get("dna", {})
        return _compute_kelly_stake_single(tip, dna)
```

Extract the current body of `_compute_kelly_stake()` into `_compute_kelly_stake_single(tip, dna)`.

#### 15.6 — Estimated Compute Budget

Per league:
- Bootstrap (2,000 resamples × 5 bots): ~30 seconds
- Monte Carlo (10,000 paths × 5 bots): ~30 seconds
- Total stress test: ~60 seconds per league
- 6 leagues: ~6 minutes total

This runs once per `run_evolution()` call, not per generation. Compute cost is negligible relative to the GA.

---

## Files to Modify

| File | Changes |
|------|---------|
| `backend/app/services/qbot_intelligence_service.py` | Fix strategy cache (lines 102-135), update `_compute_kelly_stake()` for ensemble (lines 209-231), dual-mode `enrich_tip()` (lines 238-302), add "the_strategist" archetype |
| `backend/app/services/quotico_tip_service.py` | Add `generate_score_matrix()` with factorial LUT + `compute_player_prediction()`, update `_no_signal_bet()` (line 177), update `generate_quotico_tip()` (lines 1394, 1443), add `qbot_logic` to `QuoticoTipResponse` (line 157) |
| `backend/app/routers/quotico_tips.py` | Pass `qbot_logic` in `list_tips()` (line 65) and `get_tip()` (line 254) |
| `backend/app/routers/admin.py` | Add `GET /api/admin/qbot/strategies` endpoint with stress test data (~70 lines) |
| `backend/app/services/matchday_service.py` | Update `_qbot_prediction()` (line 74) for Player Mode exact scores |
| `backend/app/workers/matchday_resolver.py` | Expand tip projection (line 98) to include `qbot_logic` |
| `tools/qbot_evolution_arena.py` | DNA expansion, advanced fitness (draw gate AFTER confidence pipeline), multi-league wrapper with `--parallel`, checkpoint/resume (npz+JSON, NOT pickle), SIGINT handler, radiation events, watch mode, deep mode (expanding-window CV, NOT symmetric k-fold), rich monitoring, stress test (bootstrap + Monte Carlo + ensemble), CLI flags (`--multi`, `--parallel`, `--resume`, `--watch`, `--mode`) |
| `backend/requirements.txt` | Add `rich>=13.0` |
| `frontend/src/composables/useQbotStrategies.ts` | **New file** — composable for strategy fetching with `StressTestResult` interface (~50 lines) |
| `frontend/src/views/admin/QbotLabView.vue` | **New file** — admin dashboard with stress test display (~220 lines) |
| `frontend/src/views/admin/AdminDashboardView.vue` | Add Qbot Lab quick-link card |
| `frontend/src/router/index.ts` | Add `/admin/qbot-lab` route |
| `frontend/src/composables/useQuoticoTip.ts` | Add `PlayerPrediction` interface, update `qbot_logic` type |
| `frontend/src/components/QuoticoTipBadge.vue` | Player Mode display: compact pill, dual-mode no_signal, expanded panel section |
| `frontend/src/locales/de.ts` | Add strategist + Player Mode + Qbot Lab + stress test labels |
| `frontend/src/locales/en.ts` | Same i18n keys in English |

**No changes needed:** `database.py` (existing indexes sufficient), `calibration_worker.py`, `quotico_tip_worker.py` (existing `enrich_tip()` call flow handles the changes — verify write order for transient `player_prediction` field)

---

## Verification

### Arena v2 (Phases 1-6)

1. **Cache bugfix:** Hit `/api/quotico-tips/{match_id}/refresh` for two different sport_keys. Verify each gets the correct per-league strategy.

2. **Single-league dry run:**
   ```bash
   python -m tools.qbot_evolution_arena --sport soccer_epl --dry-run
   ```
   Verify: 13 DNA genes, drawdown metric, validation metrics.

3. **Multi-league run:**
   ```bash
   python -m tools.qbot_evolution_arena --multi --dry-run
   ```
   Verify: per-league strategies + "all" fallback, cross-league DNA report.

4. **Parallel multi-league:**
   ```bash
   python -m tools.qbot_evolution_arena --multi --parallel --dry-run
   ```
   Verify: same results as sequential, faster wall time.

5. **Backward compatibility:** With v1 strategy active, verify `enrich_tip()` still works — all new genes use neutral defaults.

### Player Mode (Phases 7-10)

6. **Score matrix unit check:**
   ```python
   from app.services.quotico_tip_service import generate_score_matrix
   m = generate_score_matrix(1.5, 1.2, rho=-0.08)
   assert m.shape == (6, 6)
   assert abs(m.sum() - 1.0) < 0.001
   ```

7. **No-signal tip with player data:** Trigger a tip for a match with low edge. Verify DB document has `tier_signals.poisson` AND `qbot_logic.player` with `predicted_score`.

8. **Transient field cleanup:** After enrichment, verify `player_prediction` is NOT present in MongoDB document.

9. **Active tip dual-mode:** Verify `qbot_logic` has BOTH investor fields AND `player` sub-field.

10. **API response:** `GET /api/quotico-tips/` — verify `qbot_logic` present in JSON (was previously dropped).

11. **Matchday auto-tip:** Verify auto-filled predictions use Poisson peak score.

12. **Frontend build:** `pnpm build` passes with new `PlayerPrediction` type.

### Mining Mode (Phase 11)

13. **Checkpoint save:** Run `--generations 12 --sport soccer_epl`. Verify `checkpoints/qbot_pop_soccer_epl.npz` and `checkpoints/qbot_meta_soccer_epl.json` created at gen 5 and 10.

14. **SIGINT graceful exit:** Start a run, Ctrl+C mid-generation. Verify final checkpoint saved, no traceback.

15. **Resume:** Run `--resume --sport soccer_epl`. Verify log shows "Resumed from checkpoint: gen=X" and continues from where it left off.

16. **Resume with DNA expansion:** Create a checkpoint with 7-gene population. Resume with 13-gene config. Verify log shows "Padded population: 7 → 13 genes".

17. **Radiation event:** Run `--generations 60 --sport soccer_epl`. Check for "RADIATION EVENT" log message and mutation spike.

18. **Watch mode:** Run `--watch 10s --sport soccer_epl` (short interval for testing). Verify cycle 1 completes, tip reload, cycle 2 starts from checkpoint.

### Deep Search (Phase 12)

19. **Deep mode dry run:**
    ```bash
    python -m tools.qbot_evolution_arena --mode deep --sport soccer_epl --dry-run
    ```
    Verify: 4-fold expanding-window CV (NOT 5-fold symmetric), pessimistic fitness (worst fold), 200 pop / 100 gens, rich table displayed.

20. **No look-ahead:** Verify that fold 0 trains on chronologically earlier data than its validation set. No fold trains on future data.

21. **Rich table rendering:** Verify `rich.Table` updates in-place with League, Gen, Best ROI, Max DD, Sharpe, ETA columns.

### Strategy Stress Test (Phase 15)

22. **Bootstrap CI:**
    ```bash
    python -m tools.qbot_evolution_arena --sport soccer_epl
    ```
    Verify log output includes "Bootstrap: p_positive=X.XX, CI=[low, high]" for top 5 bots.

23. **Monte Carlo:**
    Verify log output includes "Monte Carlo: ruin_prob=X.XX, max_dd_median=X.XX" for surviving bots.

24. **Quality gate enforcement:** If all bots fail stress test, verify strategy is saved with `is_active=False` and `stress_test.stress_passed=False`.

25. **Kelly scaling:** If a bot fails Monte Carlo but has p_positive ≥ 0.90, verify Kelly fraction is reduced by 20% per attempt, up to 3 attempts.

26. **Ensemble in strategy doc:** Verify MongoDB strategy document contains `ensemble_dna` array with 1-5 DNA arrays.

27. **Ensemble Kelly in intelligence service:** With an ensemble strategy active, verify `_compute_kelly_stake()` computes median across ensemble members.

28. **Stress test runtime:** Verify stress test completes in <90 seconds per league.

### Qbot Lab (Phase 13)

29. **Admin API:** `GET /api/admin/qbot/strategies` returns `strategies[]` with `stress_test` object, `gene_ranges`, and `summary`. Verify `age_days`, `overfit_warning` (directional), and `all_stress_passed` are computed correctly.

30. **Qbot Lab view:** Navigate to `/admin/qbot-lab`. Verify hero cards show correct totals including stress test status. Click a strategy row — DNA detail + stress test metrics expand.

31. **Admin dashboard link:** Verify "Qbot Lab" card appears in `/admin` quick-links grid.

32. **Frontend build:** `pnpm build` passes with all new files and types.