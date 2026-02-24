import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface OddsSnapshot {
  snapshot_at: string;
  odds: Record<string, number>;
  totals?: Record<string, number>;
}

export interface OddsTimelineResponse {
  match_id: string;
  snapshots: OddsSnapshot[];
  snapshot_count: number;
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
        `/matches/${matchId}/odds-timeline`,
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
