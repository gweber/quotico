<!--
frontend/src/views/admin/AdminRefereeDetailView.vue

Purpose:
    Admin Referee Tower detail page. Displays strictness DNA and recent matches
    with xG justice context for one Sportmonks referee ID.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

type StrictnessBand = "loose" | "normal" | "extreme_strict";

interface RefereeDnaResponse {
  referee_id: number;
  referee_name: string;
  sample_size: number;
  league_sample_size: number;
  strictness_index: number;
  strictness_band: StrictnessBand;
  referee_avg: {
    yellow: number;
    red: number;
    penalty_pct: number;
    discipline_points: number;
  };
  league_avg: {
    yellow: number;
    red: number;
    penalty_pct: number;
    discipline_points: number;
  };
}

interface RefereeMatchRow {
  match_id: number;
  league_id: number;
  league_name: string;
  season_id: number;
  round_id: number | null;
  start_at: string | null;
  status: string;
  home_team: string;
  away_team: string;
  home_xg: number | null;
  away_xg: number | null;
  home_score: number | null;
  away_score: number | null;
  yellow_cards: number;
  red_cards: number;
  penalty_occurred: boolean;
  discipline_points: number;
  justice: { home: string; away: string };
}

interface RefereeMatchesResponse {
  referee_id: number;
  referee_name: string;
  items: RefereeMatchRow[];
  count: number;
}

const api = useApi();
const toast = useToast();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const dna = ref<RefereeDnaResponse | null>(null);
const matches = ref<RefereeMatchRow[]>([]);

const refereeId = computed(() => Number(route.params.refereeId));

function bandLabel(band: StrictnessBand): string {
  if (band === "extreme_strict") return t("admin.referees.bands.extremeStrict");
  if (band === "loose") return t("admin.referees.bands.loose");
  return t("admin.referees.bands.normal");
}

function strictnessBadgeClass(band: StrictnessBand): string {
  if (band === "extreme_strict") return "bg-danger/20 text-danger";
  if (band === "loose") return "bg-primary/20 text-primary";
  return "bg-surface-2 text-text-secondary";
}

function gaugePercent(index: number): number {
  return Math.max(0, Math.min(100, index));
}

function formatDate(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function justiceLabel(value: string): string {
  if (value === "unlucky") return t("admin.referees.justice.unlucky");
  if (value === "overperformed") return t("admin.referees.justice.overperformed");
  return t("admin.referees.justice.none");
}

async function loadData(): Promise<void> {
  loading.value = true;
  try {
    const [dnaResult, matchesResult] = await Promise.all([
      api.get<RefereeDnaResponse>(`/admin/referees/${refereeId.value}/dna`),
      api.get<RefereeMatchesResponse>(`/admin/referees/${refereeId.value}/matches`),
    ]);
    dna.value = dnaResult;
    matches.value = matchesResult.items;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    loading.value = false;
  }
}

function openMatch(matchId: number): void {
  void router.push({ name: "admin-match-detail", params: { matchId } });
}

onMounted(() => {
  void loadData();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <button type="button" class="text-sm text-text-secondary hover:text-text-primary" @click="router.push({ name: 'admin-referees' })">
      {{ t("common.back") }}
    </button>

    <section v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-sm text-text-muted">
      {{ t("admin.referees.loading") }}
    </section>

    <template v-else-if="dna">
      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
        <h1 class="text-xl font-bold text-text-primary">
          {{ dna.referee_name || ("#" + String(dna.referee_id)) }}
        </h1>
        <p class="text-sm text-text-muted mt-1">#{{ dna.referee_id }}</p>
        <div class="mt-3 flex flex-wrap items-center gap-2">
          <span class="rounded-full px-2 py-1 text-xs font-medium" :class="strictnessBadgeClass(dna.strictness_band)">
            {{ bandLabel(dna.strictness_band) }}
          </span>
          <span class="text-sm text-text-secondary">{{ t("admin.referees.detail.strictnessIndex") }}: {{ dna.strictness_index.toFixed(2) }}</span>
          <span class="text-sm text-text-muted">{{ t("admin.referees.detail.sampleSize", { referee: dna.sample_size, league: dna.league_sample_size }) }}</span>
        </div>
        <div class="mt-3 w-full md:w-80">
          <div class="h-3 rounded bg-surface-3 overflow-hidden">
            <div class="h-3 bg-primary" :style="{ width: `${gaugePercent(dna.strictness_index)}%` }" />
          </div>
        </div>
      </div>

      <section class="grid md:grid-cols-2 gap-4">
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
          <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.referees.detail.refereeAvg") }}</h2>
          <div class="mt-2 text-sm text-text-secondary space-y-1">
            <p>{{ t("admin.referees.table.avgYellow") }}: {{ dna.referee_avg.yellow.toFixed(2) }}</p>
            <p>{{ t("admin.referees.table.avgRed") }}: {{ dna.referee_avg.red.toFixed(2) }}</p>
            <p>{{ t("admin.referees.table.penaltyPct") }}: {{ dna.referee_avg.penalty_pct.toFixed(1) }}%</p>
            <p>{{ t("admin.referees.detail.disciplinePoints") }}: {{ dna.referee_avg.discipline_points.toFixed(3) }}</p>
          </div>
        </div>
        <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
          <h2 class="text-sm font-semibold text-text-primary">{{ t("admin.referees.detail.leagueAvg") }}</h2>
          <div class="mt-2 text-sm text-text-secondary space-y-1">
            <p>{{ t("admin.referees.table.avgYellow") }}: {{ dna.league_avg.yellow.toFixed(2) }}</p>
            <p>{{ t("admin.referees.table.avgRed") }}: {{ dna.league_avg.red.toFixed(2) }}</p>
            <p>{{ t("admin.referees.table.penaltyPct") }}: {{ dna.league_avg.penalty_pct.toFixed(1) }}%</p>
            <p>{{ t("admin.referees.detail.disciplinePoints") }}: {{ dna.league_avg.discipline_points.toFixed(3) }}</p>
          </div>
        </div>
      </section>

      <section class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
        <div class="px-4 py-3 border-b border-surface-3/60 text-sm font-semibold text-text-primary">
          {{ t("admin.referees.detail.matchesTitle", { count: matches.length }) }}
        </div>
        <div v-if="matches.length === 0" class="p-4 text-sm text-text-muted">{{ t("admin.referees.empty") }}</div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="bg-surface-2/50 border-b border-surface-3/60 text-text-secondary">
              <tr>
                <th class="px-3 py-2 text-left">{{ t("admin.referees.detail.match") }}</th>
                <th class="px-3 py-2 text-right">{{ t("admin.referees.table.avgYellow") }}</th>
                <th class="px-3 py-2 text-right">{{ t("admin.referees.table.avgRed") }}</th>
                <th class="px-3 py-2 text-right">{{ t("admin.referees.table.penaltyPct") }}</th>
                <th class="px-3 py-2 text-left">{{ t("admin.referees.detail.justice") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in matches" :key="row.match_id" class="border-b border-surface-3/40 hover:bg-surface-2/20 cursor-pointer" @click="openMatch(row.match_id)">
                <td class="px-3 py-2">
                  <div class="font-medium text-text-primary">
                    {{ row.home_team }} {{ t("admin.referees.versus") }} {{ row.away_team }}
                  </div>
                  <div class="text-xs text-text-muted">
                    {{ row.league_name }} · {{ formatDate(row.start_at) }}
                  </div>
                </td>
                <td class="px-3 py-2 text-right tabular-nums">{{ row.yellow_cards }}</td>
                <td class="px-3 py-2 text-right tabular-nums">{{ row.red_cards }}</td>
                <td class="px-3 py-2 text-right tabular-nums">{{ row.penalty_occurred ? "100%" : "0%" }}</td>
                <td class="px-3 py-2 text-xs text-text-secondary">
                  <span>{{ justiceLabel(row.justice.home) }}</span>
                  <span class="mx-1">/</span>
                  <span>{{ justiceLabel(row.justice.away) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
