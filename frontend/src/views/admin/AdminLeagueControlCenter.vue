<!--
frontend/src/views/admin/AdminLeagueControlCenter.vue

Purpose:
    Unified League & Season Control Center — consolidates IngestDiscovery and
    LeagueControlCenter into a single master-detail view with:
    - Ops snapshot strip (queue, cache, savings, rate, guard)
    - Feature toggles (tipping, match_load) + status badges per league
    - Rich job progress (phase, heartbeat, throughput, ETA, error logs, resume)
    - Per-league credit tracking with 7-day sparklines
    - Per-season health metrics (xG/odds coverage)
    - Job-auto-select on mount (Z1)
    - Telemetry-sync throttling (Z2)
    - Visual hierarchy for resume vs primary actions (Z3)
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useAdminSyncStore } from "@/stores/adminSync";
import { useLeagueStore } from "@/stores/leagues";

const api = useApi();
const { t } = useI18n();
const adminSyncStore = useAdminSyncStore();
const leagueStore = useLeagueStore();

// ---- Types ----

interface SeasonHealth {
  total_fixtures: number;
  finished: number;
  scheduled: number;
  live: number;
  xg_covered: number;
  xg_cache_count: number;
  xg_cache_pct: number;
  odds_covered: number;
  xg_pct: number;
  odds_pct: number;
  manual_check_count: number;
  status: "green" | "yellow" | "red";
}

interface JobStatus {
  job_id: string;
  type: string;
  status: string;
  season_id: number;
  phase?: string;
  progress?: { processed?: number; total?: number; percent?: number };
  is_stale?: boolean;
  can_retry?: boolean;
  rate_limit_paused?: boolean;
  heartbeat_age_seconds?: number | null;
  throughput_per_min?: number | null;
  eta_seconds?: number | null;
  page_requests_total?: number;
  duplicate_page_blocks?: number;
  error_log?: Array<{ timestamp: string; round_id?: number | null; error_msg: string }>;
}

interface Season {
  id: number;
  name: string;
  health: SeasonHealth;
  active_job: { job_id: string; type: string; status: string; progress: number } | null;
}

interface LeagueFeatures {
  tipping: boolean;
  match_load: boolean;
  xg_sync: boolean;
  odds_sync: boolean;
}

interface League {
  id: number;
  name: string;
  country: string;
  league_id: number;
  is_active: boolean;
  features: LeagueFeatures;
  ui_order: number;
  last_synced_at: string | null;
  credit_total_30d: number;
  credit_sparkline_7d: number[];
  seasons: Season[];
}

interface DashboardResponse {
  generated_at: string;
  leagues: League[];
}

interface OpsSnapshot {
  api_health: { remaining: number | null; reset_at: number | null; reserve_credits: number };
  queue_metrics: { queued: number; running: number; paused: number; active_by_type: Record<string, number> };
  efficiency: { total_fixtures: number; bulk_round_calls: number; repair_calls: number; saved_calls_estimate: number; api_savings_ratio: number };
  cache_metrics: { hits: number; misses: number; hit_ratio: number; entries_active: number };
  guard_metrics: { page_guard_blocks: number; runtime_timeouts: number };
  generated_at: string;
}

// ---- State ----

const loading = ref(true);
const error = ref("");
const refreshingDiscovery = ref(false);
const leagues = ref<League[]>([]);
const selectedLeagueId = ref<number | null>(null);
const patchBusy = reactive<Record<number, boolean>>({});
const jobBySeason = reactive<Record<number, string>>({});
const jobStatusMap = reactive<Record<string, JobStatus>>({});
const opsSnapshot = ref<OpsSnapshot | null>(null);
let pollTimer: number | null = null;
let lastDashboardRefresh = 0;
const DASHBOARD_REFRESH_INTERVAL = 5 * 60 * 1000; // Z2: 5 min hard-refresh

const selectedLeague = computed(() =>
  leagues.value.find((l) => l.id === selectedLeagueId.value) ?? null,
);

// ---- Fetch Dashboard ----

async function fetchDashboard(force = false): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const params: Record<string, string> = force ? { force: "1" } : {};
    const resp = await api.get<DashboardResponse>("/admin/ingest/metrics/league-dashboard", params);
    leagues.value = resp.leagues;
    // Sync active jobs into polling map
    for (const league of resp.leagues) {
      for (const season of league.seasons) {
        if (season.active_job) {
          jobBySeason[season.id] = season.active_job.job_id;
        }
      }
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

async function refreshFromSportmonks(): Promise<void> {
  if (refreshingDiscovery.value) return;
  refreshingDiscovery.value = true;
  error.value = "";
  try {
    await api.get("/admin/ingest/discovery", { force: "true" });
    await Promise.all([
      fetchDashboard(true),
      loadOpsSnapshot(),
      leagueStore.fetchNavigation(true),
    ]);
    lastDashboardRefresh = Date.now();
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    refreshingDiscovery.value = false;
  }
}

// ---- Ops Snapshot ----

async function loadOpsSnapshot(): Promise<void> {
  try {
    opsSnapshot.value = await api.get<OpsSnapshot>("/admin/ingest/ops/snapshot");
  } catch { /* silent */ }
}

// ---- Z1: Job-Auto-Select ----

function autoSelectActiveJobLeague(): void {
  if (selectedLeagueId.value !== null) return;
  const leagueWithJob = leagues.value.find((l) =>
    l.seasons.some((s) => s.active_job && ["running", "queued"].includes(s.active_job.status)),
  );
  if (leagueWithJob) {
    selectedLeagueId.value = leagueWithJob.id;
  } else if (leagues.value.length > 0) {
    selectedLeagueId.value = leagues.value[0].id;
  }
}

// ---- League Toggle ----

async function toggleLeague(league: League): Promise<void> {
  patchBusy[league.id] = true;
  try {
    await api.patch(`/admin/ingest/leagues/${league.id}`, { is_active: !league.is_active });
    league.is_active = !league.is_active;
    leagueStore.fetchNavigation(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    patchBusy[league.id] = false;
  }
}

// ---- Feature Toggles ----

async function patchFeature(league: League, feature: keyof LeagueFeatures, value: boolean): Promise<void> {
  patchBusy[league.id] = true;
  try {
    await api.patch(`/admin/ingest/leagues/${league.id}`, {
      features: { [feature]: value },
    });
    league.features[feature] = value;
    leagueStore.fetchNavigation(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    patchBusy[league.id] = false;
  }
}

// ---- Season Actions ----

async function startDeepSync(seasonId: number): Promise<void> {
  try {
    const resp = await api.post<{ job_id: string }>(`/admin/ingest/season/${seasonId}`, {});
    if (resp.job_id) {
      jobBySeason[seasonId] = resp.job_id;
      startPolling();
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  }
}

async function startRepairOdds(seasonId: number): Promise<void> {
  try {
    const resp = await api.post<{ job_id: string }>(`/admin/ingest/season/${seasonId}/metrics-sync`, {});
    if (resp.job_id) {
      jobBySeason[seasonId] = resp.job_id;
      startPolling();
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  }
}

async function startXgSync(seasonId: number): Promise<void> {
  try {
    const preview = await api.get<{
      finished_total: number;
      already_resolved: number;
      missing: number;
      resolvable: number;
      partial: number;
      no_cache: number;
    }>(`/admin/ingest/season/${seasonId}/xg-sync/preview`);

    const msg = t("admin.leagueControl.season.xgSyncConfirm", {
      inMatch: preview.already_resolved,
      total: preview.finished_total,
      missing: preview.missing,
      resolvable: preview.resolvable,
    });
    if (!window.confirm(msg)) return;

    const resp = await api.post<{ job_id: string }>(`/admin/ingest/season/${seasonId}/xg-sync`, {});
    if (resp.job_id) {
      jobBySeason[seasonId] = resp.job_id;
      startPolling();
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  }
}

async function triggerInitialDiscover(league: League): Promise<void> {
  const seasons = league.seasons;
  if (seasons.length > 0) {
    await startDeepSync(seasons[0].id);
    return;
  }
  error.value = t("admin.leagueControl.empty.noAvailableSeasons");
}

// ---- Resume Job (Z3: separate visual from primary actions) ----

async function resumeJob(jobId: string): Promise<void> {
  try {
    await api.post(`/admin/ingest/jobs/${jobId}/resume`, {});
    startPolling();
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  }
}

// ---- Job Polling (Z2: ops in poll loop, dashboard only on 5-min interval) ----

function hasActiveJobs(): boolean {
  return Object.keys(jobBySeason).length > 0;
}

function nextPollDelay(): number {
  const jobIds = Object.values(jobBySeason);
  if (!jobIds.length) return 10000;
  const statuses = jobIds.map((id) => jobStatusMap[id]).filter(Boolean);
  if (statuses.some((s) => s.status === "running")) return 1500;
  if (statuses.some((s) => s.status === "queued" || s.status === "paused")) return 5000;
  return 10000;
}

async function pollJobs(): Promise<void> {
  const entries = Object.entries(jobBySeason);
  if (!entries.length) return;
  await Promise.all(
    entries.map(async ([seasonIdStr, jobId]) => {
      const seasonId = Number(seasonIdStr);
      try {
        const status = await api.get<JobStatus>(`/admin/ingest/jobs/${jobId}`);
        jobStatusMap[jobId] = status;
        const isTerminal = status.status === "failed" || status.status === "succeeded";
        if (isTerminal) {
          delete jobBySeason[seasonId];
          if (status.status === "succeeded") {
            adminSyncStore.notifySyncCompleted();
          }
          await fetchDashboard(true);
          lastDashboardRefresh = Date.now();
        }
      } catch {
        delete jobBySeason[seasonId];
      }
    }),
  );
}

async function pollLoop(): Promise<void> {
  await loadOpsSnapshot();
  await pollJobs();
  // Z2: Hard-refresh dashboard only every 5 min (sparklines etc.)
  if (Date.now() - lastDashboardRefresh > DASHBOARD_REFRESH_INTERVAL) {
    await fetchDashboard();
    lastDashboardRefresh = Date.now();
  }
  clearPolling();
  if (hasActiveJobs()) {
    pollTimer = window.setTimeout(() => void pollLoop(), nextPollDelay());
  }
}

function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = window.setTimeout(() => void pollLoop(), 1500);
}

function clearPolling(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

// ---- Helpers ----

function healthColor(status: string): string {
  if (status === "green") return "bg-success";
  if (status === "yellow") return "bg-warning";
  return "bg-danger";
}

function healthBorder(status: string): string {
  if (status === "green") return "border-success/30";
  if (status === "yellow") return "border-warning/30";
  return "border-danger/30";
}

function sparkMax(sparkline: number[]): number {
  return Math.max(...sparkline, 1);
}

function getSeasonJob(seasonId: number): JobStatus | null {
  const jobId = jobBySeason[seasonId];
  return jobId ? jobStatusMap[jobId] ?? null : null;
}

function isSeasonBusy(seasonId: number): boolean {
  return seasonId in jobBySeason;
}

function statusBadgeLabel(league: League): string {
  if (league.is_active && league.features.tipping) return t("admin.leagueControl.status.liveNav");
  if (league.is_active && league.features.match_load) return t("admin.leagueControl.status.dataOnly");
  return t("admin.leagueControl.status.hidden");
}

function statusBadgeClass(league: League): string {
  if (league.is_active && league.features.tipping) return "bg-primary/15 text-primary";
  if (league.is_active && league.features.match_load) return "bg-sky-500/15 text-sky-400";
  return "bg-surface-2 text-text-muted";
}

function formatTimeAgo(iso: string | null): string {
  if (!iso) return t("admin.leagueControl.status.never");
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function formatSeconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return "–";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function formatThroughput(v: number | null | undefined): string {
  return v != null ? String(Math.round(v)) : "–";
}

function jobTypeLabel(type: string): string {
  if (type === "sportmonks_metrics_sync") return "Metrics Sync";
  if (type === "sportmonks_deep_ingest") return "Deep Ingest";
  return type;
}

function latestErrors(job: JobStatus | null): Array<{ timestamp: string; round_id?: number | null; error_msg: string }> {
  if (!job?.error_log?.length) return [];
  return [...job.error_log].slice(-3).reverse();
}

function canResume(job: JobStatus | null): boolean {
  if (!job) return false;
  return job.status === "paused" || job.can_retry === true;
}

// ---- Lifecycle ----

onMounted(async () => {
  await fetchDashboard();
  lastDashboardRefresh = Date.now();
  autoSelectActiveJobLeague(); // Z1
  await loadOpsSnapshot();
  if (hasActiveJobs()) startPolling();
});

onUnmounted(() => clearPolling());
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <!-- Header -->
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h1 class="text-xl font-bold text-text-primary">{{ t("admin.leagueControl.title") }}</h1>
          <p class="text-sm text-text-muted mt-1">{{ t("admin.leagueControl.subtitle") }}</p>
        </div>
        <button
          type="button"
          class="rounded-card bg-primary px-3 py-1.5 text-xs font-semibold text-white hover:bg-primary/90 disabled:opacity-50"
          :disabled="refreshingDiscovery"
          @click="refreshFromSportmonks"
        >
          {{ refreshingDiscovery ? t("admin.leagueControl.season.syncing") : t("admin.dataAudit.fix.refreshDiscovery") }}
        </button>
      </div>

      <!-- Ops Snapshot Strip -->
      <div v-if="opsSnapshot" class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
        <span>
          {{ t("admin.leagueControl.ops.queue") }}:
          <span class="font-mono text-text-primary">{{ opsSnapshot.queue_metrics.queued }}/{{ opsSnapshot.queue_metrics.running }}/{{ opsSnapshot.queue_metrics.paused }}</span>
        </span>
        <span>
          {{ t("admin.leagueControl.ops.cache") }}:
          <span class="font-mono text-text-primary">{{ Math.round((opsSnapshot.cache_metrics.hit_ratio || 0) * 100) }}%</span>
        </span>
        <span>
          {{ t("admin.leagueControl.ops.savings") }}:
          <span class="font-mono text-text-primary">{{ Math.round((opsSnapshot.efficiency.api_savings_ratio || 0) * 100) }}%</span>
        </span>
        <span>
          {{ t("admin.leagueControl.ops.rate") }}:
          <span class="font-mono text-text-primary">{{ opsSnapshot.api_health.remaining ?? "–" }}</span>
        </span>
        <span v-if="opsSnapshot.guard_metrics.page_guard_blocks > 0" class="text-warning">
          {{ t("admin.leagueControl.ops.guard") }}:
          <span class="font-mono">{{ opsSnapshot.guard_metrics.page_guard_blocks }}</span>
        </span>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="space-y-3">
      <div class="grid grid-cols-[280px_1fr] gap-4">
        <div class="space-y-2">
          <div v-for="n in 5" :key="n" class="bg-surface-1 rounded-card h-24 animate-pulse border border-surface-3/50" />
        </div>
        <div class="bg-surface-1 rounded-card h-64 animate-pulse border border-surface-3/50" />
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="error && !leagues.length" class="rounded-card border border-surface-3/60 bg-surface-1 p-6 text-center">
      <p class="text-sm text-danger">{{ error }}</p>
      <button
        type="button"
        class="mt-3 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-sm text-text-secondary hover:border-primary/60"
        @click="fetchDashboard(true)"
      >
        {{ t("common.retry") }}
      </button>
    </div>

    <!-- Empty -->
    <div v-else-if="!leagues.length" class="rounded-card border border-surface-3/60 bg-surface-1 p-6 text-center text-sm text-text-muted">
      {{ t("admin.leagueControl.empty.noLeagues") }}
    </div>

    <!-- Master-Detail Layout -->
    <div v-else class="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-4">
      <!-- Sidebar: League List -->
      <aside class="space-y-1.5">
        <div
          v-for="league in leagues"
          :key="league.id"
          class="rounded-card border p-3 cursor-pointer transition-colors"
          :class="
            selectedLeagueId === league.id
              ? 'border-primary/60 bg-primary/5'
              : 'border-surface-3/50 bg-surface-1 hover:border-surface-3'
          "
          @click="selectedLeagueId = league.id"
        >
          <div class="flex items-center gap-2">
            <!-- Active Toggle -->
            <button
              type="button"
              role="switch"
              :aria-checked="league.is_active"
              :disabled="Boolean(patchBusy[league.id])"
              class="relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200 focus:outline-none disabled:opacity-50"
              :class="league.is_active ? 'bg-primary' : 'bg-surface-3'"
              @click.stop="toggleLeague(league)"
            >
              <span
                class="pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform duration-200 mt-0.5"
                :class="league.is_active ? 'translate-x-[18px]' : 'translate-x-0.5'"
              />
            </button>

            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium text-text-primary truncate">{{ league.name }}</p>
              <p class="text-xs text-text-muted">{{ league.country }}</p>
            </div>
          </div>

          <!-- Status Badge + Last Synced -->
          <div class="mt-1.5 flex items-center gap-2 flex-wrap">
            <span class="inline-flex rounded-full px-1.5 py-0.5 text-[11px] font-medium" :class="statusBadgeClass(league)">
              {{ statusBadgeLabel(league) }}
            </span>
            <span class="text-[11px] text-text-muted">{{ formatTimeAgo(league.last_synced_at) }}</span>
          </div>

          <!-- Feature Toggles (compact checkboxes) -->
          <div class="mt-1.5 space-y-0.5" :class="{ 'opacity-40 pointer-events-none': !league.is_active }">
            <label class="flex items-center gap-1.5 text-[11px] text-text-secondary" @click.stop>
              <input
                type="checkbox"
                :checked="league.features.tipping"
                :disabled="!league.is_active || Boolean(patchBusy[league.id])"
                class="rounded border-surface-3 text-primary focus:ring-primary/30 h-3 w-3"
                @change="patchFeature(league, 'tipping', !league.features.tipping)"
              />
              {{ t("admin.leagueControl.toggle.tipping") }}
            </label>
            <label class="flex items-center gap-1.5 text-[11px] text-text-secondary" @click.stop>
              <input
                type="checkbox"
                :checked="league.features.match_load"
                :disabled="!league.is_active || Boolean(patchBusy[league.id])"
                class="rounded border-surface-3 text-primary focus:ring-primary/30 h-3 w-3"
                @change="patchFeature(league, 'match_load', !league.features.match_load)"
              />
              {{ t("admin.leagueControl.toggle.matchLoad") }}
            </label>
          </div>

          <!-- Credit info + Sparkline -->
          <div class="mt-2 flex items-end justify-between">
            <span class="text-xs text-text-muted tabular-nums">
              {{ league.credit_total_30d.toLocaleString() }} {{ t("admin.leagueControl.sidebar.credits30d") }}
            </span>
            <!-- CSS Sparkline -->
            <div class="flex items-end gap-px h-4">
              <div
                v-for="(val, i) in league.credit_sparkline_7d"
                :key="i"
                class="w-1 rounded-t-sm"
                :class="league.is_active ? 'bg-primary/60' : 'bg-surface-3'"
                :style="{ height: `${Math.max((val / sparkMax(league.credit_sparkline_7d)) * 100, 4)}%` }"
              />
            </div>
          </div>
        </div>
      </aside>

      <!-- Detail Panel: Seasons -->
      <main>
        <!-- Error banner (non-blocking) -->
        <div v-if="error" class="rounded-card border border-danger/30 bg-danger/5 p-3 mb-4 text-sm text-danger">
          {{ error }}
          <button class="ml-2 underline" @click="error = ''">dismiss</button>
        </div>

        <template v-if="selectedLeague">
          <!-- Empty State: No seasons with matches -->
          <div
            v-if="selectedLeague.seasons.length === 0 || selectedLeague.seasons.every((s) => s.health.total_fixtures === 0)"
            class="rounded-card border-2 border-dashed border-surface-3/80 bg-surface-1/50 p-8 text-center"
          >
            <div class="text-3xl mb-3">&#x1F4E1;</div>
            <h3 class="text-lg font-semibold text-text-primary">
              {{ t("admin.leagueControl.empty.noSeasons") }}
            </h3>
            <p class="text-sm text-text-muted mt-1 mb-4">
              {{ t("admin.leagueControl.empty.discoverHint") }}
            </p>
            <button
              v-if="selectedLeague.seasons.length > 0"
              type="button"
              :disabled="isSeasonBusy(selectedLeague.seasons[0].id)"
              class="rounded-card bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary/90 transition-colors disabled:opacity-50"
              @click="triggerInitialDiscover(selectedLeague)"
            >
              {{ t("admin.leagueControl.empty.discoverButton") }}
            </button>
            <p v-else class="text-xs text-text-muted">
              {{ t("admin.leagueControl.empty.noAvailableSeasons") }}
            </p>
          </div>

          <!-- Season Cards Grid -->
          <div v-else class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div
              v-for="season in selectedLeague.seasons"
              :key="season.id"
              class="rounded-card border bg-surface-1 p-4"
              :class="healthBorder(season.health.status)"
            >
              <!-- Header -->
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-semibold text-text-primary">{{ season.name }}</h3>
                <span
                  class="inline-block w-2.5 h-2.5 rounded-full"
                  :class="healthColor(season.health.status)"
                  :title="t(`admin.leagueControl.season.health.${season.health.status}`)"
                />
              </div>

              <!-- Progress Bar: finished / total -->
              <div class="mb-3">
                <div class="flex justify-between text-xs text-text-muted mb-1">
                  <span>{{ season.health.finished }} / {{ season.health.total_fixtures }} {{ t("admin.leagueControl.season.fixtures") }}</span>
                  <span v-if="season.health.total_fixtures > 0" class="tabular-nums">
                    {{ Math.round((season.health.finished / season.health.total_fixtures) * 100) }}%
                  </span>
                </div>
                <div class="h-1.5 rounded bg-surface-3/60 overflow-hidden">
                  <div
                    class="h-full bg-primary transition-all"
                    :style="{
                      width: season.health.total_fixtures > 0
                        ? `${(season.health.finished / season.health.total_fixtures) * 100}%`
                        : '0%',
                    }"
                  />
                </div>
              </div>

              <!-- Metrics Grid -->
              <div class="grid grid-cols-3 gap-2 mb-3 text-xs">
                <div>
                  <p class="text-text-muted">xG</p>
                  <p class="text-text-primary font-medium tabular-nums">
                    {{ season.health.xg_covered }}/{{ season.health.finished }} {{ t("admin.leagueControl.season.xgInMatch") }}
                  </p>
                  <p class="text-text-muted tabular-nums text-[10px]">
                    {{ season.health.xg_cache_count ?? 0 }}/{{ season.health.finished }} {{ t("admin.leagueControl.season.xgInCache") }}
                  </p>
                </div>
                <div>
                  <p class="text-text-muted">Odds</p>
                  <p class="text-text-primary font-medium tabular-nums">{{ season.health.odds_pct }}%</p>
                </div>
                <div>
                  <p class="text-text-muted">{{ t("admin.leagueControl.season.scheduled") }}</p>
                  <p class="text-text-primary font-medium tabular-nums">{{ season.health.scheduled }}</p>
                </div>
                <div v-if="season.health.manual_check_count > 0">
                  <p class="text-warning">{{ t("admin.leagueControl.season.manualChecks") }}</p>
                  <p class="text-warning font-medium tabular-nums">{{ season.health.manual_check_count }}</p>
                </div>
              </div>

              <!-- Rich Job Progress -->
              <div v-if="getSeasonJob(season.id)" class="mb-3 rounded bg-surface-2/40 p-3 space-y-2">
                <!-- Header: Type + Status -->
                <div class="flex items-center justify-between text-xs">
                  <span class="font-medium text-primary">
                    {{ jobTypeLabel(getSeasonJob(season.id)!.type) }} — {{ getSeasonJob(season.id)!.phase || getSeasonJob(season.id)!.status }}
                  </span>
                  <span v-if="getSeasonJob(season.id)!.rate_limit_paused" class="text-warning">
                    {{ t("admin.leagueControl.job.ratePaused") }}
                  </span>
                </div>

                <!-- Job Progress Bar -->
                <div class="h-1.5 rounded bg-surface-3/60 overflow-hidden">
                  <div
                    class="h-full bg-primary transition-all"
                    :style="{ width: `${getSeasonJob(season.id)!.progress?.percent ?? 0}%` }"
                  />
                </div>

                <!-- Metrics Grid -->
                <div class="grid grid-cols-3 gap-x-3 gap-y-1 text-xs text-text-muted tabular-nums">
                  <span>{{ getSeasonJob(season.id)!.progress?.processed ?? 0 }}/{{ getSeasonJob(season.id)!.progress?.total ?? "?" }}</span>
                  <span>{{ formatThroughput(getSeasonJob(season.id)!.throughput_per_min) }}/min</span>
                  <span>ETA {{ formatSeconds(getSeasonJob(season.id)!.eta_seconds) }}</span>
                  <span>HB {{ formatSeconds(getSeasonJob(season.id)!.heartbeat_age_seconds) }}</span>
                  <span>Pages {{ getSeasonJob(season.id)!.page_requests_total ?? 0 }}</span>
                  <span v-if="getSeasonJob(season.id)!.duplicate_page_blocks">Dupes {{ getSeasonJob(season.id)!.duplicate_page_blocks }}</span>
                </div>

                <!-- Stale Warning -->
                <p v-if="getSeasonJob(season.id)!.is_stale" class="text-xs text-danger">
                  {{ t("admin.leagueControl.job.stale") }}
                </p>

                <!-- Z3: Resume/Retry — amber outline, NOT primary -->
                <button
                  v-if="canResume(getSeasonJob(season.id))"
                  type="button"
                  class="text-xs px-2 py-0.5 rounded border border-amber-400/40 text-amber-400 hover:bg-amber-400/10 transition-colors"
                  @click="resumeJob(getSeasonJob(season.id)!.job_id)"
                >
                  {{ t("admin.leagueControl.job.resume") }}
                </button>

                <!-- Error Log (last 3) -->
                <div v-if="latestErrors(getSeasonJob(season.id)).length" class="space-y-1">
                  <div
                    v-for="err in latestErrors(getSeasonJob(season.id))"
                    :key="err.timestamp"
                    class="text-xs text-danger/80 truncate"
                  >
                    <span v-if="err.round_id != null" class="text-text-muted">R{{ err.round_id }} · </span>
                    {{ err.error_msg }}
                  </div>
                </div>
              </div>

              <!-- Action Buttons (primary styling) -->
              <div class="flex gap-2">
                <button
                  type="button"
                  :disabled="isSeasonBusy(season.id)"
                  class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-xs text-text-secondary hover:border-primary/60 transition-colors disabled:opacity-50"
                  @click="startDeepSync(season.id)"
                >
                  {{ t("admin.leagueControl.season.deepSync") }}
                </button>
                <button
                  type="button"
                  :disabled="isSeasonBusy(season.id)"
                  class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-xs text-text-secondary hover:border-primary/60 transition-colors disabled:opacity-50"
                  @click="startRepairOdds(season.id)"
                >
                  {{ t("admin.leagueControl.season.repairOdds") }}
                </button>
                <button
                  type="button"
                  :disabled="isSeasonBusy(season.id)"
                  class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-xs text-text-secondary hover:border-primary/60 transition-colors disabled:opacity-50"
                  @click="startXgSync(season.id)"
                >
                  {{ t("admin.leagueControl.season.xgSync") }}
                </button>
              </div>
            </div>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>
