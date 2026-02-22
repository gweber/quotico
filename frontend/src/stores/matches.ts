import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface Match {
  id: string;
  sport_key: string;
  teams: { home: string; away: string };
  commence_time: string;
  status: string;
  current_odds: Record<string, number>;
  odds_updated_at: string;
  result?: string;
  home_score?: number | null;
  away_score?: number | null;
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

export const useMatchesStore = defineStore("matches", () => {
  const api = useApi();
  const matches = ref<Match[]>([]);
  const liveScores = ref<Map<string, LiveScore>>(new Map());
  const loading = ref(true);
  const activeSport = ref<string | null>(null);
  const wsConnected = ref(false);

  // WebSocket state
  let ws: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let reconnectDelay = 1000;

  async function fetchMatches(sport?: string) {
    loading.value = true;
    try {
      const params: Record<string, string> = {};
      if (sport) params.sport = sport;
      matches.value = await api.get<Match[]>("/matches/", params);
    } catch {
      matches.value = [];
    } finally {
      loading.value = false;
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
    fetchMatches,
    connectLive,
    disconnectLive,
    setSport,
  };
});
