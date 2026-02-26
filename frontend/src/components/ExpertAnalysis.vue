<!--
frontend/src/components/ExpertAnalysis.vue

Purpose:
    Renders Qbot Expert Analysis in compact and expanded modes.
    Uses centralized mapping metadata from qbotExpertAnalysis.config.ts.
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { QbotLogic } from "@/composables/useQuoticoTip";
import {
  ARCHETYPE_META,
  POST_MATCH_META,
  REASONING_PARAM_LABELS,
  SYNERGY_META,
  VOLATILITY_META,
  toTitleCaseLabel,
} from "@/config/qbotExpertAnalysis.config";

const props = withDefaults(
  defineProps<{
    qbotLogic: QbotLogic;
    compact?: boolean;
    expandable?: boolean;
    initialExpanded?: boolean;
  }>(),
  {
    compact: false,
    expandable: true,
    initialExpanded: false,
  },
);

const { t } = useI18n();
const isExpanded = ref(props.initialExpanded);

const archetypeMeta = computed(() => {
  const key = props.qbotLogic.archetype as keyof typeof ARCHETYPE_META | undefined;
  return (key && ARCHETYPE_META[key]) || ARCHETYPE_META.unknown;
});

const confidencePct = computed(() => {
  const raw = props.qbotLogic.bayesian_confidence;
  if (typeof raw !== "number") return 0;
  return Math.max(0, Math.min(100, Math.round(raw * 100)));
});

const marketCtx = computed(() => props.qbotLogic.market_context ?? {});
const volatilityMeta = computed(() => {
  const raw = marketCtx.value.volatility_dim;
  const key = raw === "stable" || raw === "volatile" || raw === "extreme" ? raw : "unknown";
  return VOLATILITY_META[key];
});

const trustFactor = computed(() => {
  const raw = props.qbotLogic.market_trust_factor;
  if (typeof raw !== "number") return 0;
  return Math.max(0, Math.min(1, raw));
});

const synergyState = computed<"positive" | "negative" | "neutral">(() => {
  const raw = props.qbotLogic.market_synergy_factor;
  if (typeof raw !== "number") return "neutral";
  if (raw > 1.0) return "positive";
  if (raw < 1.0) return "negative";
  return "neutral";
});

const showSynergy = computed(() => synergyState.value !== "neutral");
const synergyMeta = computed(() => SYNERGY_META[synergyState.value]);

const reasoningEntries = computed(() => {
  const params = props.qbotLogic.reasoning_params ?? {};
  return Object.entries(params).map(([key, value]) => {
    const i18nKey = REASONING_PARAM_LABELS[key];
    const label = i18nKey ? t(i18nKey) : toTitleCaseLabel(key);
    return {
      key,
      label,
      value: String(value),
    };
  });
});

const postMatch = computed(() => props.qbotLogic.post_match_reasoning ?? null);
const postMatchMeta = computed(() => {
  if (!postMatch.value?.type) return null;
  return POST_MATCH_META[postMatch.value.type] || null;
});
const redCardsLabel = computed(() => {
  const cards = postMatch.value?.red_cards;
  if (typeof cards !== "number" || cards <= 0) return "";
  return `${cards}x 🟥`;
});
const showEfficiencyBadge = computed(() => {
  return postMatch.value?.type === "clinical_efficiency" || postMatch.value?.efficient_team != null;
});

const xgHome = computed(() => {
  const raw = postMatch.value?.xg_home;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
});
const xgAway = computed(() => {
  const raw = postMatch.value?.xg_away;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
});
const hasXgComparison = computed(() => {
  return xgHome.value !== null && xgAway.value !== null && (xgHome.value > 0 || xgAway.value > 0);
});
const xgMax = computed(() => {
  if (!hasXgComparison.value) return 1;
  return Math.max(xgHome.value || 0, xgAway.value || 0, 0.1);
});
const xgHomeWidth = computed(() => `${Math.max(4, ((xgHome.value || 0) / xgMax.value) * 100)}%`);
const xgAwayWidth = computed(() => `${Math.max(4, ((xgAway.value || 0) / xgMax.value) * 100)}%`);

const temporalLabel = computed(() => {
  if (props.qbotLogic.is_midweek === true) return t("qbotExpert.temporal.midweek");
  if (props.qbotLogic.is_weekend === true) return t("qbotExpert.temporal.weekend");
  return "";
});

const showFull = computed(() => !props.compact || isExpanded.value);

function toggleExpanded(): void {
  if (!props.expandable) return;
  isExpanded.value = !isExpanded.value;
}
</script>

<template>
  <section class="rounded-xl border border-surface-3/60 bg-surface-2/40 backdrop-blur-sm p-3 space-y-3">
    <div class="flex items-center justify-between gap-3">
      <div class="min-w-0 flex items-center gap-2">
        <span class="text-base leading-none">{{ archetypeMeta.icon }}</span>
        <span class="truncate text-sm font-semibold text-text-primary">
          {{ t(archetypeMeta.labelKey) }}
        </span>
      </div>
      <div class="shrink-0 min-w-[96px] text-right">
        <div class="text-[10px] text-text-muted">{{ $t("qbotExpert.confidenceLabel", { pct: confidencePct }) }}</div>
        <div class="mt-1 h-1.5 rounded-full bg-surface-3 overflow-hidden">
          <div
            class="h-full rounded-full bg-primary transition-all duration-300"
            :style="{ width: `${confidencePct}%` }"
          />
        </div>
      </div>
    </div>

    <div
      v-if="props.compact"
      class="flex flex-wrap items-center gap-2"
    >
      <span
        class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium"
        :class="volatilityMeta.badgeClass"
      >
        {{ t(volatilityMeta.labelKey) }}
      </span>
      <span
        v-if="showSynergy"
        class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium"
        :class="synergyMeta.badgeClass"
        :title="t(synergyMeta.tooltipKey)"
      >
        {{ t(synergyMeta.labelKey) }}
      </span>
      <span v-if="temporalLabel" class="text-[10px] text-text-muted">{{ temporalLabel }}</span>
    </div>

    <div v-if="props.compact && props.expandable" class="flex justify-end">
      <button
        class="inline-flex items-center gap-1 text-[10px] text-primary hover:text-primary/80 transition-colors"
        type="button"
        @click="toggleExpanded"
      >
        <span>{{ isExpanded ? $t("qbotExpert.actions.collapse") : $t("qbotExpert.actions.expand") }}</span>
        <span class="transition-transform duration-200" :class="{ 'rotate-180': isExpanded }">⌄</span>
      </button>
    </div>

    <div v-if="showFull" class="space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <span
          class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium"
          :class="archetypeMeta.badgeClass"
        >
          {{ t(archetypeMeta.labelKey) }}
        </span>
        <span
          class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium"
          :class="volatilityMeta.badgeClass"
        >
          {{ t(volatilityMeta.labelKey) }}
        </span>
        <span
          v-if="showSynergy"
          class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium"
          :class="synergyMeta.badgeClass"
          :title="t(synergyMeta.tooltipKey)"
        >
          {{ t(synergyMeta.labelKey) }}
        </span>
      </div>

      <div class="space-y-1">
        <div class="text-[10px] uppercase tracking-wide text-text-muted">{{ $t("qbotExpert.marketTrust") }}</div>
        <div class="grid grid-cols-3 gap-1">
          <div
            v-for="idx in 3"
            :key="idx"
            class="h-1.5 rounded-full"
            :class="trustFactor >= idx / 3 ? 'bg-primary' : 'bg-surface-3'"
          />
        </div>
      </div>

      <div v-if="reasoningEntries.length > 0" class="space-y-1">
        <div class="text-[10px] uppercase tracking-wide text-text-muted">{{ $t("qbotExpert.reasoningTitle") }}</div>
        <div class="flex flex-wrap gap-1.5">
          <span
            v-for="entry in reasoningEntries"
            :key="entry.key"
            class="inline-flex items-center rounded-full border border-surface-3 bg-surface-2 px-2 py-0.5 text-[10px] text-text-secondary"
          >
            {{ entry.label }}: {{ entry.value }}
          </span>
        </div>
      </div>

      <div v-if="temporalLabel" class="text-[11px] text-text-muted">
        {{ temporalLabel }}
      </div>

      <div
        v-if="postMatch && postMatchMeta"
        class="rounded-lg border border-surface-3 bg-surface-2/80 px-3 py-2 space-y-1"
      >
        <div class="flex items-center gap-2 text-xs font-semibold text-text-primary">
          <span>{{ postMatchMeta.icon }}</span>
          <span>{{ $t(postMatchMeta.titleKey) }}</span>
          <span
            v-if="showEfficiencyBadge"
            class="ml-auto inline-flex items-center rounded-full border border-emerald-400/30 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300"
          >
            🎯 {{ $t("qbotExpert.badges.clinicalEfficiency") }}
          </span>
          <span v-if="redCardsLabel" :class="showEfficiencyBadge ? 'text-[11px] text-rose-300' : 'ml-auto text-[11px] text-rose-300'">{{ redCardsLabel }}</span>
        </div>
        <p class="text-[11px] text-text-secondary">
          {{ $t(postMatchMeta.bodyKey) }}
        </p>
        <div v-if="hasXgComparison" class="space-y-1.5 pt-1">
          <div class="text-[10px] uppercase tracking-wide text-text-muted">{{ $t("qbotExpert.xgComparison") }}</div>
          <div class="space-y-1">
            <div class="flex items-center gap-2">
              <span class="w-14 text-[10px] text-text-muted">{{ $t("qbotExpert.xg.home") }}</span>
              <div class="h-1.5 flex-1 rounded-full bg-surface-3/80 overflow-hidden">
                <div class="h-full rounded-full bg-emerald-400/80" :style="{ width: xgHomeWidth }" />
              </div>
              <span class="w-10 text-right text-[10px] font-mono text-text-secondary">{{ (xgHome || 0).toFixed(1) }}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="w-14 text-[10px] text-text-muted">{{ $t("qbotExpert.xg.away") }}</span>
              <div class="h-1.5 flex-1 rounded-full bg-surface-3/80 overflow-hidden">
                <div class="h-full rounded-full bg-sky-400/80" :style="{ width: xgAwayWidth }" />
              </div>
              <span class="w-10 text-right text-[10px] font-mono text-text-secondary">{{ (xgAway || 0).toFixed(1) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
