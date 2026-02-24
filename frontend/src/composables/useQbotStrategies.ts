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

export type QbotStrategyCategory = "active" | "shadow" | "failed";
export type QbotStrategyArchetype =
  | "consensus"
  | "profit_hunter"
  | "volume_grinder"
  | "standard";

export interface QbotStrategyIdentity {
  id: string;
  archetype: QbotStrategyArchetype | string;
  version: string;
  generation: number;
  is_active: boolean;
  is_shadow?: boolean;
  category?: QbotStrategyCategory;
  roi: number;
  total_bets: number;
  created_at: string;
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
  is_shadow?: boolean;
  created_at: string;
  age_days: number;
  overfit_warning: boolean;
  category?: QbotStrategyCategory;
  stage_used?: number;
  stage_label?: string;
  rescue_applied?: boolean;
  rescue_scale?: number;
  rescue_label?: string;
  archetype?: QbotStrategyArchetype | string;
  identities?: Record<string, QbotStrategyIdentity>;
  active_comparison?: {
    active_id: string;
    roi_diff: number;
    bets_diff: number;
    sharpe_diff: number;
  } | null;
}

export interface QbotStrategiesResponse {
  strategies: QbotStrategy[];
  categories?: {
    active: QbotStrategy[];
    shadow: QbotStrategy[];
    failed: QbotStrategy[];
    archived?: QbotStrategy[];
  };
  by_sport?: Record<
    string,
    {
      strategy: QbotStrategy;
      category: QbotStrategyCategory;
      identities?: Record<string, QbotStrategyIdentity>;
    }
  >;
  gene_ranges: Record<string, [number, number]>;
  summary: {
    total_active: number;
    avg_val_roi: number;
    portfolio_avg_roi?: number;
    count_active?: number;
    count_shadow?: number;
    count_failed?: number;
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
