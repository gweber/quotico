<!--
frontend/src/views/admin/AdminLeagueTowerView.vue

Purpose:
    Central configuration hub for leagues. Controls sync status, tipping
    availability, and data-only modes (e.g. Champions League).

Dependencies:
    - app.services.admin_service
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import draggable from "vuedraggable";
import { HttpError, useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

interface LeagueFeatures {
  tipping: boolean;
  match_load: boolean;
  xg_sync: boolean;
  odds_sync: boolean;
}

interface LeagueItem {
  id: string;
  league_id: number;
  display_name: string;
  structure_type: "league" | "cup" | "tournament";
  season_start_month: number;
  country_code: string | null;
  current_season: number;
  ui_order: number;
  is_active: boolean;
  features: LeagueFeatures;
  external_ids: Record<string, string>;
}

interface LeagueListResponse {
  items: LeagueItem[];
}

type UnifiedIngestSource =
  | "sportmonks"
  | "matchday_sync"
  | "xg_enrichment";
type AdminJobStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";

interface UnifiedIngestState {
  source: UnifiedIngestSource;
  seasonInput: string;
  dryRun: boolean;
  hasPreview: boolean;
}

interface UnifiedMatchIngestJobStartResponse {
  accepted: boolean;
  job_id: string;
  league_id: number;
  source: UnifiedIngestSource;
  season: string | number | null;
  dry_run: boolean;
  status: string;
}

interface UnifiedMatchIngestResult {
  processed?: number;
  created?: number;
  updated?: number;
  skipped?: number;
  conflicts?: number;
  matched?: number;
  unmatched?: number;
  already_enriched?: number;
  match_ingest?: {
    matched_by_external_id?: number;
    matched_by_identity_window?: number;
    team_name_conflict?: number;
    other_conflicts?: number;
  };
  conflicts_preview?: Array<Record<string, unknown>>;
  alias_suggestions?: Array<Record<string, unknown>>;
  unmatched_teams?: string[];
  raw_rows_preview?: Array<Record<string, unknown>>;
}

interface UnifiedMatchIngestJobStatusResponse {
  job_id: string;
  type: string;
  source: UnifiedIngestSource | "";
  status: AdminJobStatus;
  phase: string;
  progress: { processed: number; total: number; percent: number };
  counters: Record<string, number>;
  results: UnifiedMatchIngestResult | null;
  error: { message: string; type: string } | null;
}

const api = useApi();
const toast = useToast();
const { t } = useI18n();

const loading = ref(true);
const leagues = ref<LeagueItem[]>([]);
const syncBusyById = reactive<Record<string, boolean>>({});
const toggleBusyById = reactive<Record<string, boolean>>({});
const orderSaving = ref(false);
const editBusy = ref(false);
const editOpen = ref(false);
const editingLeagueId = ref<string>("");
const unifiedIngestStateByLeagueId = reactive<Record<string, UnifiedIngestState>>({});
const unifiedIngestBusyByLeagueId = reactive<Record<string, boolean>>({});
const unifiedIngestJobByLeagueId = reactive<Record<string, string>>({});
const unifiedIngestJobStatusByLeagueId = reactive<Record<string, UnifiedMatchIngestJobStatusResponse | null>>({});
const unifiedIngestResultsByLeagueId = reactive<Record<string, UnifiedMatchIngestResult | null>>({});
const unifiedIngestPollTimerByLeagueId = reactive<Record<string, number | null>>({});
const xgRawFilterByLeagueId = reactive<Record<string, { action: string; query: string }>>({});

const editState = reactive({
  display_name: "",
  structure_type: "league" as "league" | "cup" | "tournament",
  season_start_month: 7,
  current_season: new Date().getUTCFullYear(),
  is_active: false,
  features: {
    tipping: false,
    match_load: true,
    xg_sync: false,
    odds_sync: false,
  } as LeagueFeatures,
  external_ids: {
    sportmonks: "",
    understat: "",
  },
});

const selectedLeague = computed(() => leagues.value.find((league) => league.id === editingLeagueId.value) ?? null);

function statusMeta(league: LeagueItem): { label: string; classes: string } {
  if (!league.is_active) {
    return {
      label: t("admin.leagues.status_disabled"),
      classes: "bg-danger-muted/20 text-danger",
    };
  }
  if (league.features.tipping) {
    return {
      label: t("admin.leagues.status_live_betting"),
      classes: "bg-primary-muted/20 text-primary",
    };
  }
  return {
    label: t("admin.leagues.status_data_only"),
    classes: "bg-sky-100 text-sky-800",
  };
}

async function fetchLeagues(): Promise<void> {
  loading.value = true;
  try {
    const result = await api.get<LeagueListResponse>("/admin/leagues");
    leagues.value = result.items;
    result.items.forEach((league) => {
      if (!unifiedIngestStateByLeagueId[league.id]) {
        unifiedIngestStateByLeagueId[league.id] = {
          source: defaultUnifiedSource(league),
          seasonInput: defaultUnifiedSeason(league),
          dryRun: true,
          hasPreview: false,
        };
      }
      ensureXgRawFilter(league.id);
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    loading.value = false;
  }
}

async function toggleActive(league: LeagueItem): Promise<void> {
  toggleBusyById[league.id] = true;
  try {
    await api.patch(`/admin/leagues/${league.id}`, { is_active: !league.is_active });
    league.is_active = !league.is_active;
    toast.success(t("admin.leagues.updated"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    toggleBusyById[league.id] = false;
  }
}

async function triggerSync(league: LeagueItem): Promise<void> {
  syncBusyById[league.id] = true;
  try {
    const result = await api.post<{ message: string }>(`/admin/leagues/${league.id}/sync`);
    toast.success(result.message || t("admin.leagues.sync_triggered"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    syncBusyById[league.id] = false;
  }
}

function importPhaseLabel(phase: string): string {
  const byPhase: Record<string, string> = {
    queued: t("admin.leagues.import.phase.queued"),
    fetching_csv: t("admin.leagues.import.phase.fetching_csv"),
    matching: t("admin.leagues.import.phase.matching"),
    ingesting_odds: t("admin.leagues.import.phase.ingesting_odds"),
    finalizing: t("admin.leagues.import.phase.finalizing"),
    done: t("admin.leagues.import.phase.done"),
  };
  return byPhase[phase] || phase;
}

function importStatusLabel(status: AdminJobStatus): string {
  const byStatus: Record<AdminJobStatus, string> = {
    queued: t("admin.leagues.import.status.queued"),
    running: t("admin.leagues.import.status.running"),
    succeeded: t("admin.leagues.import.status.succeeded"),
    failed: t("admin.leagues.import.status.failed"),
    canceled: t("admin.leagues.import.status.canceled"),
  };
  return byStatus[status];
}

function unifiedSources(): Array<{ value: UnifiedIngestSource; label: string }> {
  return [
    { value: "sportmonks", label: t("admin.leagues.unified_ingest.sources.sportmonks") },
    { value: "matchday_sync", label: t("admin.leagues.unified_ingest.sources.matchday_sync") },
    { value: "xg_enrichment", label: t("admin.leagues.unified_ingest.sources.xg_enrichment") },
  ];
}

function defaultUnifiedSeason(league: LeagueItem): string {
  return String(league.current_season || new Date().getUTCFullYear());
}

function defaultUnifiedSource(league: LeagueItem): UnifiedIngestSource {
  if (league.external_ids.sportmonks) return "sportmonks";
  return "matchday_sync";
}

function sourceNeedsSeason(source: UnifiedIngestSource): boolean {
  return source === "sportmonks" || source === "matchday_sync" || source === "xg_enrichment";
}

function sourceSeasonPlaceholder(source: UnifiedIngestSource): string {
  if (source === "xg_enrichment") return "2025 or 2024-2025";
  return "2025";
}

function canUseUnifiedSource(league: LeagueItem, source: UnifiedIngestSource): boolean {
  if (source === "sportmonks") return true;
  if (source === "matchday_sync") return true;
  if (source === "xg_enrichment") return isXgEligible(league);
  return false;
}

function isDirectLeagueSyncSource(source: UnifiedIngestSource): boolean {
  return source === "matchday_sync";
}

function unifiedSeasonPayload(league: LeagueItem): number | string | null {
  const state = unifiedIngestStateByLeagueId[league.id];
  if (!state) return null;
  const source = state.source;
  if (!sourceNeedsSeason(source)) return null;
  const seasonRaw = (state.seasonInput || "").trim();
  if (!seasonRaw) return null;
  if (source === "xg_enrichment") return seasonRaw;
  const parsed = Number(seasonRaw);
  if (!Number.isFinite(parsed)) return null;
  return Math.trunc(parsed);
}

function isUnifiedSeasonValid(league: LeagueItem): boolean {
  const state = unifiedIngestStateByLeagueId[league.id];
  if (!state) return false;
  if (!sourceNeedsSeason(state.source)) return true;
  const seasonRaw = (state.seasonInput || "").trim();
  if (state.source === "xg_enrichment") return /^\d{4}(-\d{4})?$/.test(seasonRaw);
  return /^\d{4}$/.test(seasonRaw);
}

function clearUnifiedIngestPolling(leagueId: string): void {
  const timer = unifiedIngestPollTimerByLeagueId[leagueId];
  if (timer !== null && timer !== undefined) {
    window.clearInterval(timer);
  }
  unifiedIngestPollTimerByLeagueId[leagueId] = null;
}

function isUnifiedIngestRunning(leagueId: string): boolean {
  const status = unifiedIngestJobStatusByLeagueId[leagueId]?.status;
  return status === "queued" || status === "running";
}

function groupedUnifiedConflicts(leagueId: string): Record<string, Array<Record<string, unknown>>> {
  const raw = unifiedIngestResultsByLeagueId[leagueId]?.conflicts_preview;
  const groups: Record<string, Array<Record<string, unknown>>> = {
    unresolved_league: [],
    unresolved_team: [],
    team_name_conflict: [],
    other_conflicts: [],
  };
  for (const item of raw || []) {
    const code = String(item.code || "");
    if (code === "unresolved_league") groups.unresolved_league.push(item);
    else if (code === "unresolved_team") groups.unresolved_team.push(item);
    else if (code === "team_name_conflict") groups.team_name_conflict.push(item);
    else groups.other_conflicts.push(item);
  }
  return groups;
}

function unifiedActionHint(code: string): string {
  const mapping: Record<string, string> = {
    unresolved_league: t("admin.leagues.unified_ingest.hints.unresolved_league"),
    unresolved_team: t("admin.leagues.unified_ingest.hints.unresolved_team"),
    team_name_conflict: t("admin.leagues.unified_ingest.hints.team_name_conflict"),
    other_conflicts: t("admin.leagues.unified_ingest.hints.other_conflicts"),
  };
  return mapping[code] || mapping.other_conflicts;
}

function isUnifiedXgSource(leagueId: string): boolean {
  return unifiedIngestJobStatusByLeagueId[leagueId]?.type === "xg_enrichment"
    || unifiedIngestStateByLeagueId[leagueId]?.source === "xg_enrichment";
}

function unifiedXgUnmatchedTeams(leagueId: string): string[] {
  const results = unifiedIngestResultsByLeagueId[leagueId] || (unifiedIngestJobStatusByLeagueId[leagueId]?.results as UnifiedMatchIngestResult | null);
  return (results?.unmatched_teams || []) as string[];
}

function unifiedXgRawRows(leagueId: string): Array<Record<string, unknown>> {
  const results = unifiedIngestResultsByLeagueId[leagueId] || (unifiedIngestJobStatusByLeagueId[leagueId]?.results as UnifiedMatchIngestResult | null);
  return (results?.raw_rows_preview || []) as Array<Record<string, unknown>>;
}

function ensureXgRawFilter(leagueId: string): void {
  if (!xgRawFilterByLeagueId[leagueId]) {
    xgRawFilterByLeagueId[leagueId] = { action: "all", query: "" };
  }
}

function filteredUnifiedXgRawRows(leagueId: string): Array<Record<string, unknown>> {
  ensureXgRawFilter(leagueId);
  const rows = unifiedXgRawRows(leagueId);
  const filter = xgRawFilterByLeagueId[leagueId];
  const action = (filter.action || "all").trim().toLowerCase();
  const query = (filter.query || "").trim().toLowerCase();
  return rows.filter((row) => {
    const rowAction = String(row.action || "").toLowerCase();
    if (action !== "all" && rowAction !== action) return false;
    if (!query) return true;
    const haystack = [
      String(row.reason || ""),
      String(row.home_team || ""),
      String(row.away_team || ""),
      String(row.date || ""),
      String(row.home_xg ?? ""),
      String(row.away_xg ?? ""),
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function unifiedRunButtonDisabled(league: LeagueItem): boolean {
  const state = unifiedIngestStateByLeagueId[league.id];
  if (!state) return true;
  if (!canUseUnifiedSource(league, state.source)) return true;
  if (!isUnifiedSeasonValid(league)) return true;
  return isUnifiedIngestRunning(league.id);
}

function unifiedRunButtonLabel(league: LeagueItem): string {
  const source = unifiedIngestStateByLeagueId[league.id]?.source;
  if (source === "matchday_sync") return t("admin.leagues.trigger_sync");
  return t("admin.leagues.unified_ingest.run_button");
}

function isXgEligible(league: LeagueItem): boolean {
  return Boolean(league.is_active && league.features.xg_sync && league.external_ids.understat);
}

async function runUnifiedMatchIngest(league: LeagueItem, dryRun: boolean): Promise<void> {
  const state = unifiedIngestStateByLeagueId[league.id];
  if (!state) return;
  if (state.source === "xg_enrichment") {
    if (!canUseUnifiedSource(league, state.source)) {
      toast.error(t("admin.leagues.unified_ingest.source_unavailable"));
      return;
    }
    if (!isUnifiedSeasonValid(league)) {
      toast.error(t("admin.leagues.unified_ingest.invalid_season"));
      return;
    }
    unifiedIngestBusyByLeagueId[league.id] = true;
    try {
      const season = String(unifiedSeasonPayload(league) ?? "").trim() || null;
      const start = await api.post<{ job_id: string }>(
        "/admin/enrich-xg/async",
        {
          league_id: league.league_id,
          season,
          dry_run: dryRun,
          force: false,
        },
      );
      unifiedIngestJobByLeagueId[league.id] = start.job_id;
      unifiedIngestJobStatusByLeagueId[league.id] = {
        job_id: start.job_id,
        type: "xg_enrichment",
        source: state.source,
        status: "queued",
        phase: "queued",
        progress: { processed: 0, total: 0, percent: 0 },
        counters: {},
        results: null,
        error: null,
      };
      clearUnifiedIngestPolling(league.id);
      const poll = async () => {
        const jobId = unifiedIngestJobByLeagueId[league.id];
        if (!jobId) return;
        try {
          const status = await api.get<UnifiedMatchIngestJobStatusResponse>(`/admin/leagues/import-jobs/${jobId}`);
          unifiedIngestJobStatusByLeagueId[league.id] = status;
          if (status.status === "succeeded") {
            unifiedIngestResultsByLeagueId[league.id] = status.results as UnifiedMatchIngestResult;
            state.hasPreview = dryRun;
            clearUnifiedIngestPolling(league.id);
            unifiedIngestBusyByLeagueId[league.id] = false;
            toast.success(dryRun ? t("admin.leagues.unified_ingest.preview_ready") : t("admin.leagues.unified_ingest.run_done"));
          } else if (status.status === "failed" || status.status === "canceled") {
            clearUnifiedIngestPolling(league.id);
            unifiedIngestBusyByLeagueId[league.id] = false;
            toast.error(status.error?.message || t("common.genericError"));
          }
        } catch (error) {
          clearUnifiedIngestPolling(league.id);
          unifiedIngestBusyByLeagueId[league.id] = false;
          toast.error(error instanceof Error ? error.message : t("common.genericError"));
        }
      };
      void poll();
      unifiedIngestPollTimerByLeagueId[league.id] = window.setInterval(() => void poll(), 1500);
      toast.success(t("admin.leagues.unified_ingest.job_started"));
    } catch (error) {
      if (error instanceof HttpError && error.status === 429) {
        toast.error(t("admin.leagues.unified_ingest.rate_limited"));
      } else {
        toast.error(error instanceof Error ? error.message : t("common.genericError"));
      }
      unifiedIngestBusyByLeagueId[league.id] = false;
    }
    return;
  }
  if (isDirectLeagueSyncSource(state.source)) {
    if (dryRun) return;
    syncBusyById[league.id] = true;
    try {
      const seasonPayload = unifiedSeasonPayload(league);
      const season = typeof seasonPayload === "number" ? seasonPayload : Number(seasonPayload);
      const result = await api.post<{ message: string }>(`/admin/leagues/${league.id}/sync`, {
        season: Number.isFinite(season) ? Math.trunc(season) : null,
        full_season: true,
      });
      toast.success(result.message || t("admin.leagues.sync_triggered"));
    } catch (error) {
      const message = error instanceof Error ? error.message : t("common.genericError");
      toast.error(message);
    } finally {
      syncBusyById[league.id] = false;
    }
    return;
  }
  if (!canUseUnifiedSource(league, state.source)) {
    toast.error(t("admin.leagues.unified_ingest.source_unavailable"));
    return;
  }
  if (!isUnifiedSeasonValid(league)) {
    toast.error(t("admin.leagues.unified_ingest.invalid_season"));
    return;
  }
  if (!dryRun && !state.hasPreview) {
    const confirmed = window.confirm(t("admin.leagues.unified_ingest.confirm_without_preview"));
    if (!confirmed) return;
  }
  if (!dryRun) {
    const confirmedRun = window.confirm(
      t("admin.leagues.unified_ingest.confirm_run", {
        league: league.display_name,
        source: unifiedSources().find((entry) => entry.value === state.source)?.label || state.source,
      }),
    );
    if (!confirmedRun) return;
  }

  unifiedIngestBusyByLeagueId[league.id] = true;
  try {
    const start = await api.post<UnifiedMatchIngestJobStartResponse>(
      `/admin/leagues/${league.id}/match-ingest/async`,
      {
        source: state.source,
        season: unifiedSeasonPayload(league),
        dry_run: dryRun,
      },
    );
    unifiedIngestJobByLeagueId[league.id] = start.job_id;
    unifiedIngestJobStatusByLeagueId[league.id] = {
      job_id: start.job_id,
      type: "match_ingest_unified",
      source: state.source,
      status: "queued",
      phase: "queued",
      progress: { processed: 0, total: 0, percent: 0 },
      counters: {},
      results: null,
      error: null,
    };
    clearUnifiedIngestPolling(league.id);
    const poll = async () => {
      const jobId = unifiedIngestJobByLeagueId[league.id];
      if (!jobId) return;
      try {
        const status = await api.get<UnifiedMatchIngestJobStatusResponse>(`/admin/leagues/import-jobs/${jobId}`);
        unifiedIngestJobStatusByLeagueId[league.id] = status;
        if (status.status === "succeeded") {
          unifiedIngestResultsByLeagueId[league.id] = status.results;
          state.hasPreview = dryRun;
          clearUnifiedIngestPolling(league.id);
          unifiedIngestBusyByLeagueId[league.id] = false;
          toast.success(dryRun ? t("admin.leagues.unified_ingest.preview_ready") : t("admin.leagues.unified_ingest.run_done"));
        } else if (status.status === "failed" || status.status === "canceled") {
          clearUnifiedIngestPolling(league.id);
          unifiedIngestBusyByLeagueId[league.id] = false;
          toast.error(status.error?.message || t("common.genericError"));
        }
      } catch (error) {
        clearUnifiedIngestPolling(league.id);
        unifiedIngestBusyByLeagueId[league.id] = false;
        toast.error(error instanceof Error ? error.message : t("common.genericError"));
      }
    };
    void poll();
    unifiedIngestPollTimerByLeagueId[league.id] = window.setInterval(() => void poll(), 1500);
    toast.success(t("admin.leagues.unified_ingest.job_started"));
  } catch (error) {
    if (error instanceof HttpError && error.status === 429) {
      toast.error(t("admin.leagues.unified_ingest.rate_limited"));
    } else {
      toast.error(error instanceof Error ? error.message : t("common.genericError"));
    }
    unifiedIngestBusyByLeagueId[league.id] = false;
  }
}

async function persistOrder(): Promise<void> {
  orderSaving.value = true;
  try {
    await api.put("/admin/leagues/order", {
      league_ids: leagues.value.map((league) => league.id),
    });
    toast.success(t("admin.leagues.order_saved"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
    await fetchLeagues();
  } finally {
    orderSaving.value = false;
  }
}

function openEditModal(league: LeagueItem): void {
  editingLeagueId.value = league.id;
  editState.display_name = league.display_name;
  editState.structure_type = league.structure_type || "league";
  editState.season_start_month = league.season_start_month || 7;
  editState.current_season = league.current_season;
  editState.is_active = league.is_active;
  editState.features = {
    tipping: league.features.tipping,
    match_load: league.features.match_load,
    xg_sync: league.features.xg_sync,
    odds_sync: league.features.odds_sync,
  };
  editState.external_ids = {
    sportmonks: league.external_ids.sportmonks || "",
    understat: league.external_ids.understat || "",
  };
  editOpen.value = true;
}

async function saveEdit(): Promise<void> {
  if (!editingLeagueId.value) return;
  editBusy.value = true;
  try {
    await api.patch(`/admin/leagues/${editingLeagueId.value}`, {
      display_name: editState.display_name,
      structure_type: editState.structure_type,
      season_start_month: editState.season_start_month,
      current_season: editState.current_season,
      is_active: editState.is_active,
      features: { ...editState.features },
      external_ids: { ...editState.external_ids },
    });
    toast.success(t("admin.leagues.updated"));
    editOpen.value = false;
    await fetchLeagues();
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    editBusy.value = false;
  }
}

onMounted(() => {
  void fetchLeagues();
});

onUnmounted(() => {
  Object.keys(unifiedIngestPollTimerByLeagueId).forEach((leagueId) => clearUnifiedIngestPolling(leagueId));
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.leagues.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.leagues.subtitle") }}</p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
      <div v-if="loading" class="p-4 space-y-3">
        <div v-for="n in 6" :key="n" class="h-12 rounded bg-surface-2 animate-pulse" />
      </div>

      <div v-else class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.order") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.nameCountry") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.sportKey") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.season") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.status") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.features") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.leagues.table.actions") }}</th>
            </tr>
          </thead>
          <draggable
            v-model="leagues"
            tag="tbody"
            item-key="id"
            handle=".drag-handle"
            @end="persistOrder"
          >
            <template #item="{ element: league }">
              <tr class="border-b border-surface-3/40 last:border-b-0">
                <td class="px-3 py-3 align-middle">
                  <button
                    type="button"
                    class="drag-handle cursor-grab text-text-muted hover:text-text-primary"
                    :title="t('admin.leagues.drag_handle')"
                    :aria-label="t('admin.leagues.drag_handle')"
                    :disabled="orderSaving"
                  >
                    ☰
                  </button>
                </td>
              <td class="px-3 py-3">
                <p class="font-medium text-text-primary">{{ league.display_name }}</p>
                <p class="text-xs text-text-muted">{{ league.country_code || t("admin.leagues.unknownCountry") }}</p>
              </td>
              <td class="px-3 py-3">
                <span class="font-mono text-xs text-text-secondary">{{ league.league_id }}</span>
              </td>
              <td class="px-3 py-3 text-text-primary">{{ league.current_season }}</td>
              <td class="px-3 py-3">
                <span
                  class="inline-flex rounded-full px-2.5 py-1 text-xs font-semibold"
                  :class="statusMeta(league).classes"
                >
                  {{ statusMeta(league).label }}
                </span>
              </td>
              <td class="px-3 py-3">
                <div class="flex flex-wrap gap-1.5">
                  <span
                    class="inline-flex rounded-full bg-surface-2 px-2 py-1 text-xs text-text-secondary"
                    :class="league.features.tipping ? 'ring-1 ring-primary/40' : ''"
                  >
                    {{ t("admin.leagues.features.tipping") }}
                  </span>
                  <span
                    class="inline-flex rounded-full bg-surface-2 px-2 py-1 text-xs text-text-secondary"
                    :class="league.features.match_load ? 'ring-1 ring-primary/40' : ''"
                  >
                    {{ t("admin.leagues.features.match_load") }}
                  </span>
                  <span
                    class="inline-flex rounded-full bg-surface-2 px-2 py-1 text-xs text-text-secondary"
                    :class="league.features.xg_sync ? 'ring-1 ring-primary/40' : ''"
                  >
                    {{ t("admin.leagues.features.xg_sync") }}
                  </span>
                  <span
                    class="inline-flex rounded-full bg-surface-2 px-2 py-1 text-xs text-text-secondary"
                    :class="league.features.odds_sync ? 'ring-1 ring-primary/40' : ''"
                  >
                    {{ t("admin.leagues.features.odds_sync") }}
                  </span>
                </div>
              </td>
              <td class="px-3 py-3">
                <div class="flex flex-wrap items-center gap-2">
                  <label class="inline-flex items-center gap-2 text-xs text-text-secondary">
                    <input
                      type="checkbox"
                      class="h-4 w-4 rounded border-surface-3 bg-surface-0 text-primary"
                      :checked="league.is_active"
                      :disabled="Boolean(toggleBusyById[league.id])"
                      @change="toggleActive(league)"
                    />
                    <span>{{ t("admin.leagues.status_active") }}</span>
                  </label>
                  <button
                    type="button"
                    class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60"
                    :disabled="Boolean(syncBusyById[league.id]) || orderSaving"
                    @click="triggerSync(league)"
                  >
                    {{ syncBusyById[league.id] ? t("admin.leagues.sync_loading") : t("admin.leagues.trigger_sync") }}
                  </button>
                  <button
                    type="button"
                    class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60"
                    :disabled="orderSaving"
                    @click="openEditModal(league)"
                  >
                    {{ t("admin.leagues.edit") }}
                  </button>
                </div>
                <div class="mt-2 rounded-card border border-surface-3/60 bg-surface-0 p-2 space-y-2">
                  <div class="rounded-card border border-surface-3/60 bg-surface-1 p-2 space-y-2">
                    <div class="flex items-center justify-between gap-2">
                      <p class="text-xs font-semibold text-text-primary">{{ t("admin.leagues.unified_ingest.title") }}</p>
                      <span class="text-[10px] text-text-muted">{{ t("admin.leagues.unified_ingest.dry_run_first") }}</span>
                    </div>
                    <div class="grid grid-cols-1 md:grid-cols-4 gap-2">
                      <label class="text-xs text-text-secondary">
                        {{ t("admin.leagues.unified_ingest.source") }}
                        <select
                          v-model="unifiedIngestStateByLeagueId[league.id].source"
                          class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-2 py-1 text-xs text-text-primary"
                        >
                          <option v-for="entry in unifiedSources()" :key="entry.value" :value="entry.value">
                            {{ entry.label }}
                          </option>
                        </select>
                      </label>
                      <label class="text-xs text-text-secondary">
                        {{ t("admin.leagues.unified_ingest.season") }}
                        <input
                          v-model="unifiedIngestStateByLeagueId[league.id].seasonInput"
                          type="text"
                          maxlength="9"
                          class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-2 py-1 text-xs text-text-primary"
                          :disabled="!sourceNeedsSeason(unifiedIngestStateByLeagueId[league.id].source)"
                          :placeholder="sourceSeasonPlaceholder(unifiedIngestStateByLeagueId[league.id].source)"
                        />
                      </label>
                      <button
                        type="button"
                        class="rounded-card border border-primary/60 bg-primary/10 px-2.5 py-1 text-xs text-text-primary hover:bg-primary/20 disabled:opacity-50 mt-5"
                        :disabled="unifiedRunButtonDisabled(league) || isDirectLeagueSyncSource(unifiedIngestStateByLeagueId[league.id].source)"
                        @click="runUnifiedMatchIngest(league, true)"
                      >
                        {{ t("admin.leagues.unified_ingest.preview_button") }}
                      </button>
                      <button
                        type="button"
                        class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50 mt-5"
                        :disabled="unifiedRunButtonDisabled(league)"
                        @click="runUnifiedMatchIngest(league, false)"
                      >
                        {{ unifiedRunButtonLabel(league) }}
                      </button>
                    </div>
                    <p
                      v-if="!canUseUnifiedSource(league, unifiedIngestStateByLeagueId[league.id].source)"
                      class="text-[11px] text-danger"
                    >
                      {{ t("admin.leagues.unified_ingest.source_unavailable") }}
                    </p>
                    <p
                      v-else-if="!isUnifiedSeasonValid(league)"
                      class="text-[11px] text-danger"
                    >
                      {{ t("admin.leagues.unified_ingest.invalid_season") }}
                    </p>
                    <div
                      v-if="unifiedIngestJobStatusByLeagueId[league.id]"
                      class="rounded-card border border-surface-3/60 bg-surface-0 p-2"
                    >
                      <p class="text-[11px] text-text-secondary">
                        {{ t("admin.leagues.unified_ingest.job_status") }}:
                        {{ importStatusLabel(unifiedIngestJobStatusByLeagueId[league.id]?.status || "queued") }}
                      </p>
                      <p class="text-[11px] text-text-muted">
                        {{ t("admin.leagues.unified_ingest.job_phase") }}:
                        {{ importPhaseLabel(unifiedIngestJobStatusByLeagueId[league.id]?.phase || "queued") }}
                      </p>
                      <div class="mt-1 h-1.5 w-full rounded-full bg-surface-2">
                        <div
                          class="h-1.5 rounded-full bg-primary transition-all duration-300"
                          :style="{ width: `${Math.min(100, Math.max(0, unifiedIngestJobStatusByLeagueId[league.id]?.progress?.percent || 0))}%` }"
                        />
                      </div>
                    </div>
                    <div
                      v-if="unifiedIngestResultsByLeagueId[league.id] || unifiedIngestJobStatusByLeagueId[league.id]?.results"
                      class="rounded-card border border-surface-3/60 bg-surface-0 p-2 space-y-2"
                    >
                      <p class="text-xs font-medium text-text-primary">{{ t("admin.leagues.unified_ingest.results_title") }}</p>
                      <div v-if="!isUnifiedXgSource(league.id)" class="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-text-secondary">
                        <span>{{ t("admin.leagues.unified_ingest.metrics.processed") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.processed || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.created") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.created || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.updated") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.updated || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.skipped") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.skipped || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.conflicts") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.conflicts || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.external") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.matched_by_external_id || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.identity") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.matched_by_identity_window || 0 }}</span>
                      </div>
                      <div v-else class="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-text-secondary">
                        <span>{{ t("admin.leagues.unified_ingest.metrics.processed") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.processed || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.matched") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.matched || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.unmatched") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.unmatched || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.skipped") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.skipped || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.already_enriched") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.already_enriched || 0 }}</span>
                        <span>{{ t("admin.leagues.unified_ingest.metrics.alias_recorded") }}: {{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.alias_suggestions_recorded || 0 }}</span>
                      </div>
                      <div v-if="!isUnifiedXgSource(league.id)" class="space-y-2">
                        <p class="text-xs font-medium text-text-primary">{{ t("admin.leagues.unified_ingest.conflicts_title") }}</p>
                        <div
                          v-for="group in ['unresolved_league', 'unresolved_team', 'team_name_conflict', 'other_conflicts']"
                          :key="`${league.id}-${group}`"
                          class="rounded border border-surface-3/60 bg-surface-1 p-2"
                        >
                          <p class="text-[11px] font-medium text-text-primary">
                            {{ t(`admin.leagues.unified_ingest.conflict_groups.${group}`) }}
                            ({{ groupedUnifiedConflicts(league.id)[group].length }})
                          </p>
                          <p class="text-[10px] text-text-muted mt-0.5">
                            {{ unifiedActionHint(group) }}
                          </p>
                          <p
                            v-for="(example, idx) in groupedUnifiedConflicts(league.id)[group].slice(0, 3)"
                            :key="`${league.id}-${group}-${idx}`"
                            class="text-[10px] text-text-muted"
                          >
                            {{ example.external_id || "-" }} | {{ example.message || "-" }}
                          </p>
                        </div>
                      </div>
                      <div v-else class="space-y-2">
                        <div class="rounded border border-surface-3/60 bg-surface-1 p-2">
                          <p class="text-[11px] font-medium text-text-primary">
                            {{ t("admin.leagues.unified_ingest.xg.alias_suggestions") }} ({{ unifiedIngestJobStatusByLeagueId[league.id]?.counters?.alias_suggestions_recorded || 0 }})
                          </p>
                          <p class="text-[10px] text-text-muted">
                            {{ t("admin.leagues.unified_ingest.xg.alias_hint") }}
                          </p>
                        </div>
                        <div class="rounded border border-surface-3/60 bg-surface-1 p-2">
                          <p class="text-[11px] font-medium text-text-primary">
                            {{ t("admin.leagues.unified_ingest.xg.unmatched_teams") }} ({{ unifiedXgUnmatchedTeams(league.id).length }})
                          </p>
                          <p
                            v-for="(name, idx) in unifiedXgUnmatchedTeams(league.id).slice(0, 12)"
                            :key="`xg-unmatched-${league.id}-${idx}`"
                            class="text-[10px] text-text-muted"
                          >
                            {{ name }}
                          </p>
                        </div>
                        <div class="rounded border border-surface-3/60 bg-surface-1 p-2">
                          <p class="text-[11px] font-medium text-text-primary">
                            {{ t("admin.leagues.unified_ingest.xg.raw_rows") }} ({{ filteredUnifiedXgRawRows(league.id).length }})
                          </p>
                          <div class="mt-1 grid grid-cols-1 md:grid-cols-3 gap-1">
                            <select
                              v-model="xgRawFilterByLeagueId[league.id].action"
                              class="rounded border border-surface-3 bg-surface-0 px-2 py-1 text-[10px] text-text-primary"
                              @focus="ensureXgRawFilter(league.id)"
                            >
                              <option value="all">{{ t("admin.leagues.unified_ingest.xg.filters.all_actions") }}</option>
                              <option value="would_update">would_update</option>
                              <option value="already_enriched">already_enriched</option>
                              <option value="unmatched">unmatched</option>
                              <option value="skipped">skipped</option>
                            </select>
                            <input
                              v-model="xgRawFilterByLeagueId[league.id].query"
                              type="text"
                              class="md:col-span-2 rounded border border-surface-3 bg-surface-0 px-2 py-1 text-[10px] text-text-primary"
                              :placeholder="t('admin.leagues.unified_ingest.xg.filters.search_placeholder')"
                              @focus="ensureXgRawFilter(league.id)"
                            />
                          </div>
                          <p
                            v-for="(row, idx) in filteredUnifiedXgRawRows(league.id).slice(0, 20)"
                            :key="`xg-raw-${league.id}-${idx}`"
                            class="text-[10px] text-text-muted"
                          >
                            {{ String(row.action || "-") }} / {{ String(row.reason || "-") }} |
                            {{ String(row.home_team || "-") }} vs {{ String(row.away_team || "-") }} |
                            {{ String(row.date || "-") }} |
                            xG {{ String(row.home_xg ?? "-") }} : {{ String(row.away_xg ?? "-") }}
                          </p>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </td>
              </tr>
            </template>
          </draggable>
        </table>
      </div>
    </section>

    <div v-if="editOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div class="w-full max-w-2xl rounded-card border border-surface-3 bg-surface-0 p-5">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.leagues.modal.title") }}</h2>
        <p class="text-xs text-text-muted mt-1">
          {{ selectedLeague?.league_id }}
        </p>

        <div class="grid md:grid-cols-3 gap-3 mt-4">
          <label class="text-xs text-text-secondary">
            {{ t("admin.leagues.fields.display_name") }}
            <input
              v-model="editState.display_name"
              type="text"
              class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
            />
          </label>
          <label class="text-xs text-text-secondary">
            {{ t("admin.leagues.fields.structure_type") }}
            <select
              v-model="editState.structure_type"
              class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
            >
              <option value="league">{{ t("admin.leagues.structure_type.league") }}</option>
              <option value="cup">{{ t("admin.leagues.structure_type.cup") }}</option>
              <option value="tournament">{{ t("admin.leagues.structure_type.tournament") }}</option>
            </select>
          </label>
          <label class="text-xs text-text-secondary">
            {{ t("admin.leagues.fields.season_start_month") }}
            <input
              v-model.number="editState.season_start_month"
              type="number"
              min="1"
              max="12"
              class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-2 py-1 text-xs text-text-primary"
            />
          </label>
          <label class="text-xs text-text-secondary">
            {{ t("admin.leagues.fields.current_season") }}
            <input
              v-model.number="editState.current_season"
              type="number"
              class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
            />
          </label>
        </div>

        <div class="mt-4">
          <h3 class="text-sm font-medium text-text-primary">{{ t("admin.leagues.modal.feature_matrix") }}</h3>
          <div class="grid md:grid-cols-2 gap-2 mt-2">
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.tipping"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
              />
              <span>{{ t("admin.leagues.features.tipping") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.match_load"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
              />
              <span>{{ t("admin.leagues.features.match_load") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.xg_sync"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
              />
              <span>{{ t("admin.leagues.features.xg_sync") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.odds_sync"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
              />
              <span>{{ t("admin.leagues.features.odds_sync") }}</span>
            </label>
          </div>
          <label class="inline-flex items-center gap-2 text-sm text-text-secondary mt-3">
            <input
              v-model="editState.is_active"
              type="checkbox"
              class="h-4 w-4 rounded border-surface-3 text-primary"
            />
            <span>{{ t("admin.leagues.status_active") }}</span>
          </label>
        </div>

        <div class="mt-4">
          <h3 class="text-sm font-medium text-text-primary">{{ t("admin.leagues.modal.external_ids") }}</h3>
          <div class="grid md:grid-cols-4 gap-2 mt-2">
            <label class="text-xs text-text-secondary">
              {{ t("admin.leagues.providers.sportmonks") }}
              <input
                v-model="editState.external_ids.sportmonks"
                type="text"
                class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
              />
            </label>
            <label class="text-xs text-text-secondary">
              {{ t("admin.leagues.providers.understat") }}
              <input
                v-model="editState.external_ids.understat"
                type="text"
                class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
              />
            </label>
          </div>
        </div>

        <div class="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            class="rounded-card border border-surface-3 px-3 py-2 text-sm text-text-secondary"
            @click="editOpen = false"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="rounded-card bg-primary px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
            :disabled="editBusy"
            @click="saveEdit"
          >
            {{ editBusy ? t("admin.leagues.saving") : t("common.save") }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
