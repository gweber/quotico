import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "@/composables/useApi";

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
  result: string | null;
  home_score: number | null;
  away_score: number | null;
  is_locked: boolean;
}

export interface Prediction {
  match_id: string;
  home_score: number;
  away_score: number;
  is_auto: boolean;
  points_earned: number | null;
}

export interface SpieltagPrediction {
  matchday_id: string;
  squad_id: string | null;
  auto_tipp_strategy: string;
  predictions: Prediction[];
  total_points: number | null;
  status: string;
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

  const loading = ref(false);
  const saving = ref(false);
  const activeSport = ref<string>("soccer_germany_bundesliga");
  const activeSquadId = ref<string | null>(null);

  // Computed
  const editableMatches = computed(() =>
    matches.value.filter((m) => !m.is_locked)
  );

  const lockedMatches = computed(() =>
    matches.value.filter((m) => m.is_locked)
  );

  const tippedCount = computed(() => draftPredictions.value.size);

  // Actions
  async function fetchSports() {
    try {
      sports.value = await api.get<SpieltagSport[]>("/spieltag/sports");
    } catch {
      sports.value = [];
    }
  }

  async function fetchMatchdays(sport?: string) {
    const sportKey = sport || activeSport.value;
    loading.value = true;
    try {
      matchdays.value = await api.get<Matchday[]>("/spieltag/matchdays", {
        sport: sportKey,
      });
    } catch {
      matchdays.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function fetchMatchdayDetail(matchdayId: string) {
    loading.value = true;
    try {
      const data = await api.get<{
        matchday: Matchday;
        matches: SpieltagMatch[];
      }>(`/spieltag/matchdays/${matchdayId}`);
      currentMatchday.value = data.matchday;
      matches.value = data.matches;
    } catch {
      currentMatchday.value = null;
      matches.value = [];
    } finally {
      loading.value = false;
    }
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

  function setSport(sport: string) {
    activeSport.value = sport;
    matchdays.value = [];
    currentMatchday.value = null;
    matches.value = [];
    predictions.value = null;
    draftPredictions.value = new Map();
  }

  function setSquadContext(squadId: string | null) {
    activeSquadId.value = squadId;
    // Clear predictions when squad changes (different squad = different predictions)
    predictions.value = null;
    draftPredictions.value = new Map();
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
    fetchPredictions,
    setDraftPrediction,
    removeDraftPrediction,
    savePredictions,
    setSport,
    setSquadContext,
  };
});
