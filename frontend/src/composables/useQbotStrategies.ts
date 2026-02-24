import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface StressTestResult {
  bootstrap_p_positive: number;
  bootstrap_ci_95: [number, number];
  bootstrap_mean_roi: number;
  monte_carlo_ruin_prob: number;
  monte_carlo_max_dd_median: number;
  monte_carlo_max_dd_95: number;
  ensemble_size: number;
  stress_passed: boolean;
}

export interface QbotStrategy {
  id: string;
  sport_key: string;
  version: string;
  generation: number;
  dna: Record<string, number>;
  training_fitness: {
    roi: number;
    sharpe: number;
    win_rate: number;
    total_bets: number;
    max_drawdown_pct: number;
  };
  validation_fitness: {
    roi: number;
    sharpe: number;
    win_rate: number;
    total_bets: number;
    max_drawdown_pct: number;
  };
  stress_test?: StressTestResult;
  is_active: boolean;
  created_at: string;
  age_days: number;
  overfit_warning: boolean;
}

export interface QbotStrategiesResponse {
  strategies: QbotStrategy[];
  gene_ranges: Record<string, [number, number]>;
  summary: {
    total_active: number;
    avg_val_roi: number;
    worst_league: string;
    worst_roi: number;
    oldest_strategy_days: number;
    all_stress_passed: boolean;
  };
}

export function useQbotStrategies() {
  const api = useApi();
  const data: Ref<QbotStrategiesResponse | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch() {
    loading.value = true;
    error.value = false;
    try {
      data.value = await api.get<QbotStrategiesResponse>(
        "/admin/qbot/strategies",
      );
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
