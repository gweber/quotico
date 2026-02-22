import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface SurvivorPick {
  matchday_number: number;
  team: string;
  match_id: string;
  result: string; // pending | won | lost | draw
}

export interface SurvivorEntry {
  id: string;
  status: string; // alive | eliminated | not_started
  picks: SurvivorPick[];
  used_teams: string[];
  streak: number;
  eliminated_at: string | null;
}

export interface SurvivorStanding {
  user_id: string;
  alias: string;
  status: string;
  streak: number;
  eliminated_at: string | null;
}

export const useSurvivorStore = defineStore("survivor", () => {
  const api = useApi();

  const entry = ref<SurvivorEntry | null>(null);
  const standings = ref<SurvivorStanding[]>([]);
  const loading = ref(false);

  async function fetchStatus(squadId: string, sport: string, season?: number) {
    loading.value = true;
    try {
      const params: Record<string, string> = { sport };
      if (season) params.season = String(season);
      entry.value = await api.get<SurvivorEntry>(`/survivor/${squadId}/status`, params);
    } catch {
      entry.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function makePick(squadId: string, matchId: string, team: string) {
    const result = await api.post<SurvivorEntry>(`/survivor/${squadId}/pick`, {
      match_id: matchId,
      team,
    });
    entry.value = result;
    return result;
  }

  async function fetchStandings(squadId: string, sport: string, season?: number) {
    try {
      const params: Record<string, string> = { sport };
      if (season) params.season = String(season);
      standings.value = await api.get<SurvivorStanding[]>(
        `/survivor/${squadId}/standings`,
        params,
      );
    } catch {
      standings.value = [];
    }
  }

  function reset() {
    entry.value = null;
    standings.value = [];
  }

  return {
    entry,
    standings,
    loading,
    fetchStatus,
    makePick,
    fetchStandings,
    reset,
  };
});
