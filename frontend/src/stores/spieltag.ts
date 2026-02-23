import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";
import { populateTipCache, type QuoticoTip } from "@/composables/useQuoticoTip";

export interface SpieltagSport {
  sport_key: string;
  label: string;
  matchdays_per_season: number;
}

export interface Matchday {
  id: string;
  sport_key: string;
  season: number;
  matchday_number: number;
  label: string;
  match_count: number;
  first_kickoff: string | null;
  last_kickoff: string | null;
  status: string;
  all_resolved: boolean;
}

export interface SpieltagMatch {
  id: string;
  teams: { home: string; away: string };
  commence_time: string;
  status: string;
  current_odds: Record<string, number>;
  totals_odds?: { over: number; under: number; line: number };
  spreads_odds?: { home_line: number; home_odds: number; away_line: number; away_odds: number };
  result: string | null;
  home_score: number | null;
  away_score: number | null;
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

export interface SpieltagPrediction {
  matchday_id: string;
  squad_id: string | null;
  auto_tipp_strategy: string;
  predictions: Prediction[];
  admin_unlocked_matches: string[];
  total_points: number | null;
  status: string;
}

export interface SquadMember {
  user_id: string;
  alias: string;
}

export interface MoneylineTip {
  id: string;
  match_id: string;
  selection: string; // "1", "X", "2"
  locked_odds: number;
  points_earned: number | null;
  status: string; // "pending", "won", "lost", "void"
}

export const useSpieltagStore = defineStore("spieltag", () => {
  const api = useApi();

  // State
  const sports = ref<SpieltagSport[]>([]);
  const matchdays = ref<Matchday[]>([]);
  const currentMatchday = ref<Matchday | null>(null);
  const matches = ref<SpieltagMatch[]>([]);
  const predictions = ref<SpieltagPrediction | null>(null);

  // Draft state (local edits before saving)
  const draftPredictions = ref<Map<string, { home: number; away: number }>>(new Map());
  const draftAutoStrategy = ref<string>("none");

  // Moneyline tips state (keyed by match_id)
  const moneylineTips = ref<Map<string, MoneylineTip>>(new Map());

  const loading = ref(false);
  const saving = ref(false);
  const activeSport = ref<string>("soccer_germany_bundesliga");
  const activeSquadId = ref<string | null>(null);

  // --- In-memory caches (not reactive — internal bookkeeping only) ---
  interface CachedDetail {
    matchday: Matchday;
    matches: SpieltagMatch[];
    fetchedAt: number;
  }
  const detailCache = new Map<string, CachedDetail>();
  const matchdaysCache = new Map<string, { data: Matchday[]; fetchedAt: number }>();

  function detailTTL(md: Matchday): number {
    if (md.all_resolved) return 86_400_000;   // 24 h — completed, immutable
    if (md.status === "in_progress") return 60_000; // 60 s — live scores
    return 300_000;                             // 5 min — upcoming, odds drift
  }

  // Admin state
  const squadMembers = ref<SquadMember[]>([]);
  const adminTargetUserId = ref<string | null>(null);
  const adminTargetPredictions = ref<SpieltagPrediction | null>(null);

  // Computed
  const adminUnlockedSet = computed(() =>
    new Set(predictions.value?.admin_unlocked_matches ?? [])
  );

  const editableMatches = computed(() =>
    matches.value.filter(
      (m) => !m.is_locked || adminUnlockedSet.value.has(m.id)
    )
  );

  const lockedMatches = computed(() =>
    matches.value.filter((m) => m.is_locked)
  );

  const tippedCount = computed(() => draftPredictions.value.size);

  // Actions
  async function fetchSports() {
    if (sports.value.length > 0) return; // static for the session
    try {
      sports.value = await api.get<SpieltagSport[]>("/spieltag/sports");
    } catch {
      sports.value = [];
    }
  }

  async function fetchMatchdays(sport?: string) {
    const sportKey = sport || activeSport.value;

    // Serve from cache if fresh (30 min TTL)
    const cached = matchdaysCache.get(sportKey);
    if (cached && Date.now() - cached.fetchedAt < 1_800_000) {
      matchdays.value = cached.data;
      return;
    }

    loading.value = true;
    try {
      matchdays.value = await api.get<Matchday[]>("/spieltag/matchdays", {
        sport: sportKey,
      });
      matchdaysCache.set(sportKey, { data: matchdays.value, fetchedAt: Date.now() });
    } catch {
      matchdays.value = [];
    } finally {
      loading.value = false;
    }
  }

  function applyDetail(data: { matchday: Matchday; matches: SpieltagMatch[] }) {
    currentMatchday.value = data.matchday;
    const sorted = [...data.matches];
    if (data.matchday.status === "in_progress") {
      sorted.sort((a, b) => {
        const aDone = a.status === "completed" ? 1 : 0;
        const bDone = b.status === "completed" ? 1 : 0;
        return aDone - bDone;
      });
    }
    matches.value = sorted;
    const tips: QuoticoTip[] = [];
    for (const m of data.matches) {
      if (m.quotico_tip) tips.push(m.quotico_tip);
    }
    if (tips.length) populateTipCache(tips);
  }

  async function doFetchDetail(matchdayId: string, squadId?: string | null, showSpinner = true) {
    if (showSpinner) loading.value = true;
    try {
      const params: Record<string, string> = {};
      if (squadId) params.squad_id = squadId;
      const data = await api.get<{
        matchday: Matchday;
        matches: SpieltagMatch[];
      }>(`/spieltag/matchdays/${matchdayId}`, params);

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

  async function fetchPredictions(matchdayId: string) {
    try {
      const params: Record<string, string> = {};
      if (activeSquadId.value) params.squad_id = activeSquadId.value;
      const data = await api.get<SpieltagPrediction | null>(
        `/spieltag/matchdays/${matchdayId}/predictions`,
        params
      );
      predictions.value = data;

      // Populate draft from existing predictions
      draftPredictions.value = new Map();
      if (data?.predictions) {
        for (const p of data.predictions) {
          draftPredictions.value.set(p.match_id, {
            home: p.home_score,
            away: p.away_score,
          });
        }
      }
      draftAutoStrategy.value = data?.auto_tipp_strategy || "none";
    } catch {
      predictions.value = null;
    }
  }

  function setDraftPrediction(matchId: string, home: number, away: number) {
    const newMap = new Map(draftPredictions.value);
    newMap.set(matchId, { home, away });
    draftPredictions.value = newMap;
  }

  function removeDraftPrediction(matchId: string) {
    const newMap = new Map(draftPredictions.value);
    newMap.delete(matchId);
    draftPredictions.value = newMap;
  }

  async function savePredictions(matchdayId: string): Promise<boolean> {
    saving.value = true;
    try {
      const preds = Array.from(draftPredictions.value.entries()).map(
        ([matchId, scores]) => ({
          match_id: matchId,
          home_score: scores.home,
          away_score: scores.away,
        })
      );

      await api.post(`/spieltag/matchdays/${matchdayId}/predictions`, {
        predictions: preds,
        auto_tipp_strategy: draftAutoStrategy.value,
        squad_id: activeSquadId.value,
      });

      // Refresh predictions from server
      await fetchPredictions(matchdayId);
      return true;
    } catch {
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function fetchMoneylineTips(matchIds: string[]) {
    if (matchIds.length === 0) return;
    try {
      interface TipResponse {
        id: string;
        match_id: string;
        selection: { type: string; value: string };
        locked_odds: number;
        points_earned: number | null;
        status: string;
        created_at: string;
      }
      const tips = await api.get<TipResponse[]>("/tips/mine", {
        match_ids: matchIds.join(","),
      });
      const newMap = new Map<string, MoneylineTip>();
      for (const t of tips) {
        newMap.set(t.match_id, {
          id: t.id,
          match_id: t.match_id,
          selection: t.selection.value,
          locked_odds: t.locked_odds,
          points_earned: t.points_earned,
          status: t.status,
        });
      }
      moneylineTips.value = newMap;
    } catch {
      moneylineTips.value = new Map();
    }
  }

  async function submitMoneylineTip(
    matchId: string,
    prediction: string,
    displayedOdds: number
  ): Promise<boolean> {
    saving.value = true;
    try {
      interface TipResponse {
        id: string;
        match_id: string;
        selection: { type: string; value: string };
        locked_odds: number;
        points_earned: number | null;
        status: string;
        created_at: string;
      }
      const tip = await api.post<TipResponse>("/tips/", {
        match_id: matchId,
        prediction,
        displayed_odds: displayedOdds,
      });
      const newMap = new Map(moneylineTips.value);
      newMap.set(tip.match_id, {
        id: tip.id,
        match_id: tip.match_id,
        selection: tip.selection.value,
        locked_odds: tip.locked_odds,
        points_earned: tip.points_earned,
        status: tip.status,
      });
      moneylineTips.value = newMap;
      return true;
    } catch {
      return false;
    } finally {
      saving.value = false;
    }
  }

  function setSport(sport: string) {
    activeSport.value = sport;
    // Clear view state (caches are keyed separately and stay intact)
    currentMatchday.value = null;
    matches.value = [];
    predictions.value = null;
    draftPredictions.value = new Map();
    moneylineTips.value = new Map();
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
    moneylineTips.value = new Map();
    // Clear admin state
    adminTargetUserId.value = null;
    adminTargetPredictions.value = null;
  }

  // ---------- Admin actions ----------

  async function fetchSquadMembers(squadId: string) {
    try {
      squadMembers.value = await api.get<SquadMember[]>(
        `/spieltag/admin/members/${squadId}`
      );
    } catch {
      squadMembers.value = [];
    }
  }

  async function fetchAdminPredictions(
    matchdayId: string,
    squadId: string,
    userId: string
  ) {
    try {
      adminTargetPredictions.value =
        await api.get<SpieltagPrediction | null>(
          `/spieltag/admin/predictions/${matchdayId}`,
          { squad_id: squadId, user_id: userId }
        );
    } catch {
      adminTargetPredictions.value = null;
    }
  }

  async function adminUnlockMatch(
    squadId: string,
    matchdayId: string,
    userId: string,
    matchId: string
  ): Promise<boolean> {
    try {
      await api.post("/spieltag/admin/unlock", {
        squad_id: squadId,
        matchday_id: matchdayId,
        user_id: userId,
        match_id: matchId,
      });
      // Refresh the target user's predictions
      await fetchAdminPredictions(matchdayId, squadId, userId);
      return true;
    } catch {
      return false;
    }
  }

  async function adminSavePrediction(
    squadId: string,
    matchdayId: string,
    userId: string,
    matchId: string,
    homeScore: number,
    awayScore: number
  ): Promise<{ points_earned: number | null } | null> {
    try {
      const result = await api.post<{ points_earned: number | null }>(
        "/spieltag/admin/prediction",
        {
          squad_id: squadId,
          matchday_id: matchdayId,
          user_id: userId,
          match_id: matchId,
          home_score: homeScore,
          away_score: awayScore,
        }
      );
      // Refresh the target user's predictions
      await fetchAdminPredictions(matchdayId, squadId, userId);
      return result;
    } catch {
      return null;
    }
  }

  return {
    sports,
    matchdays,
    currentMatchday,
    matches,
    predictions,
    draftPredictions,
    draftAutoStrategy,
    loading,
    saving,
    activeSport,
    activeSquadId,
    editableMatches,
    lockedMatches,
    tippedCount,
    fetchSports,
    fetchMatchdays,
    fetchMatchdayDetail,
    previewCached,
    fetchPredictions,
    setDraftPrediction,
    removeDraftPrediction,
    savePredictions,
    moneylineTips,
    fetchMoneylineTips,
    submitMoneylineTip,
    setSport,
    setSquadContext,
    invalidateCache,
    // Admin
    squadMembers,
    adminTargetUserId,
    adminTargetPredictions,
    adminUnlockedSet,
    fetchSquadMembers,
    fetchAdminPredictions,
    adminUnlockMatch,
    adminSavePrediction,
  };
});
