<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const api = useApi();

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

interface LeagueOption {
  league_id: number;
  league_name: string;
  country: string;
  current_season_id: number;
  season_name: string;
  xg_match_count: number;
  slug: string;
}

interface TableRow {
  rank: number;
  team_sm_id: number;
  team_name: string;
  team_short_code?: string | null;
  team_image_path?: string | null;
  played: number;
  real_pts: number;
  expected_pts: number;
  diff: number;
  luck_range: [number, number];
  avg_xg_for: number;
  avg_xg_against: number;
  xg_diff: number;
  real_gd: number;
  gd_justice: number;
  last_5_xp: number[];
}

interface TableResponse {
  league_id: number;
  league_name: string;
  season_id: number;
  match_count: number;
  table: TableRow[];
}

const leagues = ref<LeagueOption[]>([]);
const selectedLeagueId = ref<number | null>(null);
const tableData = ref<TableResponse | null>(null);
const loading = ref(false);
const leaguesLoading = ref(true);
const error = ref("");

type SortField =
  | "diff"
  | "real_pts"
  | "expected_pts"
  | "xg_diff"
  | "real_gd"
  | "gd_justice";
const sortField = ref<SortField>("diff");
const sortDir = ref<"asc" | "desc">("desc");

// ---------------------------------------------------------------------------
// Slug helpers
// ---------------------------------------------------------------------------

function toSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function findLeagueBySlug(slug: string): LeagueOption | undefined {
  return leagues.value.find((l) => l.slug === slug);
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function fetchLeagues() {
  leaguesLoading.value = true;
  try {
    const data = await api.get<{ items: Omit<LeagueOption, "slug">[] }>(
      "/v3/analysis/leagues"
    );
    leagues.value = (data?.items ?? []).map((l) => ({
      ...l,
      slug: toSlug(l.league_name),
    }));
  } catch {
    leagues.value = [];
  } finally {
    leaguesLoading.value = false;
  }
}

async function fetchTable(leagueId: number) {
  loading.value = true;
  error.value = "";
  tableData.value = null;
  try {
    const data = await api.get<TableResponse>(
      `/v3/analysis/unjust-table/${leagueId}`
    );
    tableData.value = data;
  } catch (e: any) {
    error.value = e?.message || t("analysis.noData");
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

const sortedTable = computed(() => {
  if (!tableData.value?.table) return [];
  const rows = [...tableData.value.table];
  const field = sortField.value;
  const dir = sortDir.value === "desc" ? -1 : 1;
  rows.sort((a, b) => ((a[field] ?? 0) - (b[field] ?? 0)) * dir);
  return rows.map((row, i) => ({ ...row, rank: i + 1 }));
});

function toggleSort(field: SortField) {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === "desc" ? "asc" : "desc";
  } else {
    sortField.value = field;
    sortDir.value = "desc";
  }
}

function sortIcon(field: SortField): string {
  if (sortField.value !== field) return "";
  return sortDir.value === "desc" ? " \u25BC" : " \u25B2";
}

// ---------------------------------------------------------------------------
// League selection + URL sync
// ---------------------------------------------------------------------------

function selectLeague(leagueId: number) {
  selectedLeagueId.value = leagueId;
  const lg = leagues.value.find((l) => l.league_id === leagueId);
  if (lg) {
    router.replace({ name: "analysis", params: { league: lg.slug } });
    fetchTable(leagueId);
  }
}

watch(
  () => route.params.league,
  (slug) => {
    if (!slug || typeof slug !== "string" || leagues.value.length === 0) return;
    const lg = findLeagueBySlug(slug);
    if (lg && lg.league_id !== selectedLeagueId.value) {
      selectedLeagueId.value = lg.league_id;
      fetchTable(lg.league_id);
    }
  }
);

// ---------------------------------------------------------------------------
// Sparkline helpers
// ---------------------------------------------------------------------------

function sparkColor(xp: number): string {
  if (xp > 2.0) return "bg-success";
  if (xp >= 1.0) return "bg-warning";
  return "bg-danger";
}

function isOutsideLuckRange(row: TableRow): boolean {
  return (
    row.real_pts < row.luck_range[0] || row.real_pts > row.luck_range[1]
  );
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

onMounted(async () => {
  await fetchLeagues();
  const slug = route.params.league;
  if (typeof slug === "string" && slug) {
    const lg = findLeagueBySlug(slug);
    if (lg) {
      selectedLeagueId.value = lg.league_id;
      fetchTable(lg.league_id);
      return;
    }
  }
  // Auto-select first league if no slug
  if (leagues.value.length > 0) {
    selectLeague(leagues.value[0].league_id);
  }
});
</script>

<template>
  <div class="max-w-7xl mx-auto px-4 py-6">
    <!-- Header -->
    <div class="mb-6">
      <h1 class="text-xl font-bold text-text-primary">
        {{ t("analysis.title") }}
      </h1>
      <p class="text-sm text-text-muted mt-1">
        {{ t("analysis.subtitle") }}
      </p>
    </div>

    <!-- League selector -->
    <div v-if="leaguesLoading" class="flex gap-2 mb-6">
      <div
        v-for="i in 4"
        :key="i"
        class="h-9 w-28 rounded-lg bg-surface-2 animate-pulse"
      />
    </div>
    <div v-else-if="leagues.length === 0" class="mb-6">
      <p class="text-sm text-text-muted">{{ t("analysis.noData") }}</p>
    </div>
    <div v-else class="flex flex-wrap gap-2 mb-6">
      <button
        v-for="lg in leagues"
        :key="lg.league_id"
        class="px-3 py-1.5 text-sm rounded-lg border transition-colors"
        :class="
          selectedLeagueId === lg.league_id
            ? 'bg-primary text-white border-primary'
            : 'bg-surface-1 text-text-secondary border-surface-3/50 hover:border-surface-3'
        "
        @click="selectLeague(lg.league_id)"
      >
        {{ lg.league_name }}
        <span class="text-[10px] opacity-70 ml-1">{{ lg.season_name }}</span>
      </button>
    </div>

    <!-- Loading skeleton -->
    <div v-if="loading" class="space-y-2">
      <div
        v-for="i in 12"
        :key="i"
        class="h-10 rounded bg-surface-2 animate-pulse"
      />
    </div>

    <!-- Error -->
    <div
      v-else-if="error"
      class="rounded-lg bg-danger-muted/10 border border-danger-muted/30 p-4 text-sm text-danger"
    >
      {{ error }}
    </div>

    <!-- Table -->
    <div
      v-else-if="sortedTable.length > 0"
      class="overflow-x-auto rounded-lg border border-surface-3/50"
    >
      <table class="w-full text-sm">
        <thead>
          <tr class="bg-surface-2 text-text-secondary text-xs">
            <th class="px-3 py-2 text-left w-8">#</th>
            <th class="px-3 py-2 text-left">{{ t("analysis.team") }}</th>
            <th class="px-3 py-2 text-center">{{ t("analysis.played") }}</th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
              :title="t('analysis.sortBy', { column: t('analysis.realPts') })"
              @click="toggleSort('real_pts')"
            >
              {{ t("analysis.realPts") }}{{ sortIcon("real_pts") }}
            </th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
              :title="t('analysis.sortBy', { column: 'xP' })"
              @click="toggleSort('expected_pts')"
            >
              xP{{ sortIcon("expected_pts") }}
            </th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary font-semibold"
              :title="t('analysis.sortBy', { column: t('analysis.diff') })"
              @click="toggleSort('diff')"
            >
              {{ t("analysis.diff") }}{{ sortIcon("diff") }}
            </th>
            <th class="px-3 py-2 text-center">{{ t("analysis.luckRange") }}</th>
            <th class="px-3 py-2 text-center">{{ t("analysis.xgFor") }}</th>
            <th class="px-3 py-2 text-center">{{ t("analysis.xgAgainst") }}</th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
              @click="toggleSort('xg_diff')"
            >
              {{ t("analysis.xgDiff") }}{{ sortIcon("xg_diff") }}
            </th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
              @click="toggleSort('real_gd')"
            >
              {{ t("analysis.goalDiff") }}{{ sortIcon("real_gd") }}
            </th>
            <th
              class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
              @click="toggleSort('gd_justice')"
            >
              {{ t("analysis.gdJustice") }}{{ sortIcon("gd_justice") }}
            </th>
            <th class="px-3 py-2 text-center">{{ t("analysis.last5") }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in sortedTable"
            :key="row.team_sm_id"
            class="border-t border-surface-3/30 hover:bg-surface-2/50 transition-colors"
            :class="{
              'border-l-2 border-l-success': row.diff > 0,
              'border-l-2 border-l-warning': row.diff < 0,
            }"
          >
            <td class="px-3 py-2 text-text-muted tabular-nums text-center">
              {{ row.rank }}
            </td>
            <td class="px-3 py-2 font-medium text-text-primary">
              <div class="flex items-center gap-1.5">
                <img
                  v-if="row.team_image_path"
                  :src="row.team_image_path"
                  :alt="row.team_name"
                  class="w-5 h-5 object-contain flex-shrink-0"
                  loading="lazy"
                />
                <span class="hidden sm:inline truncate">{{ row.team_name }}</span>
                <span class="sm:hidden truncate">{{ row.team_short_code || row.team_name }}</span>
              </div>
            </td>
            <td class="px-3 py-2 text-center tabular-nums text-text-muted">
              {{ row.played }}
            </td>
            <td class="px-3 py-2 text-center tabular-nums font-medium">
              {{ row.real_pts }}
            </td>
            <td class="px-3 py-2 text-center tabular-nums">
              {{ row.expected_pts }}
            </td>
            <td
              class="px-3 py-2 text-center tabular-nums font-bold"
              :class="row.diff > 0 ? 'text-success' : row.diff < 0 ? 'text-danger' : 'text-text-muted'"
            >
              {{ row.diff > 0 ? "+" : "" }}{{ row.diff }}
            </td>
            <td
              class="px-3 py-2 text-center tabular-nums text-xs"
              :class="isOutsideLuckRange(row) ? 'text-danger font-semibold' : 'text-text-muted'"
              :title="isOutsideLuckRange(row) ? t('analysis.significantDeviation') : ''"
            >
              {{ row.luck_range[0] }} &ndash; {{ row.luck_range[1] }}
            </td>
            <td class="px-3 py-2 text-center tabular-nums text-text-muted">
              {{ row.avg_xg_for }}
            </td>
            <td class="px-3 py-2 text-center tabular-nums text-text-muted">
              {{ row.avg_xg_against }}
            </td>
            <td
              class="px-3 py-2 text-center tabular-nums"
              :class="row.xg_diff > 0 ? 'text-success' : row.xg_diff < 0 ? 'text-danger' : 'text-text-muted'"
            >
              {{ row.xg_diff > 0 ? "+" : "" }}{{ row.xg_diff }}
            </td>
            <td
              class="px-3 py-2 text-center tabular-nums"
              :class="row.real_gd > 0 ? 'text-success' : row.real_gd < 0 ? 'text-danger' : 'text-text-muted'"
            >
              {{ row.real_gd > 0 ? "+" : "" }}{{ row.real_gd }}
            </td>
            <td
              class="px-3 py-2 text-center tabular-nums"
              :class="row.gd_justice > 0 ? 'text-success' : row.gd_justice < 0 ? 'text-danger' : 'text-text-muted'"
            >
              {{ row.gd_justice > 0 ? "+" : "" }}{{ row.gd_justice }}
            </td>
            <td class="px-3 py-2">
              <div class="flex items-center justify-center gap-0.5">
                <span
                  v-for="(xp, idx) in row.last_5_xp"
                  :key="idx"
                  class="inline-block w-2 h-2 rounded-full"
                  :class="sparkColor(xp)"
                  :title="`xP: ${xp}`"
                />
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="px-3 py-2 bg-surface-2 text-xs text-text-muted flex justify-between">
        <span>{{ tableData?.match_count ?? 0 }} {{ t("analysis.played").toLowerCase() }}</span>
        <span>Poisson MC (10k sims)</span>
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-else-if="!loading && selectedLeagueId"
      class="text-center py-12 text-text-muted"
    >
      {{ t("analysis.noData") }}
    </div>
  </div>
</template>
