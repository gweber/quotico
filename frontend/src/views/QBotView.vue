<script setup lang="ts">
import { onMounted, computed } from "vue";
import { useI18n } from "vue-i18n";
import { useQBot, type QBotBet, type QBotCandidate } from "@/composables/useQBot";
import { sportLabel } from "@/types/sports";
import { Line, Bar } from "vue-chartjs";
import {
  Chart as ChartJS,
  LineElement,
  BarElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";

ChartJS.register(
  LineElement,
  BarElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Tooltip,
  Legend,
  Filler,
);

const { t, locale } = useI18n();
const { data, loading, error, fetch: fetchDashboard } = useQBot();

onMounted(() => fetchDashboard());

const hero = computed(() => data.value?.hero);

const localeTag = computed(() => (locale.value === "en" ? "en-US" : "de-DE"));

// Streak badge
const streakText = computed(() => {
  const s = hero.value?.streak;
  if (!s?.type || !s.count) return null;
  return `${s.count}${s.type === "won" ? "S" : "N"}`;
});
const streakColor = computed(() => {
  return hero.value?.streak?.type === "won"
    ? "bg-emerald-500/20 text-emerald-400"
    : "bg-red-500/20 text-red-400";
});

// Rank badge color
const rankColor = computed(() => {
  const r = hero.value?.rank;
  if (r === 1) return "text-amber-400";
  if (r === 2) return "text-slate-300";
  if (r === 3) return "text-amber-600";
  return "text-text-primary";
});

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleString(localeTag.value, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function selectionLabel(tip: QBotBet): string {
  if (tip.selection === "1") return tip.home_team || t('match.home');
  if (tip.selection === "2") return tip.away_team || t('match.away');
  if (tip.selection === "X") return t('match.draw');
  return tip.selection;
}

// Candidate helpers
function candidatePickLabel(c: QBotCandidate): string {
  if (c.recommended_selection === "1") return c.home_team || t('match.home');
  if (c.recommended_selection === "2") return c.away_team || t('match.away');
  if (c.recommended_selection === "X") return t('match.draw');
  return c.recommended_selection;
}

function confidenceDotClass(confidence: number): string {
  if (confidence >= 0.80) return "bg-emerald-500";
  if (confidence >= 0.65) return "bg-blue-500";
  return "bg-slate-400";
}

function candidateJustification(c: QBotCandidate): string {
  const parts: string[] = [];
  if (c.true_probability > 0) {
    parts.push(t('qbot.justModel', {
      truePct: (c.true_probability * 100).toFixed(0),
      impliedPct: (c.implied_probability * 100).toFixed(0),
      edge: c.edge_pct.toFixed(1),
    }));
  }
  if (c.signals.h2h_meetings != null) {
    parts.push(t('qbot.justH2h', { count: c.signals.h2h_meetings }));
  }
  if (c.signals.momentum_gap != null && c.signals.momentum_gap > 0.15) {
    parts.push(t('qbot.justMomentum'));
  }
  if (c.signals.sharp_movement) {
    parts.push(t('qbot.justSharp'));
  }
  // Fallback for 2-way sports (no Poisson model)
  if (parts.length === 0 && c.signals.momentum_gap != null) {
    parts.push(t('qbot.justFormBased', { gap: (c.signals.momentum_gap * 100).toFixed(0) }));
  }
  return parts.join(" ");
}

// Win rate trend chart
const trendChartData = computed(() => {
  const trend = data.value?.win_rate_trend ?? [];
  return {
    labels: trend.map((t) => `#${t.bet_number}`),
    datasets: [
      {
        label: t('qbot.trendLabel'),
        data: trend.map((t) => +(t.win_rate * 100).toFixed(1)),
        borderColor: "rgb(16, 185, 129)",
        backgroundColor: "rgba(16, 185, 129, 0.1)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        pointHitRadius: 8,
        borderWidth: 2,
      },
    ],
  };
});

const trendChartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      callbacks: {
        label: (item: { parsed: { y: number | null } }) =>
          `${(item.parsed.y ?? 0).toFixed(1)}%`,
      },
    },
  },
  scales: {
    x: {
      grid: { color: "rgba(51,65,85,0.3)" },
      ticks: { color: "rgba(148,163,184,0.6)", maxTicksLimit: 8 },
    },
    y: {
      min: 30,
      max: 80,
      grid: { color: "rgba(51,65,85,0.3)" },
      ticks: {
        color: "rgba(148,163,184,0.6)",
        callback: (value: number | string) => `${value}%`,
      },
    },
  },
}));

// Calibration chart
const calibrationChartData = computed(() => {
  const cal = data.value?.calibration ?? [];
  return {
    labels: cal.map((c) => c.bucket),
    datasets: [
      {
        label: t('qbot.expectedConfidence'),
        data: cal.map((c) => +(c.avg_confidence * 100).toFixed(1)),
        backgroundColor: "rgba(59, 130, 246, 0.7)",
        borderRadius: 4,
      },
      {
        label: t('qbot.actualRate'),
        data: cal.map((c) => +(c.win_rate * 100).toFixed(1)),
        backgroundColor: "rgba(16, 185, 129, 0.7)",
        borderRadius: 4,
      },
    ],
  };
});

const calibrationChartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      labels: { color: "rgba(148,163,184,0.8)", boxWidth: 12, padding: 12 },
    },
    tooltip: {
      callbacks: {
        label: (item: { dataset: { label?: string }; parsed: { y: number | null } }) =>
          `${item.dataset.label}: ${(item.parsed.y ?? 0).toFixed(1)}%`,
      },
    },
  },
  scales: {
    x: {
      grid: { display: false },
      ticks: { color: "rgba(148,163,184,0.6)" },
    },
    y: {
      min: 40,
      max: 90,
      grid: { color: "rgba(51,65,85,0.3)" },
      ticks: {
        color: "rgba(148,163,184,0.6)",
        callback: (value: number | string) => `${value}%`,
      },
    },
  },
}));
</script>

<template>
  <div class="max-w-3xl mx-auto px-4 py-6 space-y-4">
    <!-- Header -->
    <div>
      <h1 class="text-xl font-bold text-text-primary">{{ $t('qbot.title') }}</h1>
      <p class="text-sm text-text-muted mt-0.5">
        {{ $t('qbot.subtitle') }}
      </p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="text-center py-12">
      <div
        class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto"
      />
      <p class="text-text-muted text-sm mt-3">{{ $t('qbot.loading') }}</p>
    </div>

    <!-- Error -->
    <div
      v-else-if="error"
      class="bg-surface-1 rounded-card p-6 border border-surface-3/50 text-center"
    >
      <p class="text-text-muted">{{ $t('qbot.loadError') }}</p>
      <button
        class="mt-3 text-sm text-primary hover:underline"
        @click="fetchDashboard()"
      >
        {{ $t('qbot.retry') }}
      </button>
    </div>

    <template v-else-if="data && hero">
      <!-- 1. Hero Stats -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <!-- Total tips -->
        <div
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
        >
          <div class="text-2xl font-bold text-text-primary tabular-nums">
            {{ hero.total_bets }}
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t('qbot.totalBets') }}</div>
        </div>

        <!-- Win rate + streak -->
        <div
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
        >
          <div class="flex items-baseline gap-2">
            <span class="text-2xl font-bold text-text-primary tabular-nums">
              {{ (hero.win_rate * 100).toFixed(1) }}%
            </span>
            <span
              v-if="streakText"
              class="text-xs font-bold px-1.5 py-0.5 rounded-full"
              :class="streakColor"
            >
              {{ streakText }}
            </span>
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t('qbot.winRate') }}</div>
        </div>

        <!-- Total points -->
        <div
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
        >
          <div
            class="text-2xl font-bold text-text-primary font-mono tabular-nums"
          >
            {{ hero.total_points.toFixed(1) }}
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t('qbot.totalPoints') }}</div>
        </div>

        <!-- Rank -->
        <div
          class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
        >
          <div class="text-2xl font-bold tabular-nums" :class="rankColor">
            #{{ hero.rank }}
          </div>
          <div class="text-xs text-text-muted mt-1">{{ $t('qbot.rank') }}</div>
        </div>
      </div>

      <!-- 2. Won / Lost bar -->
      <div
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <div class="flex items-center justify-between text-xs text-text-muted mb-2">
          <span>{{ t('qbot.wins', { count: hero.won }) }}</span>
          <span>{{ t('qbot.losses', { count: hero.lost }) }}</span>
        </div>
        <div class="h-2 rounded-full bg-surface-3 overflow-hidden flex">
          <div
            class="bg-emerald-500 transition-all duration-500"
            :style="{ width: hero.total_bets ? `${(hero.won / (hero.won + hero.lost)) * 100}%` : '0%' }"
          />
        </div>
      </div>

      <!-- 3. Active Tips -->
      <div
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.activeBets') }}
          <span
            v-if="data.active_bets.length"
            class="text-xs font-normal text-text-muted ml-1"
          >
            ({{ data.active_bets.length }})
          </span>
        </h2>

        <div v-if="!data.active_bets.length" class="text-sm text-text-muted text-center py-4">
          {{ $t('qbot.noActiveBets') }}
        </div>

        <div v-else class="space-y-2">
          <div
            v-for="tip in data.active_bets"
            :key="tip.match_id"
            class="flex items-center gap-3 py-2 border-b border-surface-3/30 last:border-0 text-sm"
          >
            <span
              class="shrink-0 w-2 h-2 rounded-full bg-amber-400"
              :title="$t('qbot.pending')"
            />
            <div class="flex-1 min-w-0">
              <div class="text-text-primary truncate">
                {{ tip.home_team }} – {{ tip.away_team }}
              </div>
              <div class="text-xs text-text-muted flex items-center gap-2 mt-0.5">
                <span>{{ sportLabel(String(tip.league_id)) }}</span>
                <span>{{ formatDate(tip.match_date) }}</span>
              </div>
            </div>
            <div class="text-right shrink-0">
              <div class="text-xs font-medium text-text-secondary">
                {{ selectionLabel(tip) }}
              </div>
              <div class="text-xs text-text-muted font-mono tabular-nums">
                {{ tip.locked_odds.toFixed(2) }}
              </div>
            </div>
            <div
              v-if="tip.confidence"
              class="shrink-0 text-xs px-1.5 py-0.5 rounded bg-primary/10 text-primary font-medium tabular-nums"
            >
              {{ (tip.confidence * 100).toFixed(0) }}%
            </div>
          </div>
        </div>
      </div>

      <!-- 4. EV Candidates -->
      <div
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.candidates') }}
          <span
            v-if="data.candidates.length"
            class="text-xs font-normal text-text-muted ml-1"
          >
            ({{ data.candidates.length }})
          </span>
        </h2>

        <div v-if="!data.candidates.length" class="text-sm text-text-muted text-center py-4">
          {{ $t('qbot.noCandidates') }}
        </div>

        <div v-else class="space-y-2">
          <div
            v-for="c in data.candidates"
            :key="c.match_id"
            class="py-2 border-b border-surface-3/30 last:border-0 text-sm"
          >
            <!-- Main row -->
            <div class="flex items-center gap-3">
              <span
                class="shrink-0 w-2 h-2 rounded-full"
                :class="confidenceDotClass(c.confidence)"
                :title="`${(c.confidence * 100).toFixed(0)}%`"
              />
              <div class="flex-1 min-w-0">
                <div class="text-text-primary truncate">
                  {{ c.home_team }} – {{ c.away_team }}
                </div>
                <div class="text-xs text-text-muted flex items-center gap-2 mt-0.5">
                  <span>{{ sportLabel(String(c.league_id)) }}</span>
                  <span>{{ formatDate(c.match_date) }}</span>
                </div>
              </div>
              <div class="text-right shrink-0 space-y-0.5">
                <div class="text-xs font-medium text-text-secondary">
                  {{ candidatePickLabel(c) }}
                </div>
                <div class="text-xs text-text-muted font-mono tabular-nums">
                  {{ $t('qbot.edge') }}: +{{ c.edge_pct.toFixed(1) }}%
                </div>
              </div>
              <div
                class="shrink-0 text-xs px-1.5 py-0.5 rounded font-medium tabular-nums"
                :class="c.confidence >= 0.80 ? 'bg-emerald-500/10 text-emerald-400' : c.confidence >= 0.65 ? 'bg-blue-500/10 text-blue-400' : 'bg-slate-500/10 text-slate-400'"
              >
                {{ (c.confidence * 100).toFixed(0) }}%
              </div>
            </div>

            <!-- Signal pills -->
            <div
              v-if="c.signals.h2h_meetings != null || c.signals.sharp_movement || (c.signals.momentum_gap != null && c.signals.momentum_gap > 0.15)"
              class="flex items-center gap-1.5 mt-1.5 ml-5"
            >
              <span
                v-if="c.signals.h2h_meetings != null"
                class="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-3/50 text-text-muted"
              >
                H2H: {{ t('qbot.h2hMeetings', { count: c.signals.h2h_meetings }) }}
              </span>
              <span
                v-if="c.signals.sharp_movement"
                class="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-400"
              >
                {{ $t('qbot.sharpMove') }}
              </span>
              <span
                v-if="c.signals.momentum_gap != null && c.signals.momentum_gap > 0.15"
                class="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-3/50 text-text-muted"
              >
                Momentum: {{ c.signals.momentum_gap.toFixed(2) }}
              </span>
            </div>

            <!-- Justification -->
            <p class="text-[10px] text-text-muted mt-1 ml-5">
              {{ candidateJustification(c) }}
            </p>
          </div>
        </div>
      </div>

      <!-- 5. Recent Tips -->
      <div
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.recentBets') }}
        </h2>

        <div v-if="!data.recent_bets.length" class="text-sm text-text-muted text-center py-4">
          {{ $t('qbot.noCompletedBets') }}
        </div>

        <div v-else class="max-h-[500px] overflow-y-auto space-y-1">
          <div
            v-for="tip in data.recent_bets"
            :key="tip.match_id + tip.created_at"
            class="flex items-center gap-3 py-2 border-b border-surface-3/30 last:border-0 text-sm"
          >
            <span
              class="shrink-0 w-2 h-2 rounded-full"
              :class="tip.status === 'won' ? 'bg-emerald-500' : 'bg-red-500'"
              :title="tip.status === 'won' ? $t('qbot.won') : $t('qbot.lost')"
            />
            <div class="flex-1 min-w-0">
              <div class="text-text-primary truncate">
                {{ tip.home_team }} – {{ tip.away_team }}
              </div>
              <div class="text-xs text-text-muted flex items-center gap-2 mt-0.5">
                <span>{{ sportLabel(String(tip.league_id)) }}</span>
                <span>{{ formatDate(tip.match_date) }}</span>
              </div>
            </div>
            <div class="text-right shrink-0 space-y-0.5">
              <div class="text-xs font-medium text-text-secondary">
                {{ selectionLabel(tip) }}
                <span class="font-mono text-text-muted tabular-nums ml-1">
                  {{ tip.locked_odds.toFixed(2) }}
                </span>
              </div>
              <div
                class="text-xs font-bold tabular-nums"
                :class="tip.status === 'won' ? 'text-emerald-400' : 'text-red-400'"
              >
                {{ tip.points_earned !== null ? `${tip.points_earned > 0 ? '+' : ''}${tip.points_earned.toFixed(1)}P` : '' }}
              </div>
            </div>
            <div
              v-if="tip.confidence"
              class="shrink-0 text-[10px] text-text-muted tabular-nums"
            >
              {{ (tip.confidence * 100).toFixed(0) }}%
            </div>
          </div>
        </div>
      </div>

      <!-- 6. Performance by Sport -->
      <div
        v-if="data.by_sport.length"
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.bySport') }}
        </h2>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-xs text-text-muted border-b border-surface-3/30">
                <th class="text-left py-2 font-medium">{{ $t('qbot.league') }}</th>
                <th class="text-right py-2 font-medium">{{ $t('qbot.bets') }}</th>
                <th class="text-right py-2 font-medium">{{ $t('qbot.winsCol') }}</th>
                <th class="text-right py-2 font-medium">{{ $t('qbot.rate') }}</th>
                <th class="text-right py-2 font-medium">{{ $t('qbot.points') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="sp in data.by_sport"
                :key="sp.league_id"
                class="border-b border-surface-3/20 last:border-0"
              >
                <td class="py-2 text-text-secondary">
                  {{ sportLabel(String(sp.league_id)) }}
                </td>
                <td class="py-2 text-right text-text-primary tabular-nums">
                  {{ sp.total }}
                </td>
                <td class="py-2 text-right text-emerald-400 tabular-nums">
                  {{ sp.won }}
                </td>
                <td class="py-2 text-right text-text-primary tabular-nums">
                  {{ (sp.win_rate * 100).toFixed(1) }}%
                </td>
                <td class="py-2 text-right font-mono text-text-primary tabular-nums">
                  {{ sp.total_points.toFixed(1) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 7. Win Rate Trend -->
      <div
        v-if="data.win_rate_trend.length"
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.winRateTrend') }}
        </h2>
        <div class="h-[200px]">
          <Line :data="trendChartData" :options="(trendChartOptions as any)" />
        </div>
        <p class="text-[10px] text-text-muted mt-2 text-center">
          {{ $t('qbot.trendFootnote') }}
        </p>
      </div>

      <!-- 8. Calibration -->
      <div
        v-if="data.calibration.length"
        class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
      >
        <h2 class="text-sm font-semibold text-text-primary mb-3">
          {{ $t('qbot.calibration') }}
        </h2>
        <div class="h-[200px]">
          <Bar
            :data="calibrationChartData"
            :options="(calibrationChartOptions as any)"
          />
        </div>
        <p class="text-[10px] text-text-muted mt-2 text-center">
          {{ $t('qbot.calibrationFootnote') }}
        </p>
      </div>
    </template>
  </div>
</template>
