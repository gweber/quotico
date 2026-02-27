<!--
frontend/src/views/admin/AdminMatchManager.vue

Purpose:
    Admin match list powered by matches_v3 collection. Displays v3 match data
    with team logos, v3 status badges, odds/xG/manual-check indicators,
    and pagination. No legacy shims — all IDs are native integers.

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

interface AdminMatchItemV3 {
  id: number;
  league_id: number;
  league_name: string;
  home_team: string;
  away_team: string;
  home_image: string | null;
  away_image: string | null;
  start_at: string;
  status: string;
  scores: {
    half_time?: { home?: number | null; away?: number | null };
    full_time?: { home?: number | null; away?: number | null };
  };
  round_id: number | null;
  has_odds: boolean;
  odds_updated_at: string | null;
  has_advanced_stats: boolean;
  manual_check_required: boolean;
  referee: {
    id: number;
    name: string;
    strictness_index: number;
    strictness_band: "loose" | "normal" | "strict" | "extreme_strict";
    avg_yellow: number;
    avg_red: number;
    penalty_pct: number;
  } | null;
}

interface AdminMatchListResponse {
  items: AdminMatchItemV3[];
  page: number;
  page_size: number;
  total: number;
}

interface LeagueListResponse {
  items: Array<{ id: string; display_name: string; league_id: number }>;
}

const api = useApi();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const error = ref("");
const matches = ref<AdminMatchItemV3[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(50);
const leagues = ref<Array<{ id: string; display_name: string }>>([]);

const statusFilter = ref("");
const leagueFilter = ref("");
const oddsFilter = ref<"all" | "yes" | "no">("all");
const manualCheckFilter = ref<"all" | "yes" | "no">("all");
const search = ref("");

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
  const s = status.toUpperCase();
  if (s === "SCHEDULED") return "bg-primary-muted/20 text-primary";
  if (s === "LIVE") return "bg-danger-muted/20 text-danger";
  if (s === "FINISHED") return "bg-surface-3 text-text-muted";
  if (s === "POSTPONED" || s === "WALKOVER" || s === "CANCELLED")
    return "bg-warning/20 text-warning";
  return "bg-surface-3 text-text-muted";
}

function statusLabel(status: string): string {
  const key = `admin.matches.status.${status.toUpperCase()}`;
  const translated = t(key);
  return translated === key ? status : translated;
}

function scoreDisplay(match: AdminMatchItemV3): string {
  const ft = match.scores?.full_time;
  if (ft && ft.home != null && ft.away != null) return `${ft.home}-${ft.away}`;
  return "-";
}

function strictnessBadgeClass(band: string): string {
  if (band === "extreme_strict") return "bg-danger/20 text-danger";
  if (band === "strict") return "bg-warning/20 text-warning";
  if (band === "loose") return "bg-primary/20 text-primary";
  return "bg-surface-3 text-text-secondary";
}

function strictnessBadgeLabel(match: AdminMatchItemV3): string {
  if (!match.referee) return "-";
  return `${t("admin.referees.badges.strictness")}: ${match.referee.strictness_index.toFixed(1)}`;
}

function goToRefereeDetail(refereeId: number): void {
  void router.push({ name: "admin-referee-detail", params: { refereeId } });
}

async function fetchLeagues(): Promise<void> {
  try {
    const res = await api.get<LeagueListResponse>("/admin/leagues");
    leagues.value = res.items.map((item) => ({ id: item.id, display_name: item.display_name }));
  } catch {
    leagues.value = [];
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
    if (manualCheckFilter.value === "yes") params.manual_check = "true";
    if (manualCheckFilter.value === "no") params.manual_check = "false";

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
}

function goToMatchDetail(matchId: number): void {
  void router.push({ name: "admin-match-detail", params: { matchId } });
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
  await fetchMatches();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.matches.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.matches.subtitle") }}</p>
    </div>

    <!-- Filters -->
    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-3 md:p-4">
      <div class="grid grid-cols-1 md:grid-cols-6 gap-2">
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
          <option value="SCHEDULED">{{ t("admin.matches.status.SCHEDULED") }}</option>
          <option value="LIVE">{{ t("admin.matches.status.LIVE") }}</option>
          <option value="FINISHED">{{ t("admin.matches.status.FINISHED") }}</option>
          <option value="POSTPONED">{{ t("admin.matches.status.POSTPONED") }}</option>
          <option value="WALKOVER">{{ t("admin.matches.status.WALKOVER") }}</option>
          <option value="CANCELLED">{{ t("admin.matches.status.CANCELLED") }}</option>
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

        <select
          v-model="manualCheckFilter"
          class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm text-text-primary"
          @change="applyFilters"
        >
          <option value="all">{{ t("admin.matches.filters.manual_check_all") }}</option>
          <option value="yes">{{ t("admin.matches.filters.manual_check_yes") }}</option>
          <option value="no">{{ t("admin.matches.filters.manual_check_no") }}</option>
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

    <!-- Table -->
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
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.referee") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.date") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.round") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.status") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.score") }}</th>
              <th class="px-3 py-2 text-left text-text-secondary font-medium">{{ t("admin.matches.table.odds") }}</th>
              <th class="px-3 py-2 text-center text-text-secondary font-medium">{{ t("admin.matches.table.xg") }}</th>
              <th class="px-3 py-2 text-center text-text-secondary font-medium">{{ t("admin.matches.table.check") }}</th>
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
                  class="flex items-center gap-2 text-left hover:underline text-text-primary"
                  @click="goToMatchDetail(match.id)"
                >
                  <img
                    v-if="match.home_image"
                    :src="match.home_image"
                    :alt="match.home_team"
                    class="w-5 h-5 object-contain"
                  />
                  <span>{{ match.home_team }}</span>
                  <span class="text-text-muted">vs</span>
                  <img
                    v-if="match.away_image"
                    :src="match.away_image"
                    :alt="match.away_team"
                    class="w-5 h-5 object-contain"
                  />
                  <span>{{ match.away_team }}</span>
                </button>
              </td>
              <td class="px-3 py-2 text-text-muted">{{ match.league_name }}</td>
              <td class="px-3 py-2">
                <div v-if="match.referee" class="flex items-center gap-2">
                  <button
                    type="button"
                    class="text-left text-text-primary hover:underline"
                    @click="goToRefereeDetail(match.referee.id)"
                  >
                    {{ match.referee.name || ("#" + String(match.referee.id)) }}
                  </button>
                  <span class="inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium" :class="strictnessBadgeClass(match.referee.strictness_band)">
                    {{ strictnessBadgeLabel(match) }}
                  </span>
                </div>
                <span v-else class="text-text-muted">-</span>
              </td>
              <td class="px-3 py-2 text-text-muted">{{ formatDate(match.start_at) }}</td>
              <td class="px-3 py-2 text-text-muted">{{ match.round_id ?? "-" }}</td>
              <td class="px-3 py-2">
                <span class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium" :class="statusClass(match.status)">
                  {{ statusLabel(match.status) }}
                </span>
              </td>
              <td class="px-3 py-2 text-text-primary">{{ scoreDisplay(match) }}</td>
              <td class="px-3 py-2">
                <span
                  class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="match.has_odds ? 'bg-primary-muted/20 text-primary' : 'bg-surface-3 text-text-muted'"
                >
                  {{ match.has_odds ? t("admin.matches.odds.available") : t("admin.matches.odds.missing") }}
                </span>
              </td>
              <td class="px-3 py-2 text-center">
                <span v-if="match.has_advanced_stats" class="text-primary text-xs font-medium">xG</span>
                <span v-else class="text-text-muted text-xs">-</span>
              </td>
              <td class="px-3 py-2 text-center">
                <span
                  v-if="match.manual_check_required"
                  class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium bg-warning/20 text-warning"
                  :title="t('admin.matches.table.manual_check_tooltip')"
                >!</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Pagination -->
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
  </div>
</template>
