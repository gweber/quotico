/**
 * frontend/src/composables/useQuoticoTip.ts
 *
 * Purpose:
 *     Typed API/composable layer for QuoticoTip fetch, cache, and refresh flows.
 */
import { ref, reactive, type Ref } from "vue";
import { useApi } from "./useApi";

export interface TierSignals {
  poisson: {
    lambda_home: number;
    lambda_away: number;
    h2h_weight: number;
    true_probs: Record<string, number>;
    edges: Record<string, number>;
  } | null;
  momentum: {
    home: { momentum_score: number; form_points: number; weighted_form: number };
    away: { momentum_score: number; form_points: number; weighted_form: number };
    gap: number;
    contributes: boolean;
  };
  sharp_movement: {
    has_sharp_movement: boolean;
    direction: string | null;
    max_drop_pct: number;
    is_late_money: boolean;
  };
  kings_choice: {
    has_kings_choice: boolean;
    kings_pick: string | null;
    kings_pct: number;
    total_kings: number;
    kings_who_bet: number;
    is_underdog_pick: boolean;
  };
  btb?: {
    home: { evd: number; matches_analyzed: number; btb_count: number; btb_ratio: number; contributes: boolean };
    away: { evd: number; matches_analyzed: number; btb_count: number; btb_ratio: number; contributes: boolean };
  };
  rest_advantage?: {
    home_rest_days: number;
    away_rest_days: number;
    diff: number;
    contributes: boolean;
  };
  xg_performance?: {
    home: { avg_goals: number; avg_xg: number | null; delta: number | null; matches_with_xg: number; matches_total: number; label: string };
    away: { avg_goals: number; avg_xg: number | null; delta: number | null; matches_with_xg: number; matches_total: number; label: string };
  } | null;
}

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

export interface QbotLogic {
  strategy_version: string;
  // Investor mode fields (absent for no_signal tips)
  archetype?: string;
  reasoning_key?: string;
  reasoning_params?: Record<string, string | number>;
  stake_units?: number;
  kelly_raw?: number;
  bayesian_confidence?: number;
  market_synergy_factor?: number;
  market_trust_factor?: number;
  market_context?: {
    volatility_dim?: string;
  };
  cluster_key?: string;
  cluster_sample_size?: number;
  is_midweek?: boolean;
  is_weekend?: boolean;
  post_match_reasoning?: {
    type?: string;
    red_cards?: number;
    xg_home?: number;
    xg_away?: number;
    efficiency_home?: number;
    efficiency_away?: number;
    efficient_team?: "home" | "away";
    goals_home?: number;
    goals_away?: number;
    xg_delta?: number;
    expected_winner?: "home" | "away" | "draw";
    actual_outcome?: "home" | "away" | "draw";
    [key: string]: unknown;
  };
  // Player mode (always present when Poisson data available)
  player?: PlayerPrediction;
  applied_at: string;
}

export interface QuoticoTip {
  match_id: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string;
  recommended_selection: string;
  confidence: number;
  raw_confidence?: number;
  edge_pct: number;
  true_probability: number;
  implied_probability: number;
  expected_goals_home: number;
  expected_goals_away: number;
  tier_signals: TierSignals;
  justification: string;
  skip_reason: string | null;
  generated_at: string;
  // Present on resolved tips (admin refresh)
  actual_result?: string;
  was_correct?: boolean;
  status?: string;
  // Qbot Intelligence enrichment (present when active strategy exists)
  qbot_logic?: QbotLogic;
}

// Shared reactive cache keyed by match_id (reactive so computed() tracks changes)
// A null value means "we checked and no tip exists" (negative cache).
const tipCache = reactive(new Map<string, QuoticoTip | null>());

/**
 * Prefetch QuoticoTips for the given match IDs (or all active tips if no IDs given).
 * Populates the shared cache so useQuoticoTip().fetch() returns instantly.
 */
export async function prefetchQuoticoTips(
  matchIds?: string[],
  sportKey?: string,
): Promise<void> {
  const api = useApi();
  try {
    const params: Record<string, string> = { include_no_signal: "true" };
    if (matchIds && matchIds.length > 0) {
      params.match_ids = matchIds.join(",");
      params.limit = String(matchIds.length);
    } else {
      params.limit = "100";
    }
    if (sportKey) params.sport_key = sportKey;

    const tips = await api.get<QuoticoTip[]>("/quotico-tips/", params);
    const returned = new Set(tips.map((t) => t.match_id));
    for (const tip of tips) {
      tipCache.set(tip.match_id, tip);
    }
    // Negative cache: mark requested IDs that had no tip
    if (matchIds) {
      for (const id of matchIds) {
        if (!returned.has(id)) tipCache.set(id, null);
      }
    }
  } catch {
    // Prefetch failed — individual fetches will try as fallback
  }
}

/**
 * Clear cache and re-fetch tips from the API.
 */
export async function refreshQuoticoTips(sportKey?: string): Promise<void> {
  if (sportKey) {
    for (const [key, tip] of tipCache) {
      if (tip?.sport_key === sportKey) tipCache.delete(key);
    }
  } else {
    tipCache.clear();
  }
  await prefetchQuoticoTips(undefined, sportKey);
}

/**
 * Populate the cache from embedded data (e.g. matchday detail response).
 * Avoids a separate network request when tips are included in the page payload.
 */
export function populateTipCache(tips: QuoticoTip[]): void {
  for (const tip of tips) {
    tipCache.set(tip.match_id, tip);
  }
}

/**
 * Get a cached QuoticoTip by match_id (no network call).
 * Returns undefined if no tip is cached for this match.
 */
export function getCachedTip(matchId: string): QuoticoTip | undefined {
  return tipCache.get(matchId) ?? undefined;
}

/**
 * Admin-only: recalculate a single Q-Tip and return full metrics.
 * Updates the shared cache with the fresh tip.
 */
export async function refreshSingleTip(matchId: string): Promise<QuoticoTip> {
  const api = useApi();
  const tip = await api.post<QuoticoTip>(`/quotico-tips/${matchId}/refresh`);
  tipCache.set(matchId, tip);
  return tip;
}

export function useQuoticoTip() {
  const api = useApi();
  const data: Ref<QuoticoTip | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch(matchId: string) {
    // Check cache first (null = known absent, undefined = unknown)
    if (tipCache.has(matchId)) {
      data.value = tipCache.get(matchId) ?? null;
      return;
    }

    loading.value = true;
    error.value = false;

    try {
      const result = await api.get<QuoticoTip>(`/quotico-tips/${matchId}`);
      data.value = result;
      tipCache.set(matchId, result);
    } catch {
      error.value = true;
      data.value = null;
      tipCache.set(matchId, null); // Negative cache on 404
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
