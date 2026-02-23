<script setup lang="ts">
import { ref, computed } from "vue";
import type { SpieltagMatch } from "@/stores/spieltag";
import { useWalletStore } from "@/stores/wallet";
import { useToast } from "@/composables/useToast";
import { scoreUnitLabel } from "@/types/sports";

const props = defineProps<{
  match: SpieltagMatch & { totals_odds?: { over: number; under: number; line: number } };
  sportKey?: string;
}>();

const unit = computed(() => scoreUnitLabel(props.sportKey ?? ""));

const walletStore = useWalletStore();
const toast = useToast();
const submitting = ref(false);

const totals = computed(() => props.match.totals_odds);
const line = computed(() => totals.value?.line ?? 2.5);

const existingBet = computed(() =>
  walletStore.overUnderBets.find((b) => b.match_id === props.match.id),
);

const kickoffLabel = computed(() => {
  const d = new Date(props.match.commence_time);
  return d.toLocaleString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

const totalGoals = computed(() => {
  if (props.match.home_score === null || props.match.away_score === null) return null;
  return props.match.home_score + props.match.away_score;
});

async function placeBet(prediction: "over" | "under") {
  if (!totals.value || props.match.is_locked || existingBet.value) return;

  const odds = prediction === "over" ? totals.value.over : totals.value.under;
  submitting.value = true;
  try {
    await walletStore.placeOverUnderBet(
      walletStore.wallet?.squad_id || "",
      props.match.id,
      prediction,
      0, // stake handled by backend for pure O/U mode
      odds,
    );
    toast.success(`${prediction === "over" ? "Über" : "Unter"} ${line.value} gewählt!`);
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler.");
  } finally {
    submitting.value = false;
  }
}

function betResultClass(status: string) {
  switch (status) {
    case "won": return "text-emerald-500 bg-emerald-500/10";
    case "lost": return "text-red-500 bg-red-500/10";
    case "void": return "text-amber-500 bg-amber-500/10";
    default: return "text-text-muted bg-surface-2";
  }
}
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <span
        v-if="existingBet"
        class="text-xs font-bold px-2 py-0.5 rounded-full"
        :class="betResultClass(existingBet.status)"
      >
        {{ existingBet.prediction === "over" ? "Über" : "Unter" }} {{ existingBet.line }}
        <template v-if="existingBet.status !== 'pending'">
          — {{ existingBet.status }}
        </template>
      </span>
    </div>

    <!-- Teams + Score -->
    <div class="flex items-center justify-between mb-2">
      <span class="text-sm font-medium text-text-primary truncate flex-1">
        {{ match.teams.home }}
      </span>
      <span
        v-if="match.home_score !== null"
        class="text-lg font-bold text-text-primary px-2"
      >
        {{ match.home_score }} : {{ match.away_score }}
      </span>
      <span v-else class="text-sm text-text-muted px-2">vs</span>
      <span class="text-sm font-medium text-text-primary truncate flex-1 text-right">
        {{ match.teams.away }}
      </span>
    </div>

    <!-- Total goals indicator -->
    <div v-if="totalGoals !== null" class="text-center mb-2">
      <span class="text-xs text-text-muted">
        Gesamt: {{ totalGoals }} {{ unit }}
        <span v-if="totalGoals > line" class="text-emerald-500 font-bold">(Über)</span>
        <span v-else-if="totalGoals < line" class="text-blue-500 font-bold">(Unter)</span>
        <span v-else class="text-amber-500 font-bold">(Push)</span>
      </span>
    </div>

    <!-- Line display -->
    <div v-if="totals && !existingBet && !match.is_locked" class="space-y-2">
      <div class="text-center text-xs text-text-muted mb-1">
        {{ line }} {{ unit }}
      </div>
      <div class="grid grid-cols-2 gap-2">
        <button
          class="py-2.5 rounded-lg text-sm font-semibold transition-colors bg-surface-2 text-text-primary hover:bg-emerald-500/10 hover:text-emerald-500 border border-surface-3 hover:border-emerald-500"
          :disabled="submitting"
          @click="placeBet('over')"
        >
          <div class="text-xs opacity-70">Über {{ line }}</div>
          <div>{{ totals.over?.toFixed(2) }}</div>
        </button>
        <button
          class="py-2.5 rounded-lg text-sm font-semibold transition-colors bg-surface-2 text-text-primary hover:bg-blue-500/10 hover:text-blue-500 border border-surface-3 hover:border-blue-500"
          :disabled="submitting"
          @click="placeBet('under')"
        >
          <div class="text-xs opacity-70">Unter {{ line }}</div>
          <div>{{ totals.under?.toFixed(2) }}</div>
        </button>
      </div>
    </div>

    <div v-else-if="!totals && !existingBet" class="text-center text-xs text-text-muted py-2">
      Keine Über/Unter-Quoten verfügbar
    </div>
  </div>
</template>
