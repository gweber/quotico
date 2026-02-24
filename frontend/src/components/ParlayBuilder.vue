<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import type { MatchdayMatch } from "@/stores/matchday";
import { useWalletStore } from "@/stores/wallet";
import { useToast } from "@/composables/useToast";

const props = defineProps<{
  matches: MatchdayMatch[];
  squadId: string;
  matchdayId: string;
  gameMode: string;
}>();

const { t } = useI18n();
const walletStore = useWalletStore();
const toast = useToast();

interface ParlayLeg {
  matchId: string;
  prediction: string;
  odds: number;
  homeTeam: string;
  awayTeam: string;
}

const isOpen = ref(false);
const legs = ref<ParlayLeg[]>([]);
const stake = ref(50);
const submitting = ref(false);

const REQUIRED_LEGS = 3;

const combinedOdds = computed(() => {
  if (legs.value.length === 0) return 0;
  return legs.value.reduce((acc, leg) => acc * leg.odds, 1);
});

const potentialWin = computed(() => {
  if (props.gameMode === "bankroll" && stake.value) {
    return Math.round(stake.value * combinedOdds.value * 100) / 100;
  }
  return Math.round(combinedOdds.value * 10 * 100) / 100; // Classic: bonus points
});

const canSubmit = computed(() => legs.value.length === REQUIRED_LEGS);
const existingParlay = computed(() => walletStore.parlay);

const predictionLabels = computed<Record<string, string>>(() => ({
  "1": t("match.home"),
  X: "X",
  "2": t("match.away"),
  over: t("wallet.over"),
  under: t("wallet.under"),
}));

function h2hOdds(match: MatchdayMatch): Record<string, number> {
  return ((match.odds as Record<string, unknown>)?.h2h ?? {}) as Record<string, number>;
}

function toggleLeg(match: MatchdayMatch, prediction: string) {
  const existing = legs.value.findIndex((l) => l.matchId === match.id);
  if (existing >= 0) {
    if (legs.value[existing].prediction === prediction) {
      legs.value.splice(existing, 1);
      return;
    }
    legs.value.splice(existing, 1);
  }

  if (legs.value.length >= REQUIRED_LEGS) return;

  const odds = h2hOdds(match)[prediction] || 0;
  if (!odds) return;

  legs.value.push({
    matchId: match.id,
    prediction,
    odds,
    homeTeam: match.home_team,
    awayTeam: match.away_team,
  });
}

function removeLeg(matchId: string) {
  legs.value = legs.value.filter((l) => l.matchId !== matchId);
}

function isSelected(matchId: string, prediction: string) {
  return legs.value.some((l) => l.matchId === matchId && l.prediction === prediction);
}

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  try {
    await walletStore.createParlay(
      props.squadId,
      props.matchdayId,
      legs.value.map((l) => ({
        match_id: l.matchId,
        prediction: l.prediction,
        displayed_odds: l.odds,
      })),
      props.gameMode === "bankroll" ? stake.value : null,
    );
    toast.success(t('parlay.placed'));
    legs.value = [];
    isOpen.value = false;
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : t("common.error"));
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <!-- Existing parlay display -->
  <div
    v-if="existingParlay"
    class="bg-surface-1 rounded-card p-4 border border-primary/30"
  >
    <h3 class="text-sm font-semibold text-primary mb-2">{{ $t('parlay.title') }}</h3>
    <div class="space-y-1 text-xs text-text-secondary">
      <div v-for="leg in existingParlay.legs" :key="leg.match_id" class="flex justify-between">
        <span>{{ predictionLabels[leg.prediction] || leg.prediction }}</span>
        <span
          class="font-bold"
          :class="{
            'text-emerald-500': leg.result === 'won',
            'text-red-500': leg.result === 'lost',
            'text-text-muted': leg.result === 'pending',
          }"
        >
          {{ leg.locked_odds.toFixed(2) }}
        </span>
      </div>
    </div>
    <div class="mt-2 flex justify-between text-sm">
      <span class="text-text-muted">
        {{ $t('parlay.odds') }} {{ existingParlay.combined_odds.toFixed(2) }}
      </span>
      <span
        class="font-bold"
        :class="{
          'text-emerald-500': existingParlay.status === 'won',
          'text-red-500': existingParlay.status === 'lost',
          'text-text-primary': existingParlay.status === 'pending',
        }"
      >
        {{ existingParlay.status === "pending" ? `${existingParlay.potential_win.toFixed(0)}` : existingParlay.status }}
      </span>
    </div>
  </div>

  <!-- FAB to open builder -->
  <button
    v-else
    class="fixed bottom-20 right-4 z-40 bg-primary text-surface-0 rounded-full shadow-lg px-4 py-3 text-sm font-semibold hover:bg-primary-hover transition-colors"
    @click="isOpen = !isOpen"
  >
    {{ isOpen ? $t('parlay.close') : `${$t('parlay.title')} (${legs.length}/${REQUIRED_LEGS})` }}
  </button>

  <!-- Builder slide-up -->
  <div
    v-if="isOpen && !existingParlay"
    class="fixed inset-x-0 bottom-0 z-30 bg-surface-0 border-t border-surface-3 rounded-t-2xl shadow-xl max-h-[70vh] overflow-y-auto p-4 space-y-4"
  >
    <div class="flex items-center justify-between">
      <h3 class="text-lg font-bold text-text-primary">{{ $t('parlay.title') }}</h3>
      <span class="text-sm text-text-muted">
        {{ legs.length }}/{{ REQUIRED_LEGS }} {{ $t('match.matches') }}
      </span>
    </div>

    <!-- Selected legs -->
    <div v-if="legs.length > 0" class="space-y-2">
      <div
        v-for="leg in legs"
        :key="leg.matchId"
        class="flex items-center justify-between bg-surface-1 rounded-lg p-2 border border-surface-3/50"
      >
        <div class="text-xs">
          <span class="text-text-primary font-medium">
            {{ leg.homeTeam }} vs {{ leg.awayTeam }}
          </span>
          <span class="text-text-muted ml-1">
            {{ predictionLabels[leg.prediction] }} @ {{ leg.odds.toFixed(2) }}
          </span>
        </div>
        <button
          class="text-text-muted hover:text-red-500 text-xs ml-2"
          @click="removeLeg(leg.matchId)"
        >
          X
        </button>
      </div>
    </div>

    <!-- Available matches -->
    <div class="space-y-2">
      <div
        v-for="match in matches.filter(m => !m.is_locked)"
        :key="match.id"
        class="bg-surface-1 rounded-lg p-3 border border-surface-3/50"
      >
        <div class="text-xs text-text-secondary mb-1.5">
          {{ match.home_team }} vs {{ match.away_team }}
        </div>
        <div class="flex gap-1.5">
          <button
            v-for="pred in ['1', 'X', '2']"
            :key="pred"
            class="flex-1 py-1.5 rounded text-xs font-semibold transition-colors"
            :class="
              isSelected(match.id, pred)
                ? 'bg-primary text-surface-0'
                : 'bg-surface-2 text-text-secondary hover:bg-surface-3'
            "
            :disabled="legs.length >= REQUIRED_LEGS && !isSelected(match.id, pred)"
            @click="toggleLeg(match, pred)"
          >
            {{ h2hOdds(match)[pred]?.toFixed(2) || "-" }}
          </button>
        </div>
      </div>
    </div>

    <!-- Combined odds + submit -->
    <div v-if="legs.length > 0" class="border-t border-surface-3 pt-3">
      <div class="flex justify-between text-sm mb-2">
        <span class="text-text-muted">{{ $t('parlay.combinedOdds') }}</span>
        <span class="font-bold text-text-primary">{{ combinedOdds.toFixed(2) }}</span>
      </div>

      <div v-if="gameMode === 'bankroll'" class="flex items-center gap-2 mb-2">
        <label class="text-xs text-text-muted">{{ $t('parlay.stake') }}</label>
        <input
          v-model.number="stake"
          type="number"
          min="10"
          class="w-20 text-center text-sm bg-surface-2 border border-surface-3 rounded-lg px-2 py-1 text-text-primary"
        />
        <span class="text-xs text-text-muted">C</span>
      </div>

      <div class="flex justify-between text-sm mb-3">
        <span class="text-text-muted">{{ $t('parlay.potentialWin') }}</span>
        <span class="font-bold text-emerald-500">{{ potentialWin.toFixed(0) }}</span>
      </div>

      <button
        class="w-full py-2.5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50"
        :class="
          canSubmit
            ? 'bg-primary text-surface-0 hover:bg-primary-hover'
            : 'bg-surface-2 text-text-muted cursor-not-allowed'
        "
        :disabled="!canSubmit || submitting"
        @click="submit"
      >
        {{ submitting ? $t('parlay.submitting') : `${$t('parlay.submit')} (${legs.length}/${REQUIRED_LEGS})` }}
      </button>
    </div>
  </div>
</template>
