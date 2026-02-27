<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useI18n } from "vue-i18n";
import type { Match } from "@/stores/matches";
import { useMatchesStore } from "@/stores/matches";
import { useAuthStore } from "@/stores/auth";
import { useBetSlipStore } from "@/stores/betslip";
import OddsButton from "./OddsButton.vue";
import MatchHistory from "./MatchHistory.vue";
import OddsTimelineToggle from "./OddsTimelineToggle.vue";
import QuoticoTipBadge from "./QuoticoTipBadge.vue";
import { useQuoticoTip } from "@/composables/useQuoticoTip";
import { getCachedUserBet } from "@/composables/useUserBets";
import { sportFlag, sportLabel } from "@/types/sports";
import { toOddsSummary, toOddsBadge, oddsValueBySelection, computeJusticeDiff } from "@/composables/useMatchV3Adapter";
import type { MatchV3, OddsButtonKey } from "@/types/MatchV3";

const { t, locale } = useI18n();
const localeTag = computed(() => (locale.value === "en" ? "en-US" : "de-DE"));

const props = defineProps<{
  match: Match;
}>();

const { data: quoticoTip, fetch: fetchTip } = useQuoticoTip();

const auth = useAuthStore();
const betslip = useBetSlipStore();
const matchesStore = useMatchesStore();

// User's placed tip for this match (if any)
const userTip = computed(() =>
  auth.isLoggedIn ? getCachedUserBet(props.match.id) : undefined
);

// --- Countdown timer ---
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

const commenceMs = computed(() => new Date(props.match.match_date).getTime());
const isUpcoming = computed(() => props.match.status === "scheduled");
const isLive = computed(() => props.match.status === "live");
const isCompleted = computed(() => props.match.status === "final");
const isPast = computed(() => isCompleted.value || props.match.status === "cancelled");
const isExpired = computed(() => now.value >= commenceMs.value);

// Live score from polling
const liveScore = computed(() => matchesStore.liveScores.get(props.match.id));
const hasScore = computed(
  () =>
    liveScore.value != null ||
    props.match.result.home_score != null ||
    props.match.result.away_score != null
);
const homeScore = computed(
  () => liveScore.value?.home_score ?? props.match.result.home_score ?? 0
);
const awayScore = computed(
  () => liveScore.value?.away_score ?? props.match.result.away_score ?? 0
);
const liveMinute = computed(() => liveScore.value?.minute);

const countdown = computed(() => {
  if (!isUpcoming.value) return null;
  const diff = commenceMs.value - now.value;
  if (diff <= 0) return t('dashboard.startsSoon');

  const days = Math.floor(diff / 86400000);
  const hours = Math.floor((diff % 86400000) / 3600000);
  const minutes = Math.floor((diff % 3600000) / 60000);
  const seconds = Math.floor((diff % 60000) / 1000);

  if (days > 0) return `${days}T ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
});

onMounted(() => {
  fetchTip(props.match.id);
  if (isUpcoming.value) {
    timer = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  }
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});

// --- Spread & Totals (only truthy when data exists) ---
const spread = computed(() => {
  const s = props.match.odds.spreads;
  return s && s.home_line != null ? s : null;
});
const totals = computed(() => {
  const t = props.match.odds.totals;
  return t && t.line != null ? t : null;
});

// --- Odds logic ---
const oddsSummary = computed(() => toOddsSummary(props.match as unknown as MatchV3));
const oddsBadge = computed(() => toOddsBadge(props.match as unknown as MatchV3));
const oddsEntries = computed(() => [
  { key: "1" as OddsButtonKey, label: props.match.home_team },
  { key: "X" as OddsButtonKey, label: t("match.draw") },
  { key: "2" as OddsButtonKey, label: props.match.away_team },
]);
const oddsByKey = computed<Record<OddsButtonKey, { avg: number | null; min: number | null; max: number | null; count: number | null }>>(() => {
  const out: Record<OddsButtonKey, { avg: number | null; min: number | null; max: number | null; count: number | null }> = {
    "1": { avg: null, min: null, max: null, count: null },
    "X": { avg: null, min: null, max: null, count: null },
    "2": { avg: null, min: null, max: null, count: null },
  };
  oddsSummary.value.forEach((row) => {
    out[row.key] = { avg: row.avg, min: row.min, max: row.max, count: row.count };
  });
  return out;
});

// Live bet projection: is the user's pending bet currently winning?
const liveBetStatus = computed<"winning" | "losing" | null>(() => {
  if (!userTip.value || userTip.value.status !== "pending") return null;
  if (!isLive.value) return null;
  const hs = liveScore.value?.home_score ?? props.match.result.home_score;
  const as_ = liveScore.value?.away_score ?? props.match.result.away_score;
  if (hs == null || as_ == null) return null;

  const currentOutcome = hs > as_ ? "1" : as_ > hs ? "2" : "X";
  return userTip.value.selection.value === currentOutcome ? "winning" : "losing";
});

const liveProjectedPoints = computed(() => {
  if (liveBetStatus.value !== "winning" || !userTip.value) return null;
  return userTip.value.locked_odds * 10;
});

const hasOdds = computed(() => oddsSummary.value.some((row) => row.avg != null));
const buttonsDisabled = computed(() => !isUpcoming.value || isExpired.value || !!userTip.value || !hasOdds.value);

// Justice diff value indicator
const justiceDiff = computed(() => computeJusticeDiff(props.match as unknown as MatchV3));
const justiceTooltipHome = computed(() => {
  const m = props.match as unknown as MatchV3;
  const xgH = m.teams?.home?.xg;
  const xgA = m.teams?.away?.xg;
  const oddsH = m.odds_meta?.summary_1x2?.home?.avg;
  if (xgH == null || xgA == null || !oddsH || xgH + xgA === 0) return "";
  const share = ((xgH / (xgH + xgA)) * 100).toFixed(0);
  const implied = ((1 / oddsH) * 100).toFixed(0);
  const diff = ((justiceDiff.value.home ?? 0) * 100).toFixed(1);
  return t("match.valueSummary", { share, implied, diff });
});
const justiceTooltipAway = computed(() => {
  const m = props.match as unknown as MatchV3;
  const xgH = m.teams?.home?.xg;
  const xgA = m.teams?.away?.xg;
  const oddsA = m.odds_meta?.summary_1x2?.away?.avg;
  if (xgH == null || xgA == null || !oddsA || xgH + xgA === 0) return "";
  const share = ((xgA / (xgH + xgA)) * 100).toFixed(0);
  const implied = ((1 / oddsA) * 100).toFixed(0);
  const diff = ((justiceDiff.value.away ?? 0) * 100).toFixed(1);
  return t("match.valueSummary", { share, implied, diff });
});

function handleSelect(prediction: string) {
  if (buttonsDisabled.value) return;
  const selected = oddsValueBySelection(props.match as unknown as MatchV3, prediction as OddsButtonKey);
  if (selected == null) return;
  betslip.addItem(props.match, prediction);
}

// --- Date formatting ---
const formattedDate = computed(() => {
  const d = new Date(props.match.match_date);
  if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) {
    return d.toLocaleDateString(localeTag.value, { weekday: "short", day: "numeric", month: "short" }) + ` · ${t("match.timeTbd")}`;
  }
  return d.toLocaleDateString(localeTag.value, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
});
const leagueLabel = computed(() => sportLabel(props.match.sport_key));
const leagueFlag = computed(() => sportFlag(props.match.sport_key));

const statusLabel = computed(() => {
  if (isLive.value && liveMinute.value) return `${liveMinute.value}'`;
  if (isLive.value) return t('match.live');
  if (isCompleted.value) return t('match.completed');
  if (props.match.status === "cancelled") return t('match.cancelled');
  return t('match.scheduled');
});

const statusClass = computed(() => {
  if (isLive.value) return "bg-danger-muted/20 text-danger animate-pulse";
  if (isPast.value) return "bg-surface-3 text-text-muted";
  return "bg-primary-muted/20 text-primary";
});
</script>

<template>
  <article class="relative bg-surface-1 rounded-card p-4 border border-surface-3/50 hover:border-surface-3 transition-colors">
    <!-- Header: date, countdown, status -->
    <div class="flex items-start justify-between mb-3">
      <div class="space-y-1">
        <p class="text-xs text-text-secondary font-medium">
          <span aria-hidden="true">{{ leagueFlag }}</span>
          <span class="ml-1">{{ leagueLabel }}</span>
        </p>
        <div class="flex items-center gap-2">
          <time :datetime="match.match_date" class="text-xs text-text-muted">
            {{ formattedDate }}
          </time>
          <span
            v-if="countdown"
            class="text-xs font-mono text-warning"
            :aria-label="t('match.startsIn', { time: countdown })"
          >
            {{ countdown }}
          </span>
        </div>
      </div>
      <span
        class="text-xs px-2 py-0.5 rounded-full font-medium"
        :class="statusClass"
      >
        {{ statusLabel }}
      </span>
    </div>

    <!-- Teams + Score + Odds -->
    <div class="flex items-center justify-between gap-4">
      <!-- Teams + Score -->
      <div class="flex-1 min-w-0 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <span class="text-sm font-medium text-text-primary truncate flex items-center">
            <img
              v-if="match.teams?.home?.image_path"
              :src="match.teams.home.image_path"
              :alt="match.home_team"
              class="w-5 h-5 mr-1.5 object-contain flex-shrink-0"
              loading="lazy"
            />
            {{ match.home_team }}
            <span
              v-if="justiceDiff.home != null && justiceDiff.home > 0.10"
              class="inline-flex items-center text-[10px] text-success ml-0.5"
              :title="justiceTooltipHome"
            >&#9650;</span>
          </span>
          <span class="text-sm font-medium text-text-primary truncate flex items-center mt-1">
            <img
              v-if="match.teams?.away?.image_path"
              :src="match.teams.away.image_path"
              :alt="match.away_team"
              class="w-5 h-5 mr-1.5 object-contain flex-shrink-0"
              loading="lazy"
            />
            {{ match.away_team }}
            <span
              v-if="justiceDiff.away != null && justiceDiff.away > 0.10"
              class="inline-flex items-center text-[10px] text-success ml-0.5"
              :title="justiceTooltipAway"
            >&#9650;</span>
          </span>
        </div>

        <!-- Live / Final score -->
        <div
          v-if="hasScore && (isLive || isCompleted)"
          class="text-right shrink-0 font-mono tabular-nums"
          :class="isLive ? 'text-danger font-bold' : 'text-text-secondary'"
        >
          <p class="text-sm">{{ homeScore }}</p>
          <p class="text-sm mt-1">{{ awayScore }}</p>
        </div>
      </div>

      <!-- Odds buttons -->
      <div v-if="hasOdds" class="flex gap-2 shrink-0" role="group" :aria-label="$t('match.oddsLabel')">
        <OddsButton
          v-for="entry in oddsEntries"
          :key="entry.key"
          :match-id="match.id"
          :prediction="entry.key"
          :label="entry.label"
          :odds="oddsByKey[entry.key].avg ?? undefined"
          :min="oddsByKey[entry.key].min"
          :max="oddsByKey[entry.key].max"
          :count="oddsByKey[entry.key].count"
          :disabled="buttonsDisabled"
          :user-tip="userTip"
          @click="handleSelect(entry.key)"
        />
      </div>
      <div v-else class="shrink-0 text-xs text-text-muted italic px-2">
        {{ $t('match.noOddsAvailable') }}
      </div>
    </div>

    <div v-if="oddsBadge !== 'none'" class="mt-2">
      <span class="inline-flex rounded-full border border-surface-3/70 bg-surface-2 px-2 py-0.5 text-[10px] font-medium text-text-secondary">
        {{ oddsBadge === "live" ? $t("match.liveOdds") : $t("match.closingLine") }}
      </span>
    </div>

    <!-- User tip status -->
    <div v-if="userTip" class="mt-2 flex items-center justify-end text-xs gap-2">
      <template v-if="liveBetStatus">
        <span
          class="font-semibold"
          :class="liveBetStatus === 'winning' ? 'text-success' : 'text-danger'"
        >
          {{ liveBetStatus === 'winning' ? $t('match.liveWinning') : $t('match.liveLosing') }}
        </span>
        <span v-if="liveProjectedPoints" class="text-success/70 tabular-nums">
          +{{ liveProjectedPoints.toFixed(1) }}
        </span>
        <span class="text-text-muted">@ {{ userTip.locked_odds.toFixed(2) }}</span>
      </template>
      <template v-else-if="userTip.status === 'pending'">
        <span class="text-text-muted">
          {{ t('match.betPlaced', { odds: userTip.locked_odds.toFixed(2) }) }}
        </span>
      </template>
      <span v-else-if="userTip.status === 'won'" class="text-success font-semibold">
        {{ t('match.won', { points: userTip.points_earned?.toFixed(1) }) }}
      </span>
      <span v-else-if="userTip.status === 'lost'" class="text-danger font-medium">
        {{ $t('match.lost') }}
      </span>
      <span v-else-if="userTip.status === 'void'" class="text-warning font-medium">
        {{ $t('match.void') }}
      </span>
    </div>

    <!-- Spread + Totals row (NBA, NFL) -->
    <div
      v-if="spread || totals"
      class="mt-2 flex items-center gap-3 text-xs text-text-muted"
    >
      <template v-if="spread">
        <span class="flex items-center gap-1" :title="t('match.spreadHint')">
          <span class="font-medium text-text-secondary">Spread</span>
          <span class="tabular-nums">
            {{ spread.home_line > 0 ? '+' : '' }}{{ spread.home_line }}
          </span>
          <span class="text-text-muted/60">({{ spread.home_odds?.toFixed(2) }})</span>
        </span>
      </template>
      <template v-if="totals">
        <span class="flex items-center gap-1" :title="t('match.overUnderHint')">
          <span class="font-medium text-text-secondary">O/U</span>
          <span class="tabular-nums">{{ totals.line }}</span>
        </span>
      </template>
    </div>

    <!-- Result banner (completed matches) -->
    <div v-if="match.result.outcome" class="mt-3 pt-3 border-t border-surface-3/50 flex items-center gap-2">
      <span class="text-xs text-text-muted">{{ $t('match.result') }}</span>
      <span class="text-xs font-medium text-text-primary">
        {{ match.result.outcome === '1' ? t('match.homeWins', { team: match.home_team }) : match.result.outcome === '2' ? t('match.homeWins', { team: match.away_team }) : t('match.draw') }}
      </span>
      <span v-if="match.result.home_score != null" class="text-xs text-text-muted ml-auto">
        {{ match.result.home_score }} : {{ match.result.away_score }}
      </span>
    </div>

    <!-- Historical context -->
    <MatchHistory
      v-if="match.teams?.home?.sm_id && match.teams?.away?.sm_id"
      :home-team="match.home_team"
      :away-team="match.away_team"
      :home-s-m-id="match.teams.home.sm_id"
      :away-s-m-id="match.teams.away.sm_id"
    />

    <!-- Odds timeline -->
    <OddsTimelineToggle
      :match-id="match.id"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />

    <!-- QuoticoTip value bet recommendation -->
    <QuoticoTipBadge
      v-if="quoticoTip"
      :tip="quoticoTip"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />
    <p
      v-if="auth.isAdmin"
      class="absolute bottom-2 left-2 rounded border border-surface-3/70 bg-surface-2/80 px-1.5 py-0.5 text-[10px] font-mono text-text-muted"
    >
      {{ $t("match.adminMatchId", { id: match.id }) }}
    </p>
  </article>
</template>
