/**
 * frontend/src/stores/matches.ts
 *
 * Purpose:
 * Runtime store for dashboard matches, odds refresh, and live-score websocket.
 * Uses v3 match endpoints and league_id-based filtering for the startpage nav.
 */
import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";
import { prefetchUserBets } from "@/composables/useUserBets";
import { oddsValueBySelection } from "@/composables/useMatchV3Adapter";
import type { MatchV3, OddsMetaV3, PeriodScoresV3 } from "@/types/MatchV3";
import type { MatchQtipPayload, OutputLevel } from "@/types/persona";

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
  league_id: number;
  league_name?: string;
  league_country?: string;
  home_team: string;
  away_team: string;
  match_date: string;
  start_at?: string;
  status: string;  // scheduled, live, final, cancelled
  odds: MatchOdds;
  result: MatchResult;
  odds_meta?: OddsMetaV3;
  has_advanced_stats?: boolean;
  referee_id?: number | null;
  teams?: MatchV3["teams"];
  scores?: PeriodScoresV3;
  qtip?: MatchQtipPayload | null;
  qtip_output_level?: OutputLevel;
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

interface FetchMatchesOptions {
  leagueId?: number | null;
}

export const useMatchesStore = defineStore("matches", () => {
  const api = useApi();
  const matches = ref<Match[]>([]);
  const liveScores = ref<Map<string, LiveScore>>(new Map());
  const loading = ref(true);
  const activeLeagueId = ref<number | null>(null);
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

  function _statusFromV3(status: string | undefined): string {
    const raw = String(status || "").toUpperCase();
    if (raw === "FINISHED") return "final";
    if (raw === "LIVE") return "live";
    if (raw === "POSTPONED" || raw === "WALKOVER") return "cancelled";
    return "scheduled";
  }

  function _toLegacyMatch(row: any): Match {
    const id = String(row?._id ?? row?.id ?? "");
    const homeId = Number(row?.teams?.home?.sm_id || 0);
    const awayId = Number(row?.teams?.away?.sm_id || 0);
    // FIXME: ODDS_V3_BREAK — reads odds_meta.summary_1x2 for odds display which is no longer produced by connector
    const summary = row?.odds_meta?.summary_1x2 || {};
    const h2h: Record<string, number> = {};
    const homeAvg = Number(summary?.home?.avg);
    const drawAvg = Number(summary?.draw?.avg);
    const awayAvg = Number(summary?.away?.avg);
    if (Number.isFinite(homeAvg)) h2h["1"] = homeAvg;
    if (Number.isFinite(drawAvg)) h2h["X"] = drawAvg;
    if (Number.isFinite(awayAvg)) h2h["2"] = awayAvg;
    return {
      id,
      league_id: typeof row?.league_id === "number" ? row.league_id : 0,
      league_name: typeof row?.league_name === "string" ? row.league_name : undefined,
      league_country: typeof row?.league_country === "string" ? row.league_country : undefined,
      home_team: String(row?.teams?.home?.name || `Team ${homeId || "?"}`),
      away_team: String(row?.teams?.away?.name || `Team ${awayId || "?"}`),
      match_date: String(row?.start_at || ""),
      start_at: String(row?.start_at || ""),
      status: _statusFromV3(row?.status),
      odds: { h2h, updated_at: row?.odds_meta?.updated_at || null },
      result: {
        home_score: row?.teams?.home?.score ?? null,
        away_score: row?.teams?.away?.score ?? null,
        outcome: null,
      },
      odds_meta: row?.odds_meta,
      has_advanced_stats: Boolean(row?.has_advanced_stats),
      referee_id: typeof row?.referee_id === "number" ? row.referee_id : null,
      teams: row?.teams,
      scores: row?.scores,
      qtip: row?.qtip ?? null,
      qtip_output_level: typeof row?.qtip_output_level === "string" ? row.qtip_output_level : "none",
    };
  }

  async function fetchMatches(options: FetchMatchesOptions = {}) {
    loading.value = true;
    try {
      const params: Record<string, string> = { status: "SCHEDULED" };
      const leagueId = options.leagueId ?? activeLeagueId.value;
      if (typeof leagueId === "number") params.league_id = String(leagueId);
      const response = await api.get<{ items: any[] }>("/v3/matches", params);
      const freshMatches = (response.items || []).map(_toLegacyMatch);
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
      const params: Record<string, string> = { status: "SCHEDULED" };
      if (typeof activeLeagueId.value === "number") params.league_id = String(activeLeagueId.value);
      const response = await api.get<{ items: any[] }>("/v3/matches", params);
      const freshMatches = (response.items || []).map(_toLegacyMatch);
      _detectOddsChanges(freshMatches);
      matches.value = freshMatches;
      lastRefreshAt.value = Date.now();
      nextRefreshIn.value = REFRESH_INTERVAL_MS;
    } catch {
      // Silent fail — keep showing existing data
    }
  }

  /** Refresh only selected matches and merge them into the current list. */
  async function refreshOddsByMatchIds(matchIds: string[]) {
    const ids = [...new Set(matchIds.map((id) => Number(id)).filter((id) => Number.isFinite(id)))];
    if (ids.length === 0) {
      await refreshOdds();
      return;
    }
    try {
      const query = await api.post<{ items: any[] }>("/v3/matches/query", {
        ids,
        statuses: ["SCHEDULED"],
        limit: Math.min(200, ids.length),
      });
      const freshSubset = (query.items || []).map(_toLegacyMatch);
      if (freshSubset.length === 0) return;
      const mergedMap = new Map(matches.value.map((m) => [m.id, m]));
      for (const fresh of freshSubset) mergedMap.set(fresh.id, fresh);
      const merged = [...mergedMap.values()];
      _detectOddsChanges(merged);
      matches.value = merged;
      lastRefreshAt.value = Date.now();
      nextRefreshIn.value = REFRESH_INTERVAL_MS;
    } catch {
      // Fallback to full silent refresh if targeted refresh fails.
      refreshOdds();
    }
  }

  /** Compare old vs new matches to find odds changes. */
  function _detectOddsChanges(freshMatches: Match[]) {
    const oldMap = new Map(matches.value.map((m) => [m.id, m]));
    const changed = new Set<string>();
    for (const fresh of freshMatches) {
      const old = oldMap.get(fresh.id);
      if (!old) continue;
      for (const key of ["1", "X", "2"] as const) {
        const oldValue = oddsValueBySelection(old as unknown as MatchV3, key);
        const newValue = oddsValueBySelection(fresh as unknown as MatchV3, key);
        if (oldValue !== newValue) {
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
          fetchMatches({ leagueId: activeLeagueId.value });
          // Refetch user bet for this match to get won/lost status
          const matchId = msg.data?.match_id;
          if (matchId) {
            prefetchUserBets([matchId]);
          }
        }
        if (msg.type === "odds_updated") {
          const ids = Array.isArray(msg?.data?.match_ids)
            ? msg.data.match_ids.map((x: unknown) => String(x)).filter((x: string) => x.length > 0)
            : [];
          if (ids.length > 0) {
            refreshOddsByMatchIds(ids);
          } else {
            // Server pushed fresh odds — refresh silently
            refreshOdds();
          }
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

  function setLeague(leagueId: number | null) {
    activeLeagueId.value = leagueId;
    fetchMatches({ leagueId });
  }

  return {
    matches,
    liveScores,
    loading,
    activeLeagueId,
    wsConnected,
    recentlyChangedOdds,
    refreshCountdown,
    fetchMatches,
    refreshOdds,
    refreshOddsByMatchIds,
    connectLive,
    disconnectLive,
    setLeague,
  };
});
