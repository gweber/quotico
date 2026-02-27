<!--
frontend/src/views/admin/AdminEventBusMonitor.vue

Purpose:
    Admin monitor view for qbus health (live status, handler rollups, recent
    errors, and historical trend charts).
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
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
import { useApi } from "@/composables/useApi";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend);

interface EventBusError {
  event_id: string;
  event_type: string;
  source?: string;
  handler_name: string;
  correlation_id: string;
  ts: string;
  processing_lag_ms?: number;
  error: string;
}

interface EventBusLiveStats {
  running: boolean;
  enabled: boolean;
  published_total: number;
  handled_total: number;
  failed_total: number;
  dropped_total: number;
  published_rate_1m: number;
  handled_rate_1m: number;
  failed_rate_1m: number;
  dropped_rate_1m: number;
  ingress_queue_depth: number;
  ingress_queue_limit: number;
  ingress_queue_usage_pct: number;
  max_ingress_queue_depth_seen: number;
  latency_ms: { avg: number; p50: number; p95: number };
}

interface LiveResponse {
  status_level: "green" | "yellow" | "red";
  alerts: Array<{ code: string; severity: string; value?: number; handler?: string }>;
  stats: EventBusLiveStats;
  recent_errors: EventBusError[];
  per_source_1h: Array<{
    name: string;
    published_1h: number;
    handled_1h: number;
    failed_1h: number;
    dropped_1h: number;
  }>;
  per_event_type_1h: Array<{
    name: string;
    published_1h: number;
    handled_1h: number;
    failed_1h: number;
    dropped_1h: number;
  }>;
  fallback_polling?: {
    automation_enabled: boolean;
    scheduled_jobs: Array<{ id: string; next_run: string | null }>;
  };
}

interface HistoryPoint {
  ts: string;
  ingress_depth: number;
  failed_rate_1m: number;
  dropped_rate_1m: number;
  latency_p95: number;
  status_level: "green" | "yellow" | "red";
}

interface HistoryResponse {
  window: string;
  bucket_seconds: number;
  series: HistoryPoint[];
}

interface HandlerItem {
  name: string;
  concurrency: number;
  queue_depth: number;
  queue_limit: number;
  queue_usage_pct: number;
  handled_1h: number;
  failed_1h: number;
  dropped_1h: number;
  last_error?: EventBusError;
}

interface HandlersResponse {
  window: string;
  items: HandlerItem[];
}

const api = useApi();
const { t } = useI18n();
const loading = ref(true);
const live = ref<LiveResponse | null>(null);
const history = ref<HistoryResponse | null>(null);
const handlers = ref<HandlersResponse | null>(null);
const error = ref<string | null>(null);
const windowKey = ref<"1h" | "6h" | "24h">("24h");
const sourceFilter = ref<"all" | "sportmonks">("all");
let timer: number | null = null;

const depthChartData = computed(() => {
  const series = history.value?.series || [];
  return {
    labels: series.map((row) => new Date(row.ts).toLocaleTimeString()),
    datasets: [
      {
        label: t("admin.eventBus.charts.ingressDepth"),
        data: series.map((row) => row.ingress_depth),
        borderColor: "#22d3ee",
        backgroundColor: "rgba(34,211,238,0.2)",
        pointRadius: 0,
        tension: 0.25,
      },
    ],
  };
});

const failureChartData = computed(() => {
  const series = history.value?.series || [];
  return {
    labels: series.map((row) => new Date(row.ts).toLocaleTimeString()),
    datasets: [
      {
        label: t("admin.eventBus.charts.failedRate"),
        data: series.map((row) => row.failed_rate_1m),
        borderColor: "#f97316",
        backgroundColor: "rgba(249,115,22,0.2)",
        pointRadius: 0,
        tension: 0.25,
      },
      {
        label: t("admin.eventBus.charts.droppedRate"),
        data: series.map((row) => row.dropped_rate_1m),
        borderColor: "#ef4444",
        backgroundColor: "rgba(239,68,68,0.2)",
        pointRadius: 0,
        tension: 0.25,
      },
    ],
  };
});

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: true, labels: { color: "#a3a3a3" } },
  },
  scales: {
    x: { ticks: { color: "#a3a3a3", maxTicksLimit: 8 }, grid: { color: "rgba(255,255,255,0.06)" } },
    y: { ticks: { color: "#a3a3a3" }, grid: { color: "rgba(255,255,255,0.06)" } },
  },
};

const statusClass = computed(() => {
  const level = live.value?.status_level || "green";
  if (level === "red") return "bg-danger-muted/20 text-danger border-danger/50";
  if (level === "yellow") return "bg-amber-500/20 text-amber-300 border-amber-500/50";
  return "bg-emerald-500/20 text-emerald-300 border-emerald-500/50";
});

const filteredSourceRows = computed(() => {
  const rows = live.value?.per_source_1h || [];
  if (sourceFilter.value === "all") {
    return rows;
  }
  return rows.filter((row) => row.name === sourceFilter.value);
});

const filteredErrors = computed(() => {
  const rows = live.value?.recent_errors || [];
  if (sourceFilter.value === "all") {
    return rows;
  }
  return rows.filter((row) => (row.source || "") === sourceFilter.value);
});

async function fetchAll() {
  try {
    const [liveRes, historyRes, handlersRes] = await Promise.all([
      api.get<LiveResponse>("/admin/event-bus/status"),
      api.get<HistoryResponse>(`/admin/event-bus/history?window=${windowKey.value}&bucket_seconds=10`),
      api.get<HandlersResponse>("/admin/event-bus/handlers?window=1h"),
    ]);
    live.value = liveRes;
    history.value = historyRes;
    handlers.value = handlersRes;
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  await fetchAll();
  timer = window.setInterval(() => void fetchAll(), 10000);
});

onUnmounted(() => {
  if (timer !== null) {
    window.clearInterval(timer);
  }
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
      <div class="flex items-center justify-between gap-3">
        <h1 class="text-xl font-bold text-text-primary">{{ t("admin.eventBus.title") }}</h1>
        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-xs text-text-secondary hover:border-primary/60"
          @click="fetchAll"
        >
          {{ t("admin.eventBus.refresh") }}
        </button>
      </div>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.eventBus.subtitle") }}</p>
    </div>

    <div v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-text-muted">
      {{ t("admin.eventBus.loading") }}
    </div>
    <div v-else-if="error" class="rounded-card border border-danger/60 bg-danger-muted/10 p-4 text-danger">
      {{ error }}
    </div>
    <template v-else-if="live">
      <div class="rounded-card border p-4" :class="statusClass">
        <p class="font-semibold">{{ t("admin.eventBus.overallStatus") }}: {{ live.status_level }}</p>
        <p class="text-xs mt-1">{{ t("admin.eventBus.alerts") }}: {{ live.alerts.length }}</p>
        <p class="text-xs mt-1">
          {{ t("admin.eventBus.fallbackPolling") }}:
          {{
            live.fallback_polling?.automation_enabled
              ? t("admin.eventBus.fallbackEnabled")
              : t("admin.eventBus.fallbackDisabled")
          }}
        </p>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.eventBus.kpi.ingressDepth") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ live.stats.ingress_queue_depth }} / {{ live.stats.ingress_queue_limit }}</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.eventBus.kpi.publishedRate") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ live.stats.published_rate_1m.toFixed(2) }}/s</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.eventBus.kpi.failedRate") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ live.stats.failed_rate_1m.toFixed(4) }}/s</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.eventBus.kpi.latencyP95") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ live.stats.latency_ms.p95.toFixed(0) }}ms</p>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-sm font-semibold text-text-primary mb-2">{{ t("admin.eventBus.charts.ingressDepth") }}</p>
          <div class="h-56">
            <Line :data="depthChartData" :options="chartOptions" />
          </div>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-sm font-semibold text-text-primary mb-2">{{ t("admin.eventBus.charts.failureRates") }}</p>
          <div class="h-56">
            <Line :data="failureChartData" :options="chartOptions" />
          </div>
        </div>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-x-auto">
        <div class="flex items-center justify-between gap-3 p-3 border-b border-surface-3/60">
          <p class="text-sm font-semibold text-text-primary">{{ t("admin.eventBus.sources.title") }}</p>
          <select
            v-model="sourceFilter"
            class="rounded-card border border-surface-3 bg-surface-0 px-2 py-1 text-xs text-text-primary"
          >
            <option value="all">{{ t("admin.eventBus.sources.all") }}</option>
            <option value="sportmonks">sportmonks</option>
          </select>
        </div>
        <table class="w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.sources.source") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.sources.published1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.sources.handled1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.sources.failed1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.sources.dropped1h") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in filteredSourceRows" :key="row.name" class="border-b border-surface-3/30 last:border-b-0">
              <td class="px-3 py-2 text-text-primary">{{ row.name }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.published_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.handled_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.failed_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.dropped_1h }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-x-auto">
        <p class="text-sm font-semibold text-text-primary p-3 border-b border-surface-3/60">
          {{ t("admin.eventBus.eventTypes.title") }}
        </p>
        <table class="w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.eventTypes.type") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.eventTypes.published1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.eventTypes.handled1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.eventTypes.failed1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.eventTypes.dropped1h") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in live.per_event_type_1h" :key="row.name" class="border-b border-surface-3/30 last:border-b-0">
              <td class="px-3 py-2 text-text-primary">{{ row.name }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.published_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.handled_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.failed_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.dropped_1h }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.name") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.concurrency") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.queue") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.handled1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.failed1h") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.handlers.lastError") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in handlers?.items || []" :key="row.name" class="border-b border-surface-3/30 last:border-b-0">
              <td class="px-3 py-2 text-text-primary">{{ row.name }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.concurrency }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.queue_depth }}/{{ row.queue_limit }} ({{ row.queue_usage_pct.toFixed(1) }}%)</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.handled_1h }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ row.failed_1h }}</td>
              <td class="px-3 py-2 text-[11px] text-text-muted">
                {{ row.last_error?.error || "-" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.errors.time") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.errors.event") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.errors.handler") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary">{{ t("admin.eventBus.errors.message") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in filteredErrors" :key="`${item.event_id}-${item.handler_name}-${item.ts}`" class="border-b border-surface-3/30 last:border-b-0">
              <td class="px-3 py-2 text-text-muted text-[11px]">{{ item.ts }}</td>
              <td class="px-3 py-2 text-text-secondary text-[11px]">{{ item.event_id }}</td>
              <td class="px-3 py-2 text-text-secondary">{{ item.handler_name }}</td>
              <td class="px-3 py-2 text-text-primary">{{ item.error }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
