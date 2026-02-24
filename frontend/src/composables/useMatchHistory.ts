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
}

export interface HistoricalMatch {
  match_date: string;
  home_team: string;
  away_team: string;
  home_team_key: string;
  away_team_key: string;
  result: {
    home_score: number;
    away_score: number;
    outcome: string;
    home_xg?: number;
    away_xg?: number;
  };
  season_label: string;
}

export interface MatchContext {
  h2h: {
    summary: H2HSummary;
    matches: HistoricalMatch[];
  } | null;
  home_form: HistoricalMatch[];
  away_form: HistoricalMatch[];
  home_team_key: string;
  away_team_key: string;
}

interface BulkFixture {
  home_team: string;
  away_team: string;
  sport_key: string;
}

interface BulkResult extends MatchContext {
  home_team: string;
  away_team: string;
  sport_key: string;
}

function cacheKey(home: string, away: string, sport: string): string {
  return `${home}|${away}|${sport}`;
}

// In-memory cache keyed by "home|away|sport" — shared across all composable instances
const cache = new Map<string, MatchContext>();

/**
 * Prefetch match context for multiple fixtures in a single HTTP request.
 * Populates the shared cache so individual useMatchHistory().fetch() calls
 * return instantly from cache without any network requests.
 */
export async function prefetchMatchHistory(
  fixtures: BulkFixture[],
): Promise<void> {
  // Filter out fixtures already cached
  const uncached = fixtures.filter(
    (f) => !cache.has(cacheKey(f.home_team, f.away_team, f.sport_key)),
  );
  if (uncached.length === 0) return;

  const api = useApi();
  try {
    const resp = await api.post<{ results: BulkResult[] }>(
      "/historical/match-context-bulk",
      { fixtures: uncached },
    );
    for (const r of resp.results) {
      const key = cacheKey(r.home_team, r.away_team, r.sport_key);
      cache.set(key, {
        h2h: r.h2h,
        home_form: r.home_form,
        away_form: r.away_form,
        home_team_key: r.home_team_key,
        away_team_key: r.away_team_key,
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

  async function fetch(homeTeam: string, awayTeam: string, sportKey: string) {
    const key = cacheKey(homeTeam, awayTeam, sportKey);

    const cached = cache.get(key);
    if (cached) {
      data.value = cached;
      return;
    }

    loading.value = true;
    error.value = false;

    try {
      const result = await api.get<MatchContext>("/historical/match-context", {
        home_team: homeTeam,
        away_team: awayTeam,
        sport_key: sportKey,
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
