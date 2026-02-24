<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import type { QuoticoTip } from "@/composables/useQuoticoTip";

const { t } = useI18n();

const props = defineProps<{
  tip: QuoticoTip;
  homeTeam: string;
  awayTeam: string;
}>();

const expanded = ref(false);

const isNoSignal = computed(() => !!props.tip.skip_reason);

const pickLabel = computed(() => {
  if (isNoSignal.value) return t("qtip.noRecommendation");
  if (props.tip.recommended_selection === "1") return props.homeTeam;
  if (props.tip.recommended_selection === "2") return props.awayTeam;
  return t("match.draw");
});

const confidencePct = computed(() => Math.round(props.tip.confidence * 100));

const confidenceColor = computed(() => {
  if (confidencePct.value >= 70) return "bg-emerald-500";
  if (confidencePct.value >= 50) return "bg-amber-500";
  return "bg-surface-3";
});

const confidenceTextColor = computed(() => {
  if (confidencePct.value >= 70) return "text-emerald-400";
  if (confidencePct.value >= 50) return "text-amber-400";
  return "text-text-muted";
});

const edgeLabel = computed(() => {
  if (props.tip.edge_pct > 0) return `+${props.tip.edge_pct.toFixed(1)}%`;
  return "";
});

// Tier signal indicators
const hasPoisson = computed(() => props.tip.tier_signals.poisson !== null);
const hasMomentum = computed(() => props.tip.tier_signals.momentum?.contributes);
const hasSharp = computed(() => props.tip.tier_signals.sharp_movement?.has_sharp_movement);
const hasKings = computed(() => props.tip.tier_signals.kings_choice?.has_kings_choice);

// BTB / EVD signal
const btb = computed(() => props.tip.tier_signals.btb);
const pickedBtb = computed(() => {
  if (!btb.value) return null;
  const sel = props.tip.recommended_selection;
  if (sel === "1") return btb.value.home;
  if (sel === "2") return btb.value.away;
  return null;
});
const hasBtb = computed(() => pickedBtb.value?.contributes === true);
const btbPositive = computed(() => hasBtb.value && pickedBtb.value!.evd > 0.10);
const btbNegative = computed(() => hasBtb.value && pickedBtb.value!.evd < -0.10);

// Rest advantage signal
const restAdvantage = computed(() => props.tip.tier_signals.rest_advantage);
const hasRest = computed(() => restAdvantage.value?.contributes === true);

const tierCount = computed(() =>
  [hasPoisson.value, hasMomentum.value, hasSharp.value, hasKings.value, hasBtb.value, hasRest.value].filter(Boolean).length
);

const localizedJustification = computed(() => {
  const tip = props.tip;
  const sel = tip.recommended_selection;
  const teamName = sel === "1" ? props.homeTeam : sel === "2" ? props.awayTeam : t("match.draw");
  const parts: string[] = [];

  // Recommendation headline
  parts.push(t("qbot.justRecommendation", { team: teamName, outcome: sel }));

  // Model vs market
  if (tip.true_probability > 0) {
    parts.push(t("qbot.justModel", {
      truePct: (tip.true_probability * 100).toFixed(0),
      impliedPct: (tip.implied_probability * 100).toFixed(0),
      edge: tip.edge_pct.toFixed(1),
    }));
  }

  // Expected goals
  if (tip.expected_goals_home > 0) {
    parts.push(t("qbot.justExpectedGoals", {
      home: tip.expected_goals_home.toFixed(1),
      away: tip.expected_goals_away.toFixed(1),
    }));
  }

  // H2H
  const poisson = tip.tier_signals.poisson;
  if (poisson && (poisson as any).h2h_total >= 3) {
    parts.push(t("qbot.justH2h", { count: (poisson as any).h2h_total }));
  }

  // Momentum / Form
  const mom = tip.tier_signals.momentum;
  if (mom?.contributes && mom.gap > 0.20) {
    parts.push(t("qbot.justMomentum"));
  }

  // Sharp money
  const sharp = tip.tier_signals.sharp_movement;
  if (sharp?.has_sharp_movement) {
    if (sharp.is_late_money) {
      parts.push(t("qbot.justSharpLate"));
    } else {
      parts.push(t("qbot.justSharp"));
    }
  }
  if ((sharp as any)?.has_steam_move) {
    parts.push(t("qbot.justSteam"));
  }
  if ((sharp as any)?.has_reversal) {
    parts.push(t("qbot.justReversal"));
  }

  // King's Choice
  const kings = tip.tier_signals.kings_choice;
  if (kings?.has_kings_choice) {
    parts.push(t("qbot.justKings", { pct: ((kings as any).kings_pct * 100).toFixed(0) }));
  }

  // BTB / EVD
  if (hasBtb.value && pickedBtb.value) {
    const evdVal = pickedBtb.value.evd;
    const ratioPct = (pickedBtb.value.btb_ratio * 100).toFixed(0);
    if (evdVal > 0.10) {
      parts.push(t("qbot.justBtbPositive", { team: teamName, pct: ratioPct, evd: (evdVal * 100).toFixed(1) + "%" }));
    } else if (evdVal < -0.10) {
      parts.push(t("qbot.justBtbNegative", { team: teamName, evd: (evdVal * 100).toFixed(1) + "%" }));
    }
  }

  // Rest advantage
  if (hasRest.value && restAdvantage.value) {
    const diff = restAdvantage.value.diff;
    const restedTeam = diff > 0 ? props.homeTeam : props.awayTeam;
    const penalty = Math.abs(diff) >= 4 ? "10" : "5";
    parts.push(t("qbot.justRestAdvantage", {
      team: restedTeam,
      homeDays: restAdvantage.value.home_rest_days,
      awayDays: restAdvantage.value.away_rest_days,
      penalty,
    }));
  }

  // Fallback: if only momentum-based (no poisson)
  if (!hasPoisson.value && mom?.contributes) {
    parts.length = 0;
    parts.push(t("qbot.justRecommendation", { team: teamName, outcome: sel }));
    parts.push(t("qbot.justFormBased", { gap: (mom.gap * 100).toFixed(0) }));
    if (sharp?.has_sharp_movement) {
      parts.push(t("qbot.justFormSharp"));
    }
  }

  return parts.join(" ");
});
</script>

<template>
  <div class="mt-2 border-t border-surface-3/30 pt-2">
    <!-- Compact badge row -->
    <button
      class="w-full flex items-center gap-2 text-xs py-1.5 group transition-colors"
      @click="expanded = !expanded"
      :aria-expanded="expanded"
      aria-label="QuoticoTip Details"
    >
      <!-- Expand chevron -->
      <svg
        class="w-3.5 h-3.5 shrink-0 text-text-muted transition-transform duration-200"
        :class="{ 'rotate-90': expanded }"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" />
      </svg>

      <!-- "Q-Tip" label -->
      <span :class="isNoSignal ? 'text-text-muted' : 'text-primary'" class="font-bold tracking-wide">Q-Tip</span>

      <!-- Pick -->
      <span class="font-medium truncate" :class="isNoSignal ? 'text-text-muted' : 'text-text-secondary'">
        {{ pickLabel }}
        <span v-if="!isNoSignal" class="text-text-muted font-normal">({{ tip.recommended_selection }})</span>
      </span>

      <!-- Edge -->
      <span v-if="edgeLabel && !isNoSignal" class="text-emerald-400 font-mono font-bold tabular-nums">
        {{ edgeLabel }}
      </span>

      <!-- Confidence bar (only for active tips) -->
      <div v-if="!isNoSignal" class="ml-auto flex items-center gap-1.5 shrink-0">
        <!-- Tier signal icons -->
        <div class="flex gap-0.5">
          <span
            v-if="hasPoisson"
            class="text-[10px] text-blue-400"
            :title="$t('qtipPerformance.signalPoisson')"
          >P</span>
          <span
            v-if="hasMomentum"
            class="text-[10px] text-orange-400"
            :title="$t('common.formStrength')"
          >F</span>
          <span
            v-if="hasSharp"
            class="text-[10px] text-purple-400"
            title="Sharp Money"
          >S</span>
          <span
            v-if="hasKings"
            class="text-[10px] text-yellow-400"
            title="King's Choice"
          >K</span>
          <span
            v-if="hasBtb"
            class="text-[10px]"
            :class="btbPositive ? 'text-teal-400' : btbNegative ? 'text-rose-400' : 'text-text-muted'"
            :title="btbPositive ? $t('qtip.marketEdgeTitle') : btbNegative ? $t('qtip.marketRiskTitle') : 'Beat the Books'"
          >B</span>
          <span
            v-if="hasRest"
            class="text-[10px] text-cyan-400"
            :title="$t('qtipPerformance.signalRest')"
          >R</span>
        </div>

        <!-- Confidence percentage -->
        <span class="font-mono font-bold tabular-nums" :class="confidenceTextColor">
          {{ confidencePct }}%
        </span>

        <!-- Mini bar -->
        <div class="w-10 h-1.5 rounded-full bg-surface-2 overflow-hidden">
          <div
            class="h-full rounded-full transition-all"
            :class="confidenceColor"
            :style="{ width: `${confidencePct}%` }"
          />
        </div>
      </div>
    </button>

    <!-- Expanded detail panel -->
    <Transition name="expand">
      <div v-if="expanded" class="overflow-hidden">
        <div class="space-y-3 py-3 text-xs">
          <!-- Skip reason (no_signal tips) -->
          <p v-if="isNoSignal" class="text-text-muted leading-relaxed">
            {{ tip.skip_reason }}
          </p>

          <!-- Justification (active tips) -->
          <p v-else class="text-text-secondary leading-relaxed">
            {{ localizedJustification }}
          </p>

          <!-- xG + Probability breakdown -->
          <div v-if="tip.expected_goals_home > 0" class="grid grid-cols-2 gap-3">
            <div class="bg-surface-2/50 rounded-lg p-2">
              <div class="text-text-muted mb-1">{{ $t('qtip.expectedGoals') }}</div>
              <div class="font-mono font-bold text-text-primary tabular-nums text-sm">
                {{ tip.expected_goals_home.toFixed(1) }} – {{ tip.expected_goals_away.toFixed(1) }}
              </div>
            </div>
            <div class="bg-surface-2/50 rounded-lg p-2">
              <div class="text-text-muted mb-1">{{ $t('qtip.modelVsMarket') }}</div>
              <div class="font-mono font-bold tabular-nums text-sm">
                <span class="text-emerald-400">{{ (tip.true_probability * 100).toFixed(0) }}%</span>
                <span class="text-text-muted mx-1">vs</span>
                <span class="text-text-secondary">{{ (tip.implied_probability * 100).toFixed(0) }}%</span>
              </div>
            </div>
          </div>

          <!-- Poisson probabilities -->
          <div v-if="tip.tier_signals.poisson" class="space-y-1.5">
            <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px]">
              {{ $t('qtip.poissonProbs') }}
            </div>
            <div class="flex gap-2">
              <div
                v-for="(label, key) in { '1': homeTeam, 'X': $t('match.draw'), '2': awayTeam }"
                :key="key"
                class="flex-1 bg-surface-2/50 rounded-lg p-1.5 text-center"
              >
                <div class="text-text-muted text-[10px] truncate">{{ label }}</div>
                <div class="font-mono font-bold tabular-nums" :class="key === tip.recommended_selection ? 'text-emerald-400' : 'text-text-secondary'">
                  {{ (tip.tier_signals.poisson.true_probs[key] * 100).toFixed(0) }}%
                </div>
                <div class="text-[10px] font-mono" :class="tip.tier_signals.poisson.edges[key] > 0 ? 'text-emerald-400/70' : 'text-text-muted/60'">
                  {{ tip.tier_signals.poisson.edges[key] > 0 ? '+' : '' }}{{ tip.tier_signals.poisson.edges[key].toFixed(1) }}%
                </div>
              </div>
            </div>
          </div>

          <!-- Active tier signals -->
          <div v-if="tierCount > 0" class="flex flex-wrap gap-1.5">
            <span
              v-if="hasPoisson"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-400"
            >
              <span class="font-bold">P</span> Dixon-Coles
            </span>
            <span
              v-if="hasMomentum"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-orange-500/10 text-orange-400"
            >
              <span class="font-bold">F</span> {{ $t('qtip.formLead', { gap: (tip.tier_signals.momentum.gap * 100).toFixed(0) }) }}
            </span>
            <span
              v-if="hasSharp"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-400"
            >
              <span class="font-bold">S</span> Sharp
              {{ tip.tier_signals.sharp_movement.is_late_money ? $t('qtip.sharpLate') : '' }}
              -{{ tip.tier_signals.sharp_movement.max_drop_pct }}%
            </span>
            <span
              v-if="hasKings"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-yellow-500/10 text-yellow-400"
            >
              <span class="font-bold">K</span> King's Choice {{ (tip.tier_signals.kings_choice.kings_pct * 100).toFixed(0) }}%
            </span>
            <span
              v-if="btbPositive"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-teal-500/10 text-teal-400"
            >
              <span class="font-bold">B</span> {{ $t('qtip.marketEdge') }} {{ (pickedBtb!.btb_ratio * 100).toFixed(0) }}%
            </span>
            <span
              v-if="btbNegative"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-500/10 text-rose-400"
            >
              <span class="font-bold">B</span> {{ $t('qtip.marketRisk') }}
            </span>
            <span
              v-if="hasRest && restAdvantage"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-cyan-500/10 text-cyan-400"
            >
              <span class="font-bold">R</span> {{ $t('qtip.restAdvantage') }} {{ restAdvantage.home_rest_days }}d vs {{ restAdvantage.away_rest_days }}d
            </span>
          </div>

          <!-- BTB / EVD detail section -->
          <div v-if="hasBtb && btb" class="space-y-1.5">
            <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px]">
              {{ $t('qtip.beatTheBooks') }}
            </div>
            <div class="flex gap-2">
              <!-- Home EVD -->
              <div class="flex-1 bg-surface-2/50 rounded-lg p-2">
                <div class="text-text-muted text-[10px] truncate">{{ homeTeam }}</div>
                <div class="flex items-center gap-1.5 mt-1">
                  <span
                    class="font-mono font-bold tabular-nums text-sm"
                    :class="btb.home.evd > 0.10 ? 'text-teal-400' : btb.home.evd < -0.10 ? 'text-rose-400' : 'text-text-secondary'"
                  >
                    {{ btb.home.evd > 0 ? '+' : '' }}{{ (btb.home.evd * 100).toFixed(1) }}%
                  </span>
                  <span class="text-[10px] text-text-muted">EVD</span>
                </div>
                <!-- BTB ratio bar -->
                <div class="mt-1.5 flex items-center gap-1.5">
                  <div class="flex-1 h-1.5 rounded-full bg-surface-3 overflow-hidden">
                    <div
                      class="h-full rounded-full transition-all"
                      :class="btb.home.evd > 0.10 ? 'bg-teal-500' : btb.home.evd < -0.10 ? 'bg-rose-500' : 'bg-surface-3'"
                      :style="{ width: `${btb.home.btb_ratio * 100}%` }"
                    />
                  </div>
                  <span class="text-[10px] font-mono text-text-muted tabular-nums">
                    {{ btb.home.btb_count }}/{{ btb.home.matches_analyzed }}
                  </span>
                </div>
              </div>
              <!-- Away EVD -->
              <div class="flex-1 bg-surface-2/50 rounded-lg p-2">
                <div class="text-text-muted text-[10px] truncate">{{ awayTeam }}</div>
                <div class="flex items-center gap-1.5 mt-1">
                  <span
                    class="font-mono font-bold tabular-nums text-sm"
                    :class="btb.away.evd > 0.10 ? 'text-teal-400' : btb.away.evd < -0.10 ? 'text-rose-400' : 'text-text-secondary'"
                  >
                    {{ btb.away.evd > 0 ? '+' : '' }}{{ (btb.away.evd * 100).toFixed(1) }}%
                  </span>
                  <span class="text-[10px] text-text-muted">EVD</span>
                </div>
                <!-- BTB ratio bar -->
                <div class="mt-1.5 flex items-center gap-1.5">
                  <div class="flex-1 h-1.5 rounded-full bg-surface-3 overflow-hidden">
                    <div
                      class="h-full rounded-full transition-all"
                      :class="btb.away.evd > 0.10 ? 'bg-teal-500' : btb.away.evd < -0.10 ? 'bg-rose-500' : 'bg-surface-3'"
                      :style="{ width: `${btb.away.btb_ratio * 100}%` }"
                    />
                  </div>
                  <span class="text-[10px] font-mono text-text-muted tabular-nums">
                    {{ btb.away.btb_count }}/{{ btb.away.matches_analyzed }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.expand-enter-active,
.expand-leave-active {
  transition: all 0.2s ease;
  max-height: 400px;
}
.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
