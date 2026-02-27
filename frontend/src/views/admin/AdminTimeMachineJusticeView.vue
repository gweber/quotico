<!--
frontend/src/views/admin/AdminTimeMachineJusticeView.vue

Purpose:
    Admin analytics view for Engine Time Machine justice snapshots.
    Shows filters, snapshot list, and xP table drilldown per snapshot.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";

interface JusticeTeamRow {
  rank: number;
  team_id: string;
  team_name: string;
  played: number;
  xp: number;
  xg_for: number;
  xg_against: number;
  xg_diff: number;
}

interface JusticeSnapshot {
  id: string;
  league_id: number;
  snapshot_date: string | null;
  window_start: string | null;
  window_end: string | null;
  meta: Record<string, unknown>;
  table_size: number;
  top3: JusticeTeamRow[];
  table: JusticeTeamRow[];
}

interface JusticeResponse {
  items: JusticeSnapshot[];
  count: number;
  available_sports: number[];
  filters: {
    league_id: number | null;
    limit: number;
    days: number;
  };
}

const api = useApi();
const { t } = useI18n();

const loading = ref(true);
const error = ref<string | null>(null);
const items = ref<JusticeSnapshot[]>([]);
const availableSports = ref<number[]>([]);
const selectedId = ref<string>("");

const filterSport = ref<number | null>(null);
const filterLimit = ref<number>(50);
const filterDays = ref<number>(180);
const snapshotSearch = ref<string>("");

const filteredItems = computed(() => {
  const needle = snapshotSearch.value.trim().toLowerCase();
  if (!needle) return items.value;
  return items.value.filter((item) => {
    const sport = String(item.league_id);
    const date = (item.snapshot_date || "").toLowerCase();
    return sport.includes(needle) || date.includes(needle);
  });
});

const selectedSnapshot = computed(
  () => filteredItems.value.find((item) => item.id === selectedId.value) || filteredItems.value[0] || null,
);
const topSnapshot = computed(() => items.value[0] || null);

const latestMeta = computed(() => {
  const meta = topSnapshot.value?.meta || {};
  return {
    generatedAt: String(meta.generated_at || ""),
    source: String(meta.source || ""),
    skippedMissingTeamIds: Number(meta.skipped_missing_team_ids || 0),
  };
});

async function fetchJustice() {
  loading.value = true;
  error.value = null;
  try {
    const qs = new URLSearchParams();
    qs.set("limit", String(filterLimit.value));
    qs.set("days", String(filterDays.value));
    if (filterSport.value !== null) {
      qs.set("league_id", String(filterSport.value));
    }
    const result = await api.get<JusticeResponse>(`/admin/time-machine/justice?${qs.toString()}`);
    items.value = result.items || [];
    availableSports.value = result.available_sports || [];
    if (!selectedId.value || !filteredItems.value.some((item) => item.id === selectedId.value)) {
      selectedId.value = filteredItems.value[0]?.id || "";
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString();
}

onMounted(() => {
  void fetchJustice();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
      <div class="flex items-center justify-between gap-3">
        <h1 class="text-xl font-bold text-text-primary">{{ t("admin.timeMachineJustice.title") }}</h1>
        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1.5 text-xs text-text-secondary hover:border-primary/60"
          @click="fetchJustice"
        >
          {{ t("admin.timeMachineJustice.refresh") }}
        </button>
      </div>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.timeMachineJustice.subtitle") }}</p>
    </div>

    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div>
          <label class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.filters.sport") }}</label>
          <select
            v-model="filterSport"
            class="mt-1 w-full rounded-card border border-surface-3 bg-surface-0 px-2 py-1.5 text-sm text-text-primary"
          >
            <option :value="null">{{ t("admin.timeMachineJustice.filters.allSports") }}</option>
            <option v-for="sport in availableSports" :key="sport" :value="sport">{{ sport }}</option>
          </select>
        </div>
        <div>
          <label class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.filters.days") }}</label>
          <select
            v-model.number="filterDays"
            class="mt-1 w-full rounded-card border border-surface-3 bg-surface-0 px-2 py-1.5 text-sm text-text-primary"
          >
            <option :value="30">30</option>
            <option :value="90">90</option>
            <option :value="180">180</option>
            <option :value="365">365</option>
            <option :value="730">730</option>
            <option :value="0">{{ t("admin.timeMachineJustice.filters.allTime") }}</option>
          </select>
        </div>
        <div>
          <label class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.filters.limit") }}</label>
          <select
            v-model.number="filterLimit"
            class="mt-1 w-full rounded-card border border-surface-3 bg-surface-0 px-2 py-1.5 text-sm text-text-primary"
          >
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
          </select>
        </div>
        <div class="flex items-end">
          <button
            type="button"
            class="w-full rounded-card bg-primary/15 text-primary border border-primary/30 px-3 py-2 text-sm hover:bg-primary/20"
            @click="fetchJustice"
          >
            {{ t("admin.timeMachineJustice.filters.apply") }}
          </button>
        </div>
      </div>
      <div class="mt-3">
        <label class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.filters.search") }}</label>
        <input
          v-model="snapshotSearch"
          type="text"
          class="mt-1 w-full rounded-card border border-surface-3 bg-surface-0 px-2 py-1.5 text-sm text-text-primary"
          :placeholder="t('admin.timeMachineJustice.filters.searchPlaceholder')"
        />
      </div>
    </div>

    <div v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-text-muted">
      {{ t("admin.timeMachineJustice.loading") }}
    </div>
    <div v-else-if="error" class="rounded-card border border-danger/60 bg-danger-muted/10 p-4 text-danger">
      {{ error }}
    </div>
    <template v-else>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.kpi.snapshots") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ items.length }}</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.kpi.latestSport") }}</p>
          <p class="text-sm font-semibold text-text-primary">{{ topSnapshot?.league_id || "--" }}</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.kpi.latestDate") }}</p>
          <p class="text-sm font-semibold text-text-primary">{{ formatDate(topSnapshot?.snapshot_date || null) }}</p>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
          <p class="text-[11px] text-text-muted">{{ t("admin.timeMachineJustice.kpi.skippedMissingTeams") }}</p>
          <p class="text-lg font-semibold text-text-primary">{{ latestMeta.skippedMissingTeamIds }}</p>
        </div>
      </div>

      <div v-if="!filteredItems.length" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-sm text-text-muted">
        {{ t("admin.timeMachineJustice.noData") }}
      </div>

      <div v-else class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
          <div class="px-3 py-2 border-b border-surface-3/60">
            <p class="text-sm font-semibold text-text-primary">{{ t("admin.timeMachineJustice.snapshotsTitle") }}</p>
          </div>
          <div class="max-h-[420px] overflow-auto">
            <button
              v-for="item in filteredItems"
              :key="item.id"
              type="button"
              class="w-full text-left px-3 py-2 border-b border-surface-3/40 hover:bg-surface-2/40"
              :class="selectedId === item.id ? 'bg-primary/10' : ''"
              @click="selectedId = item.id"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="text-xs text-text-secondary">{{ item.league_id }}</span>
                <span class="text-[11px] text-text-muted">{{ formatDate(item.snapshot_date) }}</span>
              </div>
              <div class="text-[11px] text-text-muted mt-0.5">
                {{ t("admin.timeMachineJustice.tableSize", { count: item.table_size }) }}
              </div>
            </button>
          </div>
        </div>

        <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
          <div class="px-3 py-2 border-b border-surface-3/60">
            <p class="text-sm font-semibold text-text-primary">{{ t("admin.timeMachineJustice.detailTitle") }}</p>
          </div>
          <div v-if="!selectedSnapshot" class="p-4 text-sm text-text-muted">
            {{ t("admin.timeMachineJustice.noSelection") }}
          </div>
          <div v-else class="p-3 space-y-3">
            <div class="text-xs text-text-muted">
              {{ selectedSnapshot.league_id }} · {{ formatDate(selectedSnapshot.snapshot_date) }}
            </div>
            <div class="text-[11px] text-text-muted">
              {{ t("admin.timeMachineJustice.window", { start: formatDate(selectedSnapshot.window_start), end: formatDate(selectedSnapshot.window_end) }) }}
            </div>
            <div v-if="selectedSnapshot.top3?.length" class="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <div
                v-for="(top, idx) in selectedSnapshot.top3"
                :key="`${selectedSnapshot.id}-top-${top.team_id}`"
                class="rounded-card border border-surface-3/60 bg-surface-2/30 px-2 py-1.5"
              >
                <p class="text-[11px] text-text-muted">#{{ idx + 1 }}</p>
                <p class="text-xs font-semibold text-text-primary truncate">{{ top.team_name }}</p>
                <p class="text-[11px] text-text-secondary font-mono">{{ top.xp.toFixed(2) }} xP</p>
              </div>
            </div>
            <div class="overflow-auto max-h-[320px] border border-surface-3/50 rounded-card">
              <table class="w-full text-xs">
                <thead class="bg-surface-2/70 border-b border-surface-3/60">
                  <tr>
                    <th class="px-2 py-1.5 text-left text-text-secondary">#</th>
                    <th class="px-2 py-1.5 text-left text-text-secondary">{{ t("admin.timeMachineJustice.team") }}</th>
                    <th class="px-2 py-1.5 text-left text-text-secondary">xP</th>
                    <th class="px-2 py-1.5 text-left text-text-secondary">xG±</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in selectedSnapshot.table.slice(0, 20)"
                    :key="`${selectedSnapshot.id}-${row.team_id}`"
                    class="border-b border-surface-3/40 last:border-0"
                  >
                    <td class="px-2 py-1.5 text-text-muted">{{ row.rank }}</td>
                    <td class="px-2 py-1.5 text-text-primary truncate">{{ row.team_name }}</td>
                    <td class="px-2 py-1.5 font-mono text-text-secondary">{{ row.xp.toFixed(2) }}</td>
                    <td class="px-2 py-1.5 font-mono" :class="row.xg_diff >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                      {{ row.xg_diff >= 0 ? "+" : "" }}{{ row.xg_diff.toFixed(2) }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="text-[11px] text-text-muted">
              {{ t("admin.timeMachineJustice.generatedAt", { ts: formatDate(String(selectedSnapshot.meta.generated_at || null)) }) }}
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
