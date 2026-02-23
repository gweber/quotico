import { ref, type Ref } from "vue";
import { useApi } from "./useApi";
import type { HistoricalMatch } from "./useMatchHistory";

export interface SeasonStats {
  season_label: string;
  matches_played: number;
  wins: number;
  draws: number;
  losses: number;
  goals_scored: number;
  goals_conceded: number;
  goal_difference: number;
  points: number;
  home_record: { w: number; d: number; l: number };
  away_record: { w: number; d: number; l: number };
}

export interface UpcomingMatch {
  id: string;
  sport_key: string;
  teams: { home: string; away: string };
  commence_time: string;
  current_odds: Record<string, number>;
  status: string;
}

export interface TeamProfile {
  team_key: string;
  display_name: string;
  sport_keys: string[];
  form: string[];
  recent_results: HistoricalMatch[];
  season_stats: SeasonStats | null;
  upcoming_matches: UpcomingMatch[];
}

/**
 * Generate a URL-safe team slug from a display name.
 * Mirrors the backend resolution: lowercase, strip accents, hyphenate.
 */
export function teamSlug(displayName: string): string {
  return displayName
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

export function useTeam() {
  const api = useApi();
  const data: Ref<TeamProfile | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch(slug: string, sportKey?: string) {
    loading.value = true;
    error.value = false;

    try {
      const params: Record<string, string> = {};
      if (sportKey) params.sport_key = sportKey;
      const result = await api.get<TeamProfile>(`/teams/${slug}`, params);
      data.value = result;
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
