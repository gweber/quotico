# Qbot Evolution v3 — Code Reality (Arena + Intelligence + Backfill)

## Context
This document supersedes `docs/evolution-v2.md` as the **current implementation reference**.
It is based on the running code in:
- `tools/qbot_evolution_arena.py`
- `tools/qbot_ensemble_miner.py`
- `backend/app/services/qbot_intelligence_service.py`
- `backend/app/services/quotico_tip_service.py`
- `tools/qtip_backfill.py`
- `backend/app/routers/quotico_tips.py`

Goal: document what is actually shipped and extended in v3.

---

## 1. Executive Summary (What changed from v2)

1. The arena now runs with a 13-gene DNA and risk-aware fitness:
   - `fitness = 0.5*ROI + 0.3*Sharpe - 0.2*MaxDrawdownPct`
2. Multi-league, deep mode, watch mode, checkpoints, and stress testing are implemented.
3. Strategy cache bug is fixed in intelligence service (per-`sport_key` cache map).
4. Qbot Intelligence has xG-first post-match reasoning (2.4) plus market synergy/temporal context (2.3).
5. Player mode is integrated end-to-end:
   - score matrix + predicted score
   - exposed via `qbot_logic.player`
   - consumed by matchday auto-tip logic
6. Historical tip regeneration moved to Greenfield-only backfill (`odds_meta` only) with explicit enrichment pipeline and operational metrics.

---

## 2. Arena v3 (Implemented)

## 2.1 DNA Model
Implemented in `tools/qbot_evolution_arena.py`:

- Genes (`DNA_GENES`, 13 total):
  - `min_edge`, `min_confidence`, `sharp_weight`, `momentum_weight`, `rest_weight`, `kelly_fraction`, `max_stake`
  - `home_bias`, `away_bias`, `h2h_weight`, `draw_threshold`, `volatility_buffer`, `bayes_trust_factor`

- Current production ranges are **narrowed** vs early v2 proposal (`DNA_RANGES` tuned bounds).

## 2.2 Fitness + Risk
Implemented:
- Vectorized population evaluation (`evaluate_population`) over 2D arrays.
- Max drawdown is computed vectorized via cumulative PnL + running peak.
- Soft sample-size penalty is active (`SOFT_PENALTY_K`, `SOFT_PENALTY_LAMBDA`).

## 2.3 Confidence Pipeline (Arena)
Per-bot scoring applies:
1. Base signal boosts (sharp/momentum/rest)
2. Venue bias (`home_bias`, `away_bias`)
3. H2H amplification (`h2h_weight * h2h_tip`)
4. Bayesian blending (`bayes_trust_factor`)
5. Draw gate on adjusted confidence
6. Volatility buffer before Kelly

## 2.4 Stress Test + Rescue
Implemented stress layers:
- Bootstrap fitness (prefilter + final)
- Monte Carlo bankroll simulation (prefilter + final)
- Adaptive risk rescue (progressive Kelly/max-stake scaling)
- Safety floors and early-stop on low ruin improvement
- Timing telemetry for stress phases

## 2.5 Modes and Runtime
CLI supports:
- Quick mode (`--mode quick`)
- Deep mode (`--mode deep`) with expanding-window CV
- Multi-league (`--multi`) and parallel (`--parallel`)
- Watch loop (`--watch 6h` etc.)
- Resume from checkpoint (`--resume`)

## 2.6 Strategy Persistence
Quick mode persists strategy docs with:
- `version: "v3"`
- `fitness_version`, `stress_version`
- `pareto_candidates`
- stage/guardrail metadata in `optimization_notes`

Notes from code reality:
- `optimization_notes.schema_version` is still `"v1"`.
- Deep mode currently persists `version: "v2"` docs.

---

## 3. Intelligence Service (Qbot 2.3 + 2.4)

Implemented in `backend/app/services/qbot_intelligence_service.py`.

## 3.1 Strategy Cache Bugfix
Implemented:
- `_strategy_cache` is now a dict keyed by `sport_key`.
- `_get_active_strategy(sport_key)` returns sport-specific strategy with fallback to `"all"`.

## 3.2 Cluster Key v3 + Fallback
Cluster key dimensions:
- `sport|sharp|edge|momentum|volatility|temporal`

Hierarchical fallback:
- full key -> strip temporal -> strip volatility -> prior.

## 3.3 Market Context Extraction
Source of truth:
- `matches.odds_meta.markets.*`
- `matches.result.home_xg/away_xg`

Includes:
- provider depth, spread, volatility dim, trust factor
- totals-line fields for synergy
- temporal dim (`day|prime|late`) and `is_midweek`/`is_weekend`

## 3.4 Post-Match Reasoning (xG-first)
Priority in `_compute_post_match_reasoning`:
1. Discipline collapse / total collapse
2. xG checks:
   - `clinical_efficiency`
   - `xg_betrayal`
3. Pressure/flow fallbacks (`siege_failure`, `disrupted_flow`, etc.)

## 3.5 Dual Mode Enrichment
`enrich_tip(...)` now supports:
- Investor enrichment (archetype, Bayesian confidence, Kelly stake)
- Player enrichment (`qbot_logic.player`) if transient `player_prediction` exists
- `decision_trace` with filter/risk/market context details

---

## 4. Tip Generation + Player Mode

Implemented in `backend/app/services/quotico_tip_service.py`.

## 4.1 Score Matrix
Implemented:
- `generate_score_matrix(...)` (Dixon-Coles corrected)
- `compute_player_prediction(...)`

## 4.2 No-signal Preservation
`_no_signal_bet(...)` accepts optional:
- `poisson_data`
- `player_prediction`

This preserves meaningful data in no-signal tips for downstream enrichment/UI.

## 4.3 Tip Orchestrator
`generate_quotico_tip(...)` now:
- computes player prediction for both active and no-signal paths
- stores transient `player_prediction` for enrichment stage

## 4.4 API Exposure
`QuoticoTipResponse` includes `qbot_logic`, and router responses pass it through.

---

## 5. Matchday Integration

Implemented:
- `backend/app/services/matchday_service.py` prefers `qbot_logic.player.predicted_score` for Q-bot auto-picks.
- Fallback remains outcome-mapping if no player score exists.
- `backend/app/workers/matchday_resolver.py` projection includes `qbot_logic`.

---

## 6. Backfill v3 (Historical Regeneration)

Implemented in `tools/qtip_backfill.py` and admin router.

## 6.1 Greenfield Data Source
Backfill runs on:
- `odds_meta.markets.h2h.current`

No legacy `odds.h2h` write path remains in the tool.

## 6.2 Snapshot-aware Temporal Injection
`load_history_snapshots(...)` loads:
- `params`
- `reliability`
- `market_performance`
- `statistical_integrity`
- `meta.schema_version`

Then injects the applicable snapshot (`snapshot_date <= match_date`) into tip-engine cache.

## 6.3 Enrichment Pipeline
Per match:
1. `generate_quotico_tip(match, before_date=match_date)`
2. `enrich_tip(tip, match=match)`
3. `resolve_tip(tip, match)`

## 6.4 Operational Metrics
Backfill logs batch and summary metrics:
- `xg_coverage_pct`
- `archetype_distribution`
- `no_signal_rate_pct`
- `skipped_missing_odds_meta`
- `error_rate_pct`

Alert:
- warning if no-signal exceeds `QTIP_BACKFILL_NO_SIGNAL_WARN_PCT`.

## 6.5 Rerun Controls
CLI:
- `--rerun`: clear scope and regenerate
- `--rerun-failed`: clear only `status="error"` tips and retry
- mutual exclusion enforced

Error runs can upsert minimal `status="error"` tip docs for targeted retries.

## 6.6 Admin Backfill Guardrails
`POST /api/quotico-tips/backfill` now:
- uses Greenfield query and explicit `enrich_tip(..., match=match)`
- enforces max-scope guard (`QTIP_BACKFILL_ADMIN_MAX_MATCHES`)
- returns operational metrics similar to CLI
- rejects oversized runs with a CLI hint

---

## 7. Current CLI Surface (Arena)

Examples from current implementation:

```bash
python -m tools.qbot_evolution_arena
python -m tools.qbot_evolution_arena --sport soccer_epl
python -m tools.qbot_evolution_arena --multi
python -m tools.qbot_evolution_arena --multi --parallel
python -m tools.qbot_evolution_arena --mode deep
python -m tools.qbot_evolution_arena --watch 6h
python -m tools.qbot_evolution_arena --resume
```

---

## 8. Config Additions Relevant to v3

Implemented settings:
- `QTIP_BACKFILL_NO_SIGNAL_WARN_PCT`
- `QTIP_BACKFILL_ADMIN_MAX_MATCHES`

These are used by backfill tooling/admin endpoint for operational safety.

---

## 9. Ensemble Miner (Implemented)

Implemented in `tools/qbot_ensemble_miner.py`.

## 9.1 Purpose
Run deterministic multi-seed arena executions and build a robust consensus DNA.

## 9.2 Core Behavior
1. Executes N runs (`--runs`, default 5) with incremented seeds (`base_seed + i`).
2. Collects per-gene statistics across successful runs:
   - mean, std, CV
3. Classifies genes:
   - robust (`CV < 0.10`)
   - medium
   - unstable (`CV > 0.30`)
4. Builds consensus DNA:
   - robust/medium genes from ensemble mean
   - unstable genes from best run

## 9.3 Persistence Model
Writes shadow strategies into `qbot_strategies` with:
- `version: "v3"`
- `is_ensemble: true`
- `deployment_method: "ensemble_miner"`
- `archetype: "consensus"` (mandatory)

Optional (`--with-archetypes`):
- `profit_hunter` (best validation ROI)
- `volume_grinder` (highest validation bet volume)

All persisted as shadow strategies requiring explicit promotion.

## 9.4 CLI Surface
```bash
python -m tools.qbot_ensemble_miner --sport soccer_epl
python -m tools.qbot_ensemble_miner --multi --runs 7 --base-seed 42
python -m tools.qbot_ensemble_miner --sport soccer_epl --mode deep
python -m tools.qbot_ensemble_miner --with-archetypes
```

## 9.5 Code Reality Notes
1. `optimization_notes.schema_version` is still `"v1"` in miner output.
2. In the consensus strategy payload, `schema_version` appears duplicated in code assignment (same value).
3. Miner depends on arena discovery and min-tip eligibility (`MIN_TIPS_PER_LEAGUE` from arena).

---

## 10. Differences vs original v2 proposal (Code Reality)

1. Arena strategy docs are now `version: "v3"` in quick mode.
2. Deep mode still emits `version: "v2"` docs (mixed-version reality).
3. Some stress thresholds in code are more permissive than early design targets
   (e.g. bootstrap/ruin gates in adaptive rescue and ensemble paths).
4. Arena uses narrowed practical DNA ranges (not the widest conceptual ranges from v2 planning).
5. Backfill v3 is now fully Greenfield and integrated with intelligence enrichment.

---

## 11. Known Technical Debt / Next Cleanup Candidates

1. Align deep mode strategy versioning (`v2` -> `v3`) and metadata schema labels.
2. Harmonize stress thresholds/constants between all stress paths (single/ensemble/deep).
3. Standardize optimization metadata schema version in strategy docs.
4. Add one canonical “effective thresholds” section to strategy docs for easier audit.

---

## 12. Validation Status (from tests/code)

Codebase already contains dedicated tests for:
- Qbot 2.3 (`backend/tests/test_qbot_2_3.py`)
- Qbot 2.4 xG-first reasoning (`backend/tests/test_qbot_2_4.py`)
- Backfill v3 behavior (`backend/tests/test_qtip_backfill_v3.py`)
- Admin backfill guards (`backend/tests/test_quotico_tips_backfill_admin_guard.py`)

This document reflects those implemented paths.
