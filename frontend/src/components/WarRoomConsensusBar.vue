<script setup lang="ts">
import { computed } from "vue";
import type { WarRoomConsensus } from "@/composables/useWarRoom";

const props = defineProps<{
  consensus: WarRoomConsensus;
  homeTeam: string;
  awayTeam: string;
}>();

const segments = computed(() =>
  [
    {
      key: "1",
      label: props.homeTeam,
      pct: props.consensus.percentages["1"] ?? 0,
      color: "bg-primary",
    },
    {
      key: "X",
      label: "Unentschieden",
      pct: props.consensus.percentages["X"] ?? 0,
      color: "bg-warning",
    },
    {
      key: "2",
      label: props.awayTeam,
      pct: props.consensus.percentages["2"] ?? 0,
      color: "bg-secondary",
    },
  ].filter((s) => s.pct > 0)
);

const majorityKey = computed(() => {
  const p = props.consensus.percentages;
  let maxKey = "";
  let maxVal = 0;
  for (const [k, v] of Object.entries(p)) {
    if (v > maxVal) {
      maxVal = v;
      maxKey = k;
    }
  }
  return maxKey;
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <div class="flex items-center justify-between mb-2">
      <h3
        class="text-xs font-semibold text-text-secondary uppercase tracking-wide"
      >
        Squad-Konsens
      </h3>
      <span class="text-xs text-text-muted font-mono">
        {{ consensus.total_tippers }} Tipps
      </span>
    </div>

    <!-- Segmented bar -->
    <div
      class="h-3 rounded-full bg-surface-3 overflow-hidden flex"
      role="progressbar"
      :aria-label="`Konsens: Heim ${consensus.percentages['1'] ?? 0}%, X ${consensus.percentages['X'] ?? 0}%, Auswärts ${consensus.percentages['2'] ?? 0}%`"
    >
      <div
        v-for="seg in segments"
        :key="seg.key"
        class="h-full transition-all duration-700 ease-out first:rounded-l-full last:rounded-r-full"
        :class="seg.color"
        :style="{ width: `${seg.pct}%` }"
      />
    </div>

    <!-- Labels below -->
    <div class="flex mt-2 gap-4">
      <div
        v-for="seg in segments"
        :key="seg.key"
        class="text-center flex-1 min-w-0"
      >
        <p class="text-[10px] text-text-muted truncate">{{ seg.label }}</p>
        <p class="text-xs font-mono font-bold text-text-primary tabular-nums">
          {{ seg.pct }}%
        </p>
        <span
          v-if="majorityKey === seg.key"
          class="text-[9px] text-primary font-semibold"
          >Mehrheit</span
        >
      </div>
    </div>
  </div>
</template>
