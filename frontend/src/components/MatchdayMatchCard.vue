<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { RouterLink } from "vue-router";
import type { MatchdayMatch } from "@/stores/matchday";
import { useMatchdayStore } from "@/stores/matchday";
import MatchHistory from "./MatchHistory.vue";
import QuoticoTipBadge from "./QuoticoTipBadge.vue";
import { getCachedTip } from "@/composables/useQuoticoTip";
import { teamSlug } from "@/composables/useTeam";

const props = defineProps<{
  match: MatchdayMatch;
  sportKey?: string;
}>();

const { t, locale } = useI18n();
const localeTag = computed(() => (locale.value === "en" ? "en-US" : "de-DE"));

const quoticoTip = computed(() => getCachedTip(props.match.id));

const matchday = useMatchdayStore();

const draft = computed(() => matchday.draftPredictions.get(props.match.id));
const isAdminUnlocked = computed(() => matchday.adminUnlockedSet.has(props.match.id));
const isEditable = computed(() => !props.match.is_locked || isAdminUnlocked.value);

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

// For final matches, prefer closing line (frozen at kickoff) over last-polled odds
const displayOdds = computed(() => {
  const odds = props.match.odds as any;
  if (!odds) return null;
  if (props.match.status === "final" && odds.closing_line?.h2h) {
    return odds.closing_line.h2h as Record<string, number>;
  }
  return odds.h2h as Record<string, number> | null;
});

const isClosingLine = computed(
  () => props.match.status === "final" && !!(props.match.odds as any)?.closing_line?.h2h
);

const kickoffLabel = computed(() => {
  const d = new Date(props.match.match_date);
  return d.toLocaleString(localeTag.value, {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

const countdown = computed(() => {
  if (props.match.is_locked && !isAdminUnlocked.value) return null;
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

function updateHome(e: Event) {
  const val = parseInt((e.target as HTMLInputElement).value) || 0;
  matchday.setDraftPrediction(
    props.match.id,
    Math.max(0, Math.min(99, val)),
    draft.value?.away ?? 0
  );
}

function updateAway(e: Event) {
  const val = parseInt((e.target as HTMLInputElement).value) || 0;
  matchday.setDraftPrediction(
    props.match.id,
    draft.value?.home ?? 0,
    Math.max(0, Math.min(99, val))
  );
}
</script>

<template>
  <div
    class="bg-surface-1 rounded-card p-4 border border-surface-3/50 transition-colors"
    :class="{ 'opacity-60': match.is_locked && !isAdminUnlocked && !pointsEarned }"
  >
    <!-- Top row: kickoff + countdown/points -->
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
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
        v-else-if="isAdminUnlocked"
        class="text-xs text-amber-500 font-medium"
      >
        {{ t('match.adminUnlocked') }}
      </span>
      <span
        v-else-if="match.is_locked"
        class="text-xs text-text-muted"
      >
        {{ t('match.locked') }}
      </span>
    </div>

    <!-- Teams + Score/Inputs -->
    <div class="flex items-center gap-3">
      <!-- Home team -->
      <div class="flex-1 text-right">
        <RouterLink
          :to="{ name: 'team-detail', params: { teamSlug: teamSlug(match.home_team) }, query: sportKey ? { sport: sportKey } : {} }"
          class="text-sm font-medium text-text-primary truncate block hover:text-primary transition-colors"
        >
          {{ match.home_team }}
        </RouterLink>
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
      <div class="flex-1">
        <RouterLink
          :to="{ name: 'team-detail', params: { teamSlug: teamSlug(match.away_team) }, query: sportKey ? { sport: sportKey } : {} }"
          class="text-sm font-medium text-text-primary truncate block hover:text-primary transition-colors"
        >
          {{ match.away_team }}
        </RouterLink>
      </div>
    </div>

    <!-- Odds context (read-only, visually subordinate) -->
    <div
      v-if="displayOdds && Object.keys(displayOdds).length > 0"
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
        :title="t('match.homeWinOdds', { odds: displayOdds['1']?.toFixed(2) ?? '-' })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">1</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['1']?.toFixed(2) ?? '-' }}</span>
      </span>
      <span
        v-if="displayOdds['X'] !== undefined"
        class="inline-flex items-baseline gap-1"
        :aria-label="t('match.drawOdds', { odds: displayOdds['X'].toFixed(2) })"
        :title="t('match.drawOdds', { odds: displayOdds['X'].toFixed(2) })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">X</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['X'].toFixed(2) }}</span>
      </span>
      <span
        class="inline-flex items-baseline gap-1"
        :aria-label="t('match.awayWinOdds', { odds: displayOdds['2']?.toFixed(2) ?? '-' })"
        :title="t('match.awayWinOdds', { odds: displayOdds['2']?.toFixed(2) ?? '-' })"
      >
        <span class="text-[10px] leading-none text-text-secondary" aria-hidden="true">2</span>
        <span class="text-xs font-mono tabular-nums">{{ displayOdds['2']?.toFixed(2) ?? '-' }}</span>
      </span>
    </div>

    <!-- Bottom: prediction vs result comparison -->
    <div
      v-if="pointsEarned !== null && draft"
      class="mt-2 text-xs text-text-muted text-center"
    >
      {{ t('match.yourPrediction', { home: draft.home, away: draft.away }) }}
    </div>

    <!-- Historical context (embedded from API response) -->
    <MatchHistory
      v-if="sportKey"
      :home-team="match.home_team"
      :away-team="match.away_team"
      :sport-key="sportKey"
      :context="(match.h2h_context as any) ?? undefined"
    />

    <!-- QuoticoTip value bet recommendation -->
    <QuoticoTipBadge
      v-if="quoticoTip"
      :tip="quoticoTip"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />
  </div>
</template>
