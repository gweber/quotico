<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useQTipPerformance } from "@/composables/useQTipPerformance";
import { sportLabel } from "@/types/sports";
import { Bar } from "vue-chartjs";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Tooltip,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip);

const { t } = useI18n();
const { data, loading, error, fetch: fetchPerf } = useQTipPerformance();

onMounted(() => fetchPerf());

const hasData = computed(() => data.value && data.value.overall.total_resolved > 0);

const winPct = computed(() =>
  hasData.value ? (data.value!.overall.win_rate * 100).toFixed(1) : "0",
);
const lossPct = computed(() =>
  hasData.value ? ((1 - data.value!.overall.win_rate) * 100).toFixed(1) : "0",
);

// Confidence chart data
const confidenceChartData = computed(() => {
  if (!data.value) return null;
  const bands = data.value.by_confidence;
  return {
    labels: bands.map((b) => b.bucket),
    datasets: [
      {
        label: t("qtipPerformance.avgConfidence"),
        data: bands.map((b) => +(b.avg_confidence * 100).toFixed(1)),
        backgroundColor: "rgba(99, 102, 241, 0.5)",
        borderColor: "rgb(99, 102, 241)",
        borderWidth: 1,
      },
      {
        label: t("qtipPerformance.winRate"),
        data: bands.map((b) => +(b.win_rate * 100).toFixed(1)),
        backgroundColor: "rgba(16, 185, 129, 0.5)",
        borderColor: "rgb(16, 185, 129)",
        borderWidth: 1,
      },
    ],
  };
});

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {
    y: {
      beginAtZero: true,
      max: 100,
      ticks: { color: "rgba(255,255,255,0.5)", callback: (v: string | number) => `${v}%` },
      grid: { color: "rgba(255,255,255,0.06)" },
    },
    x: {
      ticks: { color: "rgba(255,255,255,0.5)" },
      grid: { display: false },
    },
  },
};

const signalColors: Record<string, { bg: string; text: string; letter: string }> = {
  poisson: { bg: "bg-blue-500/10", text: "text-blue-400", letter: "P" },
  momentum: { bg: "bg-orange-500/10", text: "text-orange-400", letter: "F" },
  sharp: { bg: "bg-purple-500/10", text: "text-purple-400", letter: "S" },
  kings: { bg: "bg-yellow-500/10", text: "text-yellow-400", letter: "K" },
  btb: { bg: "bg-teal-500/10", text: "text-teal-400", letter: "B" },
  rest: { bg: "bg-cyan-500/10", text: "text-cyan-400", letter: "R" },
};

const signalLabel = (key: string) => {
  const map: Record<string, string> = {
    poisson: t("qtipPerformance.signalPoisson"),
    momentum: t("qtipPerformance.signalMomentum"),
    sharp: t("qtipPerformance.signalSharp"),
    kings: t("qtipPerformance.signalKings"),
    btb: t("qtipPerformance.signalBtb"),
    rest: t("qtipPerformance.signalRest"),
  };
  return map[key] || key;
};

function pickLabel(sel: string, home: string, away: string): string {
  if (sel === "1") return home;
  if (sel === "2") return away;
  return t("match.draw");
}
</script>

<template>
  <div class="max-w-4xl mx-auto px-4 py-8">
    <!-- Header -->
    <div class="mb-8">
      <h1 class="text-2xl font-bold text-text-primary">{{ $t("qtipPerformance.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ $t("qtipPerformance.subtitle") }}</p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="space-y-4">
      <div v-for="n in 4" :key="n" class="bg-surface-1 rounded-card h-24 animate-pulse" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ $t("qtipPerformance.loadError") }}</p>
      <button class="text-sm text-primary hover:underline" @click="fetchPerf">
        {{ $t("common.retry") }}
      </button>
    </div>

    <!-- Empty state -->
    <div v-else-if="!hasData" class="flex flex-col items-center justify-center py-20">
      <span class="text-4xl mb-4" aria-hidden="true">📊</span>
      <h2 class="text-lg font-semibold text-text-primary mb-2">{{ $t("qtipPerformance.noData") }}</h2>
    </div>

    <!-- Data -->
    <div v-else-if="data" class="space-y-6">
      <!-- Section 1: Hero stats -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div class="bg-surface-1 rounded-card p-4 text-center">
          <div class="text-2xl font-bold text-text-primary tabular-nums">
            {{ data.overall.total_resolved }}
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t("qtipPerformance.totalResolved") }}</div>
        </div>
        <div class="bg-surface-1 rounded-card p-4 text-center">
          <div class="text-2xl font-bold tabular-nums" :class="data.overall.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'">
            {{ (data.overall.win_rate * 100).toFixed(1) }}%
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t("qtipPerformance.winRate") }}</div>
        </div>
        <div class="bg-surface-1 rounded-card p-4 text-center">
          <div class="text-2xl font-bold text-text-primary tabular-nums">
            {{ (data.overall.avg_confidence * 100).toFixed(0) }}%
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t("qtipPerformance.avgConfidence") }}</div>
        </div>
        <div class="bg-surface-1 rounded-card p-4 text-center">
          <div class="text-2xl font-bold tabular-nums" :class="data.overall.avg_edge > 0 ? 'text-emerald-400' : 'text-text-primary'">
            +{{ data.overall.avg_edge.toFixed(1) }}%
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t("qtipPerformance.avgEdge") }}</div>
        </div>
      </div>

      <!-- Section 2: Win/loss bar -->
      <div class="bg-surface-1 rounded-card p-4">
        <div class="flex items-center gap-3 mb-2 text-xs text-text-muted">
          <span class="flex items-center gap-1">
            <span class="w-2.5 h-2.5 rounded-full bg-emerald-500" />
            {{ $t("qtipPerformance.correct") }} ({{ data.overall.correct }})
          </span>
          <span class="flex items-center gap-1">
            <span class="w-2.5 h-2.5 rounded-full bg-red-500" />
            {{ $t("qtipPerformance.incorrect") }} ({{ data.overall.total_resolved - data.overall.correct }})
          </span>
        </div>
        <div class="flex h-4 rounded-full overflow-hidden bg-surface-2">
          <div
            class="bg-emerald-500 transition-all"
            :style="{ width: `${winPct}%` }"
          />
          <div
            class="bg-red-500 transition-all"
            :style="{ width: `${lossPct}%` }"
          />
        </div>
      </div>

      <!-- Section 3: By sport -->
      <div v-if="data.by_sport.length > 0" class="bg-surface-1 rounded-card p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t("qtipPerformance.bySport") }}
        </h2>
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead>
              <tr class="text-text-muted border-b border-surface-3/50">
                <th class="text-left py-2 pr-3 font-medium">{{ $t("qtipPerformance.sport") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.tips") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.correctCol") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.rate") }}</th>
                <th class="text-right py-2 pl-2 font-medium">{{ $t("qtipPerformance.edge") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="sp in data.by_sport"
                :key="sp.sport_key"
                class="border-b border-surface-3/30 last:border-0 hover:bg-surface-2/50 transition-colors"
              >
                <td class="py-2 pr-3 text-text-secondary">{{ sportLabel(sp.sport_key) }}</td>
                <td class="py-2 px-2 text-right tabular-nums text-text-muted">{{ sp.total }}</td>
                <td class="py-2 px-2 text-right tabular-nums text-text-muted">{{ sp.correct }}</td>
                <td class="py-2 px-2 text-right tabular-nums font-medium" :class="sp.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'">
                  {{ (sp.win_rate * 100).toFixed(1) }}%
                </td>
                <td class="py-2 pl-2 text-right tabular-nums text-text-muted">
                  +{{ sp.avg_edge.toFixed(1) }}%
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Section 4: By confidence band (chart) -->
      <div v-if="confidenceChartData && data.by_confidence.length > 0" class="bg-surface-1 rounded-card p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-1">
          {{ $t("qtipPerformance.byConfidence") }}
        </h2>
        <div class="flex items-center gap-4 mb-3 text-[10px] text-text-muted">
          <span class="flex items-center gap-1">
            <span class="w-2.5 h-2.5 rounded-sm bg-indigo-500/50" />
            {{ $t("qtipPerformance.avgConfidence") }}
          </span>
          <span class="flex items-center gap-1">
            <span class="w-2.5 h-2.5 rounded-sm bg-emerald-500/50" />
            {{ $t("qtipPerformance.winRate") }}
          </span>
        </div>
        <div class="h-48">
          <Bar :data="confidenceChartData" :options="chartOptions" />
        </div>
      </div>

      <!-- Section 5: By tier signal -->
      <div v-if="data.by_signal.length > 0" class="bg-surface-1 rounded-card p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t("qtipPerformance.bySignal") }}
        </h2>
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
          <div
            v-for="sig in data.by_signal"
            :key="sig.signal"
            class="rounded-lg p-3 text-center"
            :class="signalColors[sig.signal]?.bg || 'bg-surface-2/50'"
          >
            <div
              class="text-lg font-bold"
              :class="signalColors[sig.signal]?.text || 'text-text-primary'"
            >
              {{ signalColors[sig.signal]?.letter || sig.signal[0].toUpperCase() }}
            </div>
            <div class="text-[10px] text-text-muted mt-0.5 truncate">
              {{ signalLabel(sig.signal) }}
            </div>
            <div class="text-sm font-bold tabular-nums mt-1" :class="sig.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'">
              {{ (sig.win_rate * 100).toFixed(0) }}%
            </div>
            <div class="text-[10px] text-text-muted tabular-nums">
              {{ sig.correct }}/{{ sig.total }}
            </div>
          </div>
        </div>
      </div>

      <!-- Section 6: Track record -->
      <div class="bg-surface-1 rounded-card p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t("qtipPerformance.trackRecord") }}
        </h2>
        <div v-if="data.recent_tips.length === 0" class="text-center py-6 text-text-muted text-sm">
          {{ $t("qtipPerformance.noData") }}
        </div>
        <div v-else class="overflow-x-auto max-h-[500px] overflow-y-auto">
          <table class="w-full text-xs">
            <thead class="sticky top-0 bg-surface-1">
              <tr class="text-text-muted border-b border-surface-3/50">
                <th class="text-left py-2 pr-2 font-medium w-5" />
                <th class="text-left py-2 pr-2 font-medium">{{ $t("qtipPerformance.match") }}</th>
                <th class="text-left py-2 px-2 font-medium">{{ $t("qtipPerformance.sport") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.pick") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.actual") }}</th>
                <th class="text-right py-2 px-2 font-medium">{{ $t("qtipPerformance.confidence") }}</th>
                <th class="text-right py-2 pl-2 font-medium">{{ $t("qtipPerformance.edge") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="tip in data.recent_tips"
                :key="tip.match_id"
                class="border-b border-surface-3/30 last:border-0 hover:bg-surface-2/50 transition-colors"
              >
                <td class="py-2 pr-2">
                  <span
                    class="w-2.5 h-2.5 rounded-full inline-block"
                    :class="tip.was_correct ? 'bg-emerald-500' : 'bg-red-500'"
                  />
                </td>
                <td class="py-2 pr-2 text-text-secondary whitespace-nowrap">
                  <span class="truncate max-w-[180px] inline-block align-bottom">
                    {{ tip.home_team }} vs {{ tip.away_team }}
                  </span>
                </td>
                <td class="py-2 px-2 text-text-muted whitespace-nowrap">
                  {{ sportLabel(tip.sport_key) }}
                </td>
                <td class="py-2 px-2 text-right tabular-nums font-medium text-text-secondary">
                  {{ pickLabel(tip.recommended_selection, tip.home_team, tip.away_team) }}
                  <span class="text-text-muted">({{ tip.recommended_selection }})</span>
                </td>
                <td class="py-2 px-2 text-right tabular-nums text-text-muted">
                  {{ tip.actual_result }}
                </td>
                <td class="py-2 px-2 text-right tabular-nums text-text-muted">
                  {{ (tip.confidence * 100).toFixed(0) }}%
                </td>
                <td class="py-2 pl-2 text-right tabular-nums" :class="tip.edge_pct > 0 ? 'text-emerald-400' : 'text-text-muted'">
                  {{ tip.edge_pct > 0 ? '+' : '' }}{{ tip.edge_pct.toFixed(1) }}%
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
