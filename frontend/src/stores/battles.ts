import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

export interface BattleSquad {
  id: string;
  name: string;
  member_count?: number;
  avg_points?: number;
  total_points?: number;
  committed_count?: number;
}

export interface SquadSearchResult {
  id: string;
  name: string;
  member_count: number;
}

export interface Battle {
  id: string;
  squad_a: BattleSquad;
  squad_b: BattleSquad | null;
  challenge_type?: string; // "open" | "direct" | "classic"
  start_time: string;
  end_time: string;
  status: string;
  my_commitment?: string | null;
  needs_commitment?: boolean;
  can_accept?: boolean;
  result?: {
    winner?: string;
    squad_a_avg?: number;
    squad_b_avg?: number;
  } | null;
}

export const useBattlesStore = defineStore("battles", () => {
  const api = useApi();
  const toast = useToast();
  const battles = ref<Battle[]>([]);
  const lobby = ref<Battle[]>([]);
  const currentBattle = ref<Battle | null>(null);
  const loading = ref(false);
  const lobbyLoading = ref(false);

  async function fetchMyBattles() {
    loading.value = true;
    try {
      battles.value = await api.get<Battle[]>("/battles/mine/active");
    } catch {
      battles.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function fetchLobby() {
    lobbyLoading.value = true;
    try {
      lobby.value = await api.get<Battle[]>("/battles/lobby");
    } catch {
      lobby.value = [];
    } finally {
      lobbyLoading.value = false;
    }
  }

  async function fetchBattle(battleId: string) {
    currentBattle.value = await api.get<Battle>(`/battles/${battleId}`);
  }

  async function commitToBattle(battleId: string, squadId: string) {
    await api.post(`/battles/${battleId}/commit`, { squad_id: squadId });
    toast.success("Commitment bestätigt!");
    await fetchBattle(battleId);
    await fetchMyBattles();
  }

  async function createChallenge(
    squadId: string,
    startTime: string,
    endTime: string,
    targetSquadId?: string | null,
  ): Promise<boolean> {
    try {
      await api.post("/battles/challenge", {
        squad_id: squadId,
        start_time: startTime,
        end_time: endTime,
        target_squad_id: targetSquadId || undefined,
      });
      toast.success("Herausforderung erstellt!");
      await fetchMyBattles();
      return true;
    } catch {
      return false;
    }
  }

  async function acceptChallenge(battleId: string, squadId: string): Promise<boolean> {
    try {
      await api.post(`/battles/${battleId}/accept`, { squad_id: squadId });
      toast.success("Herausforderung angenommen!");
      await Promise.all([fetchMyBattles(), fetchLobby()]);
      return true;
    } catch {
      return false;
    }
  }

  async function declineChallenge(battleId: string): Promise<boolean> {
    try {
      await api.post(`/battles/${battleId}/decline`);
      toast.success("Herausforderung abgelehnt.");
      await fetchLobby();
      return true;
    } catch {
      return false;
    }
  }

  async function searchSquads(query: string): Promise<SquadSearchResult[]> {
    if (!query || query.length < 1) return [];
    try {
      return await api.get<SquadSearchResult[]>("/battles/squads/search", { q: query });
    } catch {
      return [];
    }
  }

  return {
    battles,
    lobby,
    currentBattle,
    loading,
    lobbyLoading,
    fetchMyBattles,
    fetchLobby,
    fetchBattle,
    commitToBattle,
    createChallenge,
    acceptChallenge,
    declineChallenge,
    searchSquads,
  };
});
