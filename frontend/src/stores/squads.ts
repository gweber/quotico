import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import type { GameModeType, LeagueConfig } from "@/types/league";

export interface Squad {
  id: string;
  name: string;
  description?: string | null;
  invite_code: string;
  admin_id: string;
  member_count: number;
  is_admin: boolean;
  league_configs: LeagueConfig[];
  // Legacy (deprecated, kept for backward compat)
  game_mode: string;
  game_mode_config: Record<string, unknown>;
  created_at: string;
}

export interface SquadLeaderboardEntry {
  rank: number;
  user_id: string;
  alias: string;
  points: number;
  tip_count: number;
  avg_odds: number;
}

export const useSquadsStore = defineStore("squads", () => {
  const api = useApi();
  const toast = useToast();
  const squads = ref<Squad[]>([]);
  const currentSquad = ref<Squad | null>(null);
  const leaderboard = ref<SquadLeaderboardEntry[]>([]);
  const loading = ref(false);

  async function fetchMySquads() {
    loading.value = true;
    try {
      squads.value = await api.get<Squad[]>("/squads/mine");
    } catch {
      squads.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function createSquad(name: string, description?: string) {
    const squad = await api.post<Squad>("/squads/", { name, description });
    squads.value.push(squad);
    toast.success(`Squad "${squad.name}" erstellt!`);
    return squad;
  }

  async function joinSquad(inviteCode: string) {
    const squad = await api.post<Squad>("/squads/join", {
      invite_code: inviteCode,
    });
    squads.value.push(squad);
    toast.success(`Squad "${squad.name}" beigetreten!`);
    return squad;
  }

  async function fetchLeaderboard(squadId: string) {
    leaderboard.value = await api.get<SquadLeaderboardEntry[]>(
      `/squads/${squadId}/leaderboard`
    );
  }

  async function leaveSquad(squadId: string) {
    await api.post(`/squads/${squadId}/leave`);
    squads.value = squads.value.filter((s) => s.id !== squadId);
    toast.success("Squad verlassen.");
  }

  async function kickMember(squadId: string, memberId: string) {
    await api.del(`/squads/${squadId}/members/${memberId}`);
    toast.success("Mitglied entfernt.");
  }

  function setCurrentSquad(squad: Squad | null) {
    currentSquad.value = squad;
  }

  function getGameModeForSport(
    squadId: string,
    sportKey: string
  ): GameModeType {
    const squad = squads.value.find((s) => s.id === squadId);
    if (!squad) return "classic";

    // New system: league_configs has priority
    if (squad.league_configs && squad.league_configs.length > 0) {
      const config = squad.league_configs.find(
        (lc) => lc.sport_key === sportKey && !lc.deactivated_at
      );
      if (config) return config.game_mode;
      return "classic";
    }

    // Legacy fallback
    return (squad.game_mode as GameModeType) || "classic";
  }

  function getActiveLeagueConfigs(squadId: string): LeagueConfig[] {
    const squad = squads.value.find((s) => s.id === squadId);
    if (!squad?.league_configs) return [];
    return squad.league_configs.filter((lc) => !lc.deactivated_at);
  }

  async function setLeagueConfig(
    squadId: string,
    sportKey: string,
    gameMode: GameModeType,
    config?: Record<string, unknown>
  ) {
    await api.put(`/squads/${squadId}/league-config`, {
      sport_key: sportKey,
      game_mode: gameMode,
      config: config ?? {},
    });
    await fetchMySquads();
  }

  async function removeLeagueConfig(squadId: string, sportKey: string) {
    await api.del(`/squads/${squadId}/league-config/${sportKey}`);
    await fetchMySquads();
  }

  return {
    squads,
    currentSquad,
    leaderboard,
    loading,
    fetchMySquads,
    createSquad,
    joinSquad,
    fetchLeaderboard,
    leaveSquad,
    kickMember,
    setCurrentSquad,
    getGameModeForSport,
    getActiveLeagueConfigs,
    setLeagueConfig,
    removeLeagueConfig,
  };
});
