import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

export interface Squad {
  id: string;
  name: string;
  description?: string | null;
  invite_code: string;
  admin_id: string;
  member_count: number;
  is_admin: boolean;
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
  };
});
