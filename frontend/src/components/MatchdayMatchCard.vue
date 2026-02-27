<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import type { MatchdayMatch } from "@/stores/matchday";
import { useMatchdayStore } from "@/stores/matchday";
import MatchHistory from "./MatchHistory.vue";
import QuoticoTipBadge from "./QuoticoTipBadge.vue";
import OddsTimelineToggle from "./OddsTimelineToggle.vue";
import { getCachedTip } from "@/composables/useQuoticoTip";
import { toMatchCardVM, toOddsSummary, toOddsBadge } from "@/composables/useMatchV3Adapter";
import type { MatchV3 } from "@/types/MatchV3";
import { usePersonsStore } from "@/stores/persons";

const props = defineProps<{
  match: MatchdayMatch;
  sportKey?: string;
}>();

const { t, locale } = useI18n();
const localeTag = computed(() => (locale.value === "en" ? "en-US" : "de-DE"));

const quoticoTip = computed(() => getCachedTip(props.match.id));
const persons = usePersonsStore();

const matchday = useMatchdayStore();

const draft = computed(() => matchday.draftPredictions.get(props.match.id));
const isEditable = computed(() => !props.match.is_locked);

// Per-field save indicator
const saveState = computed(() => matchday.getSaveState(props.match.id));

const pointsEarned = computed(() => {
  if (!matchday.predictions) return null;
  const pred = matchday.predictions.predictions.find(
    (p) => p.match_id === props.match.id
  );
  return pred?.points_earned ?? null;
});

const pointsColor = computed(() => {
  switch (pointsEarned.value) {
    case 3:
      return "bg-emerald-500 text-white";
    case 2:
      return "bg-blue-500 text-white";
    case 1:
      return "bg-amber-500 text-white";
    case 0:
      return "bg-surface-3 text-text-muted";
    default:
      return "";
  }
});

const vm = computed(() => toMatchCardVM(props.match as unknown as MatchV3));
const oddsSummary = computed(() => toOddsSummary(props.match as unknown as MatchV3));
const oddsBadge = computed(() => toOddsBadge(props.match as unknown as MatchV3));

const displayOdds = computed(() => {
  const out: Record<string, number> = {};
  oddsSummary.value.forEach((row) => {
    if (row.avg != null) out[row.key] = row.avg;
  });
  return out;
});

const isClosingLine = computed(() => oddsBadge.value === "closing");

// Trend arrows + opening delta from fixed_snapshots
const TREND_THRESHOLD = 0.03; // minimum delta to show an arrow
const oddsTrends = computed(() => {
  const meta = (props.match as unknown as MatchV3).odds_meta;
  const opening = meta?.fixed_snapshots?.opening;
  const summary = meta?.summary_1x2;
  if (!opening || !summary) return null;

  const calc = (current: number | undefined, open: number) => {
    if (current == null) return { arrow: "" as const, delta: 0, pct: 0 };
    const delta = current - open;
    const pct = open > 0 ? (delta / open) * 100 : 0;
    const arrow = delta > TREND_THRESHOLD ? "\u2191" : delta < -TREND_THRESHOLD ? "\u2193" : "";
    return { arrow, delta, pct };
  };

  return {
    "1": calc(summary.home?.avg, opening.h),
    X: calc(summary.draw?.avg, opening.d),
    "2": calc(summary.away?.avg, opening.a),
  };
});

const xgHome = computed(() => {
  const v = (props.match.teams?.home as { xg?: number } | undefined)?.xg;
  return typeof v === "number" ? v : null;
});
const xgAway = computed(() => {
  const v = (props.match.teams?.away as { xg?: number } | undefined)?.xg;
  return typeof v === "number" ? v : null;
});
const showXgJustice = computed(() =>
  vm.value.justice.enabled && xgHome.value != null && xgAway.value != null
);

// Prefer denormalized name, fall back to persons store lookup
const refereeName = computed(() => {
  if (vm.value.refereeName) return vm.value.refereeName;
  const rid = vm.value.refereeId;
  return rid ? persons.getPersonName(rid) : null;
});

onMounted(() => {
  const rid = vm.value.refereeId;
  if (rid && !vm.value.refereeName) {
    void persons.resolveByIds([rid]);
  }
});

const kickoffLabel = computed(() => {
  const d = new Date(props.match.match_date);
  if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) {
    const datePart = d.toLocaleString(localeTag.value, {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
    });
    return `${datePart} · ${t("match.timeTbd")}`;
  }
  return d.toLocaleString(localeTag.value, {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

const countdown = computed(() => {
  if (props.match.is_locked) return null;
  const now = Date.now();
  const kickoff = new Date(props.match.match_date).getTime();
  const diff = kickoff - now;
  if (diff <= 0) return null;

  const hours = Math.floor(diff / 3_600_000);
  const mins = Math.floor((diff % 3_600_000) / 60_000);
  if (hours > 48) return null;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
});

// Track local input values separately so we can detect when both are filled
let localHome: number | null = draft.value?.home ?? null;
let localAway: number | null = draft.value?.away ?? null;

function updateHome(e: Event) {
  const raw = (e.target as HTMLInputElement).value;
  if (raw === "") {
    localHome = null;
  } else {
    localHome = Math.max(0, Math.min(99, parseInt(raw) || 0));
  }
  // Only trigger auto-save when both scores are filled
  matchday.updateLeg(props.match.id, localHome, localAway);
}

function updateAway(e: Event) {
  const raw = (e.target as HTMLInputElement).value;
  if (raw === "") {
    localAway = null;
  } else {
    localAway = Math.max(0, Math.min(99, parseInt(raw) || 0));
  }
  // Only trigger auto-save when both scores are filled
  matchday.updateLeg(props.match.id, localHome, localAway);
}
</script>

<template>
  <div
    class="bg-surface-1 rounded-card p-4 border border-surface-3/50 transition-colors"
    :class="{ 'opacity-60': match.is_locked && !pointsEarned }"
  >
    <!-- Top row: kickoff + countdown/points + save indicator -->
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <div class="flex items-center gap-2">
        <!-- Per-field save indicator -->
        <span
          v-if="saveState === 'syncing'"
          class="w-3.5 h-3.5 text-text-muted animate-spin"
        >
          <svg viewBox="0 0 16 16" fill="none" class="w-full h-full">
            <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="2" opacity="0.25" />
            <path d="M14 8a6 6 0 00-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </span>
        <span
          v-else-if="saveState === 'saved'"
          class="text-emerald-500 transition-opacity"
        >
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 16 16" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 8.5l3.5 3.5L13 4" />
          </svg>
        </span>
        <span
          v-else-if="saveState === 'error'"
          class="text-red-500 text-[10px] font-medium"
        >!</span>

        <span
          v-if="pointsEarned !== null"
          class="text-xs font-bold px-2 py-0.5 rounded-full"
          :class="pointsColor"
        >
          {{ pointsEarned }}P
        </span>
        <span
          v-else-if="countdown"
          class="text-xs text-warning font-medium"
        >
          {{ countdown }}
        </span>
        <span
          v-else-if="match.is_locked"
          class="text-xs text-text-muted"
        >
          {{ t('match.locked') }}
        </span>
      </div>
    </div>

    <!-- Teams + Score/Inputs -->
    <div class="flex items-center gap-3">
      <!-- Home team -->
        <div class="flex-1 flex items-center justify-end gap-2">
          <span
            class="text-sm font-medium truncate"
            :class="[
              vm.justice.home === 'unlucky' ? 'text-rose-400' : '',
              vm.justice.home === 'overperformed' ? 'text-emerald-400' : '',
              vm.justice.home === 'none' ? 'text-text-primary' : '',
            ]"
          >
            {{ match.home_team }}
          </span>
          <img
            v-if="match.teams?.home?.image_path"
            :src="match.teams.home.image_path"
            :alt="match.home_team"
            class="w-6 h-6 shrink-0 object-contain"
          />
        </div>

      <!-- Score inputs or result -->
      <div class="flex items-center gap-1.5 shrink-0">
        <template v-if="match.status === 'final' && (match.result as any)?.home_score !== null && (match.result as any)?.home_score !== undefined">
          <!-- Final score display -->
          <span class="w-10 text-center text-lg font-bold text-text-primary">
            {{ (match.result as any)?.home_score }}
          </span>
          <span class="text-text-muted font-bold">:</span>
          <span class="w-10 text-center text-lg font-bold text-text-primary">
            {{ (match.result as any)?.away_score }}
          </span>
        </template>
        <template v-else-if="isEditable">
          <!-- Editable inputs -->
          <input
            type="number"
            min="0"
            max="99"
            :value="draft?.home ?? ''"
            class="w-12 h-10 text-center text-lg font-bold bg-surface-2 border border-surface-3 rounded-lg text-text-primary focus:border-primary focus:ring-1 focus:ring-primary transition-colors [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            placeholder="-"
            @input="updateHome"
          />
          <span class="text-text-muted font-bold text-lg">:</span>
          <input
            type="number"
            min="0"
            max="99"
            :value="draft?.away ?? ''"
            class="w-12 h-10 text-center text-lg font-bold bg-surface-2 border border-surface-3 rounded-lg text-text-primary focus:border-primary focus:ring-1 focus:ring-primary transition-colors [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            placeholder="-"
            @input="updateAway"
          />
        </template>
        <template v-else>
          <!-- Locked: show user's prediction if any -->
          <span class="w-10 text-center text-lg font-medium text-text-muted">
            {{ draft?.home ?? "-" }}
          </span>
          <span class="text-text-muted font-bold">:</span>
          <span class="w-10 text-center text-lg font-medium text-text-muted">
            {{ draft?.away ?? "-" }}
          </span>
        </template>
      </div>

      <!-- Away team -->
        <div class="flex-1 flex items-center gap-2">
          <img
            v-if="match.teams?.away?.image_path"
            :src="match.teams.away.image_path"
            :alt="match.away_team"
            class="w-6 h-6 shrink-0 object-contain"
          />
          <span
            class="text-sm font-medium truncate"
            :class="[
              vm.justice.away === 'unlucky' ? 'text-rose-400' : '',
              vm.justice.away === 'overperformed' ? 'text-emerald-400' : '',
              vm.justice.away === 'none' ? 'text-text-primary' : '',
            ]"
          >
            {{ match.away_team }}
          </span>
        </div>
    </div>

    <div v-if="showXgJustice" class="mt-2 text-center text-xs text-text-secondary">
      {{ $t("match.xgLine", { home: xgHome?.toFixed(2), away: xgAway?.toFixed(2) }) }}
    </div>
    <div v-if="showXgJustice && (vm.justice.home !== 'none' || vm.justice.away !== 'none')" class="mt-1 flex items-center justify-center gap-2 text-[10px]">
      <span
        v-if="vm.justice.home !== 'none'"
        class="rounded-full px-2 py-0.5"
        :class="vm.justice.home === 'unlucky' ? 'bg-rose-500/15 text-rose-400' : 'bg-emerald-500/15 text-emerald-400'"
        :title="vm.justice.home === 'unlucky' ? $t('match.justice.unluckyTooltip') : $t('match.justice.overperformedTooltip')"
      >
        {{ match.home_team }} · {{ vm.justice.home === "unlucky" ? $t("match.justice.unlucky") : $t("match.justice.overperformed") }}
      </span>
      <span
        v-if="vm.justice.away !== 'none'"
        class="rounded-full px-2 py-0.5"
        :class="vm.justice.away === 'unlucky' ? 'bg-rose-500/15 text-rose-400' : 'bg-emerald-500/15 text-emerald-400'"
        :title="vm.justice.away === 'unlucky' ? $t('match.justice.unluckyTooltip') : $t('match.justice.overperformedTooltip')"
      >
        {{ match.away_team }} · {{ vm.justice.away === "unlucky" ? $t("match.justice.unlucky") : $t("match.justice.overperformed") }}
      </span>
    </div>

    <!-- Odds context (read-only, visually subordinate) -->
    <div
      v-if="Object.keys(displayOdds).length > 0"
      class="mt-2 flex items-center justify-center gap-3 text-text-muted"
      role="group"
      :aria-label="$t('match.oddsLabel')"
    >
      <span
        v-if="isClosingLine"
        class="text-[10px] text-text-muted/60"
        :title="$t('match.closingLineHint')"
      >{{ $t('match.closingLine') }}</span>
      <span
        class="inline-flex items-baseline gap-1"
        :aria-label="t('match.homeWinOdds', { odds: displayOdds['1']?.toFixed(2) ?? '-' })"
        :title="oddsTrends?.['1']?.delta ? `Opening: ${(displayOdds['1']! - oddsTrends['1'].delta).toFixed(2)} (${oddsTrends['1'].pct >= 0 ? '+' : ''}${oddsTrends['1'].pct.toFixed(1)}%)` : t('match.homeWinOdds', { odds: displayOdds['1']?.toFixed(2) ?? '-' })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">1</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['1']?.toFixed(2) ?? '-' }}</span>
        <span
          v-if="oddsTrends?.['1']?.arrow"
          class="text-[9px] leading-none font-bold"
          :class="oddsTrends['1'].arrow === '\u2191' ? 'text-emerald-400' : 'text-rose-400'"
        >{{ oddsTrends['1'].arrow }}</span>
      </span>
      <span
        v-if="displayOdds['X'] !== undefined"
        class="inline-flex items-baseline gap-1"
        :aria-label="t('match.drawOdds', { odds: displayOdds['X'].toFixed(2) })"
        :title="oddsTrends?.X?.delta ? `Opening: ${(displayOdds['X']! - oddsTrends.X.delta).toFixed(2)} (${oddsTrends.X.pct >= 0 ? '+' : ''}${oddsTrends.X.pct.toFixed(1)}%)` : t('match.drawOdds', { odds: displayOdds['X'].toFixed(2) })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">X</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['X'].toFixed(2) }}</span>
        <span
          v-if="oddsTrends?.X?.arrow"
          class="text-[9px] leading-none font-bold"
          :class="oddsTrends.X.arrow === '\u2191' ? 'text-emerald-400' : 'text-rose-400'"
        >{{ oddsTrends.X.arrow }}</span>
      </span>
      <span
        class="inline-flex items-baseline gap-1"
        :aria-label="t('match.awayWinOdds', { odds: displayOdds['2']?.toFixed(2) ?? '-' })"
        :title="oddsTrends?.['2']?.delta ? `Opening: ${(displayOdds['2']! - oddsTrends['2'].delta).toFixed(2)} (${oddsTrends['2'].pct >= 0 ? '+' : ''}${oddsTrends['2'].pct.toFixed(1)}%)` : t('match.awayWinOdds', { odds: displayOdds['2']?.toFixed(2) ?? '-' })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">2</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['2']?.toFixed(2) ?? '-' }}</span>
        <span
          v-if="oddsTrends?.['2']?.arrow"
          class="text-[9px] leading-none font-bold"
          :class="oddsTrends['2'].arrow === '\u2191' ? 'text-emerald-400' : 'text-rose-400'"
        >{{ oddsTrends['2'].arrow }}</span>
      </span>
    </div>

    <div v-if="oddsBadge !== 'none'" class="mt-2 text-center">
      <span class="inline-flex rounded-full border border-surface-3/70 bg-surface-2 px-2 py-0.5 text-[10px] font-medium text-text-secondary">
        {{ oddsBadge === "live" ? $t("match.liveOdds") : $t("match.closingLine") }}
      </span>
    </div>

    <!-- Odds timeline (lazy-loaded on expand) -->
    <OddsTimelineToggle
      :match-id="match.id"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />

    <!-- Bottom: prediction vs result comparison -->
    <div
      v-if="pointsEarned !== null && draft"
      class="mt-2 text-xs text-text-muted text-center"
    >
      {{ t('match.yourPrediction', { home: draft.home, away: draft.away }) }}
    </div>

    <!-- Historical context (embedded from API response) -->
    <MatchHistory
      v-if="match.teams?.home?.sm_id && match.teams?.away?.sm_id"
      :home-team="match.home_team"
      :away-team="match.away_team"
      :home-short-code="match.teams.home.short_code"
      :away-short-code="match.teams.away.short_code"
      :home-s-m-id="match.teams.home.sm_id"
      :away-s-m-id="match.teams.away.sm_id"
      :context="(match.h2h_context as any) ?? undefined"
    />

    <!-- QuoticoTip value bet recommendation -->
    <QuoticoTipBadge
      v-if="quoticoTip"
      :tip="quoticoTip"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />

    <div v-if="refereeName" class="mt-2 text-center text-[11px] text-text-muted">
      {{ $t("match.referee") }}: {{ refereeName }}
    </div>
  </div>
</template>
