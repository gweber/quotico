import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import type { GameModeType, LeagueConfig } from "@/types/league";

export interface Squad {
  id: string;
  name: string;
  description?: string | null;
  invite_code: string | null;
  admin_id: string;
  member_count: number;
  is_admin: boolean;
  league_configs: LeagueConfig[];
  auto_tipp_blocked: boolean;
  lock_minutes: number;
  is_public: boolean;
  is_open: boolean;
  invite_visible: boolean;
  pending_requests: number;
  // Legacy (deprecated, kept for backward compat)
  game_mode: string;
  game_mode_config: Record<string, unknown>;
  created_at: string;
}

export interface PublicSquad {
  id: string;
  name: string;
  description?: string | null;
  member_count: number;
  is_open: boolean;
}

export interface JoinRequest {
  id: string;
  squad_id: string;
  user_id: string;
  alias: string;
  status: string;
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
  const publicSquads = ref<PublicSquad[]>([]);
  const currentSquad = ref<Squad | null>(null);
  const leaderboard = ref<SquadLeaderboardEntry[]>([]);
  const joinRequests = ref<JoinRequest[]>([]);
  const loading = ref(false);
  const publicLoading = ref(false);

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

  async function fetchPublicSquads(q = "") {
    publicLoading.value = true;
    try {
      const params = q ? `?q=${encodeURIComponent(q)}` : "";
      publicSquads.value = await api.get<PublicSquad[]>(`/squads/public${params}`);
    } catch {
      publicSquads.value = [];
    } finally {
      publicLoading.value = false;
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

  async function deleteSquad(squadId: string) {
    await api.del(`/squads/${squadId}`);
    squads.value = squads.value.filter((s) => s.id !== squadId);
    toast.success("Squad gelöscht.");
  }

  async function updateSquad(squadId: string, description: string | null) {
    const updated = await api.patch<Squad>(`/squads/${squadId}`, {
      description,
    });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx] = updated;
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

  async function toggleAutoTipp(squadId: string, blocked: boolean) {
    await api.patch(`/squads/${squadId}/auto-tipp`, { blocked });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx].auto_tipp_blocked = blocked;
  }

  async function setLockMinutes(squadId: string, minutes: number) {
    await api.patch(`/squads/${squadId}/lock-minutes`, { minutes });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx].lock_minutes = minutes;
  }

  async function setVisibility(squadId: string, isPublic: boolean) {
    await api.patch(`/squads/${squadId}/visibility`, { is_public: isPublic });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx].is_public = isPublic;
  }

  async function setInviteVisible(squadId: string, visible: boolean) {
    await api.patch(`/squads/${squadId}/invite-visible`, { visible });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx].invite_visible = visible;
    // Refresh to get the invite_code if it was just revealed
    if (visible) await fetchMySquads();
  }

  async function setOpen(squadId: string, isOpen: boolean) {
    await api.patch(`/squads/${squadId}/open`, { is_open: isOpen });
    const idx = squads.value.findIndex((s) => s.id === squadId);
    if (idx !== -1) squads.value[idx].is_open = isOpen;
  }

  async function requestJoin(squadId: string) {
    await api.post(`/squads/${squadId}/request-join`);
    toast.success("Beitrittsanfrage gesendet!");
  }

  async function fetchJoinRequests(squadId: string) {
    joinRequests.value = await api.get<JoinRequest[]>(
      `/squads/${squadId}/join-requests`
    );
  }

  async function approveJoinRequest(squadId: string, requestId: string) {
    await api.post(`/squads/${squadId}/join-requests/${requestId}/approve`);
    joinRequests.value = joinRequests.value.filter((r) => r.id !== requestId);
    // Refresh squad to update member count + pending_requests
    await fetchMySquads();
  }

  async function declineJoinRequest(squadId: string, requestId: string) {
    await api.post(`/squads/${squadId}/join-requests/${requestId}/decline`);
    joinRequests.value = joinRequests.value.filter((r) => r.id !== requestId);
    await fetchMySquads();
  }

  return {
    squads,
    publicSquads,
    currentSquad,
    leaderboard,
    joinRequests,
    loading,
    publicLoading,
    fetchMySquads,
    fetchPublicSquads,
    createSquad,
    joinSquad,
    fetchLeaderboard,
    leaveSquad,
    kickMember,
    deleteSquad,
    updateSquad,
    setCurrentSquad,
    getGameModeForSport,
    getActiveLeagueConfigs,
    setLeagueConfig,
    removeLeagueConfig,
    toggleAutoTipp,
    setLockMinutes,
    setVisibility,
    setInviteVisible,
    setOpen,
    requestJoin,
    fetchJoinRequests,
    approveJoinRequest,
    declineJoinRequest,
  };
});
