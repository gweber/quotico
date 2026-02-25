<!--
frontend/src/views/admin/AdminTeamTowerView.vue

Purpose:
    Central admin interface for Team Tower operations using team_id-based workflows.
    Supports review filtering, alias CRUD, and irreversible team merges.

Dependencies:
    - @/composables/useApi
    - @/composables/useToast
    - vue-i18n
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

interface TeamAlias {
  name: string;
  normalized: string;
  sport_key: string | null;
  source: string | null;
}

interface TeamItem {
  id: string;
  display_name: string;
  canonical_id: string | null;
  needs_review: boolean;
  sport_key: string | null;
  aliases: TeamAlias[];
}

interface TeamListResponse {
  total: number;
  offset: number;
  limit: number;
  items: TeamItem[];
}

interface MergeResponse {
  message: string;
  stats: Record<string, number>;
}

const api = useApi();
const toast = useToast();
const { t } = useI18n();

const loading = ref(true);
const mergeSearchLoading = ref(false);
const teams = ref<TeamItem[]>([]);
const mergeSearchResults = ref<TeamItem[]>([]);
const search = ref("");
const showNeedsReviewOnly = ref(true);
const mergeSearch = ref("");
const sourceTeamId = ref<string>("");
const targetTeamId = ref<string>("");
const draggingSourceId = ref<string>("");
const aliasDrafts = reactive<Record<string, { name: string; sport_key: string }>>({});
const aliasSavingByTeam = reactive<Record<string, boolean>>({});
const mergeBusy = ref(false);
const confirmMergeOpen = ref(false);

const sourceTeam = computed(() => teams.value.find((team) => team.id === sourceTeamId.value) ?? null);
const targetTeam = computed(() => {
  const fromList = teams.value.find((team) => team.id === targetTeamId.value);
  if (fromList) return fromList;
  return mergeSearchResults.value.find((team) => team.id === targetTeamId.value) ?? null;
});

function hasAliasDraft(teamId: string): boolean {
  return Boolean(aliasDrafts[teamId]);
}

function ensureAliasDraft(teamId: string): void {
  if (!hasAliasDraft(teamId)) {
    aliasDrafts[teamId] = { name: "", sport_key: "" };
  }
}

function sportBadgeClass(sportKey: string | null): string {
  if (!sportKey) return "bg-surface-2 text-text-muted border-surface-3";
  if (sportKey.includes("soccer")) return "bg-emerald-100 text-emerald-800 border-emerald-300";
  if (sportKey.includes("nfl")) return "bg-sky-100 text-sky-800 border-sky-300";
  if (sportKey.includes("nba")) return "bg-orange-100 text-orange-800 border-orange-300";
  return "bg-slate-100 text-slate-800 border-slate-300";
}

async function fetchTeams(): Promise<void> {
  loading.value = true;
  try {
    const params: Record<string, string> = { limit: "200", offset: "0" };
    if (search.value.trim()) params.search = search.value.trim();
    if (showNeedsReviewOnly.value) params.needs_review = "true";
    const result = await api.get<TeamListResponse>("/admin/teams", params);
    teams.value = result.items;
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    loading.value = false;
  }
}

async function fetchMergeTargets(): Promise<void> {
  if (!mergeSearch.value.trim()) {
    mergeSearchResults.value = [];
    return;
  }
  mergeSearchLoading.value = true;
  try {
    const result = await api.get<TeamListResponse>("/admin/teams", {
      search: mergeSearch.value.trim(),
      limit: "25",
      offset: "0",
    });
    mergeSearchResults.value = result.items;
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    mergeSearchLoading.value = false;
  }
}

function setSourceTeam(teamId: string): void {
  sourceTeamId.value = teamId;
  if (teamId === targetTeamId.value) targetTeamId.value = "";
}

function setTargetTeam(teamId: string): void {
  targetTeamId.value = teamId;
  if (teamId === sourceTeamId.value) sourceTeamId.value = "";
}

function onSourceDragStart(teamId: string): void {
  draggingSourceId.value = teamId;
}

function onTargetDrop(teamId: string): void {
  if (!draggingSourceId.value || draggingSourceId.value === teamId) return;
  sourceTeamId.value = draggingSourceId.value;
  targetTeamId.value = teamId;
  draggingSourceId.value = "";
}

async function markReviewState(team: TeamItem, nextState: boolean): Promise<void> {
  try {
    await api.patch(`/admin/teams/${team.id}`, { needs_review: nextState });
    team.needs_review = nextState;
    toast.success(t("admin.teamTower.reviewUpdated"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  }
}

async function addAlias(team: TeamItem): Promise<void> {
  ensureAliasDraft(team.id);
  const draft = aliasDrafts[team.id];
  const aliasName = draft.name.trim();
  if (!aliasName) {
    toast.error(t("admin.teamTower.aliasNameRequired"));
    return;
  }
  aliasSavingByTeam[team.id] = true;
  try {
    await api.post(`/admin/teams/${team.id}/aliases`, {
      name: aliasName,
      sport_key: draft.sport_key.trim() || null,
    });
    draft.name = "";
    draft.sport_key = "";
    await fetchTeams();
    toast.success(t("admin.teamTower.aliasAdded"));
    toast.success(t("admin.teamTower.registryReinitialized"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSavingByTeam[team.id] = false;
  }
}

async function removeAlias(team: TeamItem, alias: TeamAlias): Promise<void> {
  try {
    await api.del(`/admin/teams/${team.id}/aliases`, {
      name: alias.name,
      sport_key: alias.sport_key,
    });
    await fetchTeams();
    toast.success(t("admin.teamTower.aliasRemoved"));
    toast.success(t("admin.teamTower.registryReinitialized"));
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  }
}

function requestMerge(): void {
  if (!sourceTeam.value || !targetTeam.value) {
    toast.error(t("admin.teamTower.selectSourceAndTarget"));
    return;
  }
  if (sourceTeam.value.id === targetTeam.value.id) {
    toast.error(t("admin.teamTower.mergeSameTeamError"));
    return;
  }
  confirmMergeOpen.value = true;
}

async function executeMerge(): Promise<void> {
  if (!sourceTeam.value || !targetTeam.value) return;
  mergeBusy.value = true;
  try {
    const result = await api.post<MergeResponse>(`/admin/teams/${sourceTeam.value.id}/merge`, {
      target_id: targetTeam.value.id,
    });
    toast.success(t("admin.teamTower.mergeSuccess"));
    if (result.message) {
      toast.success(result.message);
    }
    confirmMergeOpen.value = false;
    sourceTeamId.value = "";
    targetTeamId.value = "";
    mergeSearch.value = "";
    mergeSearchResults.value = [];
    await fetchTeams();
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    mergeBusy.value = false;
  }
}

let listSearchTimer: ReturnType<typeof setTimeout> | null = null;
watch([search, showNeedsReviewOnly], () => {
  if (listSearchTimer) clearTimeout(listSearchTimer);
  listSearchTimer = setTimeout(() => {
    void fetchTeams();
  }, 250);
});

let mergeSearchTimer: ReturnType<typeof setTimeout> | null = null;
watch(mergeSearch, () => {
  if (mergeSearchTimer) clearTimeout(mergeSearchTimer);
  mergeSearchTimer = setTimeout(() => {
    void fetchMergeTargets();
  }, 250);
});

onMounted(() => {
  void fetchTeams();
});
</script>

<template>
  <div class="max-w-6xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.teamTower.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.teamTower.subtitle") }}</p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <div class="flex flex-col md:flex-row md:items-center gap-3">
        <label class="inline-flex items-center gap-2 text-sm text-text-secondary">
          <input
            v-model="showNeedsReviewOnly"
            type="checkbox"
            class="h-4 w-4 rounded border-surface-3 bg-surface-0 text-primary focus:ring-primary/30"
          />
          <span>{{ t("admin.teamTower.filters.needsReviewOnly") }}</span>
        </label>
        <input
          v-model="search"
          type="search"
          class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
          :placeholder="t('admin.teamTower.filters.searchPlaceholder')"
        />
      </div>
    </section>

    <section class="rounded-card border border-warning/40 bg-warning/5 p-4 md:p-5">
      <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.teamTower.merge.title") }}</h2>
      <p class="text-sm text-text-secondary mt-1">{{ t("admin.teamTower.merge.description") }}</p>

      <div class="grid md:grid-cols-3 gap-3 mt-3">
        <div class="rounded-card border border-surface-3 bg-surface-0 p-3">
          <p class="text-xs text-text-muted">{{ t("admin.teamTower.merge.source") }}</p>
          <p class="text-sm font-medium text-text-primary mt-1 truncate">
            {{ sourceTeam?.display_name || t("admin.teamTower.merge.unselected") }}
          </p>
        </div>
        <div class="rounded-card border border-surface-3 bg-surface-0 p-3">
          <p class="text-xs text-text-muted">{{ t("admin.teamTower.merge.target") }}</p>
          <p class="text-sm font-medium text-text-primary mt-1 truncate">
            {{ targetTeam?.display_name || t("admin.teamTower.merge.unselected") }}
          </p>
        </div>
        <button
          class="rounded-card bg-danger px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!sourceTeam || !targetTeam || mergeBusy"
          @click="requestMerge"
        >
          {{ mergeBusy ? t("admin.teamTower.merge.merging") : t("admin.teamTower.merge.execute") }}
        </button>
      </div>

      <div class="mt-3">
        <input
          v-model="mergeSearch"
          type="search"
          class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
          :placeholder="t('admin.teamTower.merge.searchTargetPlaceholder')"
        />
        <div
          v-if="mergeSearchLoading"
          class="mt-2 rounded-card border border-surface-3 bg-surface-0 p-3 text-sm text-text-muted"
        >
          {{ t("admin.teamTower.loading") }}
        </div>
        <div
          v-else-if="mergeSearch.trim() && mergeSearchResults.length === 0"
          class="mt-2 rounded-card border border-surface-3 bg-surface-0 p-3 text-sm text-text-muted"
        >
          {{ t("admin.teamTower.noResults") }}
        </div>
        <div v-else-if="mergeSearchResults.length" class="mt-2 space-y-2">
          <button
            v-for="team in mergeSearchResults"
            :key="team.id"
            type="button"
            class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-left hover:border-primary/60"
            @click="setTargetTeam(team.id)"
          >
            <span class="text-sm font-medium text-text-primary">{{ team.display_name }}</span>
            <span class="ml-2 text-xs text-text-muted">{{ t("admin.teamTower.labels.teamId") }}: {{ team.id }}</span>
          </button>
        </div>
      </div>
    </section>

    <section class="space-y-3">
      <div v-if="loading" class="grid md:grid-cols-2 gap-3">
        <div
          v-for="index in 6"
          :key="index"
          class="rounded-card border border-surface-3/60 bg-surface-1 p-4 animate-pulse"
        >
          <div class="h-4 w-40 bg-surface-3 rounded mb-3" />
          <div class="h-3 w-56 bg-surface-3 rounded mb-3" />
          <div class="flex gap-2">
            <div class="h-6 w-20 bg-surface-3 rounded-full" />
            <div class="h-6 w-24 bg-surface-3 rounded-full" />
          </div>
        </div>
      </div>

      <div v-else-if="teams.length === 0" class="rounded-card border border-surface-3/60 bg-surface-1 p-6 text-center">
        <p class="text-sm text-text-muted">{{ t("admin.teamTower.noResults") }}</p>
      </div>

      <template v-else>
        <article
          v-for="team in teams"
          :key="team.id"
          class="rounded-card border border-surface-3/60 bg-surface-1 p-4"
          draggable="true"
          @dragstart="onSourceDragStart(team.id)"
          @dragover.prevent
          @drop.prevent="onTargetDrop(team.id)"
        >
        <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
          <div>
            <h3 class="text-base font-semibold text-text-primary">{{ team.display_name }}</h3>
            <p class="text-xs text-text-muted mt-1">
              {{ t("admin.teamTower.labels.canonicalId") }}: {{ team.canonical_id || t("admin.teamTower.labels.notAvailable") }}
            </p>
            <p class="text-xs text-text-muted">
              {{ t("admin.teamTower.labels.teamId") }}: {{ team.id }}
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <span
              class="rounded-full px-2.5 py-1 text-xs font-medium"
              :class="team.needs_review ? 'bg-warning/20 text-warning' : 'bg-primary-muted/20 text-primary'"
            >
              {{ team.needs_review ? t("admin.teamTower.badges.needsReview") : t("admin.teamTower.badges.reviewed") }}
            </span>
            <button
              v-if="team.needs_review"
              type="button"
              class="rounded-card border border-primary/30 bg-primary/10 px-3 py-1 text-xs font-medium text-primary hover:bg-primary/15"
              @click="markReviewState(team, false)"
            >
              {{ t("admin.teamTower.actions.confirmTeam") }}
            </button>
            <button
              v-else
              type="button"
              class="rounded-card border border-warning/40 bg-warning/10 px-3 py-1 text-xs font-medium text-warning hover:bg-warning/20"
              @click="markReviewState(team, true)"
            >
              {{ t("admin.teamTower.actions.markNeedsReview") }}
            </button>
          </div>
        </div>

        <div class="mt-3 flex flex-wrap gap-2">
          <div
            v-for="alias in team.aliases"
            :key="`${alias.normalized}:${alias.sport_key || 'any'}`"
            class="inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs"
            :class="sportBadgeClass(alias.sport_key)"
          >
            <span class="font-medium">{{ alias.name }}</span>
            <span class="opacity-80">{{ alias.sport_key || t("admin.teamTower.labels.global") }}</span>
            <button
              type="button"
              class="rounded-full px-1 leading-none hover:bg-black/10"
              :aria-label="t('admin.teamTower.actions.removeAlias')"
              @click="removeAlias(team, alias)"
            >
              x
            </button>
          </div>
          <span v-if="team.aliases.length === 0" class="text-xs text-text-muted">
            {{ t("admin.teamTower.aliases.empty") }}
          </span>
        </div>

        <div class="mt-3 grid md:grid-cols-5 gap-2">
          <input
            :value="hasAliasDraft(team.id) ? aliasDrafts[team.id].name : ''"
            type="text"
            class="md:col-span-2 rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
            :placeholder="t('admin.teamTower.aliases.namePlaceholder')"
            @focus="ensureAliasDraft(team.id)"
            @input="ensureAliasDraft(team.id); aliasDrafts[team.id].name = ($event.target as HTMLInputElement).value"
          />
          <input
            :value="hasAliasDraft(team.id) ? aliasDrafts[team.id].sport_key : ''"
            type="text"
            class="md:col-span-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
            :placeholder="t('admin.teamTower.aliases.sportPlaceholder')"
            @focus="ensureAliasDraft(team.id)"
            @input="ensureAliasDraft(team.id); aliasDrafts[team.id].sport_key = ($event.target as HTMLInputElement).value"
          />
          <button
            type="button"
            class="rounded-card bg-primary px-3 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="Boolean(aliasSavingByTeam[team.id])"
            @click="addAlias(team)"
          >
            {{ aliasSavingByTeam[team.id] ? t("admin.teamTower.actions.saving") : t("admin.teamTower.actions.addAlias") }}
          </button>
          <div class="flex gap-2">
            <button
              type="button"
              class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-xs font-medium text-text-secondary hover:border-primary/50"
              @click="setSourceTeam(team.id)"
            >
              {{ t("admin.teamTower.actions.setSource") }}
            </button>
            <button
              type="button"
              class="flex-1 rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-xs font-medium text-text-secondary hover:border-primary/50"
              @click="setTargetTeam(team.id)"
            >
              {{ t("admin.teamTower.actions.setTarget") }}
            </button>
          </div>
        </div>
        </article>
      </template>
    </section>

    <div
      v-if="confirmMergeOpen && sourceTeam && targetTeam"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
    >
      <div class="w-full max-w-xl rounded-card border border-surface-3 bg-surface-0 p-5">
        <h3 class="text-lg font-semibold text-text-primary">{{ t("admin.teamTower.merge.confirmTitle") }}</h3>
        <p class="text-sm text-text-secondary mt-2">
          {{
            t("admin.teamTower.merge.confirmBody", {
              source: sourceTeam.display_name,
              target: targetTeam.display_name,
            })
          }}
        </p>
        <p class="text-xs text-danger mt-3">{{ t("admin.teamTower.merge.irreversible") }}</p>
        <div class="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            class="rounded-card border border-surface-3 px-3 py-2 text-sm text-text-secondary"
            @click="confirmMergeOpen = false"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="rounded-card bg-danger px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
            :disabled="mergeBusy"
            @click="executeMerge"
          >
            {{ mergeBusy ? t("admin.teamTower.merge.merging") : t("admin.teamTower.merge.confirmAction") }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
