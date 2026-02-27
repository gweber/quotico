<!--
frontend/src/views/admin/AdminOddsMonitor.vue

Purpose:
    Admin Odds Quality Monitor for Sportmonks-native matches_v3. Lists anomaly
    matches, exposes quick remediation actions, and shows embedded match detail.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import { useToast } from "@/composables/useToast";

interface AnomalyDetail {
  code: string;
  severity: number;
  detail: Record<string, unknown>;
}

interface ProblemMatch {
  match_id: number;
  league_id: number;
  league_name: string;
  season_id: number;
  round_id: number | null;
  start_at: string | null;
  status: string;
  home_team: string;
  away_team: string;
  referee: {
    id: number;
    name: string;
    strictness_index: number;
    strictness_band: "loose" | "normal" | "strict" | "extreme_strict";
    avg_yellow: number;
    avg_red: number;
    penalty_pct: number;
  } | null;
  snapshot_count: number;
  overround: number | null;
  manual_check_required: boolean;
  manual_check_reasons: string[];
  model_excluded: boolean;
  quality_score: number;
  severity_score: number;
  anomalies: AnomalyDetail[];
}

interface AnomalyResponse {
  items: ProblemMatch[];
}

// FIXME: ODDS_V3_BREAK — reads odds_timeline which is no longer produced by connector
interface MatchDetailResponse {
  id: number;
  status: string;
  teams: {
    home: { name: string; score: number | null; xg: number | null };
    away: { name: string; score: number | null; xg: number | null };
  };
  odds_timeline: Array<{ timestamp: string; home: number; draw: number; away: number; source: string }>;
  manual_check_reasons: string[];
}

const api = useApi();
const toast = useToast();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const rows = ref<ProblemMatch[]>([]);
const selectedMatchId = ref<number | null>(null);
const detailLoading = ref(false);
const detail = ref<MatchDetailResponse | null>(null);

const limit = ref(200);
const kickoffWindowHours = ref(8);
const minSnapshots = ref(3);
const entropyThreshold = ref(0.12);
const overroundFloor = ref(0.99);

const busyByMatch = ref<Record<number, boolean>>({});

const selectedRow = computed(() => rows.value.find((x) => x.match_id === selectedMatchId.value) ?? null);

function severityClass(score: number): string {
  if (score >= 80) return "bg-danger/20 text-danger";
  if (score >= 45) return "bg-warning/20 text-warning";
  return "bg-primary/20 text-primary";
}

function strictnessBadgeClass(band: string): string {
  if (band === "extreme_strict") return "bg-danger/20 text-danger";
  if (band === "strict") return "bg-warning/20 text-warning";
  if (band === "loose") return "bg-primary/20 text-primary";
  return "bg-surface-3 text-text-secondary";
}

function goToReferee(refereeId: number): void {
  void router.push({ name: "admin-referee-detail", params: { refereeId } });
}

function fmtDate(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function anomalyLabel(code: string): string {
  const key = `admin.oddsMonitor.anomaly.${code}`;
  const translated = t(key);
  return translated === key ? code : translated;
}

async function loadAnomalies(): Promise<void> {
  loading.value = true;
  try {
    const result = await api.get<AnomalyResponse>("/admin/odds/anomalies", {
      limit: String(limit.value),
      kickoff_window_hours: String(kickoffWindowHours.value),
      min_snapshots: String(minSnapshots.value),
      entropy_threshold: String(entropyThreshold.value),
      overround_floor: String(overroundFloor.value),
    });
    rows.value = result.items;
    if (rows.value.length > 0 && selectedMatchId.value === null) {
      selectedMatchId.value = rows.value[0].match_id;
      await loadDetail(rows.value[0].match_id);
    }
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    loading.value = false;
  }
}

async function loadDetail(matchId: number): Promise<void> {
  detailLoading.value = true;
  selectedMatchId.value = matchId;
  try {
    detail.value = await api.get<MatchDetailResponse>(`/admin/matches/${matchId}`);
  } catch (error) {
    detail.value = null;
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    detailLoading.value = false;
  }
}

async function runAction(matchId: number, action: "fix" | "valid" | "exclude"): Promise<void> {
  busyByMatch.value = { ...busyByMatch.value, [matchId]: true };
  try {
    if (action === "fix") {
      await api.post(`/admin/odds/fix/${matchId}`);
      toast.success(t("admin.oddsMonitor.actions.fixDone"));
    } else if (action === "valid") {
      await api.post(`/admin/odds/mark-valid/${matchId}`);
      toast.success(t("admin.oddsMonitor.actions.validDone"));
    } else {
      await api.post(`/admin/odds/exclude/${matchId}`, {});
      toast.success(t("admin.oddsMonitor.actions.excludeDone"));
    }
    await Promise.all([loadAnomalies(), loadDetail(matchId)]);
  } catch (error) {
    toast.error(error instanceof Error ? error.message : t("common.genericError"));
  } finally {
    busyByMatch.value = { ...busyByMatch.value, [matchId]: false };
  }
}

onMounted(() => {
  void loadAnomalies();
});
</script>

<template>
  <div class="max-w-7xl mx-auto p-4 md:p-6 space-y-4">
    <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 md:p-5">
      <h1 class="text-xl font-bold text-text-primary">{{ t("admin.oddsMonitor.title") }}</h1>
      <p class="text-sm text-text-muted mt-1">{{ t("admin.oddsMonitor.subtitle") }}</p>
    </div>

    <section class="rounded-card border border-surface-3/60 bg-surface-1 p-3">
      <div class="grid grid-cols-2 md:grid-cols-5 gap-2">
        <div class="flex flex-col">
          <label for="oqm-limit" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.oddsMonitor.filters.limitLabel") }}</label>
          <input id="oqm-limit" v-model.number="limit" type="number" min="1" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm" :placeholder="t('admin.oddsMonitor.filters.limit')" />
        </div>
        <div class="flex flex-col">
          <label for="oqm-kickoff-window" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.oddsMonitor.filters.kickoffLabel") }}</label>
          <input id="oqm-kickoff-window" v-model.number="kickoffWindowHours" type="number" min="1" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm" :placeholder="t('admin.oddsMonitor.filters.kickoff')" />
        </div>
        <div class="flex flex-col">
          <label for="oqm-min-snapshots" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.oddsMonitor.filters.snapshotsLabel") }}</label>
          <input id="oqm-min-snapshots" v-model.number="minSnapshots" type="number" min="1" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm" :placeholder="t('admin.oddsMonitor.filters.snapshots')" />
        </div>
        <div class="flex flex-col">
          <label for="oqm-entropy-threshold" class="text-xs font-medium text-text-secondary mb-1 block">{{ t("admin.oddsMonitor.filters.entropyLabel") }}</label>
          <input id="oqm-entropy-threshold" v-model.number="entropyThreshold" type="number" step="0.01" min="0" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-2 text-sm" :placeholder="t('admin.oddsMonitor.filters.entropy')" />
        </div>
        <div class="flex items-end">
          <button type="button" class="w-full rounded-card border border-surface-3 bg-surface-0 px-3 py-2 text-sm text-text-secondary hover:border-primary/60" @click="loadAnomalies">
            {{ t("admin.oddsMonitor.filters.apply") }}
          </button>
        </div>
      </div>
    </section>

    <section v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 text-sm text-text-muted">
      {{ t("admin.oddsMonitor.loading") }}
    </section>

    <section v-else class="grid lg:grid-cols-2 gap-4">
      <div class="rounded-card border border-surface-3/60 bg-surface-1 overflow-hidden">
        <div class="px-4 py-3 border-b border-surface-3/60 text-sm font-semibold text-text-primary">
          {{ t("admin.oddsMonitor.problemMatches", { count: rows.length }) }}
        </div>
        <div v-if="rows.length === 0" class="p-4 text-sm text-text-muted">{{ t("admin.oddsMonitor.empty") }}</div>
        <div v-else class="max-h-[36rem] overflow-y-auto">
          <button
            v-for="row in rows"
            :key="row.match_id"
            type="button"
            class="w-full px-4 py-3 border-b border-surface-3/40 hover:bg-surface-2/30 text-left"
            :class="selectedMatchId === row.match_id ? 'bg-surface-2/40' : ''"
            @click="loadDetail(row.match_id)"
          >
            <div class="flex items-center justify-between gap-2">
              <p class="text-sm font-medium text-text-primary truncate">{{ row.home_team }} {{ t("admin.oddsMonitor.versus") }} {{ row.away_team }}</p>
              <span class="rounded-full px-2 py-0.5 text-xs font-medium" :class="severityClass(row.severity_score)">
                {{ row.severity_score }}
              </span>
            </div>
            <p class="text-xs text-text-muted mt-1">{{ row.league_name }} · {{ fmtDate(row.start_at) }}</p>
            <div v-if="row.referee" class="mt-1 flex items-center gap-2 text-xs">
              <button
                type="button"
                class="text-text-secondary hover:text-text-primary hover:underline"
                @click.stop="goToReferee(row.referee.id)"
              >
                {{ t("admin.oddsMonitor.referee") }}: {{ row.referee.name || ("#" + String(row.referee.id)) }}
              </button>
              <span class="inline-flex rounded-full px-2 py-0.5 font-medium" :class="strictnessBadgeClass(row.referee.strictness_band)">
                {{ t("admin.referees.badges.strictness") }}: {{ row.referee.strictness_index.toFixed(1) }}
              </span>
            </div>
            <div class="mt-1 flex flex-wrap gap-1">
              <span v-for="a in row.anomalies" :key="`${row.match_id}-${a.code}`" class="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-text-secondary">
                {{ anomalyLabel(a.code) }}
              </span>
            </div>
            <div class="mt-2 flex flex-wrap gap-2">
              <button type="button" class="rounded-card border border-primary/40 bg-primary/10 px-2 py-1 text-xs text-primary" :disabled="busyByMatch[row.match_id]" @click.stop="runAction(row.match_id, 'fix')">
                {{ t("admin.oddsMonitor.actions.forceSync") }}
              </button>
              <button type="button" class="rounded-card border border-surface-3 bg-surface-0 px-2 py-1 text-xs text-text-secondary" :disabled="busyByMatch[row.match_id]" @click.stop="runAction(row.match_id, 'valid')">
                {{ t("admin.oddsMonitor.actions.markValid") }}
              </button>
              <button type="button" class="rounded-card border border-danger/40 bg-danger/10 px-2 py-1 text-xs text-danger" :disabled="busyByMatch[row.match_id]" @click.stop="runAction(row.match_id, 'exclude')">
                {{ t("admin.oddsMonitor.actions.exclude") }}
              </button>
            </div>
          </button>
        </div>
      </div>

      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.oddsMonitor.detailTitle") }}</h2>
        <p v-if="!selectedRow" class="text-sm text-text-muted mt-2">{{ t("admin.oddsMonitor.selectPrompt") }}</p>
        <template v-else>
          <div class="mt-2 text-sm text-text-secondary">
            <p>{{ selectedRow.home_team }} {{ t("admin.oddsMonitor.versus") }} {{ selectedRow.away_team }}</p>
            <p>{{ selectedRow.league_name }} · {{ fmtDate(selectedRow.start_at) }}</p>
            <p>{{ t("admin.oddsMonitor.qualityScore") }}: <span class="font-semibold text-text-primary">{{ selectedRow.quality_score }}</span></p>
          </div>

          <div class="mt-3">
            <p class="text-sm font-semibold text-text-primary">{{ t("admin.oddsMonitor.manualReasons") }}</p>
            <div v-if="selectedRow.manual_check_reasons.length === 0" class="text-xs text-text-muted mt-1">-</div>
            <div v-else class="mt-1 flex flex-wrap gap-1">
              <span v-for="reason in selectedRow.manual_check_reasons" :key="reason" class="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-text-secondary">{{ reason }}</span>
            </div>
          </div>

          <div class="mt-3">
            <p class="text-sm font-semibold text-text-primary">{{ t("admin.oddsMonitor.timeline") }}</p>
            <div v-if="detailLoading" class="text-xs text-text-muted mt-1">{{ t("admin.oddsMonitor.loading") }}</div>
            <div v-else-if="!detail || detail.odds_timeline.length === 0" class="text-xs text-text-muted mt-1">-</div>
            <div v-else class="mt-1 max-h-56 overflow-y-auto border border-surface-3/60 rounded-card">
              <table class="w-full text-xs">
                <thead class="bg-surface-2/60 border-b border-surface-3/60">
                  <tr>
                    <th class="px-2 py-1 text-left">{{ t("admin.oddsMonitor.table.timestamp") }}</th>
                    <th class="px-2 py-1 text-left">1</th>
                    <th class="px-2 py-1 text-left">X</th>
                    <th class="px-2 py-1 text-left">2</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(entry, idx) in detail.odds_timeline" :key="`${entry.timestamp}-${idx}`" class="border-b border-surface-3/40 last:border-b-0">
                    <td class="px-2 py-1">{{ fmtDate(entry.timestamp) }}</td>
                    <td class="px-2 py-1">{{ entry.home }}</td>
                    <td class="px-2 py-1">{{ entry.draw }}</td>
                    <td class="px-2 py-1">{{ entry.away }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>
