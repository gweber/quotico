<!--
frontend/src/views/admin/AdminIngestDiscoveryView.vue

Purpose:
    Admin cockpit for Sportmonks discovery, deep ingest, and dedicated metrics
    sync (xG + odds) with adaptive polling and season health visibility.
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { HttpError, useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import { useLeagueStore } from "@/stores/leagues";

interface SeasonOption {
  id: number;
  name: string;
}

interface DiscoveryLeague {
  league_id: number;
  name: string;
  country: string;
  is_cup: boolean;
  is_active: boolean;
  ui_order: number;
  features: {
    tipping: boolean;
    match_load: boolean;
    xg_sync: boolean;
    odds_sync: boolean;
  };
  available_seasons: SeasonOption[];
  last_synced_at: string | null;
}

interface DiscoveryResponse {
  source: "cache" | "sportmonks" | "cache_fallback_rate_limited";
  ttl_minutes: number;
  last_synced_at: string | null;
  rate_limit_remaining: number | null;
  warning?: string;
  items: DiscoveryLeague[];
}

interface JobStatus {
  job_id: string;
  type: "sportmonks_deep_ingest" | "sportmonks_metrics_sync";
  status: "queued" | "running" | "paused" | "succeeded" | "failed";
  phase: string;
  season_id: number;
  total_rounds: number;
  processed_rounds: number;
  progress: { processed: number; total: number; percent: number };
  is_stale: boolean;
  can_retry: boolean;
  current_round_name?: string | null;
  rate_limit_remaining?: number | null;
  rate_limit_reset_at?: number | null;
  rate_limit_paused?: boolean;
  heartbeat_age_seconds?: number | null;
  throughput_per_min?: number | null;
  eta_seconds?: number | null;
  pages_processed?: number;
  pages_total?: number | null;
  rows_processed?: number;
  timeout_at?: string | null;
  max_runtime_minutes?: number | null;
  page_requests_total?: number;
  duplicate_page_blocks?: number;
  error_summary?: { warnings: number; errors: number };
  error_log?: Array<{ timestamp: string; round_id?: number | null; error_msg: string }>;
}

interface ActiveJobsResponse {
  items: Array<{
    job_id: string;
    type: "sportmonks_deep_ingest" | "sportmonks_metrics_sync";
    season_id: number;
    status: "queued" | "running" | "paused";
    processed_rounds: number;
    total_rounds: number;
    progress_percent: number;
    updated_at: string | null;
  }>;
}

interface MetricsHealth {
  season_id: number;
  total_matches: number;
  xg_covered_matches: number;
  xg_coverage_percent: number;
  odds_covered_matches: number;
  odds_coverage_percent: number;
}

interface OpsSnapshot {
  api_health: { remaining: number | null; reset_at: number | null; reserve_credits: number };
  queue_metrics: {
    queued: number;
    running: number;
    paused: number;
    active_by_type: Record<string, number>;
  };
  efficiency: {
    total_fixtures: number;
    bulk_round_calls: number;
    repair_calls: number;
    saved_calls_estimate: number;
    api_savings_ratio: number;
  };
  cache_metrics: { hits: number; misses: number; hit_ratio: number; entries_active: number };
  guard_metrics: { page_guard_blocks: number; runtime_timeouts: number };
  generated_at: string;
}

const api = useApi();
const toast = useToast();
const { t } = useI18n();
const leagueStore = useLeagueStore();

const loading = ref(false);
const refreshing = ref(false);
const source = ref<DiscoveryResponse["source"]>("cache");
const warning = ref<string>("");
const rateRemaining = ref<number | null>(null);
const leagues = ref<DiscoveryLeague[]>([]);
const selectedSeasonByLeague = reactive<Record<number, number | null>>({});
const deepJobBySeason = reactive<Record<number, string>>({});
const metricsJobBySeason = reactive<Record<number, string>>({});
const jobStatusByJobId = reactive<Record<string, JobStatus | null>>({});
const metricsHealthBySeason = reactive<Record<number, MetricsHealth | null>>({});
const drawerOpenBySeason = reactive<Record<number, boolean>>({});
const startBusyBySeason = reactive<Record<number, boolean>>({});
const metricsBusyBySeason = reactive<Record<number, boolean>>({});
const leaguePatchBusyById = reactive<Record<number, boolean>>({});
const pollTimer = ref<number | null>(null);
const opsSnapshot = ref<OpsSnapshot | null>(null);

const hasLeagues = computed(() => leagues.value.length > 0);

function setDefaultSeasons(): void {
  leagues.value.forEach((league) => {
    if (selectedSeasonByLeague[league.league_id] !== undefined) return;
    selectedSeasonByLeague[league.league_id] = league.available_seasons[0]?.id ?? null;
  });
}

function getSelectedSeason(leagueId: number): number | null {
  return selectedSeasonByLeague[leagueId] ?? null;
}

function deepJobStatus(seasonId: number): JobStatus | null {
  const jobId = deepJobBySeason[seasonId];
  return jobId ? (jobStatusByJobId[jobId] ?? null) : null;
}

function metricsJobStatus(seasonId: number): JobStatus | null {
  const jobId = metricsJobBySeason[seasonId];
  return jobId ? (jobStatusByJobId[jobId] ?? null) : null;
}

function isJobActive(status: JobStatus | null): boolean {
  if (!status) return false;
  return status.status === "queued" || status.status === "running" || status.status === "paused";
}

function deepRunning(seasonId: number): boolean {
  return isJobActive(deepJobStatus(seasonId));
}

function metricsRunning(seasonId: number): boolean {
  return isJobActive(metricsJobStatus(seasonId));
}

function canStartMetrics(seasonId: number): boolean {
  const health = metricsHealthBySeason[seasonId];
  return Boolean(health && health.total_matches > 0);
}

function formatSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hours}h ${remMins}m`;
}

function progressPercent(status: JobStatus | null): number {
  if (!status) return 0;
  return Math.max(0, Math.min(100, Number(status.progress?.percent || 0)));
}

function progressLabel(status: JobStatus | null): string {
  if (!status) return "-";
  const p = status.progress || { processed: 0, total: 0 };
  return `${p.processed}/${p.total || "?"}`;
}

function latestErrors(status: JobStatus | null): Array<{ timestamp: string; round_id?: number | null; error_msg: string }> {
  if (!status?.error_log?.length) return [];
  return [...status.error_log].slice(-3).reverse();
}

async function loadDiscovery(force = false): Promise<void> {
  loading.value = true;
  if (force) refreshing.value = true;
  try {
    const result = await api.get<DiscoveryResponse>("/admin/ingest/discovery", force ? { force: "true" } : undefined);
    source.value = result.source;
    warning.value = result.warning || "";
    rateRemaining.value = result.rate_limit_remaining;
    leagues.value = result.items;
    setDefaultSeasons();
    await refreshActiveJobs();
    await refreshOpenSeasonHealth();
    await loadOpsSnapshot();
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    loading.value = false;
    refreshing.value = false;
  }
}

async function loadOpsSnapshot(): Promise<void> {
  try {
    opsSnapshot.value = await api.get<OpsSnapshot>("/admin/ingest/ops/snapshot");
  } catch {
    opsSnapshot.value = null;
  }
}

async function refreshActiveJobs(): Promise<void> {
  const active = await api.get<ActiveJobsResponse>("/admin/ingest/jobs/active");
  Object.keys(deepJobBySeason).forEach((key) => delete deepJobBySeason[Number(key)]);
  Object.keys(metricsJobBySeason).forEach((key) => delete metricsJobBySeason[Number(key)]);
  active.items.forEach((item) => {
    if (item.type === "sportmonks_metrics_sync") {
      metricsJobBySeason[item.season_id] = item.job_id;
    } else {
      deepJobBySeason[item.season_id] = item.job_id;
    }
  });
}

async function pollJobStatuses(): Promise<void> {
  const deepEntries = Object.entries(deepJobBySeason);
  const metricsEntries = Object.entries(metricsJobBySeason);
  const jobIds = [...deepEntries.map(([, id]) => id), ...metricsEntries.map(([, id]) => id)];
  if (!jobIds.length) return;
  await Promise.all(
    jobIds.map(async (jobId) => {
      try {
        const status = await api.get<JobStatus>(`/admin/ingest/jobs/${jobId}`);
        jobStatusByJobId[jobId] = status;
        const isTerminal = status.status === "failed" || status.status === "succeeded";
        if (isTerminal) {
          if (status.type === "sportmonks_metrics_sync") {
            delete metricsJobBySeason[status.season_id];
            await refreshSeasonHealth(status.season_id);
          } else {
            delete deepJobBySeason[status.season_id];
            await refreshSeasonHealth(status.season_id);
          }
        }
      } catch (error) {
        toast.error(error instanceof Error ? error.message : t("common.genericError"));
      }
    }),
  );
}

function clearPolling(): void {
  if (pollTimer.value !== null) {
    window.clearTimeout(pollTimer.value);
    pollTimer.value = null;
  }
}

function nextPollDelayMs(): number {
  const activeJobIds = new Set<string>([
    ...Object.values(deepJobBySeason),
    ...Object.values(metricsJobBySeason),
  ]);
  if (!activeJobIds.size) return 10000;

  const statuses = Array.from(activeJobIds)
    .map((jobId) => jobStatusByJobId[jobId])
    .filter(Boolean) as JobStatus[];
  if (!statuses.length) return 5000;
  if (statuses.some((s) => s.status === "running")) return 1500;
  if (statuses.some((s) => s.status === "queued" || s.status === "paused")) return 5000;
  return 10000;
}

async function adaptivePollLoop(): Promise<void> {
  await loadOpsSnapshot();
  await pollJobStatuses();
  clearPolling();
  pollTimer.value = window.setTimeout(() => {
    void adaptivePollLoop();
  }, nextPollDelayMs());
}

async function startDeepIngest(seasonId: number | null): Promise<void> {
  if (!seasonId) return;
  startBusyBySeason[seasonId] = true;
  try {
    const result = await api.post<{ accepted: boolean; job_id: string; season_id: number }>(`/admin/ingest/season/${seasonId}`);
    deepJobBySeason[seasonId] = result.job_id;
    toast.success(t("admin.ingest.jobStarted"));
  } catch (error) {
    if (error instanceof HttpError && error.status === 409) {
      toast.error(t("admin.ingest.locked"));
    } else {
      toast.error(error instanceof Error ? error.message : t("common.genericError"));
    }
  } finally {
    startBusyBySeason[seasonId] = false;
  }
}

async function startMetricsSync(seasonId: number | null): Promise<void> {
  if (!seasonId) return;
  metricsBusyBySeason[seasonId] = true;
  try {
    const result = await api.post<{ accepted: boolean; job_id: string; season_id: number }>(`/admin/ingest/season/${seasonId}/metrics-sync`);
    metricsJobBySeason[seasonId] = result.job_id;
    toast.success(t("admin.ingest.metricsStarted"));
  } catch (error) {
    if (error instanceof HttpError && error.status === 428) {
      toast.error(t("admin.ingest.metricsPrecondition"));
    } else if (error instanceof HttpError && error.status === 409) {
      toast.error(t("admin.ingest.metricsLocked"));
    } else {
      toast.error(error instanceof Error ? error.message : t("common.genericError"));
    }
  } finally {
    metricsBusyBySeason[seasonId] = false;
  }
}

function canResume(status: JobStatus | null): boolean {
  if (!status) return false;
  return status.status === "paused" || status.can_retry === true;
}

async function resumeJob(status: JobStatus | null): Promise<void> {
  if (!status) return;
  try {
    await api.post(`/admin/ingest/jobs/${status.job_id}/resume`);
    if (status.type === "sportmonks_metrics_sync") {
      metricsJobBySeason[status.season_id] = status.job_id;
    } else {
      deepJobBySeason[status.season_id] = status.job_id;
    }
    toast.success(t("admin.ingest.resumed"));
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function refreshSeasonHealth(seasonId: number): Promise<void> {
  try {
    const health = await api.get<MetricsHealth>(`/admin/ingest/season/${seasonId}/metrics-health`);
    metricsHealthBySeason[seasonId] = health;
  } catch {
    metricsHealthBySeason[seasonId] = null;
  }
}

async function refreshOpenSeasonHealth(): Promise<void> {
  const seasons = new Set<number>();
  leagues.value.forEach((league) => {
    const seasonId = selectedSeasonByLeague[league.league_id];
    if (seasonId) seasons.add(seasonId);
  });
  await Promise.all(Array.from(seasons).map((sid) => refreshSeasonHealth(sid)));
}

async function patchLeagueFlags(
  league: DiscoveryLeague,
  payload: Record<string, unknown>,
): Promise<void> {
  leaguePatchBusyById[league.league_id] = true;
  try {
    const result = await api.patch<{ item: DiscoveryLeague }>(`/admin/ingest/leagues/${league.league_id}`, payload);
    const updated = result.item;
    Object.assign(league, updated);
    toast.success(t("admin.ingest.leagueUpdated"));
    leagueStore.fetchNavigation(true);
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    leaguePatchBusyById[league.league_id] = false;
  }
}

function navStatusLabel(league: DiscoveryLeague): string {
  if (league.is_active && league.features.tipping) return t("admin.ingest.statusLiveNav");
  if (league.is_active && league.features.match_load) return t("admin.ingest.statusDataOnly");
  return t("admin.ingest.statusHidden");
}

function navStatusClass(league: DiscoveryLeague): string {
  if (league.is_active && league.features.tipping) return "bg-primary/15 text-primary";
  if (league.is_active && league.features.match_load) return "bg-sky-100 text-sky-800";
  return "bg-surface-2 text-text-muted";
}

function toggleDrawer(seasonId: number): void {
  drawerOpenBySeason[seasonId] = !drawerOpenBySeason[seasonId];
  if (drawerOpenBySeason[seasonId]) {
    void refreshSeasonHealth(seasonId);
  }
}

onMounted(async () => {
  await loadDiscovery(false);
  await adaptivePollLoop();
});

onUnmounted(() => {
  clearPolling();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h1 class="text-xl font-bold text-text-primary">{{ t("admin.ingest.title") }}</h1>
          <p class="text-sm text-text-muted mt-1">{{ t("admin.ingest.subtitle") }}</p>
        </div>
        <button
          type="button"
          class="rounded-lg bg-primary px-3 py-2 text-xs font-semibold text-white disabled:opacity-60"
          :disabled="refreshing"
          @click="loadDiscovery(true)"
        >
          {{ t("admin.ingest.refresh") }}
        </button>
      </div>
      <p class="text-xs text-text-muted mt-3">
        {{ t("admin.ingest.source") }}: <span class="font-mono text-text-primary">{{ source }}</span>
        · {{ t("admin.ingest.remaining") }}: <span class="font-mono text-text-primary">{{ rateRemaining ?? "?" }}</span>
      </p>
      <p v-if="opsSnapshot" class="text-xs text-text-muted mt-2">
        {{ t("admin.ingest.apiSavings") }}:
        <span class="font-mono text-text-primary">{{ Math.round((opsSnapshot.efficiency.api_savings_ratio || 0) * 100) }}%</span>
        · {{ t("admin.ingest.repairVsBulk") }}:
        <span class="font-mono text-text-primary">{{ opsSnapshot.efficiency.repair_calls }}/{{ opsSnapshot.efficiency.bulk_round_calls }}</span>
        · {{ t("admin.ingest.queueDepth") }}:
        <span class="font-mono text-text-primary">{{ opsSnapshot.queue_metrics.queued }}/{{ opsSnapshot.queue_metrics.running }}/{{ opsSnapshot.queue_metrics.paused }}</span>
        · {{ t("admin.ingest.cacheHitRatio") }}:
        <span class="font-mono text-text-primary">{{ Math.round((opsSnapshot.cache_metrics.hit_ratio || 0) * 100) }}%</span>
        · {{ t("admin.ingest.guardBlocks") }}:
        <span class="font-mono text-text-primary">{{ opsSnapshot.guard_metrics.page_guard_blocks }}/{{ opsSnapshot.guard_metrics.runtime_timeouts }}</span>
      </p>
      <p v-if="warning" class="text-xs text-danger mt-2">{{ warning }}</p>
    </div>

    <div v-if="loading" class="space-y-3">
      <div v-for="idx in 5" :key="idx" class="h-24 rounded-card bg-surface-1 border border-surface-3/60 animate-pulse" />
    </div>

    <div v-else-if="!hasLeagues" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-sm text-text-muted">
      {{ t("admin.ingest.empty") }}
    </div>

    <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <article
        v-for="league in leagues"
        :key="league.league_id"
        class="rounded-card border border-surface-3/60 bg-surface-1 p-4 space-y-3"
      >
        <div class="flex items-start justify-between">
          <div>
            <h2 class="text-base font-semibold text-text-primary">{{ league.name }}</h2>
            <p class="text-xs text-text-muted">{{ league.country || "—" }} · {{ league.league_id || "—" }}</p>
          </div>
          <button
            type="button"
            class="text-xs px-2 py-1 rounded-md border border-surface-3 text-text-muted"
            @click="toggleDrawer(getSelectedSeason(league.league_id) || 0)"
          >
            {{ t("admin.ingest.health") }}
          </button>
        </div>

        <div class="rounded-md border border-surface-3/60 bg-surface-2/50 p-2 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-2">
              <span class="inline-flex rounded-full px-2 py-1 text-[11px] font-medium" :class="navStatusClass(league)">
                {{ navStatusLabel(league) }}
              </span>
              <span class="text-[11px] text-text-muted">
                {{ league.last_synced_at ? new Date(league.last_synced_at).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : t("admin.ingest.neverSynced") }}
              </span>
            </div>
          </div>
          <div class="space-y-2 text-xs">
            <!-- Active -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-1.5">
                <span class="text-text-secondary">{{ t("admin.ingest.toggleActive") }}</span>
                <span class="group relative">
                  <svg class="w-3.5 h-3.5 text-text-muted cursor-help" fill="none" viewBox="0 0 20 20" stroke="currentColor" stroke-width="1.5">
                    <circle cx="10" cy="10" r="8" /><path d="M8 7.5a2.5 2.5 0 0 1 4 2c0 1.5-2 2-2 2" stroke-linecap="round" /><circle cx="10" cy="14" r=".5" fill="currentColor" stroke="none" />
                  </svg>
                  <span class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-48 rounded bg-surface-3 px-2 py-1 text-[11px] text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity shadow-md z-10">
                    {{ t("admin.ingest.toggleActiveDesc") }}
                  </span>
                </span>
              </div>
              <button
                type="button" role="switch" :aria-checked="league.is_active"
                :disabled="Boolean(leaguePatchBusyById[league.league_id])"
                class="relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200 focus:outline-none disabled:opacity-50"
                :class="league.is_active ? 'bg-primary' : 'bg-surface-3'"
                @click="patchLeagueFlags(league, { is_active: !league.is_active })"
              >
                <span class="pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform duration-200 mt-0.5" :class="league.is_active ? 'translate-x-[18px]' : 'translate-x-0.5'" />
              </button>
            </div>
            <!-- Tipping + Match Load: disabled when league inactive -->
            <div :class="{ 'opacity-40 pointer-events-none': !league.is_active }" class="space-y-2">
              <!-- Tipping -->
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-1.5">
                  <span class="text-text-secondary">{{ t("admin.ingest.toggleTipping") }}</span>
                  <span class="group relative">
                    <svg class="w-3.5 h-3.5 text-text-muted cursor-help" fill="none" viewBox="0 0 20 20" stroke="currentColor" stroke-width="1.5">
                      <circle cx="10" cy="10" r="8" /><path d="M8 7.5a2.5 2.5 0 0 1 4 2c0 1.5-2 2-2 2" stroke-linecap="round" /><circle cx="10" cy="14" r=".5" fill="currentColor" stroke="none" />
                    </svg>
                    <span class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-48 rounded bg-surface-3 px-2 py-1 text-[11px] text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity shadow-md z-10">
                      {{ t("admin.ingest.toggleTippingDesc") }}
                    </span>
                  </span>
                </div>
                <button
                  type="button" role="switch" :aria-checked="league.features.tipping"
                  :disabled="Boolean(leaguePatchBusyById[league.league_id])"
                  class="relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200 focus:outline-none disabled:opacity-50"
                  :class="league.features.tipping ? 'bg-primary' : 'bg-surface-3'"
                  @click="patchLeagueFlags(league, { features: { tipping: !league.features.tipping } })"
                >
                  <span class="pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform duration-200 mt-0.5" :class="league.features.tipping ? 'translate-x-[18px]' : 'translate-x-0.5'" />
                </button>
              </div>
              <!-- Match Load -->
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-1.5">
                  <span class="text-text-secondary">{{ t("admin.ingest.toggleMatchLoad") }}</span>
                  <span class="group relative">
                    <svg class="w-3.5 h-3.5 text-text-muted cursor-help" fill="none" viewBox="0 0 20 20" stroke="currentColor" stroke-width="1.5">
                      <circle cx="10" cy="10" r="8" /><path d="M8 7.5a2.5 2.5 0 0 1 4 2c0 1.5-2 2-2 2" stroke-linecap="round" /><circle cx="10" cy="14" r=".5" fill="currentColor" stroke="none" />
                    </svg>
                    <span class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-48 rounded bg-surface-3 px-2 py-1 text-[11px] text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity shadow-md z-10">
                      {{ t("admin.ingest.toggleMatchLoadDesc") }}
                    </span>
                  </span>
                </div>
                <button
                  type="button" role="switch" :aria-checked="league.features.match_load"
                  :disabled="Boolean(leaguePatchBusyById[league.league_id])"
                  class="relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200 focus:outline-none disabled:opacity-50"
                  :class="league.features.match_load ? 'bg-primary' : 'bg-surface-3'"
                  @click="patchLeagueFlags(league, { features: { match_load: !league.features.match_load } })"
                >
                  <span class="pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform duration-200 mt-0.5" :class="league.features.match_load ? 'translate-x-[18px]' : 'translate-x-0.5'" />
                </button>
              </div>
            </div>
          </div>
        </div>

        <select
          v-model.number="selectedSeasonByLeague[league.league_id]"
          class="w-full rounded-md border border-surface-3 bg-surface-2 px-2 py-2 text-sm text-text-primary"
          @change="refreshSeasonHealth(getSelectedSeason(league.league_id) || 0)"
        >
          <option
            v-for="season in league.available_seasons"
            :key="season.id"
            :value="season.id"
          >
            {{ season.name || season.id }}
          </option>
        </select>

        <div class="grid grid-cols-2 gap-2">
          <button
            type="button"
            class="w-full rounded-md bg-primary px-3 py-2 text-sm font-semibold text-white disabled:opacity-60"
            :disabled="!getSelectedSeason(league.league_id) || startBusyBySeason[getSelectedSeason(league.league_id) || 0] || deepRunning(getSelectedSeason(league.league_id) || 0)"
            @click="startDeepIngest(getSelectedSeason(league.league_id))"
          >
            {{ t("admin.ingest.deepIngest") }}
          </button>
          <button
            type="button"
            class="w-full rounded-md bg-surface-2 border border-surface-3 px-3 py-2 text-sm font-semibold text-text-primary disabled:opacity-60"
            :disabled="!getSelectedSeason(league.league_id) || !canStartMetrics(getSelectedSeason(league.league_id) || 0) || metricsBusyBySeason[getSelectedSeason(league.league_id) || 0] || metricsRunning(getSelectedSeason(league.league_id) || 0)"
            @click="startMetricsSync(getSelectedSeason(league.league_id))"
          >
            {{ t("admin.ingest.metricsSync") }}
          </button>
        </div>

        <div v-if="getSelectedSeason(league.league_id)" class="space-y-2 text-xs">
          <p class="text-text-muted">
            {{ t("admin.ingest.status") }}:
            <span class="font-medium text-text-primary">
              {{ deepJobStatus(getSelectedSeason(league.league_id) || 0)?.status || "-" }}
            </span>
            · {{ t("admin.ingest.metricsLabel") }}:
            <span class="font-medium text-text-primary">
              {{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.status || "-" }}
            </span>
          </p>

          <div
            v-if="deepJobStatus(getSelectedSeason(league.league_id) || 0)"
            class="rounded-md border border-surface-3/60 bg-surface-2/70 p-2 space-y-1"
          >
            <p class="text-text-secondary font-medium">
              {{ t("admin.ingest.deepIngest") }} ·
              {{ deepJobStatus(getSelectedSeason(league.league_id) || 0)?.phase || "-" }}
            </p>
            <div class="h-1.5 rounded bg-surface-3/60 overflow-hidden">
              <div
                class="h-full bg-primary transition-all"
                :style="{ width: `${progressPercent(deepJobStatus(getSelectedSeason(league.league_id) || 0))}%` }"
              />
            </div>
            <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-text-muted">
              <p>{{ t("admin.ingest.progress") }}: <span class="text-text-primary">{{ progressLabel(deepJobStatus(getSelectedSeason(league.league_id) || 0)) }}</span></p>
              <p>{{ t("admin.ingest.heartbeat") }}: <span class="text-text-primary">{{ formatSeconds(deepJobStatus(getSelectedSeason(league.league_id) || 0)?.heartbeat_age_seconds) }}</span></p>
              <p>{{ t("admin.ingest.throughput") }}: <span class="text-text-primary">{{ deepJobStatus(getSelectedSeason(league.league_id) || 0)?.throughput_per_min ?? "-" }}</span></p>
              <p>{{ t("admin.ingest.eta") }}: <span class="text-text-primary">{{ formatSeconds(deepJobStatus(getSelectedSeason(league.league_id) || 0)?.eta_seconds) }}</span></p>
              <p>{{ t("admin.ingest.pageRequests") }}: <span class="text-text-primary">{{ deepJobStatus(getSelectedSeason(league.league_id) || 0)?.page_requests_total ?? 0 }}</span></p>
              <p>{{ t("admin.ingest.duplicateBlocks") }}: <span class="text-text-primary">{{ deepJobStatus(getSelectedSeason(league.league_id) || 0)?.duplicate_page_blocks ?? 0 }}</span></p>
            </div>
            <p
              v-if="deepJobStatus(getSelectedSeason(league.league_id) || 0)?.rate_limit_paused"
              class="text-warning"
            >
              {{ t("admin.ingest.ratePaused") }}
            </p>
            <p
              v-if="deepJobStatus(getSelectedSeason(league.league_id) || 0)?.phase === 'failed_timeout'"
              class="text-danger"
            >
              {{ t("admin.ingest.failedTimeout") }}
            </p>
            <p
              v-if="deepJobStatus(getSelectedSeason(league.league_id) || 0)?.phase === 'failed_duplicate_page_guard'"
              class="text-danger"
            >
              {{ t("admin.ingest.failedDuplicatePage") }}
            </p>
            <button
              v-if="canResume(deepJobStatus(getSelectedSeason(league.league_id) || 0))"
              type="button"
              class="rounded-md border border-surface-3 px-2 py-1 text-xs text-text-primary"
              @click="resumeJob(deepJobStatus(getSelectedSeason(league.league_id) || 0))"
            >
              {{ t("admin.ingest.resume") }}
            </button>
            <div class="pt-1">
              <p class="text-text-secondary font-medium">{{ t("admin.ingest.lastErrors") }}</p>
              <p
                v-if="latestErrors(deepJobStatus(getSelectedSeason(league.league_id) || 0)).length === 0"
                class="text-text-muted"
              >
                {{ t("admin.ingest.noErrors") }}
              </p>
              <ul v-else class="space-y-1">
                <li
                  v-for="(item, idx) in latestErrors(deepJobStatus(getSelectedSeason(league.league_id) || 0))"
                  :key="`deep-${idx}-${item.timestamp}`"
                  class="text-text-muted"
                >
                  <span class="font-mono text-text-secondary">{{ item.timestamp }}</span>
                  <span v-if="item.round_id != null"> · R{{ item.round_id }}</span>
                  <span> · {{ item.error_msg }}</span>
                </li>
              </ul>
            </div>
          </div>

          <div
            v-if="metricsJobStatus(getSelectedSeason(league.league_id) || 0)"
            class="rounded-md border border-surface-3/60 bg-surface-2/70 p-2 space-y-1"
          >
            <p class="text-text-secondary font-medium">
              {{ t("admin.ingest.metricsSync") }} ·
              {{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.phase || "-" }}
            </p>
            <div class="h-1.5 rounded bg-surface-3/60 overflow-hidden">
              <div
                class="h-full bg-primary transition-all"
                :style="{ width: `${progressPercent(metricsJobStatus(getSelectedSeason(league.league_id) || 0))}%` }"
              />
            </div>
            <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-text-muted">
              <p>{{ t("admin.ingest.progress") }}: <span class="text-text-primary">{{ progressLabel(metricsJobStatus(getSelectedSeason(league.league_id) || 0)) }}</span></p>
              <p>{{ t("admin.ingest.heartbeat") }}: <span class="text-text-primary">{{ formatSeconds(metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.heartbeat_age_seconds) }}</span></p>
              <p>{{ t("admin.ingest.throughput") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.throughput_per_min ?? "-" }}</span></p>
              <p>{{ t("admin.ingest.eta") }}: <span class="text-text-primary">{{ formatSeconds(metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.eta_seconds) }}</span></p>
              <p>{{ t("admin.ingest.pages") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.pages_processed ?? 0 }}/{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.pages_total ?? "?" }}</span></p>
              <p>{{ t("admin.ingest.rows") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.rows_processed ?? 0 }}</span></p>
              <p>{{ t("admin.ingest.warnings") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.error_summary?.warnings ?? 0 }}</span></p>
              <p>{{ t("admin.ingest.errors") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.error_summary?.errors ?? 0 }}</span></p>
              <p>{{ t("admin.ingest.pageRequests") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.page_requests_total ?? 0 }}</span></p>
              <p>{{ t("admin.ingest.duplicateBlocks") }}: <span class="text-text-primary">{{ metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.duplicate_page_blocks ?? 0 }}</span></p>
            </div>
            <p
              v-if="metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.rate_limit_paused"
              class="text-warning"
            >
              {{ t("admin.ingest.ratePaused") }}
            </p>
            <p
              v-if="metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.phase === 'failed_timeout'"
              class="text-danger"
            >
              {{ t("admin.ingest.failedTimeout") }}
            </p>
            <p
              v-if="metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.phase === 'failed_duplicate_page_guard'"
              class="text-danger"
            >
              {{ t("admin.ingest.failedDuplicatePage") }}
            </p>
            <button
              v-if="canResume(metricsJobStatus(getSelectedSeason(league.league_id) || 0))"
              type="button"
              class="rounded-md border border-surface-3 px-2 py-1 text-xs text-text-primary"
              @click="resumeJob(metricsJobStatus(getSelectedSeason(league.league_id) || 0))"
            >
              {{ t("admin.ingest.resume") }}
            </button>
            <div class="pt-1">
              <p class="text-text-secondary font-medium">{{ t("admin.ingest.lastErrors") }}</p>
              <p
                v-if="latestErrors(metricsJobStatus(getSelectedSeason(league.league_id) || 0)).length === 0"
                class="text-text-muted"
              >
                {{ t("admin.ingest.noErrors") }}
              </p>
              <ul v-else class="space-y-1">
                <li
                  v-for="(item, idx) in latestErrors(metricsJobStatus(getSelectedSeason(league.league_id) || 0))"
                  :key="`metrics-${idx}-${item.timestamp}`"
                  class="text-text-muted"
                >
                  <span class="font-mono text-text-secondary">{{ item.timestamp }}</span>
                  <span v-if="item.round_id != null"> · R{{ item.round_id }}</span>
                  <span> · {{ item.error_msg }}</span>
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div
          v-if="drawerOpenBySeason[getSelectedSeason(league.league_id) || 0] && getSelectedSeason(league.league_id)"
          class="rounded-md border border-surface-3/70 bg-surface-2 p-3 space-y-2"
        >
          <p class="text-xs text-text-muted">
            {{
              t("admin.ingest.xgCoverage", {
                percent: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.xg_coverage_percent ?? 0,
                covered: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.xg_covered_matches ?? 0,
                total: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.total_matches ?? 0,
              })
            }}
          </p>
          <p class="text-xs text-text-muted">
            {{
              t("admin.ingest.oddsCoverage", {
                percent: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.odds_coverage_percent ?? 0,
                covered: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.odds_covered_matches ?? 0,
                total: metricsHealthBySeason[getSelectedSeason(league.league_id) || 0]?.total_matches ?? 0,
              })
            }}
          </p>
          <p
            v-if="deepJobStatus(getSelectedSeason(league.league_id) || 0)?.is_stale || metricsJobStatus(getSelectedSeason(league.league_id) || 0)?.is_stale"
            class="text-xs text-danger"
          >
            {{ t("admin.ingest.stale") }}
          </p>
          <p
            v-if="!canStartMetrics(getSelectedSeason(league.league_id) || 0)"
            class="text-xs text-danger"
          >
            {{ t("admin.ingest.metricsPrecondition") }}
          </p>
        </div>
      </article>
    </div>
  </div>
</template>
