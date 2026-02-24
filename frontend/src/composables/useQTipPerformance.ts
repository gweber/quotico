import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface QTipOverall {
  total_resolved: number;
  correct: number;
  win_rate: number;
  avg_confidence: number;
  avg_edge: number;
}

export interface QTipSportBreakdown {
  sport_key: string;
  total: number;
  correct: number;
  win_rate: number;
  avg_confidence: number;
  avg_edge: number;
}

export interface QTipConfidenceBand {
  bucket: string;
  total: number;
  correct: number;
  win_rate: number;
  avg_confidence: number;
}

export interface QTipSignalBreakdown {
  signal: string;
  total: number;
  correct: number;
  win_rate: number;
}

export interface QTipResolvedTip {
  match_id: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string;
  recommended_selection: string;
  actual_result: string;
  was_correct: boolean;
  confidence: number;
  edge_pct: number;
}

export interface QTipPerformanceData {
  overall: QTipOverall;
  by_sport: QTipSportBreakdown[];
  by_confidence: QTipConfidenceBand[];
  by_signal: QTipSignalBreakdown[];
  recent_tips: QTipResolvedTip[];
}

export function useQTipPerformance() {
  const api = useApi();
  const data: Ref<QTipPerformanceData | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch(sportKey?: string) {
    loading.value = true;
    error.value = false;
    try {
      const url = sportKey
        ? `/quotico-tips/public-performance?sport_key=${encodeURIComponent(sportKey)}`
        : "/quotico-tips/public-performance";
      data.value = await api.get<QTipPerformanceData>(url);
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
