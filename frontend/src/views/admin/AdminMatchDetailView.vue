<!--
frontend/src/views/admin/AdminMatchDetailView.vue

Purpose:
    Admin match detail page powered by matches_v3. Shows rich match data
    including team logos, scores, xG/justice, odds snapshots, events timeline,
    and embedded odds timeline. All IDs are native integers.

Dependencies:
    - @/composables/useApi
    - @/composables/useMatchV3Adapter
    - @/types/MatchV3
    - vue-i18n
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useApi } from "@/composables/useApi";
import type {
  OddsMetaV3,
  MatchTeamV3,
  MatchEventV3,
  PeriodScoresV3,
  FixedSnapshot,
} from "@/types/MatchV3";
import { toOddsSummary, computeJustice, computeJusticeDiff } from "@/composables/useMatchV3Adapter";
import type { MatchV3 } from "@/types/MatchV3";

interface AdminMatchDetailV3 {
  id: number;
  league_id: number;
  league_name: string;
  season_id: number;
  round_id: number | null;
  referee_id: number | null;
  referee_name: string | null;
  referee: {
    id: number;
    name: string;
    strictness_index: number;
    strictness_band: "loose" | "normal" | "strict" | "extreme_strict";
    avg_yellow: number;
    avg_red: number;
    penalty_pct: number;
    season_avg?: {
      yellow: number;
      red: number;
      penalty_pct: number;
      strictness_index: number;
    } | null;
    career_avg?: {
      yellow: number;
      red: number;
      penalty_pct: number;
      strictness_index: number;
    } | null;
    trend?: "stricter" | "looser" | "flat";
  } | null;
  start_at: string;
  status: string;
  finish_type: string | null;
  has_advanced_stats: boolean;
  teams: {
    home: MatchTeamV3;
    away: MatchTeamV3;
  };
  scores: PeriodScoresV3;
  events: MatchEventV3[];
  // FIXME: ODDS_V3_BREAK — reads odds_meta and odds_timeline which are no longer produced by connector
  odds_meta: OddsMetaV3;
  odds_timeline: Array<Record<string, unknown>>;
  manual_check_required: boolean;
  manual_check_reasons: string[];
}

const api = useApi();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();

const loading = ref(true);
const error = ref("");
const match = ref<AdminMatchDetailV3 | null>(null);
const timelineOpen = ref(false);
const timelineLimit = ref(20);

const matchId = computed(() => Number(route.params.matchId));

const sortedEvents = computed(() => {
  if (!match.value?.events?.length) return [];
  return [...match.value.events].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
});

// FIXME: ODDS_V3_BREAK — reads odds_timeline which is no longer produced by connector
const timelineItems = computed(() => {
  const items = match.value?.odds_timeline ?? [];
  if (timelineLimit.value === 0) return items;
  return items.slice(0, timelineLimit.value);
});

const timelineTotal = computed(() => match.value?.odds_timeline?.length ?? 0);

const oddsSummary = computed(() => {
  if (!match.value) return [];
  return toOddsSummary(match.value as unknown as MatchV3);
});

const justice = computed(() => {
  if (!match.value) return { home: "none" as const, away: "none" as const, enabled: false };
  return computeJustice(match.value as unknown as MatchV3);
});

const justiceDiff = computed(() => {
  if (!match.value) return { home: null, away: null };
  return computeJusticeDiff(match.value as unknown as MatchV3);
});

// FIXME: ODDS_V3_BREAK — reads fixed_snapshots from odds_meta which is no longer produced by connector
const fixedSnapshots = computed(() => {
  const fs = match.value?.odds_meta?.fixed_snapshots;
  if (!fs) return [];
  const keys: Array<{ key: string; label: string }> = [
    { key: "opening", label: "Opening" },
    { key: "alpha_24h", label: "24h" },
    { key: "beta_6h", label: "6h" },
    { key: "omega_1h", label: "1h" },
    { key: "closing", label: "Closing" },
  ];
  return keys
    .filter((k) => (fs as Record<string, FixedSnapshot | undefined>)[k.key])
    .map((k) => {
      const snap = (fs as Record<string, FixedSnapshot>)[k.key];
      return { label: k.label, h: snap.h, d: snap.d, a: snap.a, ts_utc: snap.ts_utc };
    });
});

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusClass(status: string): string {
  const s = status.toUpperCase();
  if (s === "SCHEDULED") return "bg-primary-muted/20 text-primary";
  if (s === "LIVE") return "bg-danger-muted/20 text-danger";
  if (s === "FINISHED") return "bg-surface-3 text-text-muted";
  if (s === "POSTPONED" || s === "WALKOVER" || s === "CANCELLED")
    return "bg-warning/20 text-warning";
  return "bg-surface-3 text-text-muted";
}

function eventIcon(type: string): string {
  if (type === "goal") return "\u26BD";
  if (type === "card") return "\uD83D\uDFE8";
  if (type === "var") return "\uD83D\uDCFA";
  if (type === "missed_penalty") return "\u274C";
  return "\u2022";
}

function eventColor(type: string): string {
  if (type === "goal") return "text-green-400";
  if (type === "card") return "text-yellow-400";
  if (type === "var") return "text-blue-400";
  if (type === "missed_penalty") return "text-text-muted";
  return "text-text-muted";
}

function trendIcon(trend: string | undefined): string {
  if (trend === "stricter") return "▲";
  if (trend === "looser") return "▼";
  return "●";
}

function trendClass(trend: string | undefined): string {
  if (trend === "stricter") return "text-warning";
  if (trend === "looser") return "text-primary";
  return "text-text-muted";
}

function trendLabel(trend: string | undefined): string {
  if (trend === "stricter") return t("admin.matches.detail.trendStricter");
  if (trend === "looser") return t("admin.matches.detail.trendLooser");
  return t("admin.matches.detail.trendFlat");
}

async function fetchMatch(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    match.value = await api.get<AdminMatchDetailV3>(`/admin/matches/${matchId.value}`);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("common.genericError");
  } finally {
    loading.value = false;
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

    <!-- Loading skeleton -->
    <div v-if="loading" class="rounded-card border border-surface-3/60 bg-surface-1 p-4 space-y-3">
      <div v-for="n in 6" :key="n" class="h-8 rounded bg-surface-2 animate-pulse" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="rounded-card border border-danger/40 bg-danger-muted/10 p-4">
      <p class="text-sm text-danger">{{ error }}</p>
    </div>

    <template v-else-if="match">
      <!-- Card 1: Match Header -->
      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <div class="flex items-center justify-center gap-4 md:gap-8">
          <div class="flex flex-col items-center gap-1">
            <img
              v-if="match.teams.home?.image_path"
              :src="match.teams.home.image_path"
              :alt="match.teams.home.name ?? ''"
              class="w-12 h-12 object-contain"
            />
            <span class="text-sm font-semibold text-text-primary">{{ match.teams.home?.name }}</span>
            <span v-if="match.teams.home?.short_code" class="text-xs text-text-muted">{{ match.teams.home.short_code }}</span>
          </div>

          <div class="text-center">
            <div v-if="match.scores?.full_time?.home != null" class="text-3xl font-bold text-text-primary">
              {{ match.scores.full_time.home }} - {{ match.scores.full_time.away }}
            </div>
            <div v-else class="text-xl text-text-muted">vs</div>
            <div v-if="match.scores?.half_time?.home != null" class="text-xs text-text-muted mt-1">
              HT: {{ match.scores.half_time.home }} - {{ match.scores.half_time.away }}
            </div>
          </div>

          <div class="flex flex-col items-center gap-1">
            <img
              v-if="match.teams.away?.image_path"
              :src="match.teams.away.image_path"
              :alt="match.teams.away.name ?? ''"
              class="w-12 h-12 object-contain"
            />
            <span class="text-sm font-semibold text-text-primary">{{ match.teams.away?.name }}</span>
            <span v-if="match.teams.away?.short_code" class="text-xs text-text-muted">{{ match.teams.away.short_code }}</span>
          </div>
        </div>

        <div class="mt-3 flex flex-wrap justify-center gap-2 text-xs">
          <span class="rounded-full px-2 py-1" :class="statusClass(match.status)">{{ match.status }}</span>
          <span v-if="match.finish_type" class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">{{ match.finish_type }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">{{ match.league_name }}</span>
          <span v-if="match.round_id != null" class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.round") }}: {{ match.round_id }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.season") }}: {{ match.season_id }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.kickoff") }}: {{ formatDate(match.start_at) }}
          </span>
          <button
            v-if="match.referee_id"
            type="button"
            class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary hover:text-text-primary"
            @click="router.push({ name: 'admin-referee-detail', params: { refereeId: match.referee_id } })"
          >
            {{ t("admin.matches.detail.referee") }}: {{ match.referee_name || ("#" + String(match.referee_id)) }}
          </button>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-muted">ID: {{ match.id }}</span>
        </div>

        <div v-if="match.referee" class="mt-3 flex flex-wrap justify-center gap-2 text-xs">
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.refereeAvgYellow") }}: {{ match.referee.avg_yellow.toFixed(2) }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.refereeAvgRed") }}: {{ match.referee.avg_red.toFixed(2) }}
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1 text-text-secondary">
            {{ t("admin.matches.detail.refereePenaltyPct") }}: {{ match.referee.penalty_pct.toFixed(1) }}%
          </span>
          <span class="rounded-full bg-surface-2 px-2 py-1" :class="trendClass(match.referee.trend)">
            {{ t("admin.matches.detail.refereeTrend") }}: {{ trendIcon(match.referee.trend) }} {{ trendLabel(match.referee.trend) }}
          </span>
        </div>
      </div>

      <!-- Card 2: Manual Check (conditional) -->
      <div
        v-if="match.manual_check_required || (match.manual_check_reasons && match.manual_check_reasons.length > 0)"
        class="rounded-card border border-warning/40 bg-warning/5 p-4"
      >
        <h2 class="text-sm font-semibold text-warning">{{ t("admin.matches.detail.manualCheck") }}</h2>
        <div v-if="match.manual_check_reasons.length" class="mt-2 flex flex-wrap gap-1">
          <span
            v-for="reason in match.manual_check_reasons"
            :key="reason"
            class="rounded-full bg-warning/20 px-2 py-0.5 text-xs text-warning font-medium"
          >{{ reason }}</span>
        </div>
      </div>

      <!-- Card 3: xG & Justice (conditional) -->
      <div v-if="match.has_advanced_stats" class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.matches.detail.xg") }}</h2>
        <div class="mt-3 grid grid-cols-3 gap-4 text-center">
          <div>
            <div class="text-2xl font-bold text-text-primary">{{ match.teams.home?.xg?.toFixed(2) ?? "-" }}</div>
            <div class="text-xs text-text-muted">{{ match.teams.home?.name }}</div>
          </div>
          <div class="flex items-center justify-center">
            <span class="text-text-muted text-sm">xG</span>
          </div>
          <div>
            <div class="text-2xl font-bold text-text-primary">{{ match.teams.away?.xg?.toFixed(2) ?? "-" }}</div>
            <div class="text-xs text-text-muted">{{ match.teams.away?.name }}</div>
          </div>
        </div>
        <div v-if="justice.enabled" class="mt-4 space-y-2">
          <h3 class="text-sm font-semibold text-text-primary">{{ t("admin.matches.detail.justice") }}</h3>
          <div class="grid grid-cols-2 gap-3 text-xs">
            <div class="rounded-card bg-surface-0 border border-surface-3/60 p-2">
              <p class="text-text-muted">{{ match.teams.home?.name }}</p>
              <p class="font-medium" :class="justice.home === 'unlucky' ? 'text-danger' : justice.home === 'overperformed' ? 'text-green-400' : 'text-text-primary'">
                {{ justice.home === 'none' ? 'Fair' : justice.home === 'unlucky' ? 'Unlucky' : 'Overperformed' }}
              </p>
              <p v-if="justiceDiff.home != null" class="text-text-muted">diff: {{ (justiceDiff.home * 100).toFixed(1) }}%</p>
            </div>
            <div class="rounded-card bg-surface-0 border border-surface-3/60 p-2">
              <p class="text-text-muted">{{ match.teams.away?.name }}</p>
              <p class="font-medium" :class="justice.away === 'unlucky' ? 'text-danger' : justice.away === 'overperformed' ? 'text-green-400' : 'text-text-primary'">
                {{ justice.away === 'none' ? 'Fair' : justice.away === 'unlucky' ? 'Unlucky' : 'Overperformed' }}
              </p>
              <p v-if="justiceDiff.away != null" class="text-text-muted">diff: {{ (justiceDiff.away * 100).toFixed(1) }}%</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Card 4: Odds Summary -->
      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.matches.detail.oddsSummary") }}</h2>

        <div v-if="!oddsSummary.some((o) => o.avg != null)" class="text-sm text-text-muted mt-2">
          {{ t("admin.matches.detail.oddsEmpty") }}
        </div>

        <template v-else>
          <!-- 1X2 Summary Grid -->
          <div class="grid grid-cols-3 gap-3 mt-3">
            <div
              v-for="btn in oddsSummary"
              :key="btn.key"
              class="rounded-card border border-surface-3/60 bg-surface-0 p-3 text-center"
            >
              <h3 class="text-sm font-semibold text-text-primary">{{ btn.key }}</h3>
              <div class="text-xl font-bold text-text-primary mt-1">{{ btn.avg?.toFixed(2) ?? "-" }}</div>
              <div class="text-xs text-text-muted mt-1 space-y-0.5">
                <p>{{ t("admin.matches.detail.min") }}: {{ btn.min?.toFixed(2) ?? "-" }}</p>
                <p>{{ t("admin.matches.detail.max") }}: {{ btn.max?.toFixed(2) ?? "-" }}</p>
                <p>{{ t("admin.matches.detail.count") }}: {{ btn.count ?? 0 }}</p>
              </div>
            </div>
          </div>

          <!-- Fixed Snapshots -->
          <div v-if="fixedSnapshots.length" class="mt-4">
            <h3 class="text-sm font-semibold text-text-primary">{{ t("admin.matches.detail.fixedSnapshots") }}</h3>
            <div class="overflow-x-auto mt-2">
              <table class="min-w-full text-xs">
                <thead class="bg-surface-2/60 border-b border-surface-3/60">
                  <tr>
                    <th class="px-2 py-1 text-left text-text-secondary">Snapshot</th>
                    <th class="px-2 py-1 text-right text-text-secondary">1</th>
                    <th class="px-2 py-1 text-right text-text-secondary">X</th>
                    <th class="px-2 py-1 text-right text-text-secondary">2</th>
                    <th class="px-2 py-1 text-left text-text-secondary">{{ t("admin.matches.detail.timestamp") }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="snap in fixedSnapshots" :key="snap.label" class="border-b border-surface-3/40 last:border-b-0">
                    <td class="px-2 py-1 font-medium text-text-primary">{{ snap.label }}</td>
                    <td class="px-2 py-1 text-right">{{ snap.h?.toFixed(2) ?? "-" }}</td>
                    <td class="px-2 py-1 text-right">{{ snap.d?.toFixed(2) ?? "-" }}</td>
                    <td class="px-2 py-1 text-right">{{ snap.a?.toFixed(2) ?? "-" }}</td>
                    <td class="px-2 py-1 text-text-muted">{{ formatDate(snap.ts_utc) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Market Entropy -->
          <!-- FIXME: ODDS_V3_BREAK — displays market_entropy which is no longer produced by connector -->
          <div v-if="match.odds_meta?.market_entropy" class="mt-4 flex gap-4 text-xs">
            <div class="rounded-card bg-surface-0 border border-surface-3/60 px-3 py-2">
              <span class="text-text-muted">{{ t("admin.matches.detail.spreadPct") }}:</span>
              <span class="ml-1 font-medium text-text-primary">{{ (match.odds_meta.market_entropy.current_spread_pct * 100).toFixed(1) }}%</span>
            </div>
            <div class="rounded-card bg-surface-0 border border-surface-3/60 px-3 py-2">
              <span class="text-text-muted">{{ t("admin.matches.detail.driftVelocity") }}:</span>
              <span class="ml-1 font-medium text-text-primary">{{ match.odds_meta.market_entropy.drift_velocity_3h?.toFixed(4) ?? "-" }}</span>
            </div>
          </div>

          <!-- Source & Updated -->
          <div class="mt-3 flex gap-4 text-xs text-text-muted">
            <span v-if="match.odds_meta?.source">{{ t("admin.matches.detail.oddsSource") }}: {{ match.odds_meta.source }}</span>
            <span v-if="match.odds_meta?.updated_at">{{ t("admin.matches.detail.oddsUpdatedAt") }}: {{ formatDate(match.odds_meta.updated_at) }}</span>
          </div>
        </template>
      </div>

      <!-- Card 5: Match Events (conditional) -->
      <div v-if="sortedEvents.length" class="rounded-card border border-surface-3/60 bg-surface-1 p-4">
        <h2 class="text-lg font-semibold text-text-primary">{{ t("admin.matches.detail.events") }}</h2>
        <div class="mt-3 space-y-2">
          <div
            v-for="(evt, idx) in sortedEvents"
            :key="idx"
            class="flex items-center gap-3 text-sm"
          >
            <span class="w-10 text-right text-text-muted text-xs">
              {{ evt.minute != null ? `${evt.minute}'` : "" }}{{ evt.extra_minute ? `+${evt.extra_minute}` : "" }}
            </span>
            <span :class="eventColor(evt.type)" class="text-base">{{ eventIcon(evt.type) }}</span>
            <span class="text-text-primary">{{ evt.player_name || "-" }}</span>
            <span v-if="evt.detail" class="text-text-muted text-xs">({{ evt.detail }})</span>
          </div>
        </div>
      </div>

      <!-- Card 6: Odds Timeline (collapsible) -->
      <div class="rounded-card border border-surface-3/60 bg-surface-1 p-4 space-y-3">
        <div class="flex items-center justify-between gap-2">
          <h2 class="text-lg font-semibold text-text-primary">
            {{ t("admin.matches.detail.timeline") }}
            <span class="text-xs text-text-muted font-normal ml-1">({{ timelineTotal }})</span>
          </h2>
          <div class="flex items-center gap-2">
            <select
              v-model.number="timelineLimit"
              class="rounded-card border border-surface-3 bg-surface-0 px-2 py-1 text-xs text-text-primary"
            >
              <option :value="20">20</option>
              <option :value="50">50</option>
              <option :value="100">100</option>
              <option :value="0">{{ t("admin.matches.detail.timelineAll") }}</option>
            </select>
            <button
              type="button"
              class="rounded-card border border-surface-3 bg-surface-0 px-2.5 py-1 text-xs text-text-secondary hover:border-primary/60"
              @click="timelineOpen = !timelineOpen"
            >
              {{ timelineOpen ? t("admin.matches.detail.timelineHide") : t("admin.matches.detail.timelineShow") }}
            </button>
          </div>
        </div>

        <div v-if="timelineOpen">
          <p v-if="timelineTotal === 0" class="text-sm text-text-muted">
            {{ t("admin.matches.detail.timelineEmpty") }}
          </p>
          <div v-else class="overflow-x-auto max-h-96 border border-surface-3/60 rounded-card">
            <table class="min-w-full text-xs">
              <thead class="bg-surface-2/60 border-b border-surface-3/60 sticky top-0">
                <tr>
                  <th class="px-2 py-1 text-left">{{ t("admin.matches.detail.timestamp") }}</th>
                  <th class="px-2 py-1 text-right">1</th>
                  <th class="px-2 py-1 text-right">X</th>
                  <th class="px-2 py-1 text-right">2</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(item, idx) in timelineItems"
                  :key="idx"
                  class="border-b border-surface-3/40 last:border-b-0"
                >
                  <td class="px-2 py-1">{{ formatDate(item.ts as string) }}</td>
                  <td class="px-2 py-1 text-right">{{ (item.h2h_odds as Record<string, number>)?.home?.toFixed(2) ?? "-" }}</td>
                  <td class="px-2 py-1 text-right">{{ (item.h2h_odds as Record<string, number>)?.draw?.toFixed(2) ?? "-" }}</td>
                  <td class="px-2 py-1 text-right">{{ (item.h2h_odds as Record<string, number>)?.away?.toFixed(2) ?? "-" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
