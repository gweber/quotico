<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import type { MatchdayMatch } from "@/stores/matchday";
import { useWalletStore } from "@/stores/wallet";
import { useToast } from "@/composables/useToast";
import HighBetWarning from "./HighBetWarning.vue";

const props = defineProps<{
  match: MatchdayMatch;
}>();

const { t } = useI18n();
const walletStore = useWalletStore();
const toast = useToast();

const selectedPrediction = ref<string | null>(null);
const stake = ref(50);
const submitting = ref(false);
const showHighBetWarning = ref(false);

const existingBet = computed(() =>
  walletStore.bets.find((b) => b.match_id === props.match.id),
);

const h2hOdds = computed(() => ((props.match.odds as any)?.h2h || {}) as Record<string, number>);

const selectedOdds = computed(() => {
  if (!selectedPrediction.value) return 0;
  return h2hOdds.value[selectedPrediction.value] || 0;
});

const potentialWin = computed(() =>
  Math.round(stake.value * selectedOdds.value * 100) / 100,
);

const maxStake = computed(() => {
  if (!walletStore.wallet) return 0;
  const pct = 50; // default max_bet_pct
  return Math.floor(walletStore.wallet.balance * pct / 100);
});

const isHighBet = computed(() => {
  if (!walletStore.wallet) return false;
  return stake.value > walletStore.wallet.balance * 0.5;
});

const kickoffLabel = computed(() => {
  const d = new Date(props.match.match_date);
  return d.toLocaleString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

function selectPrediction(pred: string) {
  if (props.match.is_locked || existingBet.value) return;
  selectedPrediction.value = selectedPrediction.value === pred ? null : pred;
}

async function handlePlace() {
  if (isHighBet.value) {
    showHighBetWarning.value = true;
    return;
  }
  await placeBet();
}

async function placeBet() {
  showHighBetWarning.value = false;
  if (!selectedPrediction.value || !walletStore.wallet) return;

  submitting.value = true;
  try {
    await walletStore.placeBet(
      walletStore.wallet.squad_id,
      props.match.id,
      selectedPrediction.value,
      stake.value,
      selectedOdds.value,
    );
    toast.success(t("wallet.betPlaced"));
    selectedPrediction.value = null;
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.genericError"));
  } finally {
    submitting.value = false;
  }
}

const predictionLabels = computed<Record<string, string>>(() => ({
  "1": t("match.home"),
  X: t("match.draw"),
  "2": t("match.away"),
}));

function betStatusClass(status: string) {
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
    <!-- Top row -->
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <span
        v-if="existingBet"
        class="text-xs font-bold px-2 py-0.5 rounded-full"
        :class="betStatusClass(existingBet.status)"
      >
        {{ existingBet.status === "won" ? `+${Math.round(existingBet.potential_win)}` : existingBet.status === "pending" ? t('match.open') : existingBet.status === "lost" ? `-${existingBet.stake}` : $t('match.void') }}
      </span>
      <span v-else-if="match.is_locked" class="text-xs text-text-muted">{{ $t('match.locked') }}</span>
    </div>

    <!-- Teams -->
    <div class="flex items-center justify-between mb-3">
      <span class="text-sm font-medium text-text-primary flex-1 text-left truncate">
        {{ match.home_team }}
      </span>
      <span
        v-if="(match.result as any)?.home_score != null"
        class="text-lg font-bold text-text-primary px-2"
      >
        {{ (match.result as any)?.home_score }} : {{ (match.result as any)?.away_score }}
      </span>
      <span v-else class="text-sm text-text-muted px-2">vs</span>
      <span class="text-sm font-medium text-text-primary flex-1 text-right truncate">
        {{ match.away_team }}
      </span>
    </div>

    <!-- Existing bet display -->
    <div v-if="existingBet" class="bg-surface-2 rounded-lg p-3 text-sm">
      <div class="flex justify-between text-text-secondary">
        <span>{{ predictionLabels[existingBet.prediction] || existingBet.prediction }}</span>
        <span>{{ existingBet.stake }} Coins @ {{ existingBet.locked_odds.toFixed(2) }}</span>
      </div>
    </div>

    <!-- Bet placement UI -->
    <template v-else-if="!match.is_locked">
      <!-- 1 X 2 buttons -->
      <div class="grid grid-cols-3 gap-2 mb-3">
        <button
          v-for="pred in ['1', 'X', '2']"
          :key="pred"
          class="py-2 rounded-lg text-sm font-semibold transition-colors text-center"
          :class="
            selectedPrediction === pred
              ? 'bg-primary text-surface-0'
              : 'bg-surface-2 text-text-secondary hover:bg-surface-3'
          "
          @click="selectPrediction(pred)"
        >
          <div class="text-xs opacity-70">{{ predictionLabels[pred] }}</div>
          <div>{{ h2hOdds[pred]?.toFixed(2) || "-" }}</div>
        </button>
      </div>

      <!-- Stake input -->
      <div v-if="selectedPrediction" class="space-y-2">
        <div class="flex items-center gap-2">
          <label class="text-xs text-text-muted shrink-0">{{ $t('wallet.stake') }}</label>
          <input
            v-model.number="stake"
            type="range"
            :min="10"
            :max="maxStake || 100"
            step="10"
            class="flex-1 accent-primary"
          />
          <span class="text-sm font-bold text-text-primary w-16 text-right">
            {{ stake }} C
          </span>
        </div>
        <div class="flex justify-between text-xs text-text-muted">
          <span>{{ $t('wallet.potentialWin') }} {{ potentialWin.toFixed(0) }} Coins</span>
          <button
            class="px-4 py-1.5 rounded-lg bg-primary text-surface-0 font-semibold text-xs hover:bg-primary-hover transition-colors disabled:opacity-50"
            :disabled="submitting || stake <= 0"
            @click="handlePlace"
          >
            {{ submitting ? "..." : $t('match.place') }}
          </button>
        </div>
      </div>
    </template>
  </div>

  <!-- High bet warning modal -->
  <HighBetWarning
    v-if="showHighBetWarning && walletStore.wallet"
    :stake="stake"
    :balance="walletStore.wallet.balance"
    @confirm="placeBet"
    @cancel="showHighBetWarning = false"
  />
</template>
