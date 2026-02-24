<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { sportLabel } from "@/types/sports";
import { useApi } from "@/composables/useApi";
import { Line } from "vue-chartjs";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend);

const route = useRoute();
const router = useRouter();
const api = useApi();
const { t } = useI18n();

const loading = ref(false);
const error = ref(false);
const detail = ref<any | null>(null);
const backtest = ref<any | null>(null);
const ledger = ref<any[]>([]);
const activatingId = ref<string | null>(null);

const strategyId = computed(() => String(route.params.strategyId || ""));

function identityLabel(key: string): string {
  if (key === "consensus") return t("qbotLab.identityConsensus");
  if (key === "profit_hunter") return t("qbotLab.identityProfitHunter");
  if (key === "volume_grinder") return t("qbotLab.identityVolumeGrinder");
  return key;
}

const identityEntries = computed(() => {
  const identities = detail.value?.identities || {};
  const ordered = ["consensus", "profit_hunter", "volume_grinder"];
  return ordered
    .filter((k) => identities[k])
    .map((k) => ({ key: k, value: identities[k] }));
});

const sortedLedger = computed(() =>
  [...ledger.value].sort(
    (a, b) =>
      new Date(b.date).getTime() - new Date(a.date).getTime(),
  ),
);

const chartData = computed(() => {
  if (!backtest.value?.points?.length) return null;
  return {
    labels: backtest.value.points.map((p: any) => new Date(p.date).toLocaleDateString("de-DE")),
    datasets: [
      {
        label: t("qbotLab.bankrollCurve"),
        data: backtest.value.points.map((p: any) => Number(p.bankroll.toFixed(2))),
        borderColor: "rgb(16, 185, 129)",
        backgroundColor: "rgba(16, 185, 129, 0.15)",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.2,
      },
    ],
  };
});

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {
    y: {
      ticks: { color: "rgba(255,255,255,0.6)" },
      grid: { color: "rgba(255,255,255,0.08)" },
    },
    x: {
      ticks: { color: "rgba(255,255,255,0.45)", maxTicksLimit: 10 },
      grid: { display: false },
    },
  },
};

async function load() {
  if (!strategyId.value) return;
  loading.value = true;
  error.value = false;
  try {
    const detailResp = await api.get<any>(`/admin/qbot/strategies/${encodeURIComponent(strategyId.value)}`);
    const validationStart = detailResp?.optimization_notes?.validation_window?.start_date;
    const sinceParam = validationStart
      ? `since_date=${encodeURIComponent(validationStart)}`
      : "";
    const [backtestResp, ledgerResp] = await Promise.all([
      api.get(`/admin/qbot/strategies/${encodeURIComponent(strategyId.value)}/backtest${sinceParam ? `?${sinceParam}` : ""}`),
      api.get<{ ledger: any[] }>(
        `/admin/qbot/strategies/${encodeURIComponent(strategyId.value)}/backtest/ledger?limit=0${sinceParam ? `&${sinceParam}` : ""}`,
      ),
    ]);
    detail.value = detailResp;
    backtest.value = backtestResp;
    ledger.value = Array.isArray(ledgerResp?.ledger) ? ledgerResp.ledger : [];
  } catch {
    error.value = true;
    detail.value = null;
    backtest.value = null;
    ledger.value = [];
  } finally {
    loading.value = false;
  }
}

async function activateStrategy(id: string) {
  if (!id || activatingId.value) return;
  activatingId.value = id;
  try {
    await api.post(`/admin/qbot/strategies/${encodeURIComponent(id)}/activate`);
    if (id !== strategyId.value) {
      await router.push({ name: "admin-qbot-lab-detail", params: { strategyId: id } });
    }
    await load();
  } finally {
    activatingId.value = null;
  }
}

watch(strategyId, () => {
  void load();
});

function exportLedgerCsv() {
  if (!sortedLedger.value.length) return;
  const rows = [
    [
      "date",
      "match",
      "edge_pct",
      "odds",
      "stake_eur",
      "result",
      "net_profit_eur",
      "bankroll_before",
      "bankroll_after",
      "selection",
      "match_id",
    ],
    ...sortedLedger.value.map((r) => [
      r.date,
      r.match,
      r.edge_pct,
      r.odds,
      r.stake,
      r.result,
      r.net_profit,
      r.bankroll_before,
      r.bankroll_after,
      r.selection,
      r.match_id,
    ]),
  ];
  const csv = rows
    .map((row) =>
      row
        .map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`)
        .join(","),
    )
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `qbot-ledger-${strategyId.value}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

onMounted(load);
</script>

<template>
  <div class="max-w-5xl mx-auto p-4 space-y-4">
    <div class="flex items-center justify-between">
      <h1 class="text-lg font-semibold text-text-primary">{{ t("qbotLab.strategyDetail") }}</h1>
      <router-link
        :to="{ name: 'admin-qbot-lab' }"
        class="text-xs text-text-muted hover:text-text-secondary"
      >
        {{ t("common.back") }}
      </router-link>
    </div>

    <div v-if="loading" class="bg-surface-1 rounded-card p-4 text-sm text-text-muted">
      {{ t("qbotLab.loadingDetail") }}
    </div>

    <div v-else-if="error || !detail" class="bg-surface-1 rounded-card p-4 text-sm text-danger">
      {{ t("qbotLab.detailLoadError") }}
    </div>

    <template v-else>
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <div class="text-xs text-text-muted">{{ sportLabel(detail.sport_key) }}</div>
        <div class="text-sm text-text-primary font-medium mt-1">
          {{ detail.version }} · Gen {{ detail.generation }} · {{ identityLabel(detail.archetype || "standard") }}
        </div>
      </div>

      <div v-if="identityEntries.length" class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div
          v-for="item in identityEntries"
          :key="item.value.id"
          class="bg-surface-1 rounded-card p-3 border border-surface-3/50"
        >
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs text-text-muted">{{ identityLabel(item.key) }}</div>
            <span
              v-if="item.value.is_active"
              class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400"
            >
              {{ t("qbotLab.identityActive") }}
            </span>
          </div>
          <div class="text-sm font-mono tabular-nums text-text-primary">
            ROI {{ (item.value.roi * 100).toFixed(1) }}% · Bets {{ item.value.total_bets }}
          </div>
          <div class="mt-2 flex items-center gap-2">
            <router-link
              v-if="item.value.id !== strategyId"
              :to="{ name: 'admin-qbot-lab-detail', params: { strategyId: item.value.id } }"
              class="text-[11px] text-primary hover:underline"
            >
              {{ t("qbotLab.openDetail") }}
            </router-link>
            <button
              v-if="!item.value.is_active"
              class="px-2 py-1 text-[11px] rounded border border-surface-3 hover:bg-surface-2 text-text-secondary"
              :disabled="activatingId === item.value.id"
              @click="activateStrategy(item.value.id)"
            >
              {{ activatingId === item.value.id ? t("qbotLab.activating") : t("qbotLab.setActive") }}
            </button>
          </div>
        </div>
      </div>

      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <h2 class="text-sm font-semibold text-text-primary mb-3">{{ t("qbotLab.backtest") }}</h2>
        <div v-if="backtest" class="grid grid-cols-1 md:grid-cols-3 gap-2 mb-3 text-xs">
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qbotLab.startBankroll") }}</div>
            <div class="font-mono text-text-primary">1000.00</div>
          </div>
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qbotLab.endBankroll") }}</div>
            <div class="font-mono text-text-primary">{{ Number(backtest.ending_bankroll || 0).toFixed(2) }}</div>
          </div>
          <div class="bg-surface-2 rounded p-2">
            <div class="text-text-muted">{{ t("qbotLab.backtestWinRate") }}</div>
            <div class="font-mono text-text-primary">{{ (Number(backtest.win_rate || 0) * 100).toFixed(1) }}%</div>
          </div>
        </div>
        <div class="h-64">
          <Line v-if="chartData" :data="chartData" :options="chartOptions" />
        </div>
      </div>

      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold text-text-primary">{{ t("qbotLab.betLedger") }}</h2>
          <button
            class="text-xs px-2 py-1 rounded border border-surface-3 text-text-secondary hover:bg-surface-2"
            @click="exportLedgerCsv"
          >
            {{ t("qbotLab.exportCsv") }}
          </button>
        </div>
        <div v-if="sortedLedger.length === 0" class="text-xs text-text-muted">
          {{ t("qbotLab.noLedger") }}
        </div>
        <div v-else class="overflow-x-auto max-h-[420px] overflow-y-auto">
          <table class="w-full text-xs">
            <thead class="sticky top-0 bg-surface-1">
              <tr class="text-left text-text-muted border-b border-surface-3/40">
                <th class="py-2 pr-2">{{ t("qtipPerformance.date") }}</th>
                <th class="py-2 px-2">{{ t("qtipPerformance.match") }}</th>
                <th class="py-2 px-2 text-right">{{ t("qtipPerformance.edge") }}</th>
                <th class="py-2 px-2 text-right">Odds</th>
                <th class="py-2 px-2 text-right">{{ t("qbotLab.stake") }}</th>
                <th class="py-2 px-2 text-right">{{ t("qtipPerformance.actual") }}</th>
                <th class="py-2 pl-2 text-right">{{ t("qbotLab.netProfit") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in sortedLedger"
                :key="`${row.date}-${row.match_id}`"
                class="border-b border-surface-3/20 last:border-0"
              >
                <td class="py-2 pr-2 text-text-muted whitespace-nowrap">
                  {{ new Date(row.date).toLocaleDateString("de-DE") }}
                </td>
                <td class="py-2 px-2 text-text-secondary whitespace-nowrap">
                  {{ row.match }}
                </td>
                <td class="py-2 px-2 text-right font-mono tabular-nums text-text-muted">
                  {{ Number(row.edge_pct).toFixed(1) }}%
                </td>
                <td class="py-2 px-2 text-right font-mono tabular-nums text-text-muted">
                  {{ Number(row.odds).toFixed(2) }}
                </td>
                <td class="py-2 px-2 text-right font-mono tabular-nums text-text-muted">
                  {{ Number(row.stake).toFixed(2) }}€
                </td>
                <td class="py-2 px-2 text-right text-text-muted">
                  {{ row.result === "win" ? "Win" : "Loss" }}
                </td>
                <td
                  class="py-2 pl-2 text-right font-mono tabular-nums"
                  :class="Number(row.net_profit) >= 0 ? 'text-emerald-400' : 'text-danger'"
                >
                  {{ Number(row.net_profit) >= 0 ? "+" : "" }}{{ Number(row.net_profit).toFixed(2) }}€
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>
