<script setup lang="ts">
import { computed } from "vue";
import { useBetSlipStore } from "@/stores/betslip";

const props = defineProps<{
  matchId: string;
  prediction: string;
  label: string;
  odds: number | undefined;
  disabled?: boolean;
}>();

const betslip = useBetSlipStore();

const isSelected = computed(() =>
  betslip.items.some(
    (i) => i.matchId === props.matchId && i.prediction === props.prediction
  )
);

const formattedOdds = computed(() =>
  props.odds != null ? props.odds.toFixed(2) : "-"
);

const ariaLabel = computed(() =>
  `${props.label}, Quote ${formattedOdds.value}${isSelected.value ? ", ausgewählt" : ""}`
);
</script>

<template>
  <button
    class="flex flex-col items-center justify-center min-w-[3.5rem] h-touch rounded-lg border transition-all"
    :class="[
      disabled
        ? 'bg-surface-2 border-surface-3 opacity-50 cursor-not-allowed'
        : isSelected
        ? 'bg-primary-muted/20 border-primary ring-1 ring-primary'
        : 'bg-surface-2 border-surface-3 hover:border-primary hover:bg-primary-muted/10 active:scale-95 cursor-pointer',
    ]"
    :disabled="disabled"
    :aria-label="ariaLabel"
    :aria-pressed="isSelected"
    role="switch"
  >
    <span class="text-[10px] text-text-muted leading-none">{{ prediction }}</span>
    <span
      class="text-sm font-mono font-bold mt-0.5"
      :class="isSelected ? 'text-primary' : 'text-text-primary'"
    >
      {{ formattedOdds }}
    </span>
  </button>
</template>
