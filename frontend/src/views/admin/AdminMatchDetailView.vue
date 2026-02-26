<!--
frontend/src/views/admin/AdminMatchDetailView.vue

Purpose:
    Admin match detail page showing core match data, odds_meta market cards,
    and lazy-loaded raw odds events from the admin debug endpoint.

Dependencies:
    - @/composables/useApi
    - vue-i18n
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { sportLabel } from "@/types/sports";

interface MatchDetail {
  id: string;
  league_id: string | null;
  league_name: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  match_date: string | null;
  status: string;
  score: Record<string, unknown>;
  result: { home_score: number | null; away_score: number | null; outcome: string | null };
  matchday: number | null;
  stats: Record<string, unknown>;
  external_ids: Record<string, string>;
  odds_meta: {
    updated_at?: string | null;
    version?: number;
    markets?: Record<string, Record<string, unknown>>;
  };
  has_odds: boolean;
}

interface AdminOddsDebugResponse {
  match: { id: string; odds_meta: Record<string, unknown> };
  diagnostics: Record<string, Record<string, number>>;
  events: Array<{
    provider: string;
    market: string;
    selection_key: string;
    price: number;
    line: number | null;
    snapshot_at: string;
    ingested_at: string;
  }>;
  event_count: number;
}

const api = useApi();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const error = ref("");
const match = ref<MatchDetail | null>(null);
const rawOpen = ref(false);
const rawLoading = ref(false);
const rawError = ref("");
const rawLimit = ref(20);
const rawData = ref<AdminOddsDebugResponse | null>(null);

const matchId = computed(() => String(route.params.matchId || ""));
const markets = computed(() => (match.value?.odds_meta?.markets || {}) as Record<string, Record<string, unknown>>);

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatJson(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

async function fetchMatch(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    match.value = await api.get<MatchDetail>(`/admin/matches/${matchId.value}`);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
  }
}

async function loadRawOdds(): Promise<void> {
  rawOpen.value = true;
  rawLoading.value = true;
  rawError.value = "";
  try {
    rawData.value = await api.get<AdminOddsDebugResponse>(`/admin/odds/${matchId.value}`, { limit: String(rawLimit.value) });
  } catch (err) {
    rawError.value = err instanceof Error ? err.message : t("common.genericError");
    rawData.value = null;
  } finally {
    rawLoading.value = false;
  }
}

onMounted(() => {
  void fetchMatch();
});
</script>

<template>
  <div class="max-w-6xl mx-auto p-4 md:p-6 space-y-4">
    <button
      type="button"
      class="text-sm text-text-secondary hover:text-text-primary"
      @click="router.push({ name: 'admin-matches' })"
    >
      {{ t("common.back") }}
    </button>

    <div v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 space-y-3">
      <div v-for="n in 6" :key="n" class="h-8 rounded bg-surface-2 animate-pulse" />
    </div>

    <div v-else-if="error" class="rounded-card border border-danger/40 bg-danger-muted/10 p-4">
      <p class="text-sm text-danger">{{ error }}</p>
    </div>

    <template v-else-if="match">
      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h1 class="text-xl font-bold text-text-primary">{{ match.home_team }} vs {{ match.away_team }}</h1>
        <p class="text-sm text-text-muted mt-1">
          {{ match.league_name || sportLabel(match.sport_key) }} · {{ formatDate(match.match_date) }}
        </p>
        <div class="mt-2 flex flex-wrap gap-2 text-xs">
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">{{ t("admin.matches.table.status") }}: {{ match.status }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.matchday") }}: {{ match.matchday ?? t("common.not_available") }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.table.score") }}:
            {{ match.result.home_score != null ? `${match.result.home_score}-${match.result.away_score}` : "-" }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">{{ t("admin.matches.detail.odds_version") }}: {{ match.odds_meta.version ?? 0 }}</span>
        </div>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.matches.detail.odds_meta") }}</h2>
        <p v-if="!Object.keys(markets).length" class="text-sm text-text-muted mt-2">
          {{ t("admin.matches.detail.odds_meta_empty") }}
        </p>
        <div v-else class="grid md:grid-cols-3 gap-3 mt-3">
          <div
            v-for="marketKey in ['h2h', 'totals', 'spreads']"
            :key="marketKey"
            class="rounded-card border border-surface-3/60 bg-surface-0 p-3"
          >
            <h3 class="text-sm font-semibold text-text-primary">{{ t(`admin.matches.detail.market.${marketKey}`) }}</h3>
            <p class="text-xs text-text-muted mt-2">{{ t("admin.matches.detail.current") }}: {{ formatJson(markets[marketKey]?.current) }}</p>
            <p class="text-xs text-text-muted">{{ t("admin.matches.detail.opening") }}: {{ formatJson(markets[marketKey]?.opening) }}</p>
            <p class="text-xs text-text-muted">{{ t("admin.matches.detail.closing") }}: {{ formatJson(markets[marketKey]?.closing) }}</p>
            <p class="text-xs text-text-muted">{{ t("admin.matches.detail.provider_count") }}: {{ formatJson(markets[marketKey]?.provider_count) }}</p>
            <p class="text-xs text-text-muted">{{ t("admin.matches.detail.reference_line") }}: {{ formatJson(markets[marketKey]?.reference_line) }}</p>
          </div>
        </div>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 space-y-3">
        <div class="flex items-center justify-between gap-2">
          <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.matches.detail.raw_title") }}</h2>
          <div class="flex items-center gap-2">
            <select
              v-model.number="rawLimit"
              class="rounded-card border border-surface-3 bg-surface-0 px-2 py-1 text-xs text-text-primary"
            >
              <option :value="20">20</option>
              <option :value="50">50</option>
              <option :value="100">100</option>
            </select>
            <button
              type="button"
              class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60 disabled:opacity-50"
              :disabled="rawLoading"
              @click="loadRawOdds"
            >
              {{ rawLoading ? t("admin.matches.detail.raw_loading") : t("admin.matches.detail.raw_load") }}
            </button>
          </div>
        </div>

        <div v-if="rawOpen">
          <p v-if="rawError" class="text-sm text-danger">{{ rawError }}</p>
          <p v-else-if="rawLoading" class="text-sm text-text-muted">{{ t("admin.matches.detail.raw_loading") }}</p>
          <p v-else-if="rawData && rawData.event_count === 0" class="text-sm text-text-muted">
            {{ t("admin.matches.detail.raw_empty") }}
          </p>
          <div v-else-if="rawData" class="overflow-x-auto max-h-96 border border-surface-3/60 rounded-card">
            <table class="min-w-full text-xs">
              <thead class="bg-surface-2/60 border-b border-surface-3/60 sticky top-0">
                <tr>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.snapshot_at") }}</th>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.provider") }}</th>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.market") }}</th>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.selection") }}</th>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.price") }}</th>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.raw.line") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(event, idx) in rawData.events" :key="`${event.provider}-${event.market}-${event.selection_key}-${idx}`" class="border-b border-surface-3/40 last:border-b-0">
                  <td class="px-2 py-1">{{ formatDate(event.snapshot_at) }}</td>
                  <td class="px-2 py-1">{{ event.provider }}</td>
                  <td class="px-2 py-1">{{ event.market }}</td>
                  <td class="px-2 py-1">{{ event.selection_key }}</td>
                  <td class="px-2 py-1">{{ event.price }}</td>
                  <td class="px-2 py-1">{{ event.line ?? "-" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
