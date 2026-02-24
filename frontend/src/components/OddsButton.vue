<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useBetSlipStore } from "@/stores/betslip";
import { useMatchesStore } from "@/stores/matches";
import type { UserBet } from "@/composables/useUserBets";

const { t } = useI18n();

const props = defineProps<{
  matchId: string;
  prediction: string;
  label: string;
  odds: number | undefined;
  disabled?: boolean;
  userTip?: UserBet;
}>();

const betslip = useBetSlipStore();
const matchesStore = useMatchesStore();

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
  if (isTipped.value) return `${props.label}, ${t("match.betPlaced", { odds: formattedOdds.value })}`;
  return `${props.label}, Quote ${formattedOdds.value}${isSelected.value ? ", ausgewählt" : ""}`;
});

// Subtle flash when odds change
const isFlashing = ref(false);
const prevOdds = ref(props.odds);
const oddsDirection = ref<"up" | "down" | null>(null);

watch(
  () => props.odds,
  (newVal, oldVal) => {
    if (oldVal != null && newVal != null && oldVal !== newVal) {
      oddsDirection.value = newVal > oldVal ? "up" : "down";
      isFlashing.value = true;
      prevOdds.value = newVal;
      setTimeout(() => {
        isFlashing.value = false;
        oddsDirection.value = null;
      }, 1500);
    }
  }
);

// Also flash when store signals this match's odds changed
watch(
  () => matchesStore.recentlyChangedOdds.has(props.matchId),
  (changed) => {
    if (changed && !isTipped.value) {
      isFlashing.value = true;
      setTimeout(() => { isFlashing.value = false; }, 1500);
    }
  }
);
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
      isFlashing && oddsDirection === 'up' ? 'odds-flash-up' : '',
      isFlashing && oddsDirection === 'down' ? 'odds-flash-down' : '',
      isFlashing && !oddsDirection ? 'odds-flash' : '',
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
      class="text-sm font-mono font-bold mt-0.5 transition-colors duration-500"
      :class="[
        isTipped ? 'text-success' : isSelected ? 'text-primary' : 'text-text-primary',
        isFlashing && oddsDirection === 'up' ? '!text-success' : '',
        isFlashing && oddsDirection === 'down' ? '!text-danger' : '',
      ]"
    >
      {{ formattedOdds }}
    </span>
  </button>
</template>

<style scoped>
@keyframes odds-pulse-up {
  0% { background-color: transparent; }
  20% { background-color: rgb(var(--color-success) / 0.15); }
  100% { background-color: transparent; }
}
@keyframes odds-pulse-down {
  0% { background-color: transparent; }
  20% { background-color: rgb(var(--color-danger) / 0.15); }
  100% { background-color: transparent; }
}
@keyframes odds-pulse {
  0% { background-color: transparent; }
  20% { background-color: rgb(var(--color-primary) / 0.12); }
  100% { background-color: transparent; }
}
.odds-flash-up {
  animation: odds-pulse-up 1.5s ease-out;
}
.odds-flash-down {
  animation: odds-pulse-down 1.5s ease-out;
}
.odds-flash {
  animation: odds-pulse 1.5s ease-out;
}
</style>
