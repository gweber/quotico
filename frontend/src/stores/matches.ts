import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";
import { prefetchUserBets } from "@/composables/useUserBets";

export interface MatchOdds {
  h2h: Record<string, number>;
  totals?: { over: number; under: number; line: number };
  spreads?: { home_line: number; home_odds: number; away_line: number; away_odds: number };
  updated_at?: string | null;
}

export interface MatchResult {
  home_score: number | null;
  away_score: number | null;
  outcome: string | null;
}

export interface Match {
  id: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string;
  status: string;  // scheduled, live, final, cancelled
  odds: MatchOdds;
  result: MatchResult;
}

export interface LiveScore {
  match_id: string;
  home_score: number;
  away_score: number;
  minute?: number | null;
  half_time_home?: number | null;
  half_time_away?: number | null;
}

const WS_URL = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/live-scores`;

// Refresh interval: matches are re-fetched every 5 minutes (or on WS odds_updated)
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

export const useMatchesStore = defineStore("matches", () => {
  const api = useApi();
  const matches = ref<Match[]>([]);
  const liveScores = ref<Map<string, LiveScore>>(new Map());
  const loading = ref(true);
  const activeSport = ref<string | null>(null);
  const wsConnected = ref(false);

  // Odds change tracking: match IDs whose odds changed on last refresh
  const recentlyChangedOdds = ref<Set<string>>(new Set());

  // Refresh countdown
  const lastRefreshAt = ref(Date.now());
  const nextRefreshIn = ref(REFRESH_INTERVAL_MS);

  // WebSocket state
  let ws: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let countdownTimer: ReturnType<typeof setInterval> | null = null;
  let reconnectDelay = 1000;

  // Computed: seconds until next refresh
  const refreshCountdown = computed(() => Math.max(0, Math.ceil(nextRefreshIn.value / 1000)));

  async function fetchMatches(sport?: string) {
    loading.value = true;
    try {
      const params: Record<string, string> = {};
      if (sport) params.sport = sport;
      const freshMatches = await api.get<Match[]>("/matches/", params);
      _detectOddsChanges(freshMatches);
      matches.value = freshMatches;
      lastRefreshAt.value = Date.now();
      nextRefreshIn.value = REFRESH_INTERVAL_MS;
    } catch {
      matches.value = [];
    } finally {
      loading.value = false;
    }
  }

  /** Silently refresh odds without loading spinner. */
  async function refreshOdds() {
    try {
      const params: Record<string, string> = {};
      if (activeSport.value) params.sport = activeSport.value;
      const freshMatches = await api.get<Match[]>("/matches/", params);
      _detectOddsChanges(freshMatches);
      matches.value = freshMatches;
      lastRefreshAt.value = Date.now();
      nextRefreshIn.value = REFRESH_INTERVAL_MS;
    } catch {
      // Silent fail — keep showing existing data
    }
  }

  /** Compare old vs new matches to find odds changes. */
  function _detectOddsChanges(freshMatches: Match[]) {
    const oldMap = new Map(matches.value.map((m) => [m.id, m]));
    const changed = new Set<string>();
    for (const fresh of freshMatches) {
      const old = oldMap.get(fresh.id);
      if (!old) continue;
      const oldH2h = old.odds.h2h;
      const newH2h = fresh.odds.h2h;
      for (const key of Object.keys(newH2h)) {
        if (oldH2h[key] !== newH2h[key]) {
          changed.add(fresh.id);
          break;
        }
      }
    }
    if (changed.size > 0) {
      recentlyChangedOdds.value = changed;
      // Clear the flash after 2 seconds
      setTimeout(() => {
        recentlyChangedOdds.value = new Set();
      }, 2000);
    }
  }

  // --- WebSocket live scores ---

  function connectLive() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      wsConnected.value = true;
      reconnectDelay = 1000;
      pingTimer = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
      }, 30_000);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "live_scores") {
          const map = new Map<string, LiveScore>();
          for (const s of msg.data) {
            map.set(s.match_id, s);
          }
          liveScores.value = map;
        }
        if (msg.type === "match_resolved") {
          // Refetch matches to get updated status
          fetchMatches(activeSport.value ?? undefined);
          // Refetch user bet for this match to get won/lost status
          const matchId = msg.data?.match_id;
          if (matchId) {
            prefetchUserBets([matchId]);
          }
        }
        if (msg.type === "odds_updated") {
          // Server pushed fresh odds — refresh silently
          refreshOdds();
        }
      } catch {
        // Ignore non-JSON (pong)
      }
    };

    ws.onclose = () => {
      wsConnected.value = false;
      _cleanup();
      _scheduleReconnect();
    };

    ws.onerror = () => {
      // onclose fires after onerror
    };

    // Start periodic refresh + countdown tick
    _startRefreshTimer();
  }

  function disconnectLive() {
    _cleanup();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    wsConnected.value = false;
  }

  function _cleanup() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (countdownTimer) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function _startRefreshTimer() {
    if (refreshTimer) return;
    // Periodic background refresh
    refreshTimer = setInterval(() => {
      refreshOdds();
    }, REFRESH_INTERVAL_MS);
    // Countdown tick every second
    countdownTimer = setInterval(() => {
      nextRefreshIn.value = Math.max(0, REFRESH_INTERVAL_MS - (Date.now() - lastRefreshAt.value));
    }, 1000);
  }

  function _scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
      connectLive();
    }, reconnectDelay);
  }

  function setSport(sport: string | null) {
    activeSport.value = sport;
    fetchMatches(sport ?? undefined);
  }

  return {
    matches,
    liveScores,
    loading,
    activeSport,
    wsConnected,
    recentlyChangedOdds,
    refreshCountdown,
    fetchMatches,
    refreshOdds,
    connectLive,
    disconnectLive,
    setSport,
  };
});
