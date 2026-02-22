<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import type { Match } from "@/stores/matches";
import { useMatchesStore } from "@/stores/matches";
import { useBetSlipStore } from "@/stores/betslip";
import OddsButton from "./OddsButton.vue";

const props = defineProps<{
  match: Match;
}>();

const betslip = useBetSlipStore();
const matchesStore = useMatchesStore();

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

const buttonsDisabled = computed(() => !isUpcoming.value || isExpired.value);

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
          <p class="text-sm font-medium text-text-primary truncate">
            {{ match.teams.home }}
          </p>
          <p class="text-sm font-medium text-text-primary truncate mt-1">
            {{ match.teams.away }}
          </p>
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
          @click="handleSelect(entry.key)"
        />
      </div>
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
  </article>
</template>
