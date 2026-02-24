<script setup lang="ts">
import { computed } from "vue";
import type { WarRoomMatch } from "@/composables/useWarRoom";
import type { LiveScore } from "@/stores/matches";

const props = defineProps<{
  match: WarRoomMatch;
  countdown: string | null;
  phase: "pre_kickoff" | "revealed" | "live";
  liveScore: LiveScore | null;
}>();

const homeScore = computed(
  () => props.liveScore?.home_score ?? (props.match.result as any)?.home_score ?? 0
);
const awayScore = computed(
  () => props.liveScore?.away_score ?? (props.match.result as any)?.away_score ?? 0
);
const liveMinute = computed(() => props.liveScore?.minute);

const phaseLabel = computed(() => {
  if (props.phase === "pre_kickoff") return "Vor dem Anpfiff";
  if (props.phase === "live")
    return liveMinute.value ? `${liveMinute.value}'` : "Live";
  return "Tipps aufgedeckt";
});

const phaseClass = computed(() => {
  if (props.phase === "live")
    return "bg-danger-muted/20 text-danger animate-pulse";
  if (props.phase === "revealed") return "bg-primary-muted/20 text-primary";
  return "bg-warning/10 text-warning";
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <!-- Phase pill + countdown -->
    <div class="flex items-center justify-between mb-3">
      <span
        class="text-xs px-2 py-0.5 rounded-full font-medium"
        :class="phaseClass"
      >
        {{ phaseLabel }}
      </span>

      <span
        v-if="phase === 'pre_kickoff' && countdown"
        class="text-sm font-mono font-bold text-warning tabular-nums"
        :aria-label="`Anpfiff in ${countdown}`"
      >
        {{ countdown }}
      </span>
    </div>

    <!-- Teams + Score -->
    <div class="flex items-center justify-center gap-4">
      <div class="flex-1 text-right">
        <p class="font-semibold text-text-primary text-sm truncate">
          {{ match.home_team }}
        </p>
      </div>

      <div
        v-if="phase === 'live'"
        class="flex items-center gap-2 shrink-0"
      >
        <span
          class="text-2xl font-bold font-mono tabular-nums text-danger"
          >{{ homeScore }}</span
        >
        <span class="text-text-muted font-bold">:</span>
        <span
          class="text-2xl font-bold font-mono tabular-nums text-danger"
          >{{ awayScore }}</span
        >
      </div>
      <div v-else class="shrink-0 text-text-muted font-bold px-2">vs</div>

      <div class="flex-1 text-left">
        <p class="font-semibold text-text-primary text-sm truncate">
          {{ match.away_team }}
        </p>
      </div>
    </div>
  </div>
</template>
