<!--
frontend/src/views/admin/AdminTeamTowerView.vue

Purpose:
    Team Tower v3 admin view for alias-only operations on Sportmonks canonical
    teams (`teams_v3`).
-->
<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

interface TeamAlias {
  name: string;
  normalized: string;
  source: string;
  sport_key: string | null;
  alias_key: string;
  is_default: boolean;
}

interface TeamItem {
  id: number;
  name: string;
  short_code: string | null;
  image_path: string | null;
  locked_fields: string[];
  aliases: TeamAlias[];
}

interface TeamListResponse {
  total: number;
  offset: number;
  limit: number;
  items: TeamItem[];
}

interface SuggestionItem {
  id: string;
  source: string;
  sport_key: string | null;
  raw_team_name: string;
  confidence_score: number;
  suggested_team_id: number | null;
  suggested_team_name: string;
}

interface SuggestionListResponse {
  total: number;
  items: SuggestionItem[];
}

interface DeleteImpact {
  usage_30d: number;
  last_seen_at: string | null;
  orphan_risk: boolean;
  affected_sources: string[];
}

const api = useApi();
const toast = useToast();
const { t } = useI18n();

const teams = ref<TeamItem[]>([]);
const suggestions = ref<SuggestionItem[]>([]);
const loading = ref(false);
const search = ref("");
const aliasDraftByTeam = ref<Record<number, { name: string; source: string; sport_key: string }>>({});
const deleteModalOpen = ref(false);
const deleteTarget = ref<{ teamId: number; alias: TeamAlias } | null>(null);
const deleteImpact = ref<DeleteImpact | null>(null);
const deleteBlocked = ref<string | null>(null);

function ensureDraft(teamId: number): void {
  if (!aliasDraftByTeam.value[teamId]) {
    aliasDraftByTeam.value[teamId] = { name: "", source: "manual", sport_key: "" };
  }
}

function confidenceLabel(value: number): string {
  if (value >= 0.9) return t("admin.teamTower.confidence.high");
  if (value >= 0.75) return t("admin.teamTower.confidence.medium");
  return t("admin.teamTower.confidence.low");
}

async function fetchTeams(): Promise<void> {
  loading.value = true;
  try {
    const params: Record<string, string> = { limit: "300", offset: "0" };
    if (search.value.trim()) params.search = search.value.trim();
    const result = await api.get<TeamListResponse>("/admin/teams-v3", params);
    teams.value = result.items;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    loading.value = false;
  }
}

async function fetchSuggestions(): Promise<void> {
  try {
    const result = await api.get<SuggestionListResponse>("/admin/teams-v3/alias-suggestions", {
      status: "pending",
      limit: "200",
    });
    suggestions.value = result.items;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function addAlias(team: TeamItem): Promise<void> {
  ensureDraft(team.id);
  const draft = aliasDraftByTeam.value[team.id];
  if (!draft.name.trim()) {
    toast.error(t("admin.teamTower.aliasNameRequired"));
    return;
  }
  try {
    await api.post(`/admin/teams-v3/${team.id}/aliases`, {
      name: draft.name.trim(),
      source: draft.source,
      sport_key: draft.sport_key.trim() || null,
    });
    draft.name = "";
    draft.sport_key = "";
    toast.success(t("admin.teamTower.aliasAdded"));
    await fetchTeams();
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function openDeleteAlias(teamId: number, alias: TeamAlias): Promise<void> {
  deleteModalOpen.value = true;
  deleteTarget.value = { teamId, alias };
  deleteBlocked.value = null;
  deleteImpact.value = null;
  try {
    const impact = await api.post<DeleteImpact>(`/admin/teams-v3/${teamId}/aliases/impact`, {
      name: alias.name,
      source: alias.source,
      sport_key: alias.sport_key,
    });
    deleteImpact.value = impact;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function confirmDeleteAlias(): Promise<void> {
  if (!deleteTarget.value) return;
  const { teamId, alias } = deleteTarget.value;
  try {
    const result = await api.del<{
      removed: boolean;
      blocked?: { code: string; message: string };
    }>(`/admin/teams-v3/${teamId}/aliases`, {
      name: alias.name,
      source: alias.source,
      sport_key: alias.sport_key,
    });
    if (result.blocked?.code === "canonical_alias_protected") {
      deleteBlocked.value = t("admin.teamTower.alias.canonicalProtected");
      return;
    }
    if (result.removed) {
      toast.success(t("admin.teamTower.aliasRemoved"));
      deleteModalOpen.value = false;
      deleteTarget.value = null;
      deleteImpact.value = null;
      await fetchTeams();
    }
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function applySuggestion(item: SuggestionItem): Promise<void> {
  try {
    await api.post("/admin/teams-v3/alias-suggestions/apply", {
      items: [{ id: item.id, team_id: item.suggested_team_id }],
    });
    toast.success(t("admin.teamTower.aliasSuggestions.appliedSingle"));
    await Promise.all([fetchTeams(), fetchSuggestions()]);
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

async function rejectSuggestion(item: SuggestionItem): Promise<void> {
  try {
    await api.post(`/admin/teams-v3/alias-suggestions/${item.id}/reject`, {});
    toast.success(t("admin.teamTower.aliasSuggestions.rejectedSingle"));
    await fetchSuggestions();
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null;
watch(search, () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    void fetchTeams();
  }, 250);
});

onMounted(async () => {
  await Promise.all([fetchTeams(), fetchSuggestions()]);
});
</script>

<template>
  <div class="max-w-6xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.teamTower.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.teamTower.subtitleV3") }}</p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
      <input
        v-model="search"
        type="search"
        class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
        :placeholder="t('admin.teamTower.filters.searchPlaceholder')"
      />
    </section>

    <section class="rounded-card border border-primary/30 bg-primary/5 p-4">
      <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.teamTower.aliasSuggestions.title") }}</h2>
      <div v-if="suggestions.length === 0" class="mt-2 text-sm text-text-muted">{{ t("admin.teamTower.aliasSuggestions.empty") }}</div>
      <div v-else class="mt-2 space-y-2">
        <div v-for="s in suggestions" :key="s.id" class="rounded-card border border-surface-3/60 bg-surface-0 px-3 py-2 flex items-center justify-between gap-2">
          <div class="text-xs text-text-secondary">
            <span class="font-medium text-text-primary">{{ s.raw_team_name }}</span>
            <span class="mx-1">-></span>
            <span>{{ s.suggested_team_name || t("admin.teamTower.aliasSuggestions.noSuggestedTeam") }}</span>
            <span class="ml-2 px-2 py-0.5 rounded-full bg-surface-2">{{ confidenceLabel(s.confidence_score) }}</span>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="rounded-card border border-surface-3 bg-surface-1 px-3 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
              :disabled="!s.suggested_team_id"
              @click="applySuggestion(s)"
            >
              {{ t("admin.teamTower.aliasSuggestions.applyOne") }}
            </button>
            <button
              type="button"
              class="rounded-card border border-danger/40 bg-danger/10 px-3 py-1 text-xs text-danger hover:border-danger"
              @click="rejectSuggestion(s)"
            >
              {{ t("admin.teamTower.aliasSuggestions.rejectOne") }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <section v-if="loading" class="text-sm text-text-muted">{{ t("admin.teamTower.loading") }}</section>
    <section v-else-if="teams.length === 0" class="text-sm text-text-muted">{{ t("admin.teamTower.noResults") }}</section>
    <section v-else class="space-y-3">
      <article
        v-for="team in teams"
        :key="team.id"
        class="rounded-card border border-surface-3/60 bg-surface-1 p-4"
      >
        <div class="grid md:grid-cols-3 gap-3">
          <div class="space-y-1">
            <p class="text-xs text-text-muted">{{ t("admin.teamTower.labels.teamId") }}</p>
            <p class="text-sm font-medium text-text-primary">{{ team.id }}</p>
          </div>
          <div class="space-y-1">
            <p class="text-xs text-text-muted">{{ t("admin.teamTower.canonicalName") }}</p>
            <p class="text-sm font-medium text-text-primary">{{ team.name }}</p>
          </div>
          <div class="space-y-1">
            <p class="text-xs text-text-muted">{{ t("admin.teamTower.locked") }}</p>
            <p class="text-xs text-warning">{{ team.locked_fields.join(", ") }}</p>
          </div>
        </div>

        <div class="mt-3 flex flex-wrap gap-2">
          <div
            v-for="alias in team.aliases"
            :key="alias.alias_key"
            class="inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs bg-surface-0 border-surface-3"
          >
            <span class="font-medium">{{ alias.name }}</span>
            <span class="opacity-80">{{ alias.source }}</span>
            <span v-if="alias.is_default" class="text-warning">🔒</span>
            <button
              v-else
              type="button"
              class="rounded-full px-1 leading-none hover:bg-black/10"
              :aria-label="t('admin.teamTower.actions.removeAlias')"
              @click="openDeleteAlias(team.id, alias)"
            >
              x
            </button>
          </div>
          <span v-if="team.aliases.length === 0" class="text-xs text-text-muted">{{ t("admin.teamTower.aliases.empty") }}</span>
        </div>

        <div class="mt-3 grid md:grid-cols-4 gap-2">
          <input
            :value="(aliasDraftByTeam[team.id] || { name: '' }).name"
            type="text"
            class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
            :placeholder="t('admin.teamTower.aliases.namePlaceholder')"
            @focus="ensureDraft(team.id)"
            @input="ensureDraft(team.id); aliasDraftByTeam[team.id].name = ($event.target as HTMLInputElement).value"
          />
          <select
            class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
            :value="(aliasDraftByTeam[team.id] || { source: 'manual' }).source"
            @focus="ensureDraft(team.id)"
            @change="ensureDraft(team.id); aliasDraftByTeam[team.id].source = ($event.target as HTMLSelectElement).value"
          >
            <option value="manual">manual</option>
            <option value="provider_x">provider_x</option>
            <option value="crawler">crawler</option>
            <option value="provider_unknown">provider_unknown</option>
          </select>
          <input
            :value="(aliasDraftByTeam[team.id] || { sport_key: '' }).sport_key"
            type="text"
            class="rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-primary focus:border-primary focus:outline-none"
            :placeholder="t('admin.teamTower.aliases.sportPlaceholder')"
            @focus="ensureDraft(team.id)"
            @input="ensureDraft(team.id); aliasDraftByTeam[team.id].sport_key = ($event.target as HTMLInputElement).value"
          />
          <button
            type="button"
            class="rounded-card bg-primary px-3 py-2 text-sm font-semibold text-white"
            @click="addAlias(team)"
          >
            {{ t("admin.teamTower.actions.addAlias") }}
          </button>
        </div>
      </article>
    </section>

    <div v-if="deleteModalOpen && deleteTarget" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div class="w-full max-w-lg rounded-card border border-surface-3 bg-surface-0 p-5">
        <h3 class="text-lg font-semibold text-text-primary">{{ t("admin.teamTower.alias.deleteTitle") }}</h3>
        <p v-if="deleteImpact" class="text-sm text-text-secondary mt-2">
          {{ t("admin.teamTower.alias.deleteImpact", { usage: deleteImpact.usage_30d }) }}
        </p>
        <p v-if="deleteBlocked" class="text-sm text-danger mt-2">{{ deleteBlocked }}</p>
        <div class="mt-5 flex items-center justify-end gap-2">
          <button type="button" class="rounded-card border border-surface-3 px-3 py-2 text-sm text-text-secondary" @click="deleteModalOpen = false">
            {{ t("common.cancel") }}
          </button>
          <button type="button" class="rounded-card bg-danger px-3 py-2 text-sm font-semibold text-white" @click="confirmDeleteAlias">
            {{ t("admin.teamTower.alias.confirmDelete") }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
