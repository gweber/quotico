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
  auto_bet_blocked: boolean;
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
    try {
      leaderboard.value = await api.get<SquadLeaderboardEntry[]>(
        `/squads/${squadId}/leaderboard`
      );
    } catch {
      leaderboard.value = [];
    }
  }

  async function leaveSquad(squadId: string) {
    try {
      await api.post(`/squads/${squadId}/leave`);
      squads.value = squads.value.filter((s) => s.id !== squadId);
      toast.success("Squad verlassen.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Verlassen.");
      throw e;
    }
  }

  async function kickMember(squadId: string, memberId: string) {
    try {
      await api.del(`/squads/${squadId}/members/${memberId}`);
      toast.success("Mitglied entfernt.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Entfernen.");
      throw e;
    }
  }

  async function deleteSquad(squadId: string) {
    try {
      await api.del(`/squads/${squadId}`);
      squads.value = squads.value.filter((s) => s.id !== squadId);
      toast.success("Squad gelöscht.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Löschen.");
      throw e;
    }
  }

  async function updateSquad(squadId: string, description: string | null) {
    try {
      const updated = await api.patch<Squad>(`/squads/${squadId}`, {
        description,
      });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx] = updated;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
      throw e;
    }
  }

  function setCurrentSquad(squad: Squad | null) {
    currentSquad.value = squad;
  }

  function getGameModeForSport(
    squadId: string,
    leagueId: number
  ): GameModeType {
    const squad = squads.value.find((s) => s.id === squadId);
    if (!squad) return "classic";

    // New system: league_configs has priority
    if (squad.league_configs && squad.league_configs.length > 0) {
      const config = squad.league_configs.find(
        (lc) => lc.league_id === leagueId && !lc.deactivated_at
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
    leagueId: number,
    gameMode: GameModeType,
    config?: Record<string, unknown>
  ) {
    try {
      await api.put(`/squads/${squadId}/league-config`, {
        league_id: leagueId,
        game_mode: gameMode,
        config: config ?? {},
      });
      await fetchMySquads();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
      throw e;
    }
  }

  async function removeLeagueConfig(squadId: string, leagueId: number) {
    try {
      await api.del(`/squads/${squadId}/league-config/${leagueId}`);
      await fetchMySquads();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Entfernen.");
      throw e;
    }
  }

  async function toggleAutoBet(squadId: string, blocked: boolean) {
    try {
      await api.patch(`/squads/${squadId}/auto-bet`, { blocked });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx].auto_bet_blocked = blocked;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
    }
  }

  async function setLockMinutes(squadId: string, minutes: number) {
    try {
      await api.patch(`/squads/${squadId}/lock-minutes`, { minutes });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx].lock_minutes = minutes;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
    }
  }

  async function setVisibility(squadId: string, isPublic: boolean) {
    try {
      await api.patch(`/squads/${squadId}/visibility`, { is_public: isPublic });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx].is_public = isPublic;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
    }
  }

  async function setInviteVisible(squadId: string, visible: boolean) {
    try {
      await api.patch(`/squads/${squadId}/invite-visible`, { visible });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx].invite_visible = visible;
      // Refresh to get the invite_code if it was just revealed
      if (visible) await fetchMySquads();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
    }
  }

  async function setOpen(squadId: string, isOpen: boolean) {
    try {
      await api.patch(`/squads/${squadId}/open`, { is_open: isOpen });
      const idx = squads.value.findIndex((s) => s.id === squadId);
      if (idx !== -1) squads.value[idx].is_open = isOpen;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Speichern.");
    }
  }

  async function requestJoin(squadId: string) {
    try {
      await api.post(`/squads/${squadId}/request-join`);
      toast.success("Beitrittsanfrage gesendet!");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Senden.");
      throw e;
    }
  }

  async function fetchJoinRequests(squadId: string) {
    try {
      joinRequests.value = await api.get<JoinRequest[]>(
        `/squads/${squadId}/join-requests`
      );
    } catch {
      joinRequests.value = [];
    }
  }

  async function approveJoinRequest(squadId: string, requestId: string) {
    try {
      await api.post(`/squads/${squadId}/join-requests/${requestId}/approve`);
      joinRequests.value = joinRequests.value.filter((r) => r.id !== requestId);
      // Refresh squad to update member count + pending_requests
      await fetchMySquads();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Bestätigen.");
      throw e;
    }
  }

  async function declineJoinRequest(squadId: string, requestId: string) {
    try {
      await api.post(`/squads/${squadId}/join-requests/${requestId}/decline`);
      joinRequests.value = joinRequests.value.filter((r) => r.id !== requestId);
      await fetchMySquads();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Fehler beim Ablehnen.");
      throw e;
    }
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
    toggleAutoBet,
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
