<!--
frontend/src/views/admin/AdminLeagueTowerView.vue

Purpose:
    Central configuration hub for leagues. Controls sync status, tipping
    availability, and data-only modes (e.g. Champions League).

Dependencies:
    - app.services.admin_service
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import draggable from "vuedraggable";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

interface LeagueFeatures {
  tipping: boolean;
  match_load: boolean;
  xg_sync: boolean;
  odds_sync: boolean;
}

interface LeagueItem {
  id: string;
  sport_key: string;
  display_name: string;
  structure_type: "league" | "cup" | "tournament";
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

const api = useApi();
const toast = useToast();
const { t } = useI18n();

const loading = ref(true);
const leagues = ref<LeagueItem[]>([]);
const syncBusyById = reactive<Record<string, boolean>>({});
const importBusyById = reactive<Record<string, boolean>>({});
const toggleBusyById = reactive<Record<string, boolean>>({});
const orderSaving = ref(false);
const editBusy = ref(false);
const editOpen = ref(false);
const editingLeagueId = ref<string>("");

const editState = reactive({
  display_name: "",
  structure_type: "league" as "league" | "cup" | "tournament",
  current_season: new Date().getUTCFullYear(),
  is_active: false,
  features: {
    tipping: false,
    match_load: true,
    xg_sync: false,
    odds_sync: false,
  } as LeagueFeatures,
  external_ids: {
    theoddsapi: "",
    openligadb: "",
    football_data_uk: "",
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

function seasonCodeFromYear(startYear: number): string {
  const start = startYear % 100;
  const end = (startYear + 1) % 100;
  return `${String(start).padStart(2, "0")}${String(end).padStart(2, "0")}`;
}

function canImportStats(league: LeagueItem): boolean {
  return Boolean(league.external_ids.football_data_uk);
}

async function importStats(league: LeagueItem): Promise<void> {
  if (!canImportStats(league)) return;
  importBusyById[league.id] = true;
  try {
    const result = await api.post<{ message: string }>(
      `/admin/leagues/${league.id}/import-stats`,
      { season: seasonCodeFromYear(league.current_season) },
    );
    toast.success(result.message || t("admin.leagues.import_stats_triggered"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    importBusyById[league.id] = false;
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
  editState.current_season = league.current_season;
  editState.is_active = league.is_active;
  editState.features = {
    tipping: league.features.tipping,
    match_load: league.features.match_load,
    xg_sync: league.features.xg_sync,
    odds_sync: league.features.odds_sync,
  };
  editState.external_ids = {
    theoddsapi: league.external_ids.theoddsapi || "",
    openligadb: league.external_ids.openligadb || "",
    football_data_uk: league.external_ids.football_data_uk || "",
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
                <span class="font-mono text-xs text-text-secondary">{{ league.sport_key }}</span>
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
                    class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
                    :disabled="orderSaving || !canImportStats(league) || Boolean(importBusyById[league.id])"
                    @click="importStats(league)"
                  >
                    {{
                      importBusyById[league.id]
                        ? t("admin.leagues.import_stats_loading")
                        : t("admin.leagues.import_stats")
                    }}
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
          {{ selectedLeague?.sport_key }}
        </p>

        <div class="grid md:grid-cols-2 gap-3 mt-4">
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
                :disabled="!editState.is_active"
              />
              <span :class="!editState.is_active ? 'opacity-50' : ''">{{ t("admin.leagues.features.tipping") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.match_load"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
                :disabled="!editState.is_active"
              />
              <span :class="!editState.is_active ? 'opacity-50' : ''">{{ t("admin.leagues.features.match_load") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.xg_sync"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
                :disabled="!editState.is_active"
              />
              <span :class="!editState.is_active ? 'opacity-50' : ''">{{ t("admin.leagues.features.xg_sync") }}</span>
            </label>
            <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
              <input
                v-model="editState.features.odds_sync"
                type="checkbox"
                class="h-4 w-4 rounded border-surface-3 text-primary"
                :disabled="!editState.is_active"
              />
              <span :class="!editState.is_active ? 'opacity-50' : ''">{{ t("admin.leagues.features.odds_sync") }}</span>
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
              {{ t("admin.leagues.providers.theoddsapi") }}
              <input
                v-model="editState.external_ids.theoddsapi"
                type="text"
                class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
              />
            </label>
            <label class="text-xs text-text-secondary">
              {{ t("admin.leagues.providers.openligadb") }}
              <input
                v-model="editState.external_ids.openligadb"
                type="text"
                class="mt-1 w-full rounded-card border border-surface-3 bg-surface-1 px-3 py-2 text-sm text-text-primary"
              />
            </label>
            <label class="text-xs text-text-secondary">
              {{ t("admin.leagues.providers.football_data_uk") }}
              <input
                v-model="editState.external_ids.football_data_uk"
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
