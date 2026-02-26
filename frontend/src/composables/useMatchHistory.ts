/**
 * frontend/src/composables/useMatchHistory.ts
 *
 * Purpose:
 * Loads and caches historical match context (H2H + form) for fixture cards.
 * Uses Sportmonks sm_id (number) for team identity.
 */

import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface H2HSummary {
  total: number;
  home_wins: number;
  away_wins: number;
  draws: number;
  avg_goals: number;
  over_2_5_pct: number;
  btts_pct: number;
  avg_home_xg?: number;
  avg_away_xg?: number;
  avg_xg_diff?: number;
  xg_samples_used?: number;
  xg_samples_total?: number;
}

export interface HistoricalMatch {
  match_date: string;
  home_team: string;
  away_team: string;
  home_team_id: number;
  away_team_id: number;
  finish_type?: string | null;
  result: {
    home_score: number;
    away_score: number;
    outcome: string;
    home_xg?: number;
    away_xg?: number;
  };
}

export interface MatchContext {
  h2h: {
    summary: H2HSummary;
    matches: HistoricalMatch[];
  } | null;
  home_form: HistoricalMatch[];
  away_form: HistoricalMatch[];
  home_team_id: number;
  away_team_id: number;
}

function cacheKey(homeSMId: number, awaySMId: number): string {
  const lo = Math.min(homeSMId, awaySMId);
  const hi = Math.max(homeSMId, awaySMId);
  return `v3|${lo}|${hi}`;
}

// In-memory cache keyed symmetrically — shared across all composable instances
const cache = new Map<string, MatchContext>();

/**
 * Prefetch match context for multiple fixtures in a single HTTP request.
 * Populates the shared cache so individual useMatchHistory().fetch() calls
 * return instantly from cache without any network requests.
 */
export async function prefetchMatchHistory(
  fixtures: { home_sm_id: number; away_sm_id: number }[],
): Promise<void> {
  const uncached = fixtures.filter(
    (f) => !cache.has(cacheKey(f.home_sm_id, f.away_sm_id)),
  );
  if (uncached.length === 0) return;

  const api = useApi();
  try {
    const resp = await api.post<{ results: (MatchContext & { home_sm_id: number; away_sm_id: number })[] }>(
      "/historical/match-context-bulk",
      { fixtures: uncached },
    );
    for (const r of resp.results) {
      const key = cacheKey(r.home_sm_id, r.away_sm_id);
      cache.set(key, {
        h2h: r.h2h,
        home_form: r.home_form,
        away_form: r.away_form,
        home_team_id: r.home_team_id,
        away_team_id: r.away_team_id,
      });
    }
  } catch {
    // Bulk prefetch failed — individual fetches will try as fallback
  }
}

export function useMatchHistory() {
  const api = useApi();
  const data: Ref<MatchContext | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch(homeSMId: number, awaySMId: number) {
    const key = cacheKey(homeSMId, awaySMId);

    const cached = cache.get(key);
    if (cached) {
      data.value = cached;
      return;
    }

    loading.value = true;
    error.value = false;

    try {
      const result = await api.get<MatchContext>("/historical/match-context", {
        home_sm_id: String(homeSMId),
        away_sm_id: String(awaySMId),
      });
      data.value = result;
      cache.set(key, result);
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
