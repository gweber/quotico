import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useApi } from "@/composables/useApi";
import { useMatchesStore, type LiveScore } from "@/stores/matches";

export interface WarRoomSelection {
  type: string;
  value: "1" | "X" | "2";
}

export interface WarRoomMember {
  user_id: string;
  alias: string;
  has_tipped: boolean;
  is_self: boolean;
  selection: WarRoomSelection | null;
  locked_odds: number | null;
  tip_status: string | null;
  points_earned: number | null;
  is_currently_winning: boolean | null;
}

export interface WarRoomConsensus {
  percentages: Record<string, number>;
  total_tippers: number;
}

export interface WarRoomMatch {
  id: string;
  league_id: number;
  home_team: string;
  away_team: string;
  match_date: string;
  status: string;
  odds: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface WarRoomData {
  match: WarRoomMatch;
  members: WarRoomMember[];
  consensus: WarRoomConsensus | null;
  mavericks: string[] | null;
  is_post_kickoff: boolean;
}

export function useWarRoom(squadId: string, matchId: string) {
  const api = useApi();
  const matchesStore = useMatchesStore();

  const data = ref<WarRoomData | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  // --- Countdown timer ---
  const now = ref(Date.now());
  let ticker: ReturnType<typeof setInterval> | null = null;

  const commenceMs = computed(() =>
    data.value ? new Date(data.value.match.match_date).getTime() : 0
  );

  const countdown = computed(() => {
    if (!commenceMs.value) return null;
    const diff = commenceMs.value - now.value;
    if (diff <= 0) return null;
    const hours = Math.floor(diff / 3_600_000);
    const mins = Math.floor((diff % 3_600_000) / 60_000);
    const secs = Math.floor((diff % 60_000) / 1_000);
    if (hours > 0) return `${hours}h ${mins}m ${secs}s`;
    return `${mins}m ${secs}s`;
  });

  // --- Live score from existing WebSocket ---
  const liveScore = computed<LiveScore | null>(
    () => matchesStore.liveScores.get(matchId) ?? null
  );

  // --- Phase derivation ---
  const phase = computed<"pre_kickoff" | "revealed" | "live">(() => {
    if (!data.value) return "pre_kickoff";
    const matchStatus = data.value.match.status;
    if (matchStatus === "live" || liveScore.value != null) return "live";
    if (matchStatus === "final") return "live";
    if (now.value < commenceMs.value) return "pre_kickoff";
    return "revealed";
  });

  // --- Bet outcome helpers (client-side, real-time via WS) ---
  function isBetWinning(selection: "1" | "X" | "2"): boolean {
    const score = liveScore.value;
    const match = data.value?.match;
    const hs = score?.home_score ?? (match?.result as any)?.home_score;
    const as_ = score?.away_score ?? (match?.result as any)?.away_score;
    if (hs == null || as_ == null) return false;

    if (hs > as_) return selection === "1";
    if (as_ > hs) return selection === "2";
    return selection === "X";
  }

  // --- Data fetching ---
  async function fetchWarRoom() {
    loading.value = true;
    error.value = null;
    try {
      data.value = await api.get<WarRoomData>(
        `/squads/${squadId}/war-room/${matchId}`
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Fehler beim Laden.";
    } finally {
      loading.value = false;
    }
  }

  // --- Polling for phase transitions + new tips ---
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  function startPolling() {
    stopPolling();
    const interval = phase.value === "live" ? 30_000 : 60_000;
    pollTimer = setInterval(fetchWarRoom, interval);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // Restart polling when phase changes (different interval)
  watch(phase, () => {
    startPolling();
  });

  onMounted(async () => {
    ticker = setInterval(() => {
      now.value = Date.now();
    }, 1_000);
    matchesStore.connectLive();
    await fetchWarRoom();
    startPolling();
  });

  onUnmounted(() => {
    if (ticker) clearInterval(ticker);
    stopPolling();
    matchesStore.disconnectLive();
  });

  return {
    data,
    loading,
    error,
    now,
    countdown,
    phase,
    liveScore,
    isBetWinning,
    fetchWarRoom,
  };
}
