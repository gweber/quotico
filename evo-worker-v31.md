# evo-worker-v31.md

## Implementierungsplan v3.1/v3.2 — Justice-basierte Robustheit, Operational Intelligence & Safety

### Summary
Dieses Dokument ist die vollständige, implementierungsfertige Spezifikation für den Greenfield-Rework der mathematischen Pipeline (Neuneck) und aller angrenzenden Safety-/Ops-Schichten.

Ziele:
1. Vollständig v3-only (keine Legacy-Keys/-Collections im Kernpfad).
2. Deterministische, auditable und policy-gesteuerte Entscheidungen.
3. Live/Backtest-Parität durch Shared Gate Logic.
4. Robuste Evolution (Arena/Miner) mit Diversität, Kalibrierung und Datenresilienz.
5. Klare, privacy-sichere Decision-Trace-Transparenz.

---

## 1. Scope & Invarianten

### 1.1 Kern-Invarianten
1. Core-Datenquellen:
- `matches_v3`
- `teams_v3`
- `league_registry_v3`
- `quotico_tips`
- `qbot_strategies`
- `qbot_cluster_stats`
- `engine_config_history`
- `engine_time_machine_justice`
- `engine_policies_v3`
2. Identität:
- `_id == sm_id (int)` in v3-Collections.
- `quotico_tips.match_id` ist `int` und referenziert `matches_v3._id`.
3. Zeitvertrag (Kernpfade):
- Nur `updated_at_utc`.
- Nur UTC-Utils: `utcnow`, `ensure_utc`, `parse_utc`.
4. Verboten im Kern:
- `team_key`, `home_team_key`, `away_team_key`
- ObjectId-basierte Match-Joins
- neue `updated_at`-Writes

### 1.2 Betroffene Kernmodule
1. `backend/app/services/justice_service.py`
2. `backend/app/services/optimizer_service.py`
3. `backend/app/services/engine_time_machine_service.py` (neu)
4. `backend/app/services/qbot_backtest_service.py`
5. `backend/app/services/qbot_intelligence_service.py`
6. `backend/app/services/qbot_evolution_arena_service.py` (neu)
7. `backend/app/services/reliability_service.py`
8. `backend/app/services/quotico_tip_service.py`
9. `backend/app/services/qbot_ensemble_miner_service.py` (neu)

### 1.3 Integrationsmodule
1. `backend/app/workers/calibration_worker.py`
2. `backend/app/workers/quotico_tip_worker.py`
3. `backend/app/routers/admin.py`
4. `backend/app/routers/quotico_tips.py`
5. `backend/app/database.py`
6. `backend/app/models/v3_models.py`
7. `backend/app/services/sportmonks_connector.py`
8. `backend/app/services/metrics_heartbeat.py`
9. `backend/app/routers/admin_ingest.py`

### 1.4 Tool-Wrapping
1. `tools/engine_time_machine.py`
2. `tools/qbot_evolution_arena.py`
3. `tools/qbot_ensemble_miner.py`

Regel: Tools bleiben CLI-Entrypoints, enthalten keine Fachlogik mehr.

---

## 2. Public API / Type Changes

### 2.1 Breaking Changes
1. `quotico_tips.match_id: int` (kein String/ObjectId-Fallback im Kern).

### 2.2 Pflichtfelder in `qbot_logic`
1. `ensemble_stability`
2. `signal_stage` (`alpha|beta|omega|caution`)
3. `justice_check`
4. `referee_bias`
5. `lineup_stability`
6. `market_liquidity`
7. `market_inference`
8. `crowd_sentiment`
9. `decision_trace`
10. `meta_confidence`
11. `moral_trace`
12. `absurdity_check`

### 2.3 Strategie-Dokument (`qbot_strategies`)
Pflichtfelder:
1. `status` (`shadow|active|archived`)
2. `kpi_owner`
3. `schema_version`
4. `optimization_notes` inklusive:
- ensemble diversity stats
- drift/calibration stats
- delay resilience stats

---

## 3. Policy System (Runtime-Steuerung)

### 3.1 Collections
1. `engine_policies_v3`
2. `engine_policy_audit_v3`
3. optional: `engine_manual_overrides_v3`

### 3.2 Policy-Service Contract
```python
from typing import Any

class PolicyService:
    async def get(self, key: str, default: Any = None) -> Any:
        ...

    async def get_snapshot(self, keys: list[str]) -> dict[str, Any]:
        """Returns immutable policy snapshot for a single decision execution."""
        ...

    async def set(self, key: str, value: Any, changed_by: str, reason: str) -> None:
        ...
```

### 3.3 Pflicht-Policies (Auszug)
1. `JUSTICE_FLOOR`
2. `CONTRARIAN_JUSTICE_FLOOR`
3. `DIVERSITY_CAP`
4. `MAX_LINEUP_UNCERTAINTY`
5. `MARKET_DRIFT_ALERT_PCT`
6. `MARKET_BAYES_PRIOR_WEIGHT_MIN`
7. `MARKET_BAYES_PRIOR_WEIGHT_MAX`
8. `SEASON_BURNIN_MATCHDAYS`
9. `SEASON_EARLY_PCT`
10. `KILLSWITCH_HYSTERESIS_OFF`
11. `KILLSWITCH_HYSTERESIS_ON`
12. `KILLSWITCH_MIN_HOLD_SECONDS`
13. `SHADOW_MIN_TIPS`
14. `PROMOTION_MAX_ROI_DRIFT_PCT`
15. `PROMOTION_XROI_FLOOR`
16. `ECE_MIN_BUCKET_N`
17. `SENTIMENT_MIN_K`

### 3.4 Audit-Trail Pflichtfelder
- `policy_key`
- `old_value`
- `new_value`
- `changed_by`
- `changed_at_utc`
- `reason`
- `ticket_ref`

---

## 4. Shared Gate Logic (Live/Backtest-Parität)

### 4.1 Contract
```python
from dataclasses import dataclass
from typing import Any

@dataclass
class GateDecision:
    allowed: bool
    reason_code: str
    damping_factor: float
    policy_version_used: str
    gate_results: dict[str, Any]

async def can_signal_be_emitted(match_ctx: dict[str, Any], policy_ctx: dict[str, Any]) -> GateDecision:
    ...
```

### 4.2 Verbindliche Gate-Reihenfolge
1. Input Sanity Gate
2. Manual Override Gate
3. Core Risk Gates (Justice / Referee / Lineup / Market)
4. Philosophical Guardrail
- Epistemic Humility
- Moral Trace
- Absurdity Buffer
5. Final Emit / No-Signal

### 4.3 Fail-Mode Matrix
1. Justice-Gate: fail-closed
2. Sentiment-Hook: fail-open mit Warning
3. Market-Inference: default `open_with_warning` (policy-overridable)

---

## 5. Mathematical Spec 3.2 (Verbindlich)

### 5.1 ConservationGuard
Für `p = (p1, px, p2)`:
1. `p_i in [0,1]`
2. `abs((p1 + px + p2) - 1.0) <= EPS_PROB_SUM`

Defaults:
- `EPS_PROB_SUM = 1e-6`

Verhalten:
1. `fail_mode=closed` -> `ERR_PROB_CONSERVATION`
2. `fail_mode=renorm` -> normalisieren + `WARN_PROB_RENORM`

### 5.2 DNA_Complexity
Normiert in `[0,1]`:
1. `C_gene = mean_j(|z_j|)` mit `z_j=(g_j-mu_j)/(sigma_j+eps)`
2. `C_regime = mean_j(|g_low_j-g_high_j|/range_j)`
3. `C_epi = 1 - preserved_epistasis_ratio`
4. `DNA_Complexity = clip(w1*C_gene + w2*C_regime + w3*C_epi, 0, 1)`

Defaults:
- `w1=0.5, w2=0.3, w3=0.2, eps=1e-9`

### 5.3 EntropyDamping
1. `H_market`: Shannon-Entropie auf Outcome-Probs
2. `H_lineup`: normierte Lineup-Unsicherheit
3. `H_ref`: Referee-Unsicherheit

`H_total = clip(a*H_market + b*H_lineup + c*H_ref, 0, 1)`

`kelly_eff = kelly_raw * max(kelly_floor, 1 - lambda_entropy * H_total)`

Defaults:
- `a=0.5, b=0.3, c=0.2`
- `lambda_entropy=0.7`
- `kelly_floor=0.15`

### 5.4 Friction_Adjusted_ROI
Für Bet `i`:
1. `stake_exec_i = min(stake_i, liquidity_cap_i)`
2. `cost_i = stake_exec_i * (slippage_bps_i + fee_bps_i) / 10000`
3. `pnl_adj_i = pnl_nominal_i - cost_i`

Aggregat:
`Friction_Adjusted_ROI = sum(pnl_adj_i) / max(sum(stake_exec_i), eps)`

### 5.5 ECE mit Bayesian Smoothing
Buckets `M=10` (default):
1. `ECE = sum_b (|b|/N) * |acc(b)-conf(b)|`

Low-N smoothing:
1. `acc_smooth(b) = (n_b*acc_b + tau*acc_global)/(n_b + tau)`
2. `conf_smooth(b) = (n_b*conf_b + tau*conf_global)/(n_b + tau)`

Defaults:
- `ECE_MIN_BUCKET_N=20`
- `tau=20`

ECE fitness term:
`ECE_score = 1 - clip(ECE/ECE_MAX, 0, 1)` with `ECE_MAX=0.5`

### 5.6 xROI
Match-Level:
`xROI_i = p_model_i * odds_exec_i - 1`

Aggregiert:
`xROI = weighted_mean(xROI_i, weight_i)`

### 5.7 PhaseTransitionDetector
1. `ewma_t = alpha*s_t + (1-alpha)*ewma_{t-1}`
2. `roc_t = (ewma_t - ewma_{t-k}) / max(abs(ewma_{t-k}), eps)`
3. High-vol switch wenn `roc_t >= TH_UP`
4. Back-switch wenn `roc_t <= TH_DOWN`

Defaults:
- `alpha=0.2`, `k=6`, `TH_UP=0.15`, `TH_DOWN=0.05`

### 5.8 Final Fitness 3.2
`Fitness = 0.4*Friction_Adjusted_ROI + 0.3*xROI + 0.2*ECE_score - 0.1*DNA_Complexity`

Skalenregeln:
1. ROI/xROI in `[-1,1]` cappen
2. ECE_score und DNA_Complexity in `[0,1]`

---

## 6. DNA / Arena / Miner

### 6.1 Gene Set
1. `xg_trust_factor`
2. `luck_regression_weight`
3. `ref_cards_sensitivity`
4. `var_buffer_pct`
5. `rotation_penalty_weight`
6. `early_signal_confidence`
7. `expected_roi_weight`
8. `liquidity_priority_weight`

### 6.2 Regime DNA
1. `dna_regime_low_vol`
2. `dna_regime_high_vol`

### 6.3 Adaptive Mutation
`mutation_rate_eff = clip(base_rate * (1 + gamma*(1-league_reliability)), r_min, r_max)`

Defaults:
- `gamma=1.0, r_min=0.02, r_max=0.30`

### 6.4 Epistasis Preservation
1. `I_uv = corr(rank_contrib_u, rank_contrib_v)`
2. starkes Paar bei `|I_uv| >= EPI_THR`
3. pair-preserving crossover mit `p_keep_pair`

Defaults:
- `EPI_THR=0.35`, `p_keep_pair=0.75`

### 6.5 Ensemble Diversity
1. Korrelation auf Bot-PnL-Serien und Decision-Vektoren
2. Cluster-Cap (`DIVERSITY_CAP`, default 30%)
3. Auswahl: robust + diversifiziert (nicht nur Top ROI)

---

## 7. Input Sanity Layer (Punkt 20)

### 7.1 Service
- `backend/app/services/input_sanity_service.py`

### 7.2 Regeln
1. xG bounds:
- `MAX_XG_PER_TEAM=8.0` (default)
- `MAX_XG_TOTAL=12.0` (default)
2. Odds jump bounds:
- `MAX_ODDS_JUMP_PCT=0.60` (default)
3. Event density:
- `MIN_EXPECTED_EVENT_COUNT` policy-gesteuert

### 7.3 Aktionen
1. `BLOCK_AT_INGEST`
2. `QUARANTINE_AND_REVIEW`
3. `ALLOW_WITH_WARNING`

### 7.4 Code-Stub
```python
def assert_xg_sanity(xg_home: float, xg_away: float, max_per_team: float = 8.0) -> None:
    if xg_home > max_per_team or xg_away > max_per_team:
        raise ValueError("ERR_SANITY_XG_OUTLIER")
```

### 7.5 Multi-Source xG-Smoothing
1. Features:
- `xg_sources`
- `xg_consensus`
- `xg_dispersion`
2. robust consensus (median/trimmed mean)
3. `justice_confidence_interval` aus Dispersion
4. hohe Dispersion -> strengeres Justice-Gate

### 7.6 Last-Mile Checklist unter Punkt 20
| Bereich | Maßnahme | Ziel |
|---|---|---|
| Daten | xG-Sanity-Check | Blocke xG>8.0 pro Team und Provider-Ausreißer |
| UX | Change-Log Sichtbarkeit | Expert-User sehen warum Alpha->Omega wechselte |
| Architektur | Circuit Breaker | Miner/Arena pausieren bei Live-Risiko |
| Mathematik | Seed-Invarianz-Test | Top-DNA Stabilität über 10+ Seeds |

---

## 8. Market Inference Service (Punkt 20.2)

### 8.1 Service
- `backend/app/services/market_inference_service.py`

### 8.2 Latenz-Abgleich
1. `p_model` aus DC+Justice
2. `p_market` aus vig-bereinigten Odds
3. `latency_gap = p_market - p_model`

### 8.3 Drift-Alarm
Wenn `abs(latency_gap) > MARKET_DRIFT_ALERT_PCT` und keine harten Datentrigger (xG/Event/Lineup) -> Stage downgrade (`omega/beta -> caution`).

### 8.4 Bayesian Update
`p_post = (1 - w) * p_model + w * p_market`

`w` policy-gesteuert in `[MARKET_BAYES_PRIOR_WEIGHT_MIN, MARKET_BAYES_PRIOR_WEIGHT_MAX]`.

### 8.5 Code-Stub
```python
def bayes_market_update(p_model: float, p_market: float, w: float) -> float:
    return (1.0 - w) * p_model + w * p_market
```

### 8.6 Stale News Detector (Punkt 20.1)
Wenn starke Odds-Bewegung ohne harte Trigger:
1. setze `Caution Mode`
2. dämpfe Stake
3. trace codes:
- `WARN_STALE_NEWS_SUSPECTED`
- `INFO_STALE_NEWS_CLEARED`

---

## 9. Seasonality & Cold Start (Punkt 21)

### 9.1 Burn-in
1. `SEASON_BURNIN_MATCHDAYS` default `4`
2. zusätzlicher Saisonanfangskorridor `SEASON_EARLY_PCT=0.15`

### 9.2 Prior Decay
`prior_weight_t = prior_weight_0 * exp(-k * t)`

### 9.3 Verhalten
1. höheres EntropyDamping in Early Season
2. Transfer-Unsicherheitsmalus
3. Pflichttrace: `decision_trace.seasonality_mode`

---

## 10. Intelligence, Sentiment, Contrarian

### 10.1 Sentiment Hook
1. aggregierte Tippspielverteilung als Feature
2. k-anonymity gate: `SENTIMENT_MIN_K`

### 10.2 Contrarian rule
Nur wenn `justice_alignment_score >= CONTRARIAN_JUSTICE_FLOOR`.

### 10.3 Privacy
Keine user_id in `qbot_logic`/trace.

---

## 11. Philosophical Guardrail (Punkt 27)

### 11.1 Epistemic Humility
Neue Metrik `meta_confidence`.

Wenn Fall singulär (geringe historische Ähnlichkeit) und `meta_confidence < META_CONFIDENCE_FLOOR` -> Yield (no-signal).

Reason:
- `ERR_EPISTEMIC_HUMILITY_YIELD`

### 11.2 Moral Trace
`moral_trace` muss enthalten:
1. claim
2. grounds
3. counterfactual
4. user_value_if_wrong
5. limits_of_knowledge

Fehlt dies -> `ERR_MORAL_TRACE_INCOMPLETE`.

### 11.3 Absurdity Buffer
Extreme statistische Empfehlungen gegen Plausibilität prüfen.

Bei Hyperrealitätsverdacht:
1. `WARN_ABSURDITY_CAUTION` oder
2. `ERR_ABSURDITY_FILTER_BLOCK`.

---

## 12. Resource Orchestration (Punkt 22)

### 12.1 Priority Queues
1. Critical: Live tip path
2. High: reliability/risk
3. Low/Batch: arena/miner/time-machine

### 12.2 Isolation
Empfehlung: Redis/Celery mit dedizierten Worker Pools.

### 12.3 Limits
Policies:
1. `MAX_PARALLEL_ARENA_PER_NODE`
2. `MAX_PARALLEL_MINER_PER_NODE`
3. `MAX_PARALLEL_TIMEMACHINE_PER_NODE`

### 12.4 Circuit Breaker
Batch pausieren bei Verstoß gegen `p95_live_tip_latency_ms`.

---

## 13. Strategy Drift & Retraining (Punkt 23)

### 13.1 Drift Trigger
1. 30-Tage Real ROI vs xROI Divergenz
2. Signifikanztest erforderlich

### 13.2 Aktion
1. Strategie `active -> archived`
2. Auto-Arena-Rerun mit latest `engine_time_machine_justice`

### 13.3 Safeguards
1. `RETRAIN_COOLDOWN_DAYS`
2. max retrain frequency
3. optional `human_ack_required` bei wiederholten Retrains

### 13.4 KPI
1. `strategy_half_life_days`
2. `drift_score`
3. `retrain_trigger_count`

---

## 14. Shadow Promotion

### 14.1 Status
`shadow | active | archived`

### 14.2 Promotion criteria
1. `shadow_tips >= SHADOW_MIN_TIPS`
2. ROI drift <= threshold
3. xROI floor passed
4. no safety anomaly

### 14.3 Rollback
Auto fallback to `shadow` if KPI violation.

---

## 15. Emergency Manual Override (Punkt 24)

### 15.1 Admin control
Ligaweiter no-signal block für X Stunden.

### 15.2 Model
`engine_manual_overrides_v3` fields:
- `league_id`, `until_utc`, `reason`, `created_by`, `created_at_utc`

### 15.3 Reason code
`MANUAL_EMERGENCY_OVERRIDE`

---

## 16. Decision Trace & Reason Codes

Pflichtcodes:
1. `OK_SIGNAL_EMITTED`
2. `ERR_DATA_QUALITY`
3. `ERR_RISK_KILLSWITCH`
4. `ERR_JUSTICE_FLOOR`
5. `WARN_SENTIMENT_UNAVAILABLE`
6. `WARN_MARKET_DRIFT_WITHOUT_DATA_TRIGGER`
7. `WARN_MARKET_INFERENCE_UNAVAILABLE`
8. `WARN_STALE_NEWS_SUSPECTED`
9. `ERR_SANITY_XG_OUTLIER`
10. `ERR_EPISTEMIC_HUMILITY_YIELD`
11. `ERR_MORAL_TRACE_INCOMPLETE`
12. `ERR_ABSURDITY_FILTER_BLOCK`
13. `MANUAL_EMERGENCY_OVERRIDE`

---

## 17. Security & Compliance

1. Decision trace strictly non-PII.
2. k-anonymity for sentiment.
3. CI leak checks against user object ids in tip docs.

---

## 18. CI/CD Guardrails

### 18.1 Static blocks
```bash
rg -n "team_key|home_team_key|away_team_key" backend/app && exit 1
rg -n "updated_at[^_]" backend/app/services backend/app/workers backend/app/routers && exit 1
```

### 18.2 Type/Lint
1. strict python lint/type
2. strict TS typecheck

### 18.3 Schema Assertions
1. `schema_version` required
2. `match_id` int required

---

## 19. Test Matrix

### 19.1 Unit
1. Conservation guard valid/invalid path
2. ECE low-N smoothing
3. entropy damping scaling
4. phase transition triggers
5. market bayes update math
6. xG sanity reject
7. philosophical guardrail yields

### 19.2 Integration
1. End-to-end closed loop
2. live/backtest gate parity
3. stage downgrade on market drift without hard triggers
4. override gate forces no-signal

### 19.3 Contract
1. API schema fields complete
2. no legacy keys
3. match_id int everywhere

### 19.4 Performance/SRE
1. queue isolation under load
2. batch circuit breaker activation
3. p95 live latency preserved

### 19.5 Chaos/Resilience
1. missing lineups -> caution/damping, no crash
2. market feed unavailable -> warning path
3. policy reload version pinning consistency

---

## 20. Rollout Plan (A->J)
1. A: Contracts + schema + indexes
2. B: Policy service + audit
3. C: Shared gate layer
4. D: Core services refactor
5. E: Arena/miner fitness 3.2
6. F: Time-machine + dirty-data
7. G: worker/router integration
8. H: ingest sync harmonization
9. I: frontend/i18n updates
10. J: hardening + shadow rollout + promotion

---

## 21. Defaults (Frozen)
1. `eps=1e-9`
2. `EPS_PROB_SUM=1e-6`
3. `ECE_MIN_BUCKET_N=20`
4. `tau=20`
5. `MARKET_DRIFT_ALERT_PCT=0.12`
6. `SEASON_EARLY_PCT=0.15`
7. `YIELD_ON_EPISTEMIC_GAP=true`
8. `MORAL_TRACE_REQUIRED=true`

---

## 22. Definition of Done
1. Neuneck v3-only, no legacy keys.
2. `match_id:int` contract fully enforced.
3. `updated_at_utc` enforced in core paths.
4. shared gate parity between live and backtest.
5. complete decision trace with privacy-safe reason codes.
6. policy changes fully audited.
7. diversified robust ensemble selected by stability + anti-correlation.
8. all required test suites pass.
