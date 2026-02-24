<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

interface DecisionTrace {
  stage_1_engine?: Record<string, any>;
  stage_2_dna_match?: Record<string, any>;
  stage_3_filters?: Record<string, any>;
  stage_4_risk?: Record<string, any>;
  kill_point?: { stage?: number; code?: string; reason?: string } | null;
}

const props = defineProps<{
  trace: DecisionTrace | null | undefined;
}>();

const { t } = useI18n();

const killStage = computed(() => Number(props.trace?.kill_point?.stage || 0));

const stageState = (stage: number, passed: boolean) => {
  if (killStage.value === stage) return "failed";
  return passed ? "passed" : "neutral";
};
</script>

<template>
  <div class="space-y-3">
    <div class="text-xs text-text-muted uppercase tracking-wide">
      {{ t("qtipPerformance.decisionJourney") }}
    </div>

    <div class="space-y-2">
      <div class="rounded-lg border border-surface-3/40 p-3">
        <div class="flex items-center justify-between text-xs">
          <span class="font-semibold text-text-primary">{{ t("qtipPerformance.stage1Engine") }}</span>
          <span class="text-emerald-400">✓</span>
        </div>
        <div class="mt-2 text-[11px] text-text-muted grid grid-cols-2 gap-x-3 gap-y-1">
          <span>{{ t("qtipPerformance.rawEdge") }}</span>
          <span class="text-right font-mono">{{ ((trace?.stage_1_engine?.raw_edge_pct ?? 0) as number).toFixed(2) }}%</span>
          <span>{{ t("qtipPerformance.rawProbability") }}</span>
          <span class="text-right font-mono">{{ (((trace?.stage_1_engine?.raw_probability ?? 0) as number) * 100).toFixed(1) }}%</span>
        </div>
      </div>

      <div class="rounded-lg border border-surface-3/40 p-3">
        <div class="flex items-center justify-between text-xs">
          <span class="font-semibold text-text-primary">{{ t("qtipPerformance.stage2Dna") }}</span>
          <span :class="trace?.stage_2_dna_match?.matched ? 'text-emerald-400' : 'text-danger'">
            {{ trace?.stage_2_dna_match?.matched ? "✓" : "✕" }}
          </span>
        </div>
        <div class="mt-2 text-[11px] text-text-muted">
          {{ trace?.stage_2_dna_match?.strategy_label || "--" }}
        </div>
        <div class="mt-2 flex gap-2 text-[10px]">
          <span class="px-2 py-0.5 rounded bg-surface-2 text-text-muted">
            {{ t("qtipPerformance.strategyState") }}: {{ trace?.stage_2_dna_match?.strategy_state || "--" }}
          </span>
          <span class="px-2 py-0.5 rounded bg-surface-2 text-text-muted">
            {{ t("qtipPerformance.source") }}: {{ trace?.stage_2_dna_match?.source || "--" }}
          </span>
        </div>
      </div>

      <div class="rounded-lg border border-surface-3/40 p-3">
        <div class="flex items-center justify-between text-xs">
          <span class="font-semibold text-text-primary">{{ t("qtipPerformance.stage3Filters") }}</span>
          <span
            :class="stageState(3, !!trace?.stage_3_filters?.overall_passed) === 'failed' ? 'text-danger' : (trace?.stage_3_filters?.overall_passed ? 'text-emerald-400' : 'text-text-muted')"
          >
            {{
              stageState(3, !!trace?.stage_3_filters?.overall_passed) === "failed"
                ? "✕"
                : (trace?.stage_3_filters?.overall_passed ? "✓" : "•")
            }}
          </span>
        </div>
        <div class="mt-2 flex flex-wrap gap-2 text-[10px]">
          <span class="px-2 py-0.5 rounded bg-surface-2 text-text-muted">
            {{ t("qtipPerformance.dnaMinEdge") }}: {{ Number(trace?.stage_3_filters?.min_edge?.required ?? 0).toFixed(2) }}%
          </span>
          <span class="px-2 py-0.5 rounded bg-surface-2 text-text-muted">
            {{ t("qtipPerformance.dnaMinConfidence") }}: {{ (Number(trace?.stage_3_filters?.min_confidence?.required ?? 0) * 100).toFixed(1) }}%
          </span>
          <span class="px-2 py-0.5 rounded bg-surface-2 text-text-muted">
            {{ t("qtipPerformance.dnaDrawGate") }}: {{ (Number(trace?.stage_3_filters?.draw_gate?.draw_threshold ?? 0) * 100).toFixed(1) }}%
          </span>
        </div>
      </div>

      <div class="rounded-lg border border-surface-3/40 p-3">
        <div class="flex items-center justify-between text-xs">
          <span class="font-semibold text-text-primary">{{ t("qtipPerformance.stage4Risk") }}</span>
          <span
            :class="stageState(4, (trace?.stage_4_risk?.final_stake ?? 0) > 0) === 'failed' ? 'text-danger' : ((trace?.stage_4_risk?.final_stake ?? 0) > 0 ? 'text-emerald-400' : 'text-text-muted')"
          >
            {{
              stageState(4, (trace?.stage_4_risk?.final_stake ?? 0) > 0) === "failed"
                ? "✕"
                : ((trace?.stage_4_risk?.final_stake ?? 0) > 0 ? "✓" : "•")
            }}
          </span>
        </div>
        <div class="mt-2 text-[11px] text-text-muted grid grid-cols-2 gap-x-3 gap-y-1">
          <span>{{ t("qtipPerformance.kellyRaw") }}</span>
          <span class="text-right font-mono">{{ Number(trace?.stage_4_risk?.kelly_raw ?? 0).toFixed(4) }}</span>
          <span>{{ t("qtipPerformance.maxStake") }}</span>
          <span class="text-right font-mono">{{ Number(trace?.stage_4_risk?.max_stake ?? 0).toFixed(2) }}</span>
          <span>{{ t("qtipPerformance.finalStake") }}</span>
          <span class="text-right font-mono">{{ Number(trace?.stage_4_risk?.final_stake ?? 0).toFixed(2) }}</span>
        </div>
      </div>
    </div>

    <div v-if="trace?.kill_point" class="rounded-lg border border-danger/40 bg-danger/10 p-3">
      <div class="text-xs font-semibold text-danger">
        {{ t("qtipPerformance.killPoint") }} ({{ t("qtipPerformance.stageLabel") }} {{ trace.kill_point.stage || "?" }})
      </div>
      <div class="mt-1 text-[11px] text-text-secondary">
        {{ trace.kill_point.reason || trace.kill_point.code || "--" }}
      </div>
    </div>
  </div>
</template>
