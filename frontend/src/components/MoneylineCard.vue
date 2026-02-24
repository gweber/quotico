<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import type { MatchdayMatch } from "@/stores/matchday";
import { useMatchdayStore } from "@/stores/matchday";
import { useToast } from "@/composables/useToast";
import MatchHistory from "./MatchHistory.vue";
import QuoticoTipBadge from "./QuoticoTipBadge.vue";
import { getCachedTip } from "@/composables/useQuoticoTip";

const props = defineProps<{
  match: MatchdayMatch;
  sportKey?: string;
}>();

const { t } = useI18n();
const quoticoTip = computed(() => getCachedTip(props.match.id));

const matchday = useMatchdayStore();
const toast = useToast();

const selectedPrediction = ref<string | null>(null);
const submitting = ref(false);

const existingTip = computed(() =>
  matchday.moneylineBets.get(props.match.id),
);

const h2hOdds = computed(() =>
  (props.match.odds?.h2h ?? {}) as Record<string, number>
);

const matchResult = computed(() =>
  (props.match.result ?? {}) as { outcome?: string; home_score?: number | null; away_score?: number | null }
);

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

const predictionLabels = computed<Record<string, string>>(() => ({
  "1": t("match.home"),
  X: t("match.draw"),
  "2": t("match.away"),
}));

function selectPrediction(pred: string) {
  if (props.match.is_locked || existingTip.value) return;
  selectedPrediction.value = selectedPrediction.value === pred ? null : pred;
}

async function submitTip() {
  if (!selectedPrediction.value) return;
  const odds = h2hOdds.value[selectedPrediction.value];
  if (!odds) return;

  submitting.value = true;
  try {
    const ok = await matchday.submitMoneylineBet(
      props.match.id,
      selectedPrediction.value,
      odds,
    );
    if (ok) {
      toast.success(t("betslip.successSingle"));
      selectedPrediction.value = null;
    } else {
      toast.error(t("common.genericError"));
    }
  } finally {
    submitting.value = false;
  }
}

function tipStatusClass(status: string) {
  switch (status) {
    case "won": return "text-primary bg-primary/10";
    case "lost": return "text-danger bg-danger/10";
    case "void": return "text-warning bg-warning/10";
    default: return "text-text-muted bg-surface-2";
  }
}

function tipStatusLabel(status: string) {
  switch (status) {
    case "won": return t("qbot.won");
    case "lost": return t("qbot.lost");
    case "void": return t("match.void");
    default: return t('match.open');
  }
}
</script>

<template>
  <div
    class="bg-surface-1 rounded-card p-4 border border-surface-3/50 transition-colors"
    :class="{ 'opacity-60': match.is_locked && !existingTip }"
  >
    <!-- Top row: kickoff + status -->
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <span
        v-if="existingTip"
        class="text-xs font-bold px-2 py-0.5 rounded-full"
        :class="tipStatusClass(existingTip.status)"
      >
        {{ tipStatusLabel(existingTip.status) }}
      </span>
      <span v-else-if="match.is_locked" class="text-xs text-text-muted">{{ $t('match.locked') }}</span>
    </div>

    <!-- Teams + Score (mirroring classic card layout) -->
    <div class="flex items-center gap-3">
      <div class="flex-1 text-right">
        <span class="text-sm font-medium text-text-primary truncate block">
          {{ match.home_team }}
        </span>
      </div>
      <div class="flex items-center gap-1.5 shrink-0">
        <template v-if="match.status === 'final' && matchResult.home_score !== null">
          <span class="w-10 text-center text-lg font-bold text-text-primary">{{ matchResult.home_score }}</span>
          <span class="text-text-muted font-bold">:</span>
          <span class="w-10 text-center text-lg font-bold text-text-primary">{{ matchResult.away_score }}</span>
        </template>
        <template v-else>
          <span class="text-sm text-text-muted px-2">vs</span>
        </template>
      </div>
      <div class="flex-1">
        <span class="text-sm font-medium text-text-primary truncate block">
          {{ match.away_team }}
        </span>
      </div>
    </div>

    <!-- Existing tip display -->
    <div v-if="existingTip" class="bg-surface-2 rounded-lg p-3 text-sm mt-3">
      <div class="flex justify-between text-text-secondary">
        <span>{{ predictionLabels[existingTip.selection] || existingTip.selection }}</span>
        <span>@ {{ existingTip.locked_odds.toFixed(2) }}</span>
      </div>
    </div>

    <!-- Tip placement UI -->
    <template v-else-if="!match.is_locked">
      <!-- 1 X 2 buttons -->
      <div class="grid grid-cols-3 gap-2 mt-3 mb-3">
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

      <!-- Submit button -->
      <div v-if="selectedPrediction" class="flex justify-end">
        <button
          class="px-4 py-1.5 rounded-lg bg-primary text-surface-0 font-semibold text-xs hover:bg-primary-hover transition-colors disabled:opacity-50"
          :disabled="submitting"
          @click="submitTip"
        >
          {{ submitting ? "..." : $t('betslip.submit') }}
        </button>
      </div>
    </template>

    <!-- Historical context (H2H + form) -->
    <MatchHistory
      v-if="sportKey"
      :home-team="match.home_team"
      :away-team="match.away_team"
      :sport-key="sportKey"
      :context="(match.h2h_context as any) ?? undefined"
    />

    <!-- QuoticoTip value bet recommendation -->
    <QuoticoTipBadge
      v-if="quoticoTip"
      :tip="quoticoTip"
      :home-team="match.home_team"
      :away-team="match.away_team"
    />
  </div>
</template>
