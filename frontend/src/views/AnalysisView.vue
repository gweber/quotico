<!--
frontend/src/views/AnalysisView.vue

Purpose:
    Render the v3.1 justice analysis ("Wahre Tabelle") for leagues and seasons.
    Focuses on real points vs expected points (xP), luck factor, and clinicality.
-->
<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const api = useApi();

interface LeagueOption {
  league_id: number;
  league_name: string;
  country: string;
  current_season_id: number;
  season_name: string;
  xg_match_count: number;
  slug: string;
}

interface CalculationMeta {
  simulations: number;
  cached: boolean;
  generated_at_utc: string;
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
  luck_factor: number;
  clinicality: number;
}

interface TableResponse {
  league_id: number;
  league_name: string;
  season_id: number;
  match_count: number;
  excluded_matches_count?: number;
  calculation_meta?: CalculationMeta;
  table: TableRow[];
}

const leagues = ref<LeagueOption[]>([]);
const selectedLeagueId = ref<number | null>(null);
const tableData = ref<TableResponse | null>(null);
const loading = ref(false);
const leaguesLoading = ref(true);
const error = ref("");

type SortField = "expected_pts" | "real_pts" | "luck_factor" | "clinicality";
const sortField = ref<SortField>("expected_pts");
const sortDir = ref<"asc" | "desc">("desc");

function toSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function findLeagueBySlug(slug: string): LeagueOption | undefined {
  return leagues.value.find((l) => l.slug === slug);
}

async function fetchLeagues() {
  leaguesLoading.value = true;
  try {
    const data = await api.get<{ items: Omit<LeagueOption, "slug">[] }>("/v3/analysis/leagues");
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
    const data = await api.get<TableResponse>(`/v3/analysis/unjust-table/${leagueId}`);
    tableData.value = data;
  } catch (e: any) {
    error.value = e?.message || t("analysis.noData");
  } finally {
    loading.value = false;
  }
}

const sortedTable = computed(() => {
  if (!tableData.value?.table) return [];
  const rows = [...tableData.value.table];
  const field = sortField.value;
  const dir = sortDir.value === "desc" ? -1 : 1;
  rows.sort((a, b) => ((a[field] ?? 0) - (b[field] ?? 0)) * dir);
  return rows.map((row, i) => ({ ...row, rank: i + 1 }));
});

const lueckspilz = computed(() => {
  const rows = tableData.value?.table ?? [];
  if (rows.length === 0) return null;
  const winner = [...rows].sort((a, b) => b.luck_factor - a.luck_factor)[0];
  if (!winner || winner.luck_factor <= 0) return null;
  return winner;
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

function luckClass(luck: number): string {
  if (luck > 0) return "text-danger";
  if (luck < 0) return "text-success";
  return "text-text-muted";
}

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
  if (leagues.value.length > 0) {
    selectLeague(leagues.value[0].league_id);
  }
});
</script>

<template>
  <div class="max-w-7xl mx-auto px-4 py-6">
    <div class="mb-6">
      <h1 class="text-xl font-bold text-text-primary">{{ t("analysis.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("analysis.subtitle") }}</p>
    </div>

    <div v-if="leaguesLoading" class="flex gap-2 mb-6">
      <div v-for="i in 4" :key="i" class="h-9 w-28 rounded-lg bg-surface-2 animate-pulse" />
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

    <div v-if="loading" class="space-y-2">
      <div v-for="i in 12" :key="i" class="h-10 rounded bg-surface-2 animate-pulse" />
    </div>

    <div
      v-else-if="error"
      class="rounded-lg bg-danger-muted/10 border border-danger-muted/30 p-4 text-sm text-danger"
    >
      {{ error }}
    </div>

    <div v-else-if="sortedTable.length > 0" class="space-y-3">
      <div
        v-if="lueckspilz"
        class="rounded-lg border border-danger-muted/40 bg-danger-muted/10 px-3 py-2 text-sm text-danger"
      >
        <span class="font-semibold">{{ t("analysis.lueckspilzLabel") }}:</span>
        {{ lueckspilz.team_name }} ({{ lueckspilz.luck_factor > 0 ? "+" : "" }}{{ lueckspilz.luck_factor }})
      </div>

      <div class="text-xs text-text-muted flex flex-wrap gap-3">
        <span>{{ t("analysis.legendUnder") }}</span>
        <span>{{ t("analysis.legendOver") }}</span>
        <span>{{ t("analysis.legendNeutral") }}</span>
      </div>

      <div class="overflow-x-auto rounded-lg border border-surface-3/50">
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
                :title="t('analysis.sortBy', { column: t('analysis.expectedPts') })"
                @click="toggleSort('expected_pts')"
              >
                {{ t("analysis.expectedPts") }}{{ sortIcon("expected_pts") }}
              </th>
              <th
                class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary font-semibold"
                :title="t('analysis.sortBy', { column: t('analysis.luck') })"
                @click="toggleSort('luck_factor')"
              >
                {{ t("analysis.luck") }}{{ sortIcon("luck_factor") }}
              </th>
              <th
                class="px-3 py-2 text-center cursor-pointer select-none hover:text-text-primary"
                :title="t('analysis.sortBy', { column: t('analysis.clinicality') })"
                @click="toggleSort('clinicality')"
              >
                {{ t("analysis.clinicality") }}{{ sortIcon("clinicality") }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in sortedTable"
              :key="row.team_sm_id"
              class="border-t border-surface-3/30 hover:bg-surface-2/50 transition-colors"
            >
              <td class="px-3 py-2 text-text-muted tabular-nums text-center">{{ row.rank }}</td>
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
              <td class="px-3 py-2 text-center tabular-nums text-text-muted">{{ row.played }}</td>
              <td class="px-3 py-2 text-center tabular-nums font-medium">{{ row.real_pts }}</td>
              <td class="px-3 py-2 text-center tabular-nums">{{ row.expected_pts.toFixed(2) }}</td>
              <td class="px-3 py-2 text-center tabular-nums font-bold" :class="luckClass(row.luck_factor)">
                {{ row.luck_factor > 0 ? "+" : "" }}{{ row.luck_factor.toFixed(2) }}
              </td>
              <td
                class="px-3 py-2 text-center tabular-nums"
                :class="row.clinicality > 0 ? 'text-success' : row.clinicality < 0 ? 'text-danger' : 'text-text-muted'"
              >
                {{ row.clinicality > 0 ? "+" : "" }}{{ row.clinicality.toFixed(2) }}
              </td>
            </tr>
          </tbody>
        </table>

        <div class="px-3 py-2 bg-surface-2 text-xs text-text-muted flex flex-wrap gap-3 justify-between">
          <span>{{ tableData?.match_count ?? 0 }} {{ t("analysis.played").toLowerCase() }}</span>
          <span>{{ t("analysis.excluded", { count: tableData?.excluded_matches_count ?? 0 }) }}</span>
          <span>
            {{
              t("analysis.simulationInfo", {
                count: tableData?.calculation_meta?.simulations ?? 10000,
              })
            }}
          </span>
          <span>{{ tableData?.calculation_meta?.cached ? t("analysis.cacheHit") : t("analysis.cacheFresh") }}</span>
        </div>
      </div>
    </div>

    <div v-else-if="!loading && selectedLeagueId" class="text-center py-12 text-text-muted">
      {{ t("analysis.noData") }}
    </div>
  </div>
</template>
