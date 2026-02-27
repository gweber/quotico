import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";
import { populateTipCache, type QuoticoTip } from "@/composables/useQuoticoTip";
import type { MatchV3, OddsMetaV3 } from "@/types/MatchV3";

export interface MatchdaySport {
  league_id: number;
  label: string;
  matchdays_per_season: number;
}

export interface Matchday {
  id: string;
  league_id: number;
  season: number;
  matchday_number: number;
  label: string;
  match_count: number;
  first_kickoff: string | null;
  last_kickoff: string | null;
  status: string;
  all_resolved: boolean;
}

// FIXME: ODDS_V3_BREAK — type includes odds_meta which is no longer produced by connector
export interface MatchdayMatch {
  id: string;
  home_team: string;
  away_team: string;
  match_date: string;
  start_at?: string;
  status: string;  // scheduled, live, final, cancelled
  odds: Record<string, unknown>;
  result: Record<string, unknown>;
  odds_meta?: OddsMetaV3;
  has_advanced_stats?: boolean;
  referee_id?: number | string | null;
  referee_name?: string | null;
  teams?: MatchV3["teams"];
  is_locked: boolean;
  h2h_context?: Record<string, unknown> | null;
  quotico_tip?: QuoticoTip | null;
}

export interface Prediction {
  match_id: string;
  home_score: number;
  away_score: number;
  is_auto: boolean;
  is_admin_entry: boolean;
  points_earned: number | null;
}

export interface MatchdayPrediction {
  matchday_id: string;
  squad_id: string | null;
  auto_bet_strategy: string;
  predictions: Prediction[];
  admin_unlocked_matches: string[];
  total_points: number | null;
  status: string;
}

export interface MoneylineBet {
  id: string;
  match_id: string;
  selection: string; // "1", "X", "2"
  locked_odds: number;
  points_earned: number | null;
  status: string; // "pending", "won", "lost", "void"
}

/** Per-match save state for the auto-save indicator */
export type SaveState = "idle" | "syncing" | "saved" | "error";

/** Betslip response from the server */
interface SlipResponse {
  id: string;
  selections: Array<{
    match_id: string;
    market: string;
    pick: { home: number; away: number } | string;
    points_earned: number | null;
    is_auto: boolean;
    is_admin_entry?: boolean;
    status: string;
  }>;
  auto_bet_strategy?: string;
  total_points?: number | null;
  admin_unlocked_matches?: string[];
  status: string;
}

interface LeagueNavigationResponse {
  items: Array<{
    league_id?: number;
    id?: number;
    name: string;
  }>;
}

export const useMatchdayStore = defineStore("matchday", () => {
  const api = useApi();

  // State
  const sports = ref<MatchdaySport[]>([]);
  const matchdays = ref<Matchday[]>([]);
  const currentMatchday = ref<Matchday | null>(null);
  const matches = ref<MatchdayMatch[]>([]);
  const predictions = ref<MatchdayPrediction | null>(null);
  const legacyPredictionMatches = ref<string[]>([]);

  // Draft slip state (server-side draft)
  const slipId = ref<string | null>(null);
  const draftPredictions = ref<Map<string, { home: number; away: number }>>(new Map());
  const draftAutoStrategy = ref<string>("none");

  // Per-match save state for indicator (idle/syncing/saved/error)
  const saveStates = ref<Map<string, SaveState>>(new Map());

  // Moneyline bets state (keyed by match_id)
  const moneylineBets = ref<Map<string, MoneylineBet>>(new Map());

  const loading = ref(false);
  const saving = ref(false);
  const activeSport = ref<number>(0);
  const activeSquadId = ref<string | null>(null);

  // --- Per-match debounce timers ---
  const _debounceTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const _pendingPatches = new Map<string, { home: number; away: number }>();
  const DEBOUNCE_MS = 300;

  // --- In-memory caches (not reactive — internal bookkeeping only) ---
  interface CachedDetail {
    matchday: Matchday;
    matches: MatchdayMatch[];
    fetchedAt: number;
  }
  const detailCache = new Map<string, CachedDetail>();
  const matchdaysCache = new Map<string, { data: Matchday[]; fetchedAt: number }>();

  function detailTTL(md: Matchday): number {
    if (md.all_resolved) return 86_400_000;   // 24 h — completed, immutable
    if (md.status === "in_progress") return 60_000; // 60 s — live scores
    return 300_000;                             // 5 min — upcoming, odds drift
  }

  const editableMatches = computed(() =>
    matches.value.filter((m) => !m.is_locked)
  );

  const lockedMatches = computed(() =>
    matches.value.filter((m) => m.is_locked)
  );

  const betCount = computed(() => draftPredictions.value.size);

  /** Whether all draft predictions are synced to server (no pending debounced saves) */
  const allSaved = computed(() =>
    _pendingPatches.size === 0 &&
    Array.from(saveStates.value.values()).every((s) => s === "idle" || s === "saved")
  );

  function getSaveState(matchId: string): SaveState {
    return saveStates.value.get(matchId) ?? "idle";
  }

  function _setSaveState(matchId: string, state: SaveState) {
    const newMap = new Map(saveStates.value);
    newMap.set(matchId, state);
    saveStates.value = newMap;

    // Auto-clear "saved" state after 1.5s
    if (state === "saved") {
      setTimeout(() => {
        if (saveStates.value.get(matchId) === "saved") {
          const m = new Map(saveStates.value);
          m.set(matchId, "idle");
          saveStates.value = m;
        }
      }, 1500);
    }
  }

  // Actions
  async function fetchSports() {
    if (sports.value.length > 0) return; // static for the session
    try {
      const response = await api.get<LeagueNavigationResponse>("/leagues/navigation");
      sports.value = (response.items || []).map((item) => ({
        league_id: Number(item.league_id ?? item.id),
        label: String(item.name || ""),
        matchdays_per_season: 0,
      })).filter((item) => Number.isInteger(item.league_id) && item.league_id > 0);
    } catch {
      sports.value = [];
    }
  }

  async function fetchMatchdays(sport?: number) {
    const leagueId = sport ?? activeSport.value;
    const cacheKey = String(leagueId);

    // Serve from cache if fresh (30 min TTL)
    const cached = matchdaysCache.get(cacheKey);
    if (cached && Date.now() - cached.fetchedAt < 1_800_000) {
      matchdays.value = cached.data;
      return;
    }

    loading.value = true;
    try {
      matchdays.value = await api.get<Matchday[]>("/matchday/matchdays", {
        league_id: String(leagueId),
      });
      matchdaysCache.set(cacheKey, { data: matchdays.value, fetchedAt: Date.now() });
    } catch {
      matchdays.value = [];
    } finally {
      loading.value = false;
    }
  }

  function applyDetail(data: { matchday: Matchday; matches: MatchdayMatch[] }) {
    currentMatchday.value = data.matchday;
    const sorted = [...data.matches];
    if (data.matchday.status === "in_progress") {
      sorted.sort((a, b) => {
        const aDone = a.status === "final" ? 1 : 0;
        const bDone = b.status === "final" ? 1 : 0;
        return aDone - bDone;
      });
    }
    matches.value = sorted;
    const qTips: QuoticoTip[] = [];
    for (const m of data.matches) {
      if (m.quotico_tip) qTips.push(m.quotico_tip);
    }
    if (qTips.length) populateTipCache(qTips);
  }

  async function doFetchDetail(matchdayId: string, squadId?: string | null, showSpinner = true) {
    if (showSpinner) loading.value = true;
    try {
      const params: Record<string, string> = {};
      if (squadId) params.squad_id = squadId;
      const data = await api.get<{
        matchday: Matchday;
        matches: MatchdayMatch[];
      }>(`/matchday/matchdays/${matchdayId}`, params);

      detailCache.set(matchdayId, {
        matchday: data.matchday,
        matches: data.matches,
        fetchedAt: Date.now(),
      });
      applyDetail(data);
    } catch {
      if (showSpinner) {
        currentMatchday.value = null;
        matches.value = [];
      }
    } finally {
      if (showSpinner) loading.value = false;
    }
  }

  async function fetchMatchdayDetail(matchdayId: string, squadId?: string | null) {
    // Prevent state bleed when switching from legacy-like IDs to v3 IDs (or vice versa).
    const currentlyV3 = String(currentMatchday.value?.id || "").startsWith("v3:");
    const nextV3 = String(matchdayId || "").startsWith("v3:");
    if (currentMatchday.value && currentlyV3 !== nextV3) {
      currentMatchday.value = null;
      matches.value = [];
      predictions.value = null;
      draftPredictions.value = new Map();
      legacyPredictionMatches.value = [];
      slipId.value = null;
    }
    const cached = detailCache.get(matchdayId);

    if (cached) {
      // Show cached data immediately (no spinner)
      applyDetail(cached);

      // Still fresh? Done.
      if (Date.now() - cached.fetchedAt < detailTTL(cached.matchday)) return;

      // Stale — background refresh without spinner
      doFetchDetail(matchdayId, squadId, false);
      return;
    }

    // No cache — full fetch with spinner
    await doFetchDetail(matchdayId, squadId, true);
  }

  /** Show cached matchday detail during drag — no network, no spinner, no side-effects. */
  function previewCached(matchdayId: string): boolean {
    const cached = detailCache.get(matchdayId);
    if (!cached) return false;
    if (Date.now() - cached.fetchedAt > detailTTL(cached.matchday)) return false;
    applyDetail(cached);
    return true;
  }

  // ---------- Server-side draft lifecycle ----------

  /**
   * Ensure a server-side draft slip exists for this matchday.
   * Creates one via POST /api/betting-slips/draft if none exists.
   * Populates slipId + draftPredictions from server state.
   */
  async function ensureDraft(matchdayId: string, squadId?: string | null) {
    try {
      const slip = await api.post<SlipResponse>("/betting-slips/draft", {
        type: "matchday_round",
        matchday_id: matchdayId,
        squad_id: squadId || null,
        league_id: activeSport.value,
      });
      slipId.value = slip.id;

      // Populate draft predictions from server state
      draftPredictions.value = new Map();
      legacyPredictionMatches.value = [];
      const activeMatchIds = new Set(matches.value.map((m) => String(m.id)));

      for (const sel of slip.selections) {
        const pick = sel.pick as { home: number; away: number };
        if (!pick || pick.home == null || pick.away == null) continue;
        if (!activeMatchIds.has(String(sel.match_id))) {
          legacyPredictionMatches.value.push(String(sel.match_id));
          continue;
        }
        draftPredictions.value.set(sel.match_id, {
          home: pick.home,
          away: pick.away,
        });
      }
      draftAutoStrategy.value = slip.auto_bet_strategy || "none";

      // Also set predictions for display compatibility (points, total_points, etc.)
      predictions.value = {
        matchday_id: matchdayId,
        squad_id: squadId || null,
        auto_bet_strategy: slip.auto_bet_strategy || "none",
        predictions: slip.selections
          .filter((s) => s.market === "exact_score" && typeof s.pick === "object")
          .map((s) => {
            const p = s.pick as { home: number; away: number };
            return {
              match_id: s.match_id,
              home_score: p.home,
              away_score: p.away,
              is_auto: s.is_auto,
              is_admin_entry: s.is_admin_entry ?? false,
              points_earned: s.points_earned,
            };
          }),
        admin_unlocked_matches: slip.admin_unlocked_matches || [],
        total_points: slip.total_points ?? null,
        status: slip.status,
      };
    } catch {
      slipId.value = null;
    }
  }

  /**
   * Update a single leg on the server-side draft.
   * Per-match debounce: only fires when both home AND away are filled.
   * 300ms timer per match_id — resets on each call.
   */
  function updateLeg(matchId: string, home: number | null, away: number | null) {
    // Update local draft immediately for responsive UI
    if (home != null && away != null) {
      const newMap = new Map(draftPredictions.value);
      newMap.set(matchId, { home, away });
      draftPredictions.value = newMap;
    }

    // Only fire PATCH when both scores are filled
    if (home == null || away == null) return;
    if (!slipId.value) return;

    // Store pending patch
    _pendingPatches.set(matchId, { home, away });

    // Clear existing timer for this match
    const existing = _debounceTimers.get(matchId);
    if (existing) clearTimeout(existing);

    // Set new debounce timer
    _debounceTimers.set(
      matchId,
      setTimeout(() => {
        _debounceTimers.delete(matchId);
        void _firePatch(matchId);
      }, DEBOUNCE_MS)
    );
  }

  /** Fire a PATCH for a single match to the server. */
  async function _firePatch(matchId: string) {
    const patch = _pendingPatches.get(matchId);
    if (!patch || !slipId.value) return;
    _pendingPatches.delete(matchId);

    _setSaveState(matchId, "syncing");

    // Determine action: add if not in selections, update if already there
    const existingPred = predictions.value?.predictions.find(
      (p) => p.match_id === matchId
    );
    const action = existingPred ? "update" : "add";

    try {
      await api.patch(`/betting-slips/${slipId.value}/selections`, {
        action,
        match_id: matchId,
        market: "exact_score",
        pick: { home: patch.home, away: patch.away },
      });

      // Update predictions state to reflect the save
      if (predictions.value) {
        const idx = predictions.value.predictions.findIndex(
          (p) => p.match_id === matchId
        );
        const pred: Prediction = {
          match_id: matchId,
          home_score: patch.home,
          away_score: patch.away,
          is_auto: false,
          is_admin_entry: false,
          points_earned: null,
        };
        if (idx >= 0) {
          predictions.value.predictions[idx] = pred;
        } else {
          predictions.value.predictions.push(pred);
        }
      }

      _setSaveState(matchId, "saved");
    } catch {
      _setSaveState(matchId, "error");
    }
  }

  /** Remove a leg from the server-side draft. */
  async function removeLeg(matchId: string) {
    // Cancel any pending debounce
    const timer = _debounceTimers.get(matchId);
    if (timer) {
      clearTimeout(timer);
      _debounceTimers.delete(matchId);
    }
    _pendingPatches.delete(matchId);

    // Remove from local state
    const newMap = new Map(draftPredictions.value);
    newMap.delete(matchId);
    draftPredictions.value = newMap;

    if (!slipId.value) return;

    _setSaveState(matchId, "syncing");
    try {
      await api.patch(`/betting-slips/${slipId.value}/selections`, {
        action: "remove",
        match_id: matchId,
        market: "exact_score",
      });

      if (predictions.value) {
        predictions.value.predictions = predictions.value.predictions.filter(
          (p) => p.match_id !== matchId
        );
      }

      _setSaveState(matchId, "idle");
    } catch {
      _setSaveState(matchId, "error");
    }
  }

  /**
   * Flush all pending debounced saves immediately.
   * Called on beforeunload to prevent data loss.
   */
  function flushPending() {
    for (const [matchId, timer] of _debounceTimers) {
      clearTimeout(timer);
      _debounceTimers.delete(matchId);
      // Fire synchronously via sendBeacon if available
      const patch = _pendingPatches.get(matchId);
      if (patch && slipId.value) {
        _pendingPatches.delete(matchId);
        const body = JSON.stringify({
          action: predictions.value?.predictions.find((p) => p.match_id === matchId)
            ? "update"
            : "add",
          match_id: matchId,
          market: "exact_score",
          pick: { home: patch.home, away: patch.away },
        });
        // Use sendBeacon for reliable delivery on page unload
        if (navigator.sendBeacon) {
          navigator.sendBeacon(
            `/api/betting-slips/${slipId.value}/selections`,
            new Blob([body], { type: "application/json" })
          );
        }
      }
    }
  }

  // ---------- Legacy fetchPredictions (now reads from draft) ----------

  async function fetchPredictions(matchdayId: string) {
    try {
      const params: Record<string, string> = {};
      if (activeSquadId.value) params.squad_id = activeSquadId.value;
      const data = await api.get<MatchdayPrediction | null>(
        `/matchday/matchdays/${matchdayId}/predictions`,
        params
      );
      predictions.value = data;
      legacyPredictionMatches.value = [];

      // Populate draft from existing predictions
      draftPredictions.value = new Map();
      if (data?.predictions) {
        const activeMatchIds = new Set(matches.value.map((m) => String(m.id)));
        for (const p of data.predictions) {
          if (!activeMatchIds.has(String(p.match_id))) {
            legacyPredictionMatches.value.push(String(p.match_id));
            continue;
          }
          draftPredictions.value.set(p.match_id, {
            home: p.home_score,
            away: p.away_score,
          });
        }
      }
      draftAutoStrategy.value = data?.auto_bet_strategy || "none";
    } catch {
      predictions.value = null;
      legacyPredictionMatches.value = [];
    }
  }

  function setDraftPrediction(matchId: string, home: number, away: number) {
    updateLeg(matchId, home, away);
  }

  function removeDraftPrediction(matchId: string) {
    removeLeg(matchId);
  }

  async function savePredictions(_matchdayId: string): Promise<boolean> {
    // No-op in new auto-save flow — predictions are saved per-leg via PATCH
    // This remains for backward compatibility but the save button is removed
    return true;
  }

  async function fetchMoneylineBets(matchIds: string[]) {
    if (matchIds.length === 0) return;
    try {
      interface BetResponse {
        id: string;
        match_id: string;
        selection: { type: string; value: string };
        locked_odds: number;
        points_earned: number | null;
        status: string;
        created_at: string;
      }
      const bets = await api.get<BetResponse[]>("/bets/mine", {
        match_ids: matchIds.join(","),
      });
      const newMap = new Map<string, MoneylineBet>();
      for (const t of bets) {
        newMap.set(t.match_id, {
          id: t.id,
          match_id: t.match_id,
          selection: t.selection.value,
          locked_odds: t.locked_odds,
          points_earned: t.points_earned,
          status: t.status,
        });
      }
      moneylineBets.value = newMap;
    } catch {
      moneylineBets.value = new Map();
    }
  }

  async function submitMoneylineBet(
    matchId: string,
    prediction: string,
    displayedOdds: number
  ): Promise<boolean> {
    saving.value = true;
    try {
      interface BetResponse {
        id: string;
        match_id: string;
        selection: { type: string; value: string };
        locked_odds: number;
        points_earned: number | null;
        status: string;
        created_at: string;
      }
      const bet = await api.post<BetResponse>("/bets/", {
        match_id: matchId,
        prediction,
        displayed_odds: displayedOdds,
      });
      const newMap = new Map(moneylineBets.value);
      newMap.set(bet.match_id, {
        id: bet.id,
        match_id: bet.match_id,
        selection: bet.selection.value,
        locked_odds: bet.locked_odds,
        points_earned: bet.points_earned,
        status: bet.status,
      });
      moneylineBets.value = newMap;
      return true;
    } catch {
      return false;
    } finally {
      saving.value = false;
    }
  }

  function setSport(sport: number) {
    activeSport.value = sport;
    // Clear view state (caches are keyed separately and stay intact)
    currentMatchday.value = null;
    matches.value = [];
    predictions.value = null;
    draftPredictions.value = new Map();
    moneylineBets.value = new Map();
    slipId.value = null;
    saveStates.value = new Map();
  }

  function invalidateCache(matchdayId?: string) {
    if (matchdayId) {
      detailCache.delete(matchdayId);
    } else {
      detailCache.clear();
      matchdaysCache.clear();
    }
  }

  function setSquadContext(squadId: string | null) {
    activeSquadId.value = squadId;
    // Clear predictions when squad changes (different squad = different predictions)
    predictions.value = null;
    draftPredictions.value = new Map();
    moneylineBets.value = new Map();
    slipId.value = null;
    saveStates.value = new Map();
  }

  return {
    sports,
    matchdays,
    currentMatchday,
    matches,
    predictions,
    legacyPredictionMatches,
    draftPredictions,
    draftAutoStrategy,
    loading,
    saving,
    activeSport,
    activeSquadId,
    editableMatches,
    lockedMatches,
    betCount,
    allSaved,
    saveStates,
    getSaveState,
    fetchSports,
    fetchMatchdays,
    fetchMatchdayDetail,
    previewCached,
    fetchPredictions,
    ensureDraft,
    updateLeg,
    removeLeg,
    flushPending,
    setDraftPrediction,
    removeDraftPrediction,
    savePredictions,
    moneylineBets,
    fetchMoneylineBets,
    submitMoneylineBet,
    setSport,
    setSquadContext,
    invalidateCache,
  };
});
