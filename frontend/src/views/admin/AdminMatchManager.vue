<!--
frontend/src/views/admin/AdminMatchManager.vue

Purpose:
    Admin match list with pagination, filters, odds-availability indicator,
    and quick access to detail/override actions using the odds_meta-native API.

Dependencies:
    - @/composables/useApi
    - @/composables/useToast
    - vue-i18n
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";
import { sportLabel } from "@/types/sports";

interface AdminMatchItem {
  id: string;
  league_id: string | null;
  league_name: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string;
  status: string;
  score: Record<string, unknown>;
  result: { home_score: number | null; away_score: number | null; outcome: string | null };
  matchday: number | null;
  has_odds: boolean;
  odds_updated_at: string | null;
  bet_count: number;
}

interface AdminMatchListResponse {
  items: AdminMatchItem[];
  page: number;
  page_size: number;
  total: number;
}

interface LeagueListResponse {
  items: Array<{ id: string; display_name: string; sport_key: string }>;
}

interface MatchDuplicateRow {
  id: string;
  status: string;
  match_date: string;
  is_keeper: boolean;
}

interface MatchDuplicateGroup {
  key: string;
  league_id: string;
  league_name: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_day: string;
  count: number;
  keeper_id: string;
  matches: MatchDuplicateRow[];
}

interface MatchDuplicateListResponse {
  total_groups: number;
  total_matches: number;
  groups: MatchDuplicateGroup[];
}

interface MatchDuplicateCleanupResponse {
  dry_run: boolean;
  groups: number;
  deleted?: number;
  would_delete?: number;
}

const api = useApi();
const toast = useToast();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const error = ref<string>("");
const matches = ref<AdminMatchItem[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(50);
const leagues = ref<Array<{ id: string; display_name: string; sport_key: string }>>([]);
const duplicateLoading = ref(false);
const duplicateBusy = ref(false);
const duplicateGroups = ref<MatchDuplicateGroup[]>([]);
const duplicateDryRun = ref(true);

const statusFilter = ref("");
const leagueFilter = ref("");
const oddsFilter = ref<"all" | "yes" | "no">("all");
const search = ref("");

const overrideMatch = ref<AdminMatchItem | null>(null);
const overrideResult = ref("1");
const overrideHome = ref(0);
const overrideAway = ref(0);
const overrideBusy = ref(false);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const isEmpty = computed(() => !loading.value && !error.value && matches.value.length === 0);

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusClass(status: string): string {
  if (status === "scheduled") return "bg-primary-muted/20 text-primary";
  if (status === "live" || status === "in_play") return "bg-danger-muted/20 text-danger";
  return "bg-surface-3 text-text-muted";
}

function statusLabel(status: string): string {
  const key = `admin.matches.status.${status}`;
  const translated = t(key);
  return translated === key ? status : translated;
}

async function fetchLeagues(): Promise<void> {
  try {
    const res = await api.get<LeagueListResponse>("/admin/leagues");
    leagues.value = res.items.map((item) => ({ id: item.id, display_name: item.display_name, sport_key: item.sport_key }));
  } catch {
    leagues.value = [];
  }
}

function currentDuplicateScope(): { league_id?: string; sport_key?: string } {
  if (leagueFilter.value) {
    const selected = leagues.value.find((row) => row.id === leagueFilter.value);
    if (selected) {
      return { league_id: selected.id, sport_key: selected.sport_key };
    }
    return { league_id: leagueFilter.value };
  }
  return {};
}

async function fetchDuplicateGroups(): Promise<void> {
  duplicateLoading.value = true;
  try {
    const scope = currentDuplicateScope();
    const params: Record<string, string> = { limit_groups: "100" };
    if (scope.league_id) params.league_id = scope.league_id;
    if (scope.sport_key) params.sport_key = scope.sport_key;
    const res = await api.get<MatchDuplicateListResponse>("/admin/match-duplicates", params);
    duplicateGroups.value = res.groups || [];
  } catch (err) {
    const message = err instanceof Error ? err.message : t("common.genericError");
    toast.error(message);
  } finally {
    duplicateLoading.value = false;
  }
}

async function cleanupDuplicateGroups(): Promise<void> {
  duplicateBusy.value = true;
  try {
    const scope = currentDuplicateScope();
    const res = await api.post<MatchDuplicateCleanupResponse>("/admin/match-duplicates/cleanup", {
      league_id: scope.league_id || null,
      sport_key: scope.sport_key || null,
      dry_run: duplicateDryRun.value,
      limit_groups: 500,
    });
    if (duplicateDryRun.value) {
      toast.success(
        t("admin.matches.duplicates.dry_run_done", {
          groups: res.groups || 0,
          count: res.would_delete || 0,
        }),
      );
    } else {
      toast.success(
        t("admin.matches.duplicates.cleanup_done", {
          groups: res.groups || 0,
          count: res.deleted || 0,
        }),
      );
    }
    await Promise.all([fetchDuplicateGroups(), fetchMatches()]);
  } catch (err) {
    const message = err instanceof Error ? err.message : t("common.genericError");
    toast.error(message);
  } finally {
    duplicateBusy.value = false;
  }
}

async function fetchMatches(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const params: Record<string, string> = {
      page: String(page.value),
      page_size: String(pageSize.value),
    };
    if (statusFilter.value) params.status = statusFilter.value;
    if (leagueFilter.value) params.league_id = leagueFilter.value;
    if (search.value.trim()) params.search = search.value.trim();
    if (oddsFilter.value === "yes") params.odds_available = "true";
    if (oddsFilter.value === "no") params.odds_available = "false";

    const res = await api.get<AdminMatchListResponse>("/admin/matches", params);
    matches.value = res.items;
    total.value = res.total;
    page.value = res.page;
    pageSize.value = res.page_size;
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

function applyFilters(): void {
  page.value = 1;
  void fetchMatches();
  void fetchDuplicateGroups();
}

function goToMatchDetail(matchId: string): void {
  void router.push({ name: "admin-match-detail", params: { matchId } });
}

function openOverride(match: AdminMatchItem): void {
  overrideMatch.value = match;
  overrideResult.value = match.result.outcome || "1";
  overrideHome.value = match.result.home_score ?? 0;
  overrideAway.value = match.result.away_score ?? 0;
}

async function submitOverride(): Promise<void> {
  if (!overrideMatch.value) return;
  overrideBusy.value = true;
  try {
    await api.post(`/admin/matches/${overrideMatch.value.id}/override`, {
      result: overrideResult.value,
      home_score: overrideHome.value,
      away_score: overrideAway.value,
    });
    toast.success(t("admin.matches.override_success"));
    overrideMatch.value = null;
    await fetchMatches();
  } catch (err) {
    const message = err instanceof Error ? err.message : t("common.genericError");
    toast.error(message);
  } finally {
    overrideBusy.value = false;
  }
}

function prevPage(): void {
  if (page.value <= 1) return;
  page.value -= 1;
  void fetchMatches();
}

function nextPage(): void {
  if (page.value >= totalPages.value) return;
  page.value += 1;
  void fetchMatches();
}

onMounted(async () => {
  await fetchLeagues();
  await Promise.all([fetchMatches(), fetchDuplicateGroups()]);
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.matches.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.matches.subtitle") }}</p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-3 md:p-4">
      <div class="grid grid-cols-1 md:grid-cols-5 gap-2">
        <select
          v-model="leagueFilter"
          class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm text-text-primary"
          @change="applyFilters"
        >
          <option value="">{{ t("admin.matches.filters.all_leagues") }}</option>
          <option v-for="league in leagues" :key="league.id" :value="league.id">
            {{ league.display_name }}
          </option>
        </select>

        <select
          v-model="statusFilter"
          class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm text-text-primary"
          @change="applyFilters"
        >
          <option value="">{{ t("admin.matches.filters.all_statuses") }}</option>
          <option value="scheduled">{{ t("admin.matches.status.scheduled") }}</option>
          <option value="live">{{ t("admin.matches.status.live") }}</option>
          <option value="in_play">{{ t("admin.matches.status.in_play") }}</option>
          <option value="final">{{ t("admin.matches.status.final") }}</option>
        </select>

        <select
          v-model="oddsFilter"
          class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm text-text-primary"
          @change="applyFilters"
        >
          <option value="all">{{ t("admin.matches.filters.odds_all") }}</option>
          <option value="yes">{{ t("admin.matches.filters.odds_yes") }}</option>
          <option value="no">{{ t("admin.matches.filters.odds_no") }}</option>
        </select>

        <input
          v-model="search"
          :placeholder="t('admin.matches.filters.search_placeholder')"
          class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm text-text-primary"
          @keyup.enter="applyFilters"
        />

        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-secondary hover:border-primary/60"
          @click="applyFilters"
        >
          {{ t("admin.matches.filters.apply") }}
        </button>
      </div>
    </section>

    <section class="rounded-card border border-warning/40 bg-warning/5 p-3 md:p-4 space-y-2">
      <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <div>
          <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.matches.duplicates.title") }}</h2>
          <p class="text-xs text-text-muted">{{ t("admin.matches.duplicates.subtitle") }}</p>
        </div>
        <div class="flex items-center gap-2">
          <label class="inline-flex items-center gap-2 text-xs text-text-secondary">
            <input v-model="duplicateDryRun" type="checkbox" class="h-4 w-4 rounded border-surface-3 bg-surface-0 text-primary" />
            <span>{{ t("admin.matches.duplicates.dry_run") }}</span>
          </label>
          <button
            type="button"
            class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
            :disabled="duplicateBusy"
            @click="cleanupDuplicateGroups"
          >
            {{ duplicateBusy ? t("admin.matches.duplicates.running") : t("admin.matches.duplicates.run") }}
          </button>
        </div>
      </div>
      <div v-if="duplicateLoading" class="text-xs text-text-muted">{{ t("admin.matches.duplicates.loading") }}</div>
      <div v-else-if="duplicateGroups.length === 0" class="text-xs text-text-muted">{{ t("admin.matches.duplicates.empty") }}</div>
      <div v-else class="space-y-2">
        <div
          v-for="group in duplicateGroups"
          :key="group.key"
          class="rounded-card border border-surface-3/60 bg-surface-0 p-2"
        >
          <p class="text-xs text-text-primary font-medium">
            {{ group.match_day }} | {{ group.home_team }} vs {{ group.away_team }} ({{ group.count }})
          </p>
          <p class="text-[11px] text-text-muted">
            {{ group.league_name || group.sport_key }}
          </p>
          <div class="mt-1 space-y-1">
            <p
              v-for="row in group.matches"
              :key="row.id"
              class="text-[11px]"
              :class="row.is_keeper ? 'text-primary font-medium' : 'text-text-muted'"
            >
              {{ row.is_keeper ? t("admin.matches.duplicates.keeper") : t("admin.matches.duplicates.loser") }}
              | {{ row.id }} | {{ row.match_date }} | {{ row.status }}
            </p>
          </div>
        </div>
      </div>
    </section>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
      <div v-if="loading" class="p-4 space-y-2">
        <div v-for="n in 8" :key="n" class="h-11 rounded bg-surface-2 animate-pulse" />
      </div>

      <div v-else-if="error" class="p-6 text-center">
        <p class="text-sm text-danger">{{ error }}</p>
        <button
          type="button"
          class="mt-3 rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-sm text-text-secondary hover:border-primary/60"
          @click="fetchMatches"
        >
          {{ t("common.retry") }}
        </button>
      </div>

      <div v-else-if="isEmpty" class="p-6 text-center text-sm text-text-muted">
        {{ t("admin.matches.empty") }}
      </div>

      <div v-else class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-surface-2/60 border-b border-surface-3/60">
            <tr>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.match") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.league") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.date") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.matchday") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.status") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.score") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.odds") }}</th>
              <th class="px-3 py-2 text-right text-text-secondary font-medium">{{ t("admin.matches.table.actions") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="match in matches"
              :key="match.id"
              class="border-b border-surface-3/40 last:border-b-0 hover:bg-surface-2/30"
            >
              <td class="px-3 py-2">
                <button
                  type="button"
                  class="text-left hover:underline text-text-primary"
                  @click="goToMatchDetail(match.id)"
                >
                  {{ match.home_team }} vs {{ match.away_team }}
                </button>
              </td>
              <td class="px-3 py-2 text-text-muted">
                {{ match.league_name || sportLabel(match.sport_key) }}
              </td>
              <td class="px-3 py-2 text-text-muted">{{ formatDate(match.match_date) }}</td>
              <td class="px-3 py-2 text-text-muted">{{ match.matchday ?? "-" }}</td>
              <td class="px-3 py-2">
                <span class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium" :class="statusClass(match.status)">
                  {{ statusLabel(match.status) }}
                </span>
              </td>
              <td class="px-3 py-2 text-text-primary">
                <span v-if="match.result.home_score != null">{{ match.result.home_score }}-{{ match.result.away_score }}</span>
                <span v-else class="text-text-muted">-</span>
              </td>
              <td class="px-3 py-2">
                <span
                  class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="match.has_odds ? 'bg-primary-muted/20 text-primary' : 'bg-surface-3 text-text-muted'"
                >
                  {{ match.has_odds ? t("admin.matches.odds.available") : t("admin.matches.odds.missing") }}
                </span>
              </td>
              <td class="px-3 py-2 text-right">
                <button
                  type="button"
                  class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60"
                  @click="openOverride(match)"
                >
                  {{ t("admin.matches.actions.override") }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="flex items-center justify-between rounded-card border border-surface-3/60 bg-surface-1 px-3 py-2 text-sm">
      <p class="text-text-muted">
        {{ t("admin.matches.pagination.summary", { total, page, pageSize }) }}
      </p>
      <div class="flex gap-2">
        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-text-secondary disabled:opacity-40"
          :disabled="page <= 1"
          @click="prevPage"
        >
          {{ t("admin.matches.pagination.previous") }}
        </button>
        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-text-secondary disabled:opacity-40"
          :disabled="page >= totalPages"
          @click="nextPage"
        >
          {{ t("admin.matches.pagination.next") }}
        </button>
      </div>
    </section>

    <Teleport to="body">
      <div
        v-if="overrideMatch"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
        @click.self="overrideMatch = null"
      >
        <div class="w-full max-w-md rounded-card border border-surface-3 bg-surface-1 p-4 space-y-3">
          <h2 class="text-base font-semibold text-text-primary">{{ t("admin.matches.override_title") }}</h2>
          <p class="text-xs text-text-muted">{{ overrideMatch.home_team }} vs {{ overrideMatch.away_team }}</p>
          <select
            v-model="overrideResult"
            class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary"
          >
            <option value="1">{{ t("admin.matches.override_home") }}</option>
            <option value="X">{{ t("admin.matches.override_draw") }}</option>
            <option value="2">{{ t("admin.matches.override_away") }}</option>
          </select>
          <div class="grid grid-cols-2 gap-2">
            <input
              v-model.number="overrideHome"
              type="number"
              min="0"
              class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary"
            />
            <input
              v-model.number="overrideAway"
              type="number"
              min="0"
              class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary"
            />
          </div>
          <div class="flex justify-end gap-2">
            <button
              type="button"
              class="rounded-card border border-surface-3 px-3 py-1.5 text-sm text-text-secondary"
              @click="overrideMatch = null"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="rounded-card bg-danger px-3 py-1.5 text-sm text-white disabled:opacity-50"
              :disabled="overrideBusy"
              @click="submitOverride"
            >
              {{ overrideBusy ? t("admin.matches.override_loading") : t("admin.matches.actions.override") }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
