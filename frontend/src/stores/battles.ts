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

export interface Battle {
  id: string;
  squad_a: BattleSquad;
  squad_b: BattleSquad;
  start_time: string;
  end_time: string;
  status: string;
  my_commitment?: string | null;
  needs_commitment?: boolean;
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
  const currentBattle = ref<Battle | null>(null);
  const loading = ref(false);

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

  async function fetchBattle(battleId: string) {
    currentBattle.value = await api.get<Battle>(`/battles/${battleId}`);
  }

  async function commitToBattle(battleId: string, squadId: string) {
    await api.post(`/battles/${battleId}/commit`, { squad_id: squadId });
    toast.success("Commitment bestätigt!");
    // Refresh battle details
    await fetchBattle(battleId);
    await fetchMyBattles();
  }

  return {
    battles,
    currentBattle,
    loading,
    fetchMyBattles,
    fetchBattle,
    commitToBattle,
  };
});
