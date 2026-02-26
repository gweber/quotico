<!--
frontend/src/components/QuoticoTipBadge.vue

Purpose:
    Compact and expanded card for QuoticoTip details inside match views.
    Includes optional ExpertAnalysis rendering when qbot_logic is present.
-->
<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import type { QuoticoTip } from "@/composables/useQuoticoTip";
import { refreshSingleTip } from "@/composables/useQuoticoTip";
import ExpertAnalysis from "@/components/ExpertAnalysis.vue";

const { t } = useI18n();
const auth = useAuthStore();

const props = defineProps<{
  tip: QuoticoTip;
  homeTeam: string;
  awayTeam: string;
}>();

const emit = defineEmits<{ (e: "refreshed", tip: QuoticoTip): void }>();

const expanded = ref(false);

// Admin refresh state
const refreshing = ref(false);
const showMetrics = ref(false);
const metricsTip = ref<QuoticoTip | null>(null);
const refreshError = ref("");

async function handleRefresh(ev: Event) {
  ev.stopPropagation();
  refreshing.value = true;
  refreshError.value = "";
  try {
    const fresh = await refreshSingleTip(props.tip.match_id);
    metricsTip.value = fresh;
    showMetrics.value = true;
    emit("refreshed", fresh);
  } catch (e: any) {
    refreshError.value = e?.message || "Refresh failed";
  } finally {
    refreshing.value = false;
  }
}

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

// Qbot Intelligence
const qbot = computed(() => props.tip.qbot_logic ?? null);

// Player Mode
const playerData = computed(() => qbot.value?.player ?? null);
const playerScoreLabel = computed(() => {
  if (!playerData.value?.predicted_score) return "";
  const s = playerData.value.predicted_score;
  return `${s.home}:${s.away}`;
});
const playerReasoning = computed(() => {
  if (!playerData.value) return "";
  return t(playerData.value.reasoning_key, playerData.value.reasoning_params);
});

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

// --- Metrics modal helpers ---
const mt = computed(() => metricsTip.value);
const mtPoisson = computed(() => mt.value?.tier_signals.poisson);
const mtMomentum = computed(() => mt.value?.tier_signals.momentum);
const mtSharp = computed(() => mt.value?.tier_signals.sharp_movement);
const mtKings = computed(() => mt.value?.tier_signals.kings_choice);
const mtBtb = computed(() => mt.value?.tier_signals.btb);
const mtRest = computed(() => mt.value?.tier_signals.rest_advantage);
const mtXg = computed(() => mt.value?.tier_signals.xg_performance);

// xG signal for the recommended team
const mtPickedXg = computed(() => {
  if (!mtXg.value || !mt.value) return null;
  const sel = mt.value.recommended_selection;
  if (sel === "1") return mtXg.value.home;
  if (sel === "2") return mtXg.value.away;
  return null;
});

function mtPickTeam(tip: QuoticoTip) {
  if (tip.recommended_selection === "1") return tip.home_team;
  if (tip.recommended_selection === "2") return tip.away_team;
  return "Draw";
}

function fmtPct(v: number | undefined, decimals = 1): string {
  if (v == null) return "-";
  return (v * 100).toFixed(decimals) + "%";
}
function fmtNum(v: number | undefined, decimals = 2): string {
  if (v == null) return "-";
  return v.toFixed(decimals);
}
function fmtSignedPct(v: number | undefined, decimals = 1): string {
  if (v == null) return "-";
  const s = (v * 100).toFixed(decimals);
  return v > 0 ? "+" + s + "%" : s + "%";
}
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

      <!-- Admin refresh button -->
      <button
        v-if="auth.isAdmin"
        class="p-0.5 rounded hover:bg-surface-2 text-text-muted hover:text-warning transition-colors"
        :class="{ 'animate-spin': refreshing }"
        :title="$t('qtip.refreshTip')"
        :disabled="refreshing"
        @click="handleRefresh"
      >
        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </button>

      <!-- Pick -->
      <span class="font-medium truncate" :class="isNoSignal ? 'text-text-muted' : 'text-text-secondary'">
        {{ pickLabel }}
        <span v-if="!isNoSignal" class="text-text-muted font-normal">({{ tip.recommended_selection }})</span>
      </span>

      <!-- Edge -->
      <span v-if="edgeLabel && !isNoSignal" class="text-emerald-400 font-mono font-bold tabular-nums">
        {{ edgeLabel }}
      </span>

      <!-- Player Mode score pill (no_signal: show player score as primary info) -->
      <span v-if="isNoSignal && playerScoreLabel" class="ml-1 text-text-muted">
        {{ $t('qtip.playerMode') }}:
        <span class="px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 font-mono font-bold tabular-nums">
          {{ playerScoreLabel }}
        </span>
      </span>

      <!-- Player Mode score pill (active tips: compact pill next to confidence) -->
      <span v-if="!isNoSignal && playerScoreLabel" class="px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-400 font-mono font-bold tabular-nums text-[10px]">
        {{ playerScoreLabel }}
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

          <ExpertAnalysis
            v-if="qbot"
            :qbot-logic="qbot"
            :compact="true"
            :expandable="true"
            :initial-expanded="false"
          />

          <!-- Player Mode section (when player data exists) -->
          <div v-if="playerData" class="space-y-2">
            <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px]">
              {{ $t('qtip.playerMode') }}
            </div>
            <div class="bg-indigo-500/10 rounded-lg p-2.5">
              <div class="flex items-center gap-2 mb-1.5">
                <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-indigo-500/20 text-indigo-400">
                  {{ $t('qbot.archetypes.the_strategist') }}
                </span>
              </div>
              <div class="flex items-center gap-3 mb-2">
                <span class="font-mono font-bold text-2xl tabular-nums text-indigo-300">
                  {{ playerScoreLabel }}
                </span>
                <span class="text-[10px] text-text-muted">
                  {{ (playerData.score_probability * 100).toFixed(1) }}% {{ $t('qtip.playerScore') }}
                </span>
              </div>
              <p class="text-[11px] text-text-secondary leading-relaxed">
                {{ playerReasoning }}
              </p>
            </div>
            <div class="grid grid-cols-2 gap-2">
              <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                <div class="text-text-muted text-[10px]">{{ $t('qtip.playerScore') }}</div>
                <div class="font-mono font-bold text-sm tabular-nums text-indigo-400">
                  {{ (playerData.score_probability * 100).toFixed(1) }}%
                </div>
              </div>
              <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                <div class="text-text-muted text-[10px]">{{ $t('qtip.investorMode') }}</div>
                <div class="font-mono font-bold text-sm tabular-nums" :class="isNoSignal ? 'text-text-muted' : 'text-emerald-400'">
                  {{ isNoSignal ? $t('qtip.playerNoSignal') : edgeLabel }}
                </div>
              </div>
            </div>
          </div>

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

          <!-- Refresh error -->
          <p v-if="refreshError" class="text-rose-400 text-xs">{{ refreshError }}</p>
        </div>
      </div>
    </Transition>

    <!-- ================================================================ -->
    <!-- Admin Metrics Modal                                              -->
    <!-- ================================================================ -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showMetrics && mt"
          class="fixed inset-0 z-50 flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
          @click.self="showMetrics = false"
        >
          <!-- Backdrop -->
          <div class="absolute inset-0 bg-black/70" />

          <!-- Panel -->
          <div class="relative bg-surface-1 rounded-2xl shadow-2xl max-w-lg w-full max-h-[85vh] overflow-y-auto border border-surface-3/50">
            <!-- Header -->
            <div class="sticky top-0 bg-surface-1 border-b border-surface-3/50 px-5 py-4 flex items-center justify-between z-10">
              <div>
                <h2 class="text-sm font-bold text-text-primary">{{ $t('qtip.metricsTitle') }}</h2>
                <p class="text-[11px] text-text-muted mt-0.5">{{ mt.home_team }} vs {{ mt.away_team }}</p>
              </div>
              <button
                class="p-1.5 rounded-lg hover:bg-surface-2 text-text-muted hover:text-text-primary transition-colors"
                @click="showMetrics = false"
              >
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div class="px-5 py-4 space-y-4 text-xs">
              <!-- ── Decision Summary ── -->
              <section>
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">{{ $t('qtip.decision') }}</div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2.5">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.selection') }}</div>
                    <div class="font-bold text-sm mt-0.5" :class="mt.skip_reason ? 'text-text-muted' : 'text-primary'">
                      {{ mt.skip_reason ? '-' : mtPickTeam(mt) }}
                      <span v-if="!mt.skip_reason" class="text-text-muted font-normal text-xs">({{ mt.recommended_selection }})</span>
                    </div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2.5">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.edgeLabel') }}</div>
                    <div class="font-mono font-bold text-sm mt-0.5 tabular-nums" :class="mt.edge_pct > 0 ? 'text-emerald-400' : 'text-text-muted'">
                      {{ mt.edge_pct > 0 ? '+' : '' }}{{ mt.edge_pct.toFixed(1) }}%
                    </div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2.5">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.rawConfidence') }}</div>
                    <div class="font-mono font-bold text-sm mt-0.5 tabular-nums text-text-secondary">
                      {{ mt.raw_confidence != null ? (mt.raw_confidence * 100).toFixed(1) + '%' : '-' }}
                    </div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2.5">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.finalConfidence') }}</div>
                    <div class="font-mono font-bold text-sm mt-0.5 tabular-nums" :class="mt.confidence >= 0.7 ? 'text-emerald-400' : mt.confidence >= 0.5 ? 'text-amber-400' : 'text-text-muted'">
                      {{ (mt.confidence * 100).toFixed(1) }}%
                    </div>
                  </div>
                </div>

                <!-- Model vs Market -->
                <div v-if="mt.true_probability > 0" class="mt-2 bg-surface-2/50 rounded-lg p-2.5 flex items-center justify-between">
                  <span class="text-text-muted text-[10px]">{{ $t('qtip.modelVsMarket') }}</span>
                  <span class="font-mono font-bold tabular-nums text-sm">
                    <span class="text-emerald-400">{{ fmtPct(mt.true_probability, 0) }}</span>
                    <span class="text-text-muted mx-1">vs</span>
                    <span class="text-text-secondary">{{ fmtPct(mt.implied_probability, 0) }}</span>
                  </span>
                </div>

                <!-- Result (resolved tips) -->
                <div v-if="mt.actual_result" class="mt-2 bg-surface-2/50 rounded-lg p-2.5 flex items-center justify-between">
                  <span class="text-text-muted text-[10px]">{{ $t('qtip.result') }}</span>
                  <div class="flex items-center gap-2">
                    <span class="font-mono font-bold text-sm text-text-primary">{{ mt.actual_result }}</span>
                    <span
                      class="px-1.5 py-0.5 rounded-full text-[10px] font-bold"
                      :class="mt.was_correct ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'"
                    >
                      {{ mt.was_correct ? 'CORRECT' : 'WRONG' }}
                    </span>
                  </div>
                </div>

                <!-- Skip reason -->
                <div v-if="mt.skip_reason" class="mt-2 bg-amber-500/10 rounded-lg p-2.5">
                  <span class="text-amber-400 text-[10px] font-semibold uppercase">{{ $t('qtip.skipReason') }}</span>
                  <p class="text-text-secondary mt-1">{{ mt.skip_reason }}</p>
                </div>
              </section>

              <!-- ── xG Performance Signal (in Decision context) ── -->
              <section v-if="mtPickedXg && mtPickedXg.label !== 'no_data' && mt">
                <div
                  class="rounded-lg p-3 flex items-start gap-2.5"
                  :class="mtPickedXg.label === 'underperformer' ? 'bg-emerald-500/10' : mtPickedXg.label === 'overperformer' ? 'bg-amber-500/10' : 'bg-surface-2/50'"
                >
                  <span class="text-lg leading-none mt-0.5">{{ mtPickedXg.label === 'underperformer' ? '\u2191' : mtPickedXg.label === 'overperformer' ? '\u26A0' : '\u2022' }}</span>
                  <div>
                    <div class="font-semibold text-xs" :class="mtPickedXg.label === 'underperformer' ? 'text-emerald-400' : mtPickedXg.label === 'overperformer' ? 'text-amber-400' : 'text-text-secondary'">
                      {{ mtPickedXg.label === 'underperformer' ? $t('qtip.xgUnderperformer') : mtPickedXg.label === 'overperformer' ? $t('qtip.xgOverperformer') : $t('qtip.xgNeutral') }}
                    </div>
                    <p class="text-[11px] text-text-muted mt-0.5">
                      {{ mtPickTeam(mt) }}: {{ fmtNum(mtPickedXg.avg_goals) }} {{ $t('qtip.goalsPerGame') }} vs {{ fmtNum(mtPickedXg.avg_xg ?? 0) }} xG
                      <span class="font-mono tabular-nums" :class="(mtPickedXg.delta ?? 0) > 0 ? 'text-amber-400' : (mtPickedXg.delta ?? 0) < 0 ? 'text-emerald-400' : ''">
                        ({{ (mtPickedXg.delta ?? 0) > 0 ? '+' : '' }}{{ fmtNum(mtPickedXg.delta ?? 0) }})
                      </span>
                      <span class="text-text-muted"> &middot; {{ mtPickedXg.matches_with_xg }}/{{ mtPickedXg.matches_total }} {{ $t('qtip.withXg') }}</span>
                    </p>
                    <p class="text-[10px] text-text-muted mt-1 leading-relaxed">
                      {{ mtPickedXg.label === 'underperformer' ? $t('qtip.xgUnderperformerDesc') : mtPickedXg.label === 'overperformer' ? $t('qtip.xgOverperformerDesc') : $t('qtip.xgNeutralDesc') }}
                    </p>
                  </div>
                </div>
              </section>

              <!-- ── xG Performance (Both Teams Detail) ── -->
              <section v-if="mtXg && (mtXg.home.label !== 'no_data' || mtXg.away.label !== 'no_data') && mt">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  {{ $t('qtip.xgTier') }}
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px] truncate">{{ mt.home_team }}</div>
                    <div v-if="mtXg.home.avg_xg != null" class="mt-1">
                      <div class="flex items-center gap-1.5">
                        <span class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ fmtNum(mtXg.home.avg_goals) }}</span>
                        <span class="text-[10px] text-text-muted">{{ $t('qtip.goals') }}</span>
                        <span class="text-text-muted text-[10px]">vs</span>
                        <span class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ fmtNum(mtXg.home.avg_xg ?? 0) }}</span>
                        <span class="text-[10px] text-text-muted">xG</span>
                      </div>
                      <div class="mt-1 text-[10px]">
                        <span class="font-mono tabular-nums font-bold" :class="(mtXg.home.delta ?? 0) < -0.3 ? 'text-emerald-400' : (mtXg.home.delta ?? 0) > 0.3 ? 'text-amber-400' : 'text-text-muted'">
                          {{ (mtXg.home.delta ?? 0) > 0 ? '+' : '' }}{{ fmtNum(mtXg.home.delta ?? 0) }}
                        </span>
                        <span class="ml-1 px-1 py-0.5 rounded text-[9px] font-semibold" :class="mtXg.home.label === 'underperformer' ? 'bg-emerald-500/20 text-emerald-400' : mtXg.home.label === 'overperformer' ? 'bg-amber-500/20 text-amber-400' : 'bg-surface-3 text-text-muted'">
                          {{ mtXg.home.label === 'underperformer' ? 'VALUE' : mtXg.home.label === 'overperformer' ? 'FADE' : 'OK' }}
                        </span>
                      </div>
                    </div>
                    <div v-else class="text-[10px] text-text-muted mt-1">{{ $t('qtip.noXgData') }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px] truncate">{{ mt.away_team }}</div>
                    <div v-if="mtXg.away.avg_xg != null" class="mt-1">
                      <div class="flex items-center gap-1.5">
                        <span class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ fmtNum(mtXg.away.avg_goals) }}</span>
                        <span class="text-[10px] text-text-muted">{{ $t('qtip.goals') }}</span>
                        <span class="text-text-muted text-[10px]">vs</span>
                        <span class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ fmtNum(mtXg.away.avg_xg ?? 0) }}</span>
                        <span class="text-[10px] text-text-muted">xG</span>
                      </div>
                      <div class="mt-1 text-[10px]">
                        <span class="font-mono tabular-nums font-bold" :class="(mtXg.away.delta ?? 0) < -0.3 ? 'text-emerald-400' : (mtXg.away.delta ?? 0) > 0.3 ? 'text-amber-400' : 'text-text-muted'">
                          {{ (mtXg.away.delta ?? 0) > 0 ? '+' : '' }}{{ fmtNum(mtXg.away.delta ?? 0) }}
                        </span>
                        <span class="ml-1 px-1 py-0.5 rounded text-[9px] font-semibold" :class="mtXg.away.label === 'underperformer' ? 'bg-emerald-500/20 text-emerald-400' : mtXg.away.label === 'overperformer' ? 'bg-amber-500/20 text-amber-400' : 'bg-surface-3 text-text-muted'">
                          {{ mtXg.away.label === 'underperformer' ? 'VALUE' : mtXg.away.label === 'overperformer' ? 'FADE' : 'OK' }}
                        </span>
                      </div>
                    </div>
                    <div v-else class="text-[10px] text-text-muted mt-1">{{ $t('qtip.noXgData') }}</div>
                  </div>
                </div>
              </section>

              <!-- ── Poisson / Model Tier ── -->
              <section v-if="mtPoisson">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-blue-400 mr-1">P</span> {{ $t('qtip.poissonTier') }}
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.lambdaHome') }}</div>
                    <div class="font-mono font-bold text-sm text-text-primary tabular-nums">{{ fmtNum(mtPoisson.lambda_home) }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.lambdaAway') }}</div>
                    <div class="font-mono font-bold text-sm text-text-primary tabular-nums">{{ fmtNum(mtPoisson.lambda_away) }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.h2hWeight') }}</div>
                    <div class="font-mono font-bold text-sm text-text-primary tabular-nums">{{ fmtPct(mtPoisson.h2h_weight) }}</div>
                  </div>
                </div>
                <!-- Probabilities & edges row -->
                <div class="mt-2 flex gap-2">
                  <div
                    v-for="(label, key) in { '1': mt.home_team, 'X': $t('match.draw'), '2': mt.away_team }"
                    :key="key"
                    class="flex-1 bg-surface-2/50 rounded-lg p-2 text-center"
                  >
                    <div class="text-text-muted text-[10px] truncate">{{ label }}</div>
                    <div class="font-mono font-bold tabular-nums" :class="key === mt.recommended_selection ? 'text-emerald-400' : 'text-text-secondary'">
                      {{ fmtPct(mtPoisson.true_probs[key], 0) }}
                    </div>
                    <div class="text-[10px] font-mono" :class="mtPoisson.edges[key] > 0 ? 'text-emerald-400/70' : 'text-text-muted/60'">
                      {{ mtPoisson.edges[key] > 0 ? '+' : '' }}{{ mtPoisson.edges[key].toFixed(1) }}%
                    </div>
                  </div>
                </div>
              </section>

              <!-- ── Momentum Tier ── -->
              <section v-if="mtMomentum">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-orange-400 mr-1">F</span> {{ $t('qtip.momentumTier') }}
                  <span
                    class="ml-2 px-1.5 py-0.5 rounded-full text-[9px] font-bold"
                    :class="mtMomentum.contributes ? 'bg-orange-500/20 text-orange-400' : 'bg-surface-3 text-text-muted'"
                  >{{ mtMomentum.contributes ? 'ACTIVE' : 'INACTIVE' }}</span>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ mt.home_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">
                      {{ fmtNum(mtMomentum.home.momentum_score) }}
                    </div>
                    <div class="text-[10px] text-text-muted">{{ $t('qtip.formPts') }}: {{ fmtNum(mtMomentum.home.form_points, 1) }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ mt.away_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">
                      {{ fmtNum(mtMomentum.away.momentum_score) }}
                    </div>
                    <div class="text-[10px] text-text-muted">{{ $t('qtip.formPts') }}: {{ fmtNum(mtMomentum.away.form_points, 1) }}</div>
                  </div>
                </div>
                <div class="mt-2 bg-surface-2/50 rounded-lg p-2 flex items-center justify-between">
                  <span class="text-text-muted text-[10px]">{{ $t('qtip.momentumGap') }}</span>
                  <span class="font-mono font-bold text-sm tabular-nums" :class="mtMomentum.gap > 0.20 ? 'text-orange-400' : 'text-text-secondary'">
                    {{ fmtPct(mtMomentum.gap) }}
                  </span>
                </div>
              </section>

              <!-- ── Sharp Money Tier ── -->
              <section v-if="mtSharp">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-purple-400 mr-1">S</span> {{ $t('qtip.sharpTier') }}
                  <span
                    class="ml-2 px-1.5 py-0.5 rounded-full text-[9px] font-bold"
                    :class="mtSharp.has_sharp_movement ? 'bg-purple-500/20 text-purple-400' : 'bg-surface-3 text-text-muted'"
                  >{{ mtSharp.has_sharp_movement ? 'DETECTED' : 'NONE' }}</span>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.direction') }}</div>
                    <div class="font-mono font-bold text-sm text-text-primary">{{ mtSharp.direction || '-' }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.maxDrop') }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">-{{ mtSharp.max_drop_pct }}%</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.lateMoney') }}</div>
                    <div class="font-bold text-sm" :class="mtSharp.is_late_money ? 'text-purple-400' : 'text-text-muted'">
                      {{ mtSharp.is_late_money ? 'Yes' : 'No' }}
                    </div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.steamMove') }}</div>
                    <div class="font-bold text-sm" :class="(mtSharp as any).has_steam_move ? 'text-purple-400' : 'text-text-muted'">
                      {{ (mtSharp as any).has_steam_move ? 'Yes' : 'No' }}
                    </div>
                  </div>
                </div>
                <div v-if="(mtSharp as any).has_reversal" class="mt-2 bg-amber-500/10 rounded-lg p-2 text-amber-400 text-[10px] font-semibold">
                  {{ $t('qtip.reversal') }}
                </div>
              </section>

              <!-- ── King's Choice Tier ── -->
              <section v-if="mtKings">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-yellow-400 mr-1">K</span> {{ $t('qtip.kingsTier') }}
                  <span
                    class="ml-2 px-1.5 py-0.5 rounded-full text-[9px] font-bold"
                    :class="mtKings.has_kings_choice ? 'bg-yellow-500/20 text-yellow-400' : 'bg-surface-3 text-text-muted'"
                  >{{ mtKings.has_kings_choice ? 'ACTIVE' : 'INACTIVE' }}</span>
                </div>
                <div class="grid grid-cols-3 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.kingsPick') }}</div>
                    <div class="font-bold text-sm text-text-primary">{{ mtKings.kings_pick || '-' }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.kingsAgreement') }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-yellow-400">{{ fmtPct(mtKings.kings_pct, 0) }}</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.kingsCount') }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ mtKings.kings_who_bet }}/{{ mtKings.total_kings }}</div>
                  </div>
                </div>
                <div v-if="mtKings.is_underdog_pick" class="mt-2 bg-yellow-500/10 rounded-lg p-2 text-yellow-400 text-[10px] font-semibold">
                  {{ $t('qtip.underdogPick') }}
                </div>
              </section>

              <!-- ── BTB / EVD Tier ── -->
              <section v-if="mtBtb">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-teal-400 mr-1">B</span> {{ $t('qtip.btbTier') }}
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px] truncate">{{ mt.home_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums mt-0.5" :class="mtBtb.home.evd > 0.10 ? 'text-teal-400' : mtBtb.home.evd < -0.10 ? 'text-rose-400' : 'text-text-secondary'">
                      {{ fmtSignedPct(mtBtb.home.evd) }}
                    </div>
                    <div class="text-[10px] text-text-muted">BTB {{ mtBtb.home.btb_count }}/{{ mtBtb.home.matches_analyzed }} ({{ fmtPct(mtBtb.home.btb_ratio, 0) }})</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2">
                    <div class="text-text-muted text-[10px] truncate">{{ mt.away_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums mt-0.5" :class="mtBtb.away.evd > 0.10 ? 'text-teal-400' : mtBtb.away.evd < -0.10 ? 'text-rose-400' : 'text-text-secondary'">
                      {{ fmtSignedPct(mtBtb.away.evd) }}
                    </div>
                    <div class="text-[10px] text-text-muted">BTB {{ mtBtb.away.btb_count }}/{{ mtBtb.away.matches_analyzed }} ({{ fmtPct(mtBtb.away.btb_ratio, 0) }})</div>
                  </div>
                </div>
              </section>

              <!-- ── Rest Advantage Tier ── -->
              <section v-if="mtRest">
                <div class="text-text-muted font-semibold uppercase tracking-wider text-[10px] mb-2">
                  <span class="text-cyan-400 mr-1">R</span> {{ $t('qtip.restTier') }}
                  <span
                    class="ml-2 px-1.5 py-0.5 rounded-full text-[9px] font-bold"
                    :class="mtRest.contributes ? 'bg-cyan-500/20 text-cyan-400' : 'bg-surface-3 text-text-muted'"
                  >{{ mtRest.contributes ? 'ACTIVE' : 'INACTIVE' }}</span>
                </div>
                <div class="grid grid-cols-3 gap-2">
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ mt.home_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ mtRest.home_rest_days }}d</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ mt.away_team }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums text-text-primary">{{ mtRest.away_rest_days }}d</div>
                  </div>
                  <div class="bg-surface-2/50 rounded-lg p-2 text-center">
                    <div class="text-text-muted text-[10px]">{{ $t('qtip.restDiff') }}</div>
                    <div class="font-mono font-bold text-sm tabular-nums" :class="Math.abs(mtRest.diff) >= 2 ? 'text-cyan-400' : 'text-text-muted'">
                      {{ mtRest.diff > 0 ? '+' : '' }}{{ mtRest.diff }}d
                    </div>
                  </div>
                </div>
              </section>

              <!-- ── Generated At ── -->
              <div class="text-center text-[10px] text-text-muted pt-2 border-t border-surface-3/30">
                {{ $t('qtip.generatedAt') }}: {{ mt.generated_at }}
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
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

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
