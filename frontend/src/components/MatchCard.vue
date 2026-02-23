<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import type { Match } from "@/stores/matches";
import { useMatchesStore } from "@/stores/matches";
import { useAuthStore } from "@/stores/auth";
import { useBetSlipStore } from "@/stores/betslip";
import { RouterLink } from "vue-router";
import OddsButton from "./OddsButton.vue";
import MatchHistory from "./MatchHistory.vue";
import OddsTimelineToggle from "./OddsTimelineToggle.vue";
import QuoticoTipBadge from "./QuoticoTipBadge.vue";
import { getCachedTip } from "@/composables/useQuoticoTip";
import { getCachedUserTip } from "@/composables/useUserTips";
import { teamSlug } from "@/composables/useTeam";

const props = defineProps<{
  match: Match;
}>();

const quoticoTip = computed(() => getCachedTip(props.match.id));

const auth = useAuthStore();
const betslip = useBetSlipStore();
const matchesStore = useMatchesStore();

// User's placed tip for this match (if any)
const userTip = computed(() =>
  auth.isLoggedIn ? getCachedUserTip(props.match.id) : undefined
);

// --- Countdown timer ---
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

const commenceMs = computed(() => new Date(props.match.commence_time).getTime());
const isUpcoming = computed(() => props.match.status === "upcoming");
const isLive = computed(() => props.match.status === "live");
const isCompleted = computed(() => props.match.status === "completed");
const isPast = computed(() => isCompleted.value || props.match.status === "cancelled");
const isExpired = computed(() => now.value >= commenceMs.value);

// Live score from polling
const liveScore = computed(() => matchesStore.liveScores.get(props.match.id));
const hasScore = computed(
  () =>
    liveScore.value != null ||
    props.match.home_score != null ||
    props.match.away_score != null
);
const homeScore = computed(
  () => liveScore.value?.home_score ?? props.match.home_score ?? 0
);
const awayScore = computed(
  () => liveScore.value?.away_score ?? props.match.away_score ?? 0
);
const liveMinute = computed(() => liveScore.value?.minute);

const countdown = computed(() => {
  if (!isUpcoming.value) return null;
  const diff = commenceMs.value - now.value;
  if (diff <= 0) return "Startet gleich";

  const days = Math.floor(diff / 86400000);
  const hours = Math.floor((diff % 86400000) / 3600000);
  const minutes = Math.floor((diff % 3600000) / 60000);
  const seconds = Math.floor((diff % 60000) / 1000);

  if (days > 0) return `${days}T ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
});

onMounted(() => {
  if (isUpcoming.value) {
    timer = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  }
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});

// --- Spread & Totals ---
const spread = computed(() => props.match.spreads_odds);
const totals = computed(() => props.match.totals_odds);

// --- Odds logic ---
const isThreeWay = computed(() => props.match.current_odds["X"] !== undefined);

const oddsEntries = computed(() => {
  const m = props.match;
  if (isThreeWay.value) {
    return [
      { key: "1", label: m.teams.home },
      { key: "X", label: "Unentschieden" },
      { key: "2", label: m.teams.away },
    ];
  }
  return [
    { key: "1", label: m.teams.home },
    { key: "2", label: m.teams.away },
  ];
});

const buttonsDisabled = computed(() => !isUpcoming.value || isExpired.value || !!userTip.value);

function handleSelect(prediction: string) {
  if (buttonsDisabled.value) return;
  betslip.addItem(props.match, prediction);
}

// --- Date formatting ---
const formattedDate = computed(() =>
  new Date(props.match.commence_time).toLocaleDateString("de-DE", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  })
);

const statusLabel = computed(() => {
  if (isLive.value && liveMinute.value) return `${liveMinute.value}'`;
  if (isLive.value) return "Live";
  if (isCompleted.value) return "Beendet";
  if (props.match.status === "cancelled") return "Abgesagt";
  return "Geplant";
});

const statusClass = computed(() => {
  if (isLive.value) return "bg-danger-muted/20 text-danger animate-pulse";
  if (isPast.value) return "bg-surface-3 text-text-muted";
  return "bg-primary-muted/20 text-primary";
});
</script>

<template>
  <article class="bg-surface-1 rounded-card p-4 border border-surface-3/50 hover:border-surface-3 transition-colors">
    <!-- Header: date, countdown, status -->
    <div class="flex items-center justify-between mb-3">
      <div class="flex items-center gap-2">
        <time :datetime="match.commence_time" class="text-xs text-text-muted">
          {{ formattedDate }}
        </time>
        <span
          v-if="countdown"
          class="text-xs font-mono text-warning"
          :aria-label="`Startet in ${countdown}`"
        >
          {{ countdown }}
        </span>
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
          <RouterLink
            :to="{ name: 'team-detail', params: { teamSlug: teamSlug(match.teams.home) }, query: { sport: match.sport_key } }"
            class="text-sm font-medium text-text-primary truncate block hover:text-primary transition-colors"
          >
            {{ match.teams.home }}
          </RouterLink>
          <RouterLink
            :to="{ name: 'team-detail', params: { teamSlug: teamSlug(match.teams.away) }, query: { sport: match.sport_key } }"
            class="text-sm font-medium text-text-primary truncate block mt-1 hover:text-primary transition-colors"
          >
            {{ match.teams.away }}
          </RouterLink>
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
      <div class="flex gap-2 shrink-0" role="group" aria-label="Quoten">
        <OddsButton
          v-for="entry in oddsEntries"
          :key="entry.key"
          :match-id="match.id"
          :prediction="entry.key"
          :label="entry.label"
          :odds="match.current_odds[entry.key]"
          :disabled="buttonsDisabled"
          :user-tip="userTip"
          @click="handleSelect(entry.key)"
        />
      </div>
    </div>

    <!-- User tip status -->
    <div v-if="userTip" class="mt-2 flex items-center justify-end text-xs">
      <span v-if="userTip.status === 'pending'" class="text-text-muted">
        Getippt @ {{ userTip.locked_odds.toFixed(2) }}
      </span>
      <span v-else-if="userTip.status === 'won'" class="text-success font-semibold">
        Gewonnen +{{ userTip.points_earned?.toFixed(1) }} Pkt
      </span>
      <span v-else-if="userTip.status === 'lost'" class="text-danger font-medium">
        Verloren
      </span>
      <span v-else-if="userTip.status === 'void'" class="text-warning font-medium">
        Ungültig
      </span>
    </div>

    <!-- Spread + Totals row (NBA, NFL) -->
    <div
      v-if="spread || totals"
      class="mt-2 flex items-center gap-3 text-xs text-text-muted"
    >
      <template v-if="spread">
        <span class="flex items-center gap-1" title="Spread (Handicap)">
          <span class="font-medium text-text-secondary">Spread</span>
          <span class="tabular-nums">
            {{ spread.home_line > 0 ? '+' : '' }}{{ spread.home_line }}
          </span>
          <span class="text-text-muted/60">({{ spread.home_odds?.toFixed(2) }})</span>
        </span>
      </template>
      <template v-if="totals">
        <span class="flex items-center gap-1" title="Over/Under (Gesamtpunkte)">
          <span class="font-medium text-text-secondary">O/U</span>
          <span class="tabular-nums">{{ totals.line }}</span>
        </span>
      </template>
    </div>

    <!-- Result banner (completed matches) -->
    <div v-if="match.result" class="mt-3 pt-3 border-t border-surface-3/50 flex items-center gap-2">
      <span class="text-xs text-text-muted">Ergebnis:</span>
      <span class="text-xs font-medium text-text-primary">
        {{ match.result === '1' ? match.teams.home + ' gewinnt' : match.result === '2' ? match.teams.away + ' gewinnt' : 'Unentschieden' }}
      </span>
      <span v-if="match.home_score != null" class="text-xs text-text-muted ml-auto">
        {{ match.home_score }} : {{ match.away_score }}
      </span>
    </div>

    <!-- Historical context -->
    <MatchHistory
      :home-team="match.teams.home"
      :away-team="match.teams.away"
      :sport-key="match.sport_key"
    />

    <!-- Odds timeline -->
    <OddsTimelineToggle
      :match-id="match.id"
      :home-team="match.teams.home"
      :away-team="match.teams.away"
    />

    <!-- QuoticoTip value bet recommendation -->
    <QuoticoTipBadge
      v-if="quoticoTip"
      :tip="quoticoTip"
      :home-team="match.teams.home"
      :away-team="match.teams.away"
    />
  </article>
</template>
