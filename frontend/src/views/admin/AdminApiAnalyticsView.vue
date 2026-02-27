<!--
frontend/src/views/admin/AdminApiAnalyticsView.vue

Purpose:
    Admin view for Sportmonks API credit consumption analytics.
    Displays KPI cards, stacked bar chart (credits per hour by module),
    and a module breakdown table.

Dependencies:
    - @/composables/useApi
    - vue-chartjs (Bar) + chart.js + chartjs-adapter-date-fns
    - vue-i18n
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { Bar } from "vue-chartjs";
import {
  Chart as ChartJS,
  BarElement,
  CategoryScale,
  LinearScale,
  TimeScale,
  Tooltip,
  Legend,
} from "chart.js";
import "chartjs-adapter-date-fns";

ChartJS.register(BarElement, CategoryScale, LinearScale, TimeScale, Tooltip, Legend);

interface ModuleEntry {
  module: string;
  credits: number;
  calls: number;
  pct?: number;
}

interface HourlyEntry {
  hour: string;
  total_credits: number;
  modules: ModuleEntry[];
}

interface ConsumptionSummary {
  total_credits: number;
  total_calls: number;
  avg_credits_per_hour: number;
  top_module: string | null;
  top_module_credits: number;
  burn_rate_per_minute: number;
  modules: (ModuleEntry & { pct: number })[];
}

interface ConsumptionResponse {
  range: string;
  since: string;
  generated_at: string;
  hourly: HourlyEntry[];
  summary: ConsumptionSummary;
}

const MODULE_COLORS: Record<string, string> = {
  sync_season_deep: "#3b82f6",
  sync_prematch_odds: "#22c55e",
  repair_broken_rounds: "#f59e0b",
  repair_broken_round_fixtures: "#ef4444",
  sync_expected_fixtures: "#8b5cf6",
  get_available_leagues: "#6b7280",
  unknown: "#94a3b8",
};

function moduleColor(name: string): string {
  return MODULE_COLORS[name] || "#94a3b8";
}

function moduleLabel(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const api = useApi();
const { t } = useI18n();

const loading = ref(true);
const error = ref("");
const selectedRange = ref<"24h" | "7d" | "30d">("24h");
const data = ref<ConsumptionResponse | null>(null);

const summary = computed(() => data.value?.summary ?? null);
const isEmpty = computed(() => !loading.value && !error.value && (!data.value || data.value.hourly.length === 0));

async function fetchData(range: "24h" | "7d" | "30d"): Promise<void> {
  selectedRange.value = range;
  loading.value = true;
  error.value = "";
  try {
    data.value = await api.get<ConsumptionResponse>("/admin/ingest/metrics/consumption", { range });
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

// Chart data: build one dataset per module, stacked
const allModules = computed(() => {
  if (!data.value) return [];
  const names = new Set<string>();
  for (const entry of data.value.hourly) {
    for (const m of entry.modules) names.add(m.module);
  }
  return Array.from(names).sort();
});

const chartData = computed(() => {
  if (!data.value) return { labels: [], datasets: [] };
  const hours = data.value.hourly.map((h) => h.hour);

  const datasets = allModules.value.map((mod) => {
    const color = moduleColor(mod);
    return {
      label: moduleLabel(mod),
      data: data.value!.hourly.map((h) => {
        const entry = h.modules.find((m) => m.module === mod);
        return entry ? entry.credits : 0;
      }),
      backgroundColor: color,
      borderColor: color,
      borderWidth: 1,
      borderRadius: 2,
    };
  });

  return { labels: hours, datasets };
});

const chartOptions = computed(() => {
  const isWide = selectedRange.value !== "24h";
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index" as const, intersect: false },
    plugins: {
      legend: {
        display: true,
        labels: {
          color: "#94a3b8",
          boxWidth: 12,
          padding: 8,
          font: { size: 11 },
        },
      },
      tooltip: {
        backgroundColor: "#1e293b",
        titleColor: "#e2e8f0",
        bodyColor: "#cbd5e1",
        borderColor: "#334155",
        borderWidth: 1,
        padding: 8,
        titleFont: { size: 11 },
        bodyFont: { size: 11 },
        callbacks: {
          title: (items: { label?: string }[]) => {
            if (!items.length || !items[0].label) return "";
            const d = new Date(items[0].label);
            return d.toLocaleString("de-DE", {
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            });
          },
        },
      },
    },
    scales: {
      x: {
        type: "time" as const,
        stacked: true,
        time: {
          unit: (isWide ? "day" : "hour") as "day" | "hour",
          displayFormats: { hour: "HH:mm", day: "dd.MM" },
        },
        grid: { color: "rgba(51, 65, 85, 0.3)" },
        ticks: {
          color: "#64748b",
          font: { size: 10 },
          maxTicksLimit: isWide ? 15 : 24,
        },
      },
      y: {
        stacked: true,
        grid: { color: "rgba(51, 65, 85, 0.3)" },
        ticks: { color: "#64748b", font: { size: 10 } },
        title: {
          display: true,
          text: t("admin.apiAnalytics.chart.yLabel"),
          color: "#64748b",
          font: { size: 11 },
        },
      },
    },
  };
});

onMounted(() => fetchData("24h"));
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <!-- Header -->
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.apiAnalytics.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.apiAnalytics.subtitle") }}</p>
    </div>

    <!-- Range selector -->
    <div class="flex gap-2">
      <button
        v-for="r in (['24h', '7d', '30d'] as const)"
        :key="r"
        type="button"
        class="rounded-card border px-3 py-1.5 text-sm transition-colors"
        :class="
          selectedRange === r
            ? 'border-primary bg-primary/10 text-primary font-medium'
            : 'border-surface-3 bg-surface-0 text-text-secondary hover:border-primary/60'
        "
        @click="fetchData(r)"
      >
        {{ t(`admin.apiAnalytics.range${r.replace('d', 'D').replace('h', 'H')}`) }}
      </button>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="space-y-3">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div v-for="n in 4" :key="n" class="bg-surface-1 rounded-card h-20 animate-pulse border border-surface-3/50" />
      </div>
      <div class="bg-surface-1 rounded-card h-64 animate-pulse border border-surface-3/50" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="rounded-card border border-surface-3/60 bg-surface-1 p-6 text-center">
      <p class="text-sm text-danger">{{ error }}</p>
      <button
        type="button"
        class="mt-3 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-sm text-text-secondary hover:border-primary/60"
        @click="fetchData(selectedRange)"
      >
        {{ t("common.retry") }}
      </button>
    </div>

    <!-- Empty -->
    <div v-else-if="isEmpty" class="rounded-card border border-surface-3/60 bg-surface-1 p-6 text-center text-sm text-text-muted">
      {{ t("admin.apiAnalytics.empty") }}
    </div>

    <!-- Data -->
    <template v-else-if="summary">
      <!-- KPI Cards -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.apiAnalytics.kpi.totalCredits") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">
            {{ summary.total_credits.toLocaleString() }}
          </p>
          <p class="text-xs text-text-muted">{{ summary.total_calls }} calls</p>
        </div>

        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.apiAnalytics.kpi.topModule") }}</p>
          <p class="text-lg font-bold text-text-primary truncate">
            {{ summary.top_module ? moduleLabel(summary.top_module) : "-" }}
          </p>
          <p v-if="summary.top_module && summary.total_credits > 0" class="text-xs text-primary">
            {{ Math.round((summary.top_module_credits / summary.total_credits) * 100) }}%
          </p>
        </div>

        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.apiAnalytics.kpi.avgPerHour") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">
            {{ summary.avg_credits_per_hour }}
          </p>
          <p class="text-xs text-text-muted">credits/h</p>
        </div>

        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.apiAnalytics.kpi.burnRate") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">
            {{ summary.burn_rate_per_minute }}
          </p>
          <p class="text-xs text-text-muted">credits/min</p>
        </div>
      </div>

      <!-- Stacked Bar Chart -->
      <section class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-sm font-semibold text-text-primary mb-3">{{ t("admin.apiAnalytics.chart.title") }}</h2>
        <div style="height: 280px">
          <Bar :data="chartData" :options="chartOptions" />
        </div>
      </section>

      <!-- Module Breakdown Table -->
      <section class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
        <table class="min-w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.apiAnalytics.table.module") }}</th>
              <th class="px-3 py-2 text-right text-text-secondary font-medium">{{ t("admin.apiAnalytics.table.credits") }}</th>
              <th class="px-3 py-2 text-right text-text-secondary font-medium">{{ t("admin.apiAnalytics.table.calls") }}</th>
              <th class="px-3 py-2 text-right text-text-secondary font-medium">{{ t("admin.apiAnalytics.table.share") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="mod in summary.modules"
              :key="mod.module"
              class="border-b border-surface-3/40 last:border-b-0"
            >
              <td class="px-3 py-2 flex items-center gap-2">
                <span
                  class="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
                  :style="{ backgroundColor: moduleColor(mod.module) }"
                />
                <span class="text-text-primary">{{ moduleLabel(mod.module) }}</span>
              </td>
              <td class="px-3 py-2 text-right text-text-primary tabular-nums">{{ mod.credits.toLocaleString() }}</td>
              <td class="px-3 py-2 text-right text-text-muted tabular-nums">{{ mod.calls.toLocaleString() }}</td>
              <td class="px-3 py-2 text-right text-text-muted tabular-nums">{{ mod.pct }}%</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </div>
</template>
