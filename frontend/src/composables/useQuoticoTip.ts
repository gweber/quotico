import { ref, reactive, type Ref } from "vue";
import { useApi } from "./useApi";

export interface TierSignals {
  poisson: {
    lambda_home: number;
    lambda_away: number;
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
    kings_who_tipped: number;
    is_underdog_pick: boolean;
  };
  btb?: {
    home: { evd: number; matches_analyzed: number; btb_count: number; btb_ratio: number; contributes: boolean };
    away: { evd: number; matches_analyzed: number; btb_count: number; btb_ratio: number; contributes: boolean };
  };
}

export interface QuoticoTip {
  match_id: string;
  sport_key: string;
  teams: Record<string, string>;
  commence_time: string;
  recommended_selection: string;
  confidence: number;
  edge_pct: number;
  true_probability: number;
  implied_probability: number;
  expected_goals_home: number;
  expected_goals_away: number;
  tier_signals: TierSignals;
  justification: string;
  skip_reason: string | null;
  generated_at: string;
}

// Shared reactive cache keyed by match_id (reactive so computed() tracks changes)
const tipCache = reactive(new Map<string, QuoticoTip>());

/**
 * Prefetch all active QuoticoTips in a single request.
 * Populates the shared cache so useQuoticoTip().fetch() returns instantly.
 */
export async function prefetchQuoticoTips(sportKey?: string): Promise<void> {
  const api = useApi();
  try {
    const params: Record<string, string> = { limit: "50", include_no_signal: "true" };
    if (sportKey) params.sport_key = sportKey;

    const tips = await api.get<QuoticoTip[]>("/quotico-tips/", params);
    for (const tip of tips) {
      tipCache.set(tip.match_id, tip);
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
      if (tip.sport_key === sportKey) tipCache.delete(key);
    }
  } else {
    tipCache.clear();
  }
  await prefetchQuoticoTips(sportKey);
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
  return tipCache.get(matchId);
}

export function useQuoticoTip() {
  const api = useApi();
  const data: Ref<QuoticoTip | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch(matchId: string) {
    // Check cache first
    const cached = tipCache.get(matchId);
    if (cached) {
      data.value = cached;
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
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
