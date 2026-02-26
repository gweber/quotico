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

interface AliasSuggestion {
  id: string;
  status: string;
  source: string;
  sport_key: string | null;
  league_id: string | null;
  league_name: string;
  league_external_id: string | null;
  raw_team_name: string;
  normalized_name: string;
  reason: string;
  confidence: number | null;
  seen_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  suggested_team_id: string | null;
  suggested_team_name: string;
  applied_to_team_id: string | null;
  applied_to_team_name: string;
  sample_refs: Array<Record<string, unknown>>;
}

interface AliasSuggestionListResponse {
  total: number;
  items: AliasSuggestion[];
}

const api = useApi();
const toast = useToast();
const { t } = useI18n();

const loading = ref(true);
const teamsError = ref("");
const mergeSearchLoading = ref(false);
const teams = ref<TeamItem[]>([]);
const mergeSearchResults = ref<TeamItem[]>([]);
const search = ref("");
const showNeedsReviewOnly = ref(false);
const mergeSearch = ref("");
const sourceTeamId = ref<string>("");
const targetTeamId = ref<string>("");
const draggingSourceId = ref<string>("");
const dragOverTargetId = ref<string>("");
const sourceZoneDragDepth = ref(0);
const targetZoneDragDepth = ref(0);
const aliasDrafts = reactive<Record<string, { name: string; sport_key: string }>>({});
const aliasSavingByTeam = reactive<Record<string, boolean>>({});
const mergeBusy = ref(false);
const confirmMergeOpen = ref(false);
const aliasSuggestionsLoading = ref(false);
const aliasSuggestionsApplying = ref(false);
const aliasSuggestions = ref<AliasSuggestion[]>([]);
const draggingSuggestionId = ref<string>("");

const sourceTeam = computed(() => {
  const fromList = teams.value.find((team) => team.id === sourceTeamId.value);
  if (fromList) return fromList;
  return mergeSearchResults.value.find((team) => team.id === sourceTeamId.value) ?? null;
});
const targetTeam = computed(() => {
  const fromList = teams.value.find((team) => team.id === targetTeamId.value);
  if (fromList) return fromList;
  return mergeSearchResults.value.find((team) => team.id === targetTeamId.value) ?? null;
});
const sourceZoneActive = computed(() => sourceZoneDragDepth.value > 0);
const targetZoneActive = computed(() => targetZoneDragDepth.value > 0);

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
  teamsError.value = "";
  try {
    const params: Record<string, string> = { limit: "200", offset: "0" };
    if (search.value.trim()) params.search = search.value.trim();
    if (showNeedsReviewOnly.value) params.needs_review = "true";
    const result = await api.get<TeamListResponse>("/admin/teams", params);
    teams.value = result.items;
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    teamsError.value = message;
    toast.error(message);
  } finally {
    loading.value = false;
  }
}

async function fetchAliasSuggestions(): Promise<void> {
  aliasSuggestionsLoading.value = true;
  try {
    const result = await api.get<AliasSuggestionListResponse>("/admin/teams/alias-suggestions", { limit: "200" });
    aliasSuggestions.value = result.items;
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSuggestionsLoading.value = false;
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

function onSourceDragStart(event: DragEvent, teamId: string): void {
  draggingSourceId.value = teamId;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-quotico-team-id", teamId);
    event.dataTransfer.setData("text/plain", teamId);
  }
}

function onSourceDragEnd(): void {
  draggingSourceId.value = "";
  dragOverTargetId.value = "";
  sourceZoneDragDepth.value = 0;
  targetZoneDragDepth.value = 0;
}

function readDraggedTeamId(event: DragEvent): string {
  const customTeamId = (event.dataTransfer?.getData("application/x-quotico-team-id") || "").trim();
  if (customTeamId) return customTeamId;
  const textPayload = (event.dataTransfer?.getData("text/plain") || "").trim();
  if (!textPayload || textPayload.startsWith("alias:")) return "";
  return textPayload;
}

function readDraggedSuggestionId(event: DragEvent): string {
  const customId = (event.dataTransfer?.getData("application/x-quotico-alias-suggestion") || "").trim();
  if (customId) return customId;
  const textPayload = (event.dataTransfer?.getData("text/plain") || "").trim();
  if (textPayload.startsWith("alias:")) {
    return textPayload.slice("alias:".length).trim();
  }
  return draggingSuggestionId.value.trim();
}

function teamIdFromSuggestion(suggestionId: string): string {
  const suggestion = aliasSuggestions.value.find((item) => item.id === suggestionId);
  if (!suggestion) return "";
  return (suggestion.suggested_team_id || suggestion.applied_to_team_id || "").trim();
}

function onTargetDrop(event: DragEvent, teamId: string): void {
  const draggedId = readDraggedTeamId(event) || draggingSourceId.value;
  if (!draggedId || draggedId === teamId) return;
  sourceTeamId.value = draggedId;
  targetTeamId.value = teamId;
  if (sourceTeamId.value === targetTeamId.value) {
    targetTeamId.value = "";
  }
  draggingSourceId.value = "";
  dragOverTargetId.value = "";
  sourceZoneDragDepth.value = 0;
  targetZoneDragDepth.value = 0;
}

async function onSourceDropZoneDrop(event: DragEvent): Promise<void> {
  const suggestionId = readDraggedSuggestionId(event);
  if (suggestionId) {
    const mappedTeamId = teamIdFromSuggestion(suggestionId);
    if (!mappedTeamId) {
      toast.error(t("admin.teamTower.aliasSuggestions.noSuggestedTeam"));
      draggingSuggestionId.value = "";
      sourceZoneDragDepth.value = 0;
      targetZoneDragDepth.value = 0;
      return;
    }
    sourceTeamId.value = mappedTeamId;
    if (targetTeamId.value === mappedTeamId) {
      targetTeamId.value = "";
    }
    draggingSuggestionId.value = "";
    sourceZoneDragDepth.value = 0;
    targetZoneDragDepth.value = 0;
    return;
  }

  const draggedId = readDraggedTeamId(event) || draggingSourceId.value;
  if (!draggedId) return;
  sourceTeamId.value = draggedId;
  if (targetTeamId.value === draggedId) {
    targetTeamId.value = "";
  }
  draggingSourceId.value = "";
  sourceZoneDragDepth.value = 0;
  targetZoneDragDepth.value = 0;
}

function onTargetDropZoneDrop(event: DragEvent): void {
  const suggestionId = readDraggedSuggestionId(event);
  if (suggestionId) {
    const mappedTeamId = teamIdFromSuggestion(suggestionId);
    if (!mappedTeamId) {
      toast.error(t("admin.teamTower.aliasSuggestions.noSuggestedTeam"));
      draggingSuggestionId.value = "";
      sourceZoneDragDepth.value = 0;
      targetZoneDragDepth.value = 0;
      return;
    }
    if (sourceTeamId.value === mappedTeamId) {
      targetTeamId.value = "";
    } else {
      targetTeamId.value = mappedTeamId;
    }
    draggingSuggestionId.value = "";
    sourceZoneDragDepth.value = 0;
    targetZoneDragDepth.value = 0;
    return;
  }

  const draggedId = readDraggedTeamId(event) || draggingSourceId.value;
  if (!draggedId) return;
  if (sourceTeamId.value === draggedId) {
    // Same team cannot be source and target.
    targetTeamId.value = "";
  } else {
    targetTeamId.value = draggedId;
  }
  draggingSourceId.value = "";
  sourceZoneDragDepth.value = 0;
  targetZoneDragDepth.value = 0;
}

function onTargetDragEnter(teamId: string): void {
  if (!draggingSourceId.value || draggingSourceId.value === teamId) return;
  dragOverTargetId.value = teamId;
}

function onTargetDragLeave(teamId: string): void {
  if (dragOverTargetId.value === teamId) {
    dragOverTargetId.value = "";
  }
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
    await Promise.all([fetchTeams(), fetchAliasSuggestions()]);
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
    const mergedSourceId = sourceTeam.value.id;
    const result = await api.post<MergeResponse>(`/admin/teams/${mergedSourceId}/merge`, {
      target_id: targetTeam.value.id,
    });
    confirmMergeOpen.value = false;
    toast.success(t("admin.teamTower.mergeSuccess"));
    if (result?.message) {
      toast.success(result.message);
    }
    // Immediate UI cleanup before server roundtrip refresh.
    mergeSearchResults.value = mergeSearchResults.value.filter((team) => team.id !== mergedSourceId);
    sourceTeamId.value = "";
    targetTeamId.value = "";
    await Promise.all([
      fetchTeams(),
      fetchMergeTargets(),
    ]);
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    mergeBusy.value = false;
  }
}

async function applyAliasSuggestion(suggestion: AliasSuggestion): Promise<void> {
  aliasSuggestionsApplying.value = true;
  try {
    const result = await api.post<{ applied: number; failed: Array<{ message: string }> }>(
      "/admin/teams/alias-suggestions/apply",
      {
        items: [
          {
            id: suggestion.id,
            team_id: suggestion.suggested_team_id,
          },
        ],
      },
    );
    if ((result.failed || []).length > 0) {
      toast.error(result.failed[0]?.message || t("common.genericError"));
      return;
    }
    toast.success(t("admin.teamTower.aliasSuggestions.appliedSingle"));
    await Promise.all([fetchTeams(), fetchAliasSuggestions()]);
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSuggestionsApplying.value = false;
  }
}

async function applyAliasSuggestionToTeamId(suggestionId: string, teamId: string): Promise<void> {
  const suggestion = aliasSuggestions.value.find((item) => item.id === suggestionId);
  if (!suggestion) return;
  aliasSuggestionsApplying.value = true;
  try {
    const result = await api.post<{ applied: number; failed: Array<{ message: string }> }>(
      "/admin/teams/alias-suggestions/apply",
      {
        items: [
          {
            id: suggestion.id,
            team_id: teamId,
          },
        ],
      },
    );
    if ((result.failed || []).length > 0) {
      toast.error(result.failed[0]?.message || t("common.genericError"));
      return;
    }
    toast.success(t("admin.teamTower.aliasSuggestions.appliedSingle"));
    await Promise.all([fetchTeams(), fetchAliasSuggestions()]);
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSuggestionsApplying.value = false;
  }
}

async function applyAllAliasSuggestions(): Promise<void> {
  if (aliasSuggestions.value.length === 0) return;
  aliasSuggestionsApplying.value = true;
  try {
    const result = await api.post<{ applied: number; failed: Array<{ message: string }> }>(
      "/admin/teams/alias-suggestions/apply",
      {
        items: aliasSuggestions.value.map((item) => ({
          id: item.id,
          team_id: item.suggested_team_id,
        })),
      },
    );
    if ((result.failed || []).length > 0) {
      toast.error(
        t("admin.teamTower.aliasSuggestions.bulkPartial", {
          applied: result.applied,
          failed: result.failed.length,
        }),
      );
    } else {
      toast.success(
        t("admin.teamTower.aliasSuggestions.bulkApplied", { count: result.applied }),
      );
    }
    await Promise.all([fetchAliasSuggestions(), fetchTeams()]);
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSuggestionsApplying.value = false;
  }
}

async function rejectAliasSuggestion(suggestion: AliasSuggestion): Promise<void> {
  aliasSuggestionsApplying.value = true;
  try {
    await api.post(`/admin/teams/alias-suggestions/${suggestion.id}/reject`, {});
    toast.success(t("admin.teamTower.aliasSuggestions.rejectedSingle"));
    await fetchAliasSuggestions();
  } catch (error) {
    const message = error instanceof Error ? error.message : t("common.genericError");
    toast.error(message);
  } finally {
    aliasSuggestionsApplying.value = false;
  }
}

function onAliasSuggestionDragStart(event: DragEvent, suggestionId: string): void {
  draggingSuggestionId.value = suggestionId;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-quotico-alias-suggestion", suggestionId);
    // Fallback channel for browsers that drop custom MIME types.
    event.dataTransfer.setData("text/plain", `alias:${suggestionId}`);
  }
}

function onAliasSuggestionDragEnd(): void {
  draggingSuggestionId.value = "";
  sourceZoneDragDepth.value = 0;
  targetZoneDragDepth.value = 0;
}

async function onTeamCardDrop(event: DragEvent, teamId: string): Promise<void> {
  const suggestionId = readDraggedSuggestionId(event);
  if (suggestionId) {
    draggingSuggestionId.value = "";
    await applyAliasSuggestionToTeamId(suggestionId, teamId);
    sourceZoneDragDepth.value = 0;
    targetZoneDragDepth.value = 0;
    return;
  }
  onTargetDrop(event, teamId);
}

function onSourceZoneDragEnter(): void {
  sourceZoneDragDepth.value += 1;
}

function onSourceZoneDragLeave(): void {
  sourceZoneDragDepth.value = Math.max(0, sourceZoneDragDepth.value - 1);
}

function onTargetZoneDragEnter(): void {
  targetZoneDragDepth.value += 1;
}

function onTargetZoneDragLeave(): void {
  targetZoneDragDepth.value = Math.max(0, targetZoneDragDepth.value - 1);
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
  void fetchAliasSuggestions();
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

    <section class="rounded-card border border-primary/30 bg-primary/5 p-4 md:p-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.teamTower.aliasSuggestions.title") }}</h2>
          <p class="text-xs text-text-muted mt-1">{{ t("admin.teamTower.aliasSuggestions.subtitle") }}</p>
        </div>
        <button
          type="button"
          class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
          :disabled="aliasSuggestionsApplying || aliasSuggestions.length === 0"
          @click="applyAllAliasSuggestions"
        >
          {{ t("admin.teamTower.aliasSuggestions.applyAll") }}
        </button>
      </div>
      <div v-if="aliasSuggestionsLoading" class="mt-3 text-sm text-text-muted">
        {{ t("admin.teamTower.loading") }}
      </div>
      <div v-else-if="aliasSuggestions.length === 0" class="mt-3 text-sm text-text-muted">
        {{ t("admin.teamTower.aliasSuggestions.empty") }}
      </div>
      <div v-else class="mt-3 space-y-2">
        <div
          v-for="suggestion in aliasSuggestions"
          :key="suggestion.id"
          class="rounded-card border border-surface-3/60 bg-surface-0 px-3 py-2 flex flex-col md:flex-row md:items-center md:justify-between gap-2"
          draggable="true"
          @dragstart="onAliasSuggestionDragStart($event, suggestion.id)"
          @dragend="onAliasSuggestionDragEnd"
        >
          <div class="text-xs text-text-secondary flex flex-wrap items-center gap-x-2 gap-y-1">
            <span class="font-medium text-text-primary">{{ suggestion.raw_team_name }}</span>
            <span>-> {{ suggestion.suggested_team_name || t("admin.teamTower.aliasSuggestions.noSuggestedTeam") }}</span>
            <span class="ml-1">({{ suggestion.source }})</span>
            <span v-if="suggestion.league_name" class="ml-1">{{ suggestion.league_name }}</span>
            <span class="ml-1">{{ t("admin.teamTower.aliasSuggestions.seenCount", { count: suggestion.seen_count }) }}</span>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="rounded-card border border-surface-3 bg-surface-1 px-3 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
              :disabled="aliasSuggestionsApplying || !suggestion.suggested_team_id"
              @click="applyAliasSuggestion(suggestion)"
            >
              {{ t("admin.teamTower.aliasSuggestions.applyOne") }}
            </button>
            <button
              type="button"
              class="rounded-card border border-danger/40 bg-danger/10 px-3 py-1 text-xs text-danger hover:border-danger disabled:opacity-50"
              :disabled="aliasSuggestionsApplying"
              @click="rejectAliasSuggestion(suggestion)"
            >
              {{ t("admin.teamTower.aliasSuggestions.rejectOne") }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <section class="rounded-card border border-warning/40 bg-warning/5 p-4 md:p-5">
      <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.teamTower.merge.title") }}</h2>
      <p class="text-sm text-text-secondary mt-1">{{ t("admin.teamTower.merge.description") }}</p>

      <div class="grid md:grid-cols-3 gap-3 mt-3">
        <div
          class="rounded-card border bg-surface-0 p-3 transition-colors"
          :class="sourceZoneActive ? 'border-primary/70 bg-primary/10' : 'border-surface-3'"
          @dragenter.prevent="onSourceZoneDragEnter"
          @dragleave.prevent="onSourceZoneDragLeave"
          @dragover.prevent
          @drop.prevent="onSourceDropZoneDrop"
        >
          <p class="text-xs text-text-muted">{{ t("admin.teamTower.merge.source") }}</p>
          <p class="text-sm font-medium text-text-primary mt-1 truncate">
            {{ sourceTeam?.display_name || t("admin.teamTower.merge.unselected") }}
          </p>
        </div>
        <div
          class="rounded-card border bg-surface-0 p-3 transition-colors"
          :class="targetZoneActive ? 'border-danger/70 bg-danger/10' : 'border-surface-3'"
          @dragenter.prevent="onTargetZoneDragEnter"
          @dragleave.prevent="onTargetZoneDragLeave"
          @dragover.prevent
          @drop.prevent="onTargetDropZoneDrop"
        >
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
          <div
            v-for="team in mergeSearchResults"
            :key="team.id"
            class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-left hover:border-primary/60 cursor-grab active:cursor-grabbing"
            draggable="true"
            @dragstart="onSourceDragStart($event, team.id)"
            @dragend="onSourceDragEnd"
          >
            <span class="text-sm font-medium text-text-primary">{{ team.display_name }}</span>
            <span class="ml-2 text-xs text-text-muted">{{ t("admin.teamTower.labels.teamId") }}: {{ team.id }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="space-y-3">
      <div
        v-if="teamsError"
        class="rounded-card border border-danger/40 bg-danger/10 p-3 text-sm text-danger"
      >
        {{ teamsError }}
      </div>
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
          :class="{
            'ring-2 ring-primary/50 border-primary/50': dragOverTargetId === team.id,
          }"
          @dragstart="onSourceDragStart($event, team.id)"
          @dragend="onSourceDragEnd"
          @dragenter.prevent="onTargetDragEnter(team.id)"
          @dragleave.prevent="onTargetDragLeave(team.id)"
          @dragover.prevent
          @drop.prevent="onTeamCardDrop($event, team.id)"
        >
        <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
          <div>
            <h3 class="text-base font-semibold text-text-primary">{{ team.display_name }}</h3>
            <p class="text-xs text-text-muted mt-1">
              {{ t("admin.teamTower.labels.teamId") }}: {{ team.id }}
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              draggable="true"
              class="rounded-card border border-surface-3 bg-surface-0 px-3 py-1 text-xs font-medium text-text-secondary hover:border-primary/50 cursor-grab active:cursor-grabbing"
              @dragstart="onSourceDragStart($event, team.id)"
              @dragend="onSourceDragEnd"
            >
              {{ t("admin.teamTower.actions.dragAsSource") }}
            </button>
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
