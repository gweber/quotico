import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface FantasyPick {
  id: string;
  team: string;
  match_id: string;
  matchday_number: number;
  goals_scored: number | null;
  goals_conceded: number | null;
  match_result: string | null;
  fantasy_points: number | null;
  status: string;
}

export interface FantasyStanding {
  user_id: string;
  alias: string;
  total_points: number;
  matchdays_played: number;
  avg_points: number;
}

export const useFantasyStore = defineStore("fantasy", () => {
  const api = useApi();

  const pick = ref<FantasyPick | null>(null);
  const standings = ref<FantasyStanding[]>([]);
  const loading = ref(false);

  async function fetchPick(
    squadId: string,
    sport: string,
    season: number,
    matchdayNumber: number,
  ) {
    loading.value = true;
    try {
      pick.value = await api.get<FantasyPick | null>(`/fantasy/${squadId}/pick`, {
        sport,
        season: String(season),
        matchday_number: String(matchdayNumber),
      });
    } catch {
      pick.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function makePick(
    squadId: string,
    matchId: string,
    team: string,
  ): Promise<FantasyPick> {
    const result = await api.post<FantasyPick>(`/fantasy/${squadId}/pick`, {
      match_id: matchId,
      team,
    });
    pick.value = result;
    return result;
  }

  async function fetchStandings(squadId: string, sport: string, season?: number) {
    try {
      const params: Record<string, string> = { sport };
      if (season) params.season = String(season);
      standings.value = await api.get<FantasyStanding[]>(
        `/fantasy/${squadId}/standings`,
        params,
      );
    } catch {
      standings.value = [];
    }
  }

  function reset() {
    pick.value = null;
    standings.value = [];
  }

  return {
    pick,
    standings,
    loading,
    fetchPick,
    makePick,
    fetchStandings,
    reset,
  };
});
