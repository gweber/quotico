<!--
frontend/src/views/admin/AdminRefereeTower.vue

Purpose:
    Admin Referee Tower list view. Displays Sportmonks-native referee DNA
    summary with filters, strictness gauge, and navigation to detail view.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

type StrictnessBand = "loose" | "normal" | "extreme_strict";
type SortableKey = "referee" | "matches" | "avgYellow" | "avgRed" | "penaltyPct" | "strictness" | "lastSeen";
type SortDirection = "asc" | "desc";

interface RefereeRow {
  referee_id: number;
  referee_name: string;
  matches_officiated: number;
  avg_yellow: number;
  avg_red: number;
  penalty_pct: number;
  strictness_points_per_match: number;
  strictness_index: number;
  strictness_band: StrictnessBand;
  last_seen_at: string | null;
}

interface LeagueOption {
  league_id: number;
  league_name: string;
}

interface RefereeListResponse {
  items: RefereeRow[];
  count: number;
  baseline_points_per_match: number;
  league_options: LeagueOption[];
}

const api = useApi();
const toast = useToast();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const rows = ref<RefereeRow[]>([]);
const leagueOptions = ref<LeagueOption[]>([]);
const baselinePoints = ref<number>(0);
const search = ref("");
const strictness = ref<"all" | StrictnessBand>("all");
const leagueId = ref<string>("");
const sortKey = ref<SortableKey | null>(null);
const sortDirection = ref<SortDirection | null>(null);

const strictnessOptions = computed(() => [
  { value: "all", label: t("admin.referees.filters.strictnessAll") },
  { value: "loose", label: t("admin.referees.bands.loose") },
  { value: "normal", label: t("admin.referees.bands.normal") },
  { value: "extreme_strict", label: t("admin.referees.bands.extremeStrict") },
]);

function gaugePercent(index: number): number {
  return Math.max(0, Math.min(100, index));
}

function strictnessBadgeClass(band: StrictnessBand): string {
  if (band === "extreme_strict") return "bg-danger/20 text-danger";
  if (band === "loose") return "bg-primary/20 text-primary";
  return "bg-surface-2 text-text-secondary";
}

function bandLabel(band: StrictnessBand): string {
  if (band === "extreme_strict") return t("admin.referees.bands.extremeStrict");
  if (band === "loose") return t("admin.referees.bands.loose");
  return t("admin.referees.bands.normal");
}

function formatDate(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function defaultDirectionFor(key: SortableKey): SortDirection {
  if (key === "referee") return "asc";
  return "desc";
}

function rowRefereeValue(row: RefereeRow): string {
  const name = String(row.referee_name || "").trim();
  if (name) return name.toLowerCase();
  return `#${row.referee_id}`;
}

function rowNumericValue(row: RefereeRow, key: Exclude<SortableKey, "referee" | "lastSeen">): number {
  if (key === "matches") return Number(row.matches_officiated || 0);
  if (key === "avgYellow") return Number(row.avg_yellow || 0);
  if (key === "avgRed") return Number(row.avg_red || 0);
  if (key === "penaltyPct") return Number(row.penalty_pct || 0);
  return Number(row.strictness_index || 0);
}

function rowDateValue(row: RefereeRow): number {
  if (!row.last_seen_at) return 0;
  const ts = new Date(row.last_seen_at).getTime();
  return Number.isFinite(ts) ? ts : 0;
}

function compareRows(a: RefereeRow, b: RefereeRow, key: SortableKey, direction: SortDirection): number {
  let result = 0;
  if (key === "referee") {
    result = rowRefereeValue(a).localeCompare(rowRefereeValue(b));
  } else if (key === "lastSeen") {
    result = rowDateValue(a) - rowDateValue(b);
  } else {
    result = rowNumericValue(a, key) - rowNumericValue(b, key);
  }
  if (result === 0) {
    result = Number(a.referee_id) - Number(b.referee_id);
  }
  return direction === "asc" ? result : -result;
}

function toggleSort(key: SortableKey): void {
  const defaultDirection = defaultDirectionFor(key);
  if (sortKey.value !== key) {
    sortKey.value = key;
    sortDirection.value = defaultDirection;
    return;
  }
  if (sortDirection.value === defaultDirection) {
    sortDirection.value = defaultDirection === "asc" ? "desc" : "asc";
    return;
  }
  sortKey.value = null;
  sortDirection.value = null;
}

function sortIndicator(key: SortableKey): string {
  if (sortKey.value !== key || !sortDirection.value) return "↕";
  return sortDirection.value === "asc" ? "▲" : "▼";
}

function sortActionLabel(key: SortableKey): string {
  const defaultDirection = defaultDirectionFor(key);
  if (sortKey.value !== key || !sortDirection.value) {
    return defaultDirection === "asc" ? t("admin.referees.table.sortAsc") : t("admin.referees.table.sortDesc");
  }
  if (sortDirection.value === defaultDirection) {
    return defaultDirection === "asc" ? t("admin.referees.table.sortDesc") : t("admin.referees.table.sortAsc");
  }
  return t("admin.referees.table.sortReset");
}

const displayRows = computed(() => {
  if (!sortKey.value || !sortDirection.value) return rows.value;
  return [...rows.value].sort((a, b) => compareRows(a, b, sortKey.value as SortableKey, sortDirection.value as SortDirection));
});

async function loadReferees(): Promise<void> {
  loading.value = true;
  try {
    const query: Record<string, string> = {};
    if (search.value.trim()) query.search = search.value.trim();
    if (leagueId.value) query.league_id = leagueId.value;
    if (strictness.value !== "all") query.strictness = strictness.value;
    const result = await api.get<RefereeListResponse>("/admin/referees", query);
    rows.value = result.items;
    leagueOptions.value = result.league_options;
    baselinePoints.value = result.baseline_points_per_match;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    loading.value = false;
  }
}

function openDetail(refereeId: number): void {
  void router.push({ name: "admin-referee-detail", params: { refereeId } });
}

onMounted(() => {
  void loadReferees();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.referees.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.referees.subtitle") }}</p>
      <p class="text-xs text-text-muted mt-2">
        {{ t("admin.referees.baseline", { value: baselinePoints.toFixed(3) }) }}
      </p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-2">
        <div class="flex flex-col">
          <label for="ref-search" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.referees.filters.searchLabel") }}</label>
          <input id="ref-search" v-model="search" type="text" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm" :placeholder="t('admin.referees.filters.searchPlaceholder')" @keyup.enter="loadReferees" />
        </div>
        <div class="flex flex-col">
          <label for="ref-league" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.referees.filters.leagueLabel") }}</label>
          <select id="ref-league" v-model="leagueId" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm">
            <option value="">{{ t("admin.referees.filters.leagueAll") }}</option>
            <option v-for="league in leagueOptions" :key="league.league_id" :value="String(league.league_id)">
              {{ league.league_name || league.league_id }}
            </option>
          </select>
        </div>
        <div class="flex flex-col">
          <label for="ref-strictness" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.referees.filters.strictnessLabel") }}</label>
          <select id="ref-strictness" v-model="strictness" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm">
            <option v-for="option in strictnessOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </div>
        <div class="flex items-end">
          <button type="button" class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-secondary hover:border-primary/60" @click="loadReferees">
            {{ t("admin.referees.filters.apply") }}
          </button>
        </div>
      </div>
    </section>

    <section v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-sm text-text-muted">
      {{ t("admin.referees.loading") }}
    </section>

    <section v-else class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
      <div class="px-4 py-3 border-b border-surface-3/60 text-sm font-semibold text-text-primary">
        {{ t("admin.referees.results", { count: rows.length }) }}
      </div>
      <div v-if="rows.length === 0" class="p-4 text-sm text-text-muted">{{ t("admin.referees.empty") }}</div>
      <div v-else class="overflow-auto max-h-[32rem]">
        <table class="w-full text-sm">
          <thead class="sticky top-0 z-10 bg-surface-2/95 border-b border-surface-3/60 text-text-secondary">
            <tr>
              <th class="px-3 py-2 text-left bg-surface-2/95">
                <button type="button" class="w-full text-left flex items-center justify-between gap-2" :title="sortActionLabel('referee')" :aria-label="sortActionLabel('referee')" @click="toggleSort('referee')">
                  <span>{{ t("admin.referees.table.referee") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("referee") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-right bg-surface-2/95">
                <button type="button" class="w-full text-right flex items-center justify-end gap-2" :title="sortActionLabel('matches')" :aria-label="sortActionLabel('matches')" @click="toggleSort('matches')">
                  <span>{{ t("admin.referees.table.matches") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("matches") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-right bg-surface-2/95">
                <button type="button" class="w-full text-right flex items-center justify-end gap-2" :title="sortActionLabel('avgYellow')" :aria-label="sortActionLabel('avgYellow')" @click="toggleSort('avgYellow')">
                  <span>{{ t("admin.referees.table.avgYellow") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("avgYellow") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-right bg-surface-2/95">
                <button type="button" class="w-full text-right flex items-center justify-end gap-2" :title="sortActionLabel('avgRed')" :aria-label="sortActionLabel('avgRed')" @click="toggleSort('avgRed')">
                  <span>{{ t("admin.referees.table.avgRed") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("avgRed") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-right bg-surface-2/95">
                <button type="button" class="w-full text-right flex items-center justify-end gap-2" :title="sortActionLabel('penaltyPct')" :aria-label="sortActionLabel('penaltyPct')" @click="toggleSort('penaltyPct')">
                  <span>{{ t("admin.referees.table.penaltyPct") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("penaltyPct") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-left bg-surface-2/95">
                <button type="button" class="w-full text-left flex items-center justify-between gap-2" :title="sortActionLabel('strictness')" :aria-label="sortActionLabel('strictness')" @click="toggleSort('strictness')">
                  <span>{{ t("admin.referees.table.strictness") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("strictness") }}</span>
                </button>
              </th>
              <th class="px-3 py-2 text-right bg-surface-2/95">
                <button type="button" class="w-full text-right flex items-center justify-end gap-2" :title="sortActionLabel('lastSeen')" :aria-label="sortActionLabel('lastSeen')" @click="toggleSort('lastSeen')">
                  <span>{{ t("admin.referees.table.lastSeen") }}</span>
                  <span class="text-[10px]">{{ sortIndicator("lastSeen") }}</span>
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in displayRows" :key="row.referee_id" class="border-b border-surface-3/40 hover:bg-surface-2/20 cursor-pointer" @click="openDetail(row.referee_id)">
              <td class="px-3 py-2">
                <div class="font-medium text-text-primary">{{ row.referee_name || ("#" + String(row.referee_id)) }}</div>
                <div class="text-xs text-text-muted">#{{ row.referee_id }}</div>
              </td>
              <td class="px-3 py-2 text-right tabular-nums">{{ row.matches_officiated }}</td>
              <td class="px-3 py-2 text-right tabular-nums">{{ row.avg_yellow.toFixed(2) }}</td>
              <td class="px-3 py-2 text-right tabular-nums">{{ row.avg_red.toFixed(2) }}</td>
              <td class="px-3 py-2 text-right tabular-nums">{{ row.penalty_pct.toFixed(1) }}%</td>
              <td class="px-3 py-2">
                <div class="flex items-center gap-2">
                  <div class="w-28 h-2 rounded bg-surface-3 overflow-hidden">
                    <div class="h-2 bg-primary" :style="{ width: `${gaugePercent(row.strictness_index)}%` }" />
                  </div>
                  <span class="rounded-full px-2 py-0.5 text-xs font-medium" :class="strictnessBadgeClass(row.strictness_band)">
                    {{ bandLabel(row.strictness_band) }} ({{ row.strictness_index.toFixed(1) }})
                  </span>
                </div>
              </td>
              <td class="px-3 py-2 text-right text-xs text-text-muted">{{ formatDate(row.last_seen_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
