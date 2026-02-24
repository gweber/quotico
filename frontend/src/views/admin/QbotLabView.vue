<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { sportLabel as canonicalSportLabel } from "@/types/sports";
import { useApi } from "@/composables/useApi";
import {
  useQbotStrategies,
  type QbotStrategy,
  type QbotStrategyIdentity,
} from "@/composables/useQbotStrategies";
import { Line } from "vue-chartjs";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend);

const { t } = useI18n();
const api = useApi();
const router = useRouter();
const { data, loading, error, fetch: fetchStrategies } = useQbotStrategies();

const expandedId = ref<string | null>(null);
const activeTab = ref<"active" | "shadow" | "archived">("active");
const activatingId = ref<string | null>(null);
const backtestLoading = ref<Record<string, boolean>>({});
const backtestError = ref<Record<string, boolean>>({});
const backtestData = ref<Record<string, { points: Array<{ date: string; bankroll: number; is_win: boolean; stake: number }>; ending_bankroll: number; total_bets: number; win_rate: number }>>({});

function toggleExpand(id: string) {
  expandedId.value = expandedId.value === id ? null : id;
  if (expandedId.value === id) {
    void loadBacktest(id);
  }
}

function leagueLabel(key: string): string {
  if (key === "all") return "All (Fallback)";
  return canonicalSportLabel(key);
}

function penultimateSeasonStartIso(): string {
  const now = new Date();
  const year = now.getUTCFullYear();
  const month = now.getUTCMonth() + 1;
  const currentSeasonStartYear = month >= 8 ? year : year - 1;
  const penultimateStartYear = currentSeasonStartYear - 1;
  return `${penultimateStartYear}-08-01T00:00:00+00:00`;
}

async function loadBacktest(id: string): Promise<void> {
  if (backtestData.value[id] || backtestLoading.value[id]) return;
  backtestLoading.value = { ...backtestLoading.value, [id]: true };
  backtestError.value = { ...backtestError.value, [id]: false };
  try {
    const sinceParam = `since_date=${encodeURIComponent(penultimateSeasonStartIso())}`;
    const result = await api.get<{
      ending_bankroll: number;
      total_bets: number;
      win_rate: number;
      points: Array<{ date: string; bankroll: number; is_win: boolean; stake: number }>;
    }>(`/admin/qbot/strategies/${encodeURIComponent(id)}/backtest?${sinceParam}`);
    backtestData.value = { ...backtestData.value, [id]: result };
  } catch {
    backtestError.value = { ...backtestError.value, [id]: true };
  } finally {
    backtestLoading.value = { ...backtestLoading.value, [id]: false };
  }
}

async function activateStrategy(id: string): Promise<void> {
  if (!id || activatingId.value) return;
  activatingId.value = id;
  try {
    await api.post(`/admin/qbot/strategies/${encodeURIComponent(id)}/activate`);
    await fetchStrategies();
  } finally {
    activatingId.value = null;
  }
}

function identityLabel(key: string): string {
  if (key === "consensus") return t("qbotLab.identityConsensus");
  if (key === "profit_hunter") return t("qbotLab.identityProfitHunter");
  if (key === "volume_grinder") return t("qbotLab.identityVolumeGrinder");
  return key;
}

function identityList(s: QbotStrategy): Array<{ key: string; value: QbotStrategyIdentity }> {
  if (!s.identities) return [];
  const ordered = ["consensus", "profit_hunter", "volume_grinder"];
  return ordered
    .filter((k) => s.identities && s.identities[k])
    .map((k) => ({ key: k, value: s.identities![k] }));
}

function goToStrategyDetail(id: string): void {
  if (!id) return;
  void router.push({ name: "admin-qbot-lab-detail", params: { strategyId: id } });
}

function backtestChartData(id: string) {
  const bt = backtestData.value[id];
  if (!bt || bt.points.length === 0) return null;
  return {
    labels: bt.points.map((p) => new Date(p.date).toLocaleDateString("de-DE")),
    datasets: [
      {
        label: t("qbotLab.bankrollCurve"),
        data: bt.points.map((p) => Number(p.bankroll.toFixed(2))),
        borderColor: "rgb(16, 185, 129)",
        backgroundColor: "rgba(16, 185, 129, 0.15)",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.2,
      },
    ],
  };
}

const backtestChartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
  },
  scales: {
    y: {
      ticks: { color: "rgba(255,255,255,0.6)" },
      grid: { color: "rgba(255,255,255,0.08)" },
    },
    x: {
      ticks: { color: "rgba(255,255,255,0.45)", maxTicksLimit: 8 },
      grid: { display: false },
    },
  },
};

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

const activeStrategies = computed(() => {
  if (!data.value) return [];
  const list = data.value.categories?.active ?? data.value.strategies ?? [];
  return [...list].sort((a, b) => {
    if (a.sport_key === "all") return 1;
    if (b.sport_key === "all") return -1;
    return b.validation_fitness.roi - a.validation_fitness.roi;
  });
});

const shadowStrategies = computed(() => {
  if (!data.value) return [];
  const list = data.value.categories?.shadow ?? [];
  return [...list].sort((a, b) => b.validation_fitness.roi - a.validation_fitness.roi);
});

const archivedStrategies = computed(() => {
  if (!data.value) return [];
  const list = data.value.categories?.archived ?? data.value.categories?.failed ?? [];
  return [...list].sort((a, b) => {
    const aRuin = a.stress_test?.monte_carlo_ruin_prob ?? 0;
    const bRuin = b.stress_test?.monte_carlo_ruin_prob ?? 0;
    if (bRuin !== aRuin) return bRuin - aRuin;
    return a.validation_fitness.roi - b.validation_fitness.roi;
  });
});

const visibleStrategies = computed(() => {
  if (activeTab.value === "active") return activeStrategies.value;
  if (activeTab.value === "shadow") return shadowStrategies.value;
  return archivedStrategies.value;
});

const tabTitle = computed(() => {
  if (activeTab.value === "active") return t("qbotLab.tabActive");
  if (activeTab.value === "shadow") return t("qbotLab.tabShadow");
  return t("qbotLab.tabArchived");
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
            {{ data.summary.count_active ?? data.summary.total_active }}
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.portfolioRoi") }}</p>
          <p
            class="text-2xl font-bold tabular-nums"
            :class="
              (data.summary.portfolio_avg_roi ?? data.summary.avg_val_roi) >= 0
                ? 'text-emerald-400'
                : 'text-danger'
            "
          >
            {{
              (
                (data.summary.portfolio_avg_roi ?? data.summary.avg_val_roi) * 100
              ).toFixed(1)
            }}%
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.tabShadow") }}</p>
          <p class="text-2xl font-bold text-amber-400 tabular-nums">
            {{ data.summary.count_shadow ?? 0 }}
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("qbotLab.tabArchived") }}</p>
          <p class="text-2xl font-bold text-danger tabular-nums">
            {{ data.summary.count_failed ?? 0 }}
          </p>
        </div>
      </div>

      <div class="mb-4 flex gap-2">
        <button
          class="px-3 py-1.5 text-xs rounded-lg border transition-colors"
          :class="
            activeTab === 'active'
              ? 'bg-emerald-500/20 border-emerald-400 text-emerald-300'
              : 'border-surface-3 text-text-secondary hover:bg-surface-2'
          "
          @click="activeTab = 'active'"
        >
          {{ t("qbotLab.tabActive") }} ({{ activeStrategies.length }})
        </button>
        <button
          class="px-3 py-1.5 text-xs rounded-lg border transition-colors"
          :class="
            activeTab === 'shadow'
              ? 'bg-amber-500/20 border-amber-400 text-amber-300'
              : 'border-surface-3 text-text-secondary hover:bg-surface-2'
          "
          @click="activeTab = 'shadow'"
        >
          {{ t("qbotLab.tabShadow") }} ({{ shadowStrategies.length }})
        </button>
        <button
          class="px-3 py-1.5 text-xs rounded-lg border transition-colors"
          :class="
            activeTab === 'archived'
              ? 'bg-danger/20 border-danger text-danger'
              : 'border-surface-3 text-text-secondary hover:bg-surface-2'
          "
          @click="activeTab = 'archived'"
        >
          {{ t("qbotLab.tabArchived") }} ({{ archivedStrategies.length }})
        </button>
      </div>

      <!-- Strategy Table -->
      <div
        class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden"
      >
        <div class="px-4 py-3 border-b border-surface-3/50">
          <h2 class="text-sm font-semibold text-text-primary">
            {{ tabTitle }}
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
              <template v-for="s in visibleStrategies" :key="s.id">
                <tr
                  class="border-b border-surface-3/20 last:border-0 cursor-pointer hover:bg-surface-2/50 transition-colors"
                  @click="toggleExpand(s.id)"
                >
                  <td class="px-4 py-2.5">
                    <div class="flex items-center gap-1.5 flex-wrap">
                      <span class="text-text-primary font-medium">
                        {{ leagueLabel(s.sport_key) }}
                      </span>
                      <span
                        v-if="s.overfit_warning"
                        class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-400"
                      >
                        {{ t("qbotLab.overfit") }}
                      </span>
                    </div>
                    <div
                      v-if="identityList(s).length > 0"
                      class="mt-1.5 flex flex-wrap gap-1.5"
                    >
                      <button
                        v-for="item in identityList(s)"
                        :key="`${s.id}-${item.key}`"
                        class="rounded-md border border-surface-3/60 px-2 py-1 text-[10px] leading-tight text-left hover:bg-surface-2/60 transition-colors"
                        @click.stop="goToStrategyDetail(item.value.id)"
                      >
                        <div class="text-text-muted">
                          {{ identityLabel(item.key) }}
                        </div>
                        <div
                          class="font-mono tabular-nums"
                          :class="
                            item.value.roi >= 0 ? 'text-emerald-400' : 'text-danger'
                          "
                        >
                          {{ (item.value.roi * 100).toFixed(1) }}%
                        </div>
                      </button>
                    </div>
                    <div
                      v-if="s.active_comparison"
                      class="mt-1 text-[10px] text-text-muted"
                    >
                      {{ t("qbotLab.vsActiveShort") }}:
                      <span
                        :class="s.active_comparison.roi_diff >= 0 ? 'text-emerald-400' : 'text-danger'"
                      >
                        {{ s.active_comparison.roi_diff >= 0 ? "+" : "" }}{{ (s.active_comparison.roi_diff * 100).toFixed(1) }}%
                      </span>
                      {{ t("qbotLab.roiShort") }} ·
                      <span
                        :class="s.active_comparison.bets_diff >= 0 ? 'text-emerald-400' : 'text-danger'"
                      >
                        {{ s.active_comparison.bets_diff >= 0 ? "+" : "" }}{{ s.active_comparison.bets_diff }}
                      </span>
                      {{ t("qbotLab.betsShort") }}
                    </div>
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
                        <p class="text-xs text-text-muted mb-3">
                          {{ s.stage_label || "--" }} · {{ s.rescue_label || "--" }}
                        </p>
                        <router-link
                          :to="{ name: 'admin-qbot-lab-detail', params: { strategyId: s.id } }"
                          class="inline-block text-xs text-primary hover:underline mb-3"
                        >
                          {{ t("qbotLab.openDetail") }}
                        </router-link>
                        <div
                          v-if="identityList(s).length > 0"
                          class="grid grid-cols-1 md:grid-cols-3 gap-2 mb-3"
                        >
                          <div
                            v-for="item in identityList(s)"
                            :key="`${s.id}-detail-${item.key}`"
                            class="rounded-lg border border-surface-3/40 bg-surface-1 p-2"
                          >
                            <div class="flex items-center justify-between mb-1">
                              <span class="text-[11px] text-text-muted">
                                {{ identityLabel(item.key) }}
                              </span>
                              <span
                                v-if="item.value.is_active"
                                class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400"
                              >
                                {{ t("qbotLab.identityActive") }}
                              </span>
                            </div>
                            <div class="text-xs font-mono tabular-nums mb-2">
                              ROI {{ (item.value.roi * 100).toFixed(1) }}% · Bets {{ item.value.total_bets }}
                            </div>
                            <button
                              v-if="!item.value.is_active"
                              class="w-full px-2 py-1 text-[11px] rounded border border-surface-3 hover:bg-surface-2 text-text-secondary"
                              :disabled="activatingId === item.value.id"
                              @click.stop="activateStrategy(item.value.id)"
                            >
                              {{
                                activatingId === item.value.id
                                  ? t("qbotLab.activating")
                                  : t("qbotLab.setActive")
                              }}
                            </button>
                          </div>
                        </div>
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

                      <div
                        class="bg-surface-1 rounded-lg p-3 border border-surface-3/30 text-xs"
                      >
                        <p
                          class="text-text-muted font-medium mb-2 uppercase tracking-wide"
                        >
                          {{ t("qbotLab.backtest") }}
                        </p>
                        <div v-if="backtestLoading[s.id]" class="text-text-muted">
                          {{ t("qbotLab.loadingBacktest") }}
                        </div>
                        <div v-else-if="backtestError[s.id]" class="text-danger">
                          {{ t("qbotLab.backtestLoadError") }}
                        </div>
                        <div v-else-if="backtestData[s.id]" class="space-y-3">
                          <div class="grid grid-cols-1 md:grid-cols-3 gap-2">
                            <div class="bg-surface-2 rounded p-2">
                              <div class="text-text-muted">{{ t("qbotLab.startBankroll") }}</div>
                              <div class="font-mono text-text-primary">1000.00</div>
                            </div>
                            <div class="bg-surface-2 rounded p-2">
                              <div class="text-text-muted">{{ t("qbotLab.endBankroll") }}</div>
                              <div class="font-mono text-text-primary">
                                {{ backtestData[s.id].ending_bankroll.toFixed(2) }}
                              </div>
                            </div>
                            <div class="bg-surface-2 rounded p-2">
                              <div class="text-text-muted">{{ t("qbotLab.backtestWinRate") }}</div>
                              <div class="font-mono text-text-primary">
                                {{ (backtestData[s.id].win_rate * 100).toFixed(1) }}%
                              </div>
                            </div>
                          </div>
                          <div class="h-48">
                            <Line
                              v-if="backtestChartData(s.id)"
                              :data="backtestChartData(s.id)!"
                              :options="backtestChartOptions"
                            />
                          </div>
                        </div>
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
          v-if="visibleStrategies.length === 0"
          class="px-4 py-8 text-center text-text-muted text-sm"
        >
          {{ t("qbotLab.noStrategiesInTab") }}
        </div>
      </div>
    </template>
  </div>
</template>
