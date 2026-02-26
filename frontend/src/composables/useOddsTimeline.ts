import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface OddsSnapshot {
  timestamp: string;
  home: number;
  draw: number;
  away: number;
  source?: string;
}

export interface OddsTimelineResponse {
  match_id: number;
  items: OddsSnapshot[];
}

// Shared cache keyed by match_id
const cache = new Map<string, OddsTimelineResponse>();

export function useOddsTimeline() {
  const api = useApi();
  const data: Ref<OddsTimelineResponse | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetchForMatch(matchId: string) {
    const cached = cache.get(matchId);
    if (cached) {
      data.value = cached;
      return;
    }

    loading.value = true;
    error.value = false;

    try {
      const result = await api.get<OddsTimelineResponse>(
        `/v3/matches/${matchId}/odds-timeline`,
      );
      data.value = result;
      cache.set(matchId, result);
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetchForMatch };
}
