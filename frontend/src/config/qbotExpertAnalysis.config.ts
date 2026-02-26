/**
 * frontend/src/config/qbotExpertAnalysis.config.ts
 *
 * Purpose:
 *     Central mapping and formatting helpers for the ExpertAnalysis UI.
 *     Keeps presentation metadata separate from component rendering logic.
 */

export type ArchetypeKey =
  | "value_oracle"
  | "sharp_hunter"
  | "steam_snatcher"
  | "night_owl"
  | "steady_hand"
  | "unknown";

export type VolatilityKey = "stable" | "volatile" | "extreme" | "unknown";

export const ARCHETYPE_META: Record<ArchetypeKey, { icon: string; labelKey: string; badgeClass: string }> = {
  value_oracle: {
    icon: "🔮",
    labelKey: "qbotExpert.archetypes.value_oracle",
    badgeClass: "bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-400/30",
  },
  sharp_hunter: {
    icon: "🎯",
    labelKey: "qbotExpert.archetypes.sharp_hunter",
    badgeClass: "bg-rose-500/15 text-rose-300 border-rose-400/30",
  },
  steam_snatcher: {
    icon: "🚂",
    labelKey: "qbotExpert.archetypes.steam_snatcher",
    badgeClass: "bg-orange-500/15 text-orange-300 border-orange-400/30",
  },
  night_owl: {
    icon: "🦉",
    labelKey: "qbotExpert.archetypes.night_owl",
    badgeClass: "bg-sky-900/40 text-sky-200 border-sky-600/40",
  },
  steady_hand: {
    icon: "⚖️",
    labelKey: "qbotExpert.archetypes.steady_hand",
    badgeClass: "bg-slate-500/15 text-slate-300 border-slate-400/30",
  },
  unknown: {
    icon: "🧠",
    labelKey: "qbotExpert.archetypes.unknown",
    badgeClass: "bg-surface-3/70 text-text-secondary border-surface-3",
  },
};

export const VOLATILITY_META: Record<VolatilityKey, { labelKey: string; badgeClass: string }> = {
  stable: {
    labelKey: "qbotExpert.volatility.stable",
    badgeClass: "bg-emerald-500/15 text-emerald-300 border-emerald-400/30",
  },
  volatile: {
    labelKey: "qbotExpert.volatility.volatile",
    badgeClass: "bg-amber-500/15 text-amber-300 border-amber-400/30",
  },
  extreme: {
    labelKey: "qbotExpert.volatility.extreme",
    badgeClass: "bg-rose-500/15 text-rose-300 border-rose-400/30",
  },
  unknown: {
    labelKey: "qbotExpert.volatility.unknown",
    badgeClass: "bg-surface-3/70 text-text-secondary border-surface-3",
  },
};

export const REASONING_PARAM_LABELS: Record<string, string> = {
  league: "qbotExpert.reasoningLabels.league",
  signal: "qbotExpert.reasoningLabels.signal",
  generation: "qbotExpert.reasoningLabels.generation",
  confidence: "qbotExpert.reasoningLabels.confidence",
  cluster_win_rate: "qbotExpert.reasoningLabels.cluster_win_rate",
  edge: "qbotExpert.reasoningLabels.edge",
  clv_delta: "qbotExpert.reasoningLabels.clv_delta",
  clv_delta_pct: "qbotExpert.reasoningLabels.clv_delta_pct",
  time: "qbotExpert.reasoningLabels.time",
};

export const POST_MATCH_META: Record<string, { icon: string; titleKey: string; bodyKey: string }> = {
  discipline_collapse: {
    icon: "🟥",
    titleKey: "qbotExpert.postMatch.discipline_collapse.title",
    bodyKey: "qbotExpert.postMatch.discipline_collapse.body",
  },
  siege_failure: {
    icon: "📊",
    titleKey: "qbotExpert.postMatch.siege_failure.title",
    bodyKey: "qbotExpert.postMatch.siege_failure.body",
  },
  home_dominant_lost: {
    icon: "📊",
    titleKey: "qbotExpert.postMatch.siege_failure.title",
    bodyKey: "qbotExpert.postMatch.siege_failure.body",
  },
  disrupted_flow: {
    icon: "⚠️",
    titleKey: "qbotExpert.postMatch.disrupted_flow.title",
    bodyKey: "qbotExpert.postMatch.disrupted_flow.body",
  },
  total_collapse: {
    icon: "🟥🟥",
    titleKey: "qbotExpert.postMatch.total_collapse.title",
    bodyKey: "qbotExpert.postMatch.total_collapse.body",
  },
  clinical_efficiency: {
    icon: "🎯",
    titleKey: "qbotExpert.postMatch.clinical_efficiency.title",
    bodyKey: "qbotExpert.postMatch.clinical_efficiency.body",
  },
  xg_betrayal: {
    icon: "📉",
    titleKey: "qbotExpert.postMatch.xg_betrayal.title",
    bodyKey: "qbotExpert.postMatch.xg_betrayal.body",
  },
};

export const SYNERGY_META = {
  positive: {
    badgeClass: "bg-cyan-500/15 text-cyan-300 border-cyan-400/30",
    labelKey: "qbotExpert.synergy.positive.label",
    tooltipKey: "qbotExpert.synergy.positive.tooltip",
  },
  negative: {
    badgeClass: "bg-rose-500/15 text-rose-300 border-rose-400/30",
    labelKey: "qbotExpert.synergy.negative.label",
    tooltipKey: "qbotExpert.synergy.negative.tooltip",
  },
  neutral: {
    badgeClass: "bg-surface-3/70 text-text-secondary border-surface-3",
    labelKey: "qbotExpert.synergy.neutral.label",
    tooltipKey: "qbotExpert.synergy.neutral.tooltip",
  },
};

export function toTitleCaseLabel(input: string): string {
  return input
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
