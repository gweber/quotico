<script setup lang="ts">
import { computed } from "vue";
import { useBetSlipStore } from "@/stores/betslip";
import type { UserTip } from "@/composables/useUserTips";

const props = defineProps<{
  matchId: string;
  prediction: string;
  label: string;
  odds: number | undefined;
  disabled?: boolean;
  userTip?: UserTip;
}>();

const betslip = useBetSlipStore();

const isSelected = computed(() =>
  betslip.items.some(
    (i) => i.matchId === props.matchId && i.prediction === props.prediction
  )
);

// This odds button matches the user's tipped outcome
const isTipped = computed(() =>
  props.userTip?.selection.value === props.prediction
);

// User tipped a different outcome on this match
const isTippedOther = computed(() =>
  props.userTip != null && props.userTip.selection.value !== props.prediction
);

// Show locked odds on the tipped button, current odds on others
const displayedOdds = computed(() => {
  if (isTipped.value) return props.userTip!.locked_odds;
  return props.odds;
});

const formattedOdds = computed(() =>
  displayedOdds.value != null ? displayedOdds.value.toFixed(2) : "-"
);

const ariaLabel = computed(() => {
  if (isTipped.value) return `${props.label}, Getippt @ ${formattedOdds.value}`;
  return `${props.label}, Quote ${formattedOdds.value}${isSelected.value ? ", ausgewählt" : ""}`;
});
</script>

<template>
  <button
    class="flex flex-col items-center justify-center min-w-[3.5rem] h-touch rounded-lg border transition-all"
    :class="[
      isTipped
        ? 'bg-success/15 border-success ring-1 ring-success cursor-default'
        : isTippedOther
        ? 'bg-surface-2 border-surface-3 opacity-30 cursor-default'
        : disabled
        ? 'bg-surface-2 border-surface-3 opacity-50 cursor-not-allowed'
        : isSelected
        ? 'bg-primary-muted/20 border-primary ring-1 ring-primary'
        : 'bg-surface-2 border-surface-3 hover:border-primary hover:bg-primary-muted/10 active:scale-95 cursor-pointer',
    ]"
    :disabled="disabled || !!userTip"
    :aria-label="ariaLabel"
    :aria-pressed="isSelected || isTipped"
    role="switch"
  >
    <span class="text-[10px] leading-none" :class="isTipped ? 'text-success' : 'text-text-muted'">
      {{ prediction }}
    </span>
    <span
      class="text-sm font-mono font-bold mt-0.5"
      :class="isTipped ? 'text-success' : isSelected ? 'text-primary' : 'text-text-primary'"
    >
      {{ formattedOdds }}
    </span>
  </button>
</template>
