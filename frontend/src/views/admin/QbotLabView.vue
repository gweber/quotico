<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import {
  useQbotStrategies,
  type QbotStrategy,
} from "@/composables/useQbotStrategies";

const { t } = useI18n();
const { data, loading, error, fetch: fetchStrategies } = useQbotStrategies();

const expandedId = ref<string | null>(null);

function toggleExpand(id: string) {
  expandedId.value = expandedId.value === id ? null : id;
}

function leagueLabel(key: string): string {
  const map: Record<string, string> = {
    all: "All (Fallback)",
    soccer_germany_bundesliga: "Bundesliga",
    soccer_epl: "Premier League",
    soccer_spain_la_liga: "La Liga",
    soccer_italy_serie_a: "Serie A",
    soccer_france_ligue_one: "Ligue 1",
  };
  return map[key] ?? key;
}

function pctOf(value: number, range: [number, number]): number {
  const [lo, hi] = range;
  if (hi === lo) return 50;
  return Math.round(((value - lo) / (hi - lo)) * 100);
}

function statusClass(s: QbotStrategy): string {
  if (s.validation_fitness.roi < 0) return "text-danger";
  if (s.stress_test && !s.stress_test.stress_passed) return "text-danger";
  if (s.age_days > 21 || s.overfit_warning) return "text-amber-400";
  return "text-emerald-400";
}

function statusLabel(s: QbotStrategy): string {
  if (s.validation_fitness.roi < 0) return "negative";
  if (s.stress_test && !s.stress_test.stress_passed)
    return t("qbotLab.stressFailed");
  if (s.age_days > 21) return t("qbotLab.stale");
  if (s.overfit_warning) return t("qbotLab.overfit");
  return "OK";
}

const sortedStrategies = computed(() => {
  if (!data.value) return [];
  return [...data.value.strategies].sort((a, b) => {
    if (a.sport_key === "all") return 1;
    if (b.sport_key === "all") return -1;
    return b.validation_fitness.roi - a.validation_fitness.roi;
  });
});

onMounted(fetchStrategies);
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <!-- Header -->
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-text-primary">
        {{ t("qbotLab.title") }}
      </h1>
      <button
        class="px-3 py-1.5 text-sm rounded-lg border border-surface-3 text-text-secondary hover:bg-surface-2 transition-colors"
        :disabled="loading"
        @click="fetchStrategies"
      >
        {{ loading ? "..." : "Refresh" }}
      </button>
    </div>

    <!-- Loading -->
    <div v-if="loading && !data" class="space-y-4">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div
          v-for="n in 4"
          :key="n"
          class="bg-surface-1 rounded-card h-24 animate-pulse"
        />
      </div>
      <div class="bg-surface-1 rounded-card h-64 animate-pulse" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading strategies.</p>
      <button
        class="text-sm text-primary hover:underline"
        @click="fetchStrategies"
      >
        Try again
      </button>
    </div>

    <template v-if="data">
      <!-- Hero Stats -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">
            {{ t("qbotLab.activeStrategies") }}
          </p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">
            {{ data.summary.total_active }}
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.avgRoi") }}</p>
          <p
            class="text-2xl font-bold tabular-nums"
            :class="
              data.summary.avg_val_roi >= 0
                ? 'text-emerald-400'
                : 'text-danger'
            "
          >
            {{ (data.summary.avg_val_roi * 100).toFixed(1) }}%
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.worstLeague") }}</p>
          <p class="text-sm font-bold text-text-primary">
            {{ leagueLabel(data.summary.worst_league) }}
          </p>
          <p
            class="text-xs tabular-nums"
            :class="
              data.summary.worst_roi < 0.05
                ? 'text-amber-400'
                : 'text-text-muted'
            "
          >
            {{ (data.summary.worst_roi * 100).toFixed(1) }}%
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.stressTest") }}</p>
          <p
            class="text-sm font-bold"
            :class="
              data.summary.all_stress_passed
                ? 'text-emerald-400'
                : 'text-danger'
            "
          >
            {{
              data.summary.all_stress_passed
                ? t("qbotLab.allPassed")
                : t("qbotLab.stressFailed")
            }}
          </p>
        </div>
      </div>

      <!-- Strategy Table -->
      <div
        class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden"
      >
        <div class="px-4 py-3 border-b border-surface-3/50">
          <h2 class="text-sm font-semibold text-text-primary">
            {{ t("qbotLab.activeStrategies") }}
          </h2>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr
                class="text-left text-xs text-text-muted border-b border-surface-3/30"
              >
                <th class="px-4 py-2 font-medium">League</th>
                <th class="px-4 py-2 font-medium text-right">Gen</th>
                <th class="px-4 py-2 font-medium text-right">ROI</th>
                <th class="px-4 py-2 font-medium text-right">Sharpe</th>
                <th class="px-4 py-2 font-medium text-right">Win Rate</th>
                <th class="px-4 py-2 font-medium text-right">Bets</th>
                <th class="px-4 py-2 font-medium text-right">Ruin%</th>
                <th class="px-4 py-2 font-medium text-right">Age</th>
                <th class="px-4 py-2 font-medium text-right">Status</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="s in sortedStrategies" :key="s.id">
                <tr
                  class="border-b border-surface-3/20 last:border-0 cursor-pointer hover:bg-surface-2/50 transition-colors"
                  @click="toggleExpand(s.id)"
                >
                  <td class="px-4 py-2.5">
                    <span class="text-text-primary font-medium">
                      {{ leagueLabel(s.sport_key) }}
                    </span>
                    <span
                      v-if="s.overfit_warning"
                      class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-400"
                    >
                      {{ t("qbotLab.overfit") }}
                    </span>
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums text-text-muted"
                  >
                    {{ s.generation }}
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums font-medium"
                    :class="
                      s.validation_fitness.roi >= 0
                        ? 'text-emerald-400'
                        : 'text-danger'
                    "
                  >
                    {{ (s.validation_fitness.roi * 100).toFixed(1) }}%
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums text-text-secondary"
                  >
                    {{ s.validation_fitness.sharpe.toFixed(2) }}
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums text-text-secondary"
                  >
                    {{ (s.validation_fitness.win_rate * 100).toFixed(1) }}%
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums text-text-muted"
                  >
                    {{ s.validation_fitness.total_bets }}
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums"
                    :class="
                      s.stress_test && s.stress_test.monte_carlo_ruin_prob > 0.05
                        ? 'text-danger'
                        : 'text-text-muted'
                    "
                  >
                    {{
                      s.stress_test
                        ? (s.stress_test.monte_carlo_ruin_prob * 100).toFixed(1) + "%"
                        : "--"
                    }}
                  </td>
                  <td
                    class="px-4 py-2.5 text-right font-mono tabular-nums text-text-muted"
                  >
                    {{ s.age_days }}d
                  </td>
                  <td class="px-4 py-2.5 text-right">
                    <span
                      class="text-xs font-medium"
                      :class="statusClass(s)"
                    >
                      {{ statusLabel(s) }}
                    </span>
                  </td>
                </tr>

                <!-- Expanded Detail -->
                <tr v-if="expandedId === s.id">
                  <td colspan="9" class="bg-surface-0/50 px-4 py-4">
                    <div class="space-y-4">
                      <!-- DNA Bars -->
                      <div>
                        <h3
                          class="text-xs font-semibold text-text-muted uppercase tracking-wide mb-3"
                        >
                          {{ t("qbotLab.dnaDetail") }} — {{ leagueLabel(s.sport_key) }}
                          (Gen {{ s.generation }}, {{ s.version }})
                        </h3>
                        <div class="space-y-1.5">
                          <div
                            v-for="(val, gene) in s.dna"
                            :key="gene"
                            class="flex items-center gap-3 text-xs"
                          >
                            <span
                              class="w-32 text-text-muted text-right shrink-0 font-mono"
                            >
                              {{ gene }}
                            </span>
                            <div
                              class="flex-1 h-4 bg-surface-2 rounded overflow-hidden"
                            >
                              <div
                                class="h-full bg-indigo-500/70 rounded transition-all"
                                :style="{
                                  width:
                                    (data?.gene_ranges[gene]
                                      ? pctOf(val, data.gene_ranges[gene])
                                      : 50) + '%',
                                }"
                              />
                            </div>
                            <span
                              class="w-16 text-right font-mono tabular-nums text-text-secondary"
                            >
                              {{
                                val >= 10
                                  ? val.toFixed(1)
                                  : val >= 1
                                    ? val.toFixed(2)
                                    : val.toFixed(3)
                              }}
                            </span>
                          </div>
                        </div>
                      </div>

                      <!-- Training vs Validation -->
                      <div
                        class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs"
                      >
                        <div
                          class="bg-surface-1 rounded-lg p-3 border border-surface-3/30"
                        >
                          <p
                            class="text-text-muted font-medium mb-1 uppercase tracking-wide"
                          >
                            {{ t("qbotLab.training") }}
                          </p>
                          <div
                            class="grid grid-cols-2 gap-y-1 font-mono tabular-nums"
                          >
                            <span class="text-text-muted">ROI</span>
                            <span class="text-right text-text-primary">
                              {{
                                (s.training_fitness.roi * 100).toFixed(1)
                              }}%
                            </span>
                            <span class="text-text-muted">Sharpe</span>
                            <span class="text-right text-text-primary">
                              {{ s.training_fitness.sharpe.toFixed(2) }}
                            </span>
                            <span class="text-text-muted">Win Rate</span>
                            <span class="text-right text-text-primary">
                              {{
                                (s.training_fitness.win_rate * 100).toFixed(1)
                              }}%
                            </span>
                            <span class="text-text-muted">Bets</span>
                            <span class="text-right text-text-primary">
                              {{ s.training_fitness.total_bets }}
                            </span>
                            <span class="text-text-muted">Max DD</span>
                            <span class="text-right text-text-primary">
                              {{
                                (
                                  s.training_fitness.max_drawdown_pct * 100
                                ).toFixed(1)
                              }}%
                            </span>
                          </div>
                        </div>
                        <div
                          class="bg-surface-1 rounded-lg p-3 border border-surface-3/30"
                        >
                          <p
                            class="text-text-muted font-medium mb-1 uppercase tracking-wide"
                          >
                            {{ t("qbotLab.validation") }}
                          </p>
                          <div
                            class="grid grid-cols-2 gap-y-1 font-mono tabular-nums"
                          >
                            <span class="text-text-muted">ROI</span>
                            <span
                              class="text-right"
                              :class="
                                s.validation_fitness.roi >= 0
                                  ? 'text-emerald-400'
                                  : 'text-danger'
                              "
                            >
                              {{
                                (s.validation_fitness.roi * 100).toFixed(1)
                              }}%
                            </span>
                            <span class="text-text-muted">Sharpe</span>
                            <span class="text-right text-text-primary">
                              {{ s.validation_fitness.sharpe.toFixed(2) }}
                            </span>
                            <span class="text-text-muted">Win Rate</span>
                            <span class="text-right text-text-primary">
                              {{
                                (s.validation_fitness.win_rate * 100).toFixed(1)
                              }}%
                            </span>
                            <span class="text-text-muted">Bets</span>
                            <span class="text-right text-text-primary">
                              {{ s.validation_fitness.total_bets }}
                            </span>
                            <span class="text-text-muted">Max DD</span>
                            <span class="text-right text-text-primary">
                              {{
                                (
                                  s.validation_fitness.max_drawdown_pct * 100
                                ).toFixed(1)
                              }}%
                            </span>
                          </div>
                        </div>
                      </div>

                      <!-- Stress Test -->
                      <div
                        v-if="s.stress_test"
                        class="bg-surface-1 rounded-lg p-3 border border-surface-3/30 text-xs"
                      >
                        <p
                          class="text-text-muted font-medium mb-2 uppercase tracking-wide"
                        >
                          {{ t("qbotLab.stressTest") }}
                        </p>
                        <div class="space-y-1.5 font-mono tabular-nums">
                          <div class="flex justify-between">
                            <span class="text-text-muted">
                              {{ t("qbotLab.bootstrap") }}
                            </span>
                            <span class="text-text-primary">
                              ROI
                              {{
                                (s.stress_test.bootstrap_mean_roi * 100).toFixed(
                                  1,
                                )
                              }}%
                              [{{
                                (s.stress_test.bootstrap_ci_95[0] * 100).toFixed(
                                  1,
                                )
                              }}%,
                              {{
                                (s.stress_test.bootstrap_ci_95[1] * 100).toFixed(
                                  1,
                                )
                              }}%]
                              | p(+)={{ (s.stress_test.bootstrap_p_positive * 100).toFixed(0) }}%
                            </span>
                          </div>
                          <div class="flex justify-between">
                            <span class="text-text-muted">Monte Carlo</span>
                            <span class="text-text-primary">
                              {{ t("qbotLab.ruinRisk") }}
                              {{
                                (
                                  s.stress_test.monte_carlo_ruin_prob * 100
                                ).toFixed(1)
                              }}%
                              | Max DD median
                              {{
                                (
                                  s.stress_test.monte_carlo_max_dd_median * 100
                                ).toFixed(0)
                              }}%
                              | 95th
                              {{
                                (
                                  s.stress_test.monte_carlo_max_dd_95 * 100
                                ).toFixed(0)
                              }}%
                            </span>
                          </div>
                          <div class="flex justify-between">
                            <span class="text-text-muted">
                              {{ t("qbotLab.ensembleSize") }}
                            </span>
                            <span
                              :class="
                                s.stress_test.stress_passed
                                  ? 'text-emerald-400'
                                  : 'text-danger'
                              "
                            >
                              {{ s.stress_test.ensemble_size }} bots —
                              {{
                                s.stress_test.stress_passed
                                  ? t("qbotLab.stressPassed")
                                  : t("qbotLab.stressFailed")
                              }}
                            </span>
                          </div>
                        </div>
                      </div>
                      <div
                        v-else
                        class="text-xs text-text-muted italic"
                      >
                        No stress test data available.
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <!-- Empty state -->
        <div
          v-if="sortedStrategies.length === 0"
          class="px-4 py-8 text-center text-text-muted text-sm"
        >
          No active strategies. Run the Qbot Evolution Arena to generate
          strategies.
        </div>
      </div>
    </template>
  </div>
</template>
