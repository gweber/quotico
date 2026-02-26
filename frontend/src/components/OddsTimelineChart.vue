<script setup lang="ts">
import { computed } from "vue";
import { Line } from "vue-chartjs";
import {
  Chart as ChartJS,
  LineElement,
  PointElement,
  LinearScale,
  TimeScale,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import "chartjs-adapter-date-fns";
import type { OddsSnapshot } from "@/composables/useOddsTimeline";

ChartJS.register(LineElement, PointElement, LinearScale, TimeScale, Tooltip, Legend, Filler);

const props = withDefaults(
  defineProps<{
    snapshots: OddsSnapshot[];
    compact?: boolean;
    homeTeam?: string;
    awayTeam?: string;
  }>(),
  { compact: false, homeTeam: "Heim", awayTeam: "Auswärts" },
);

const hasDrawOdds = computed(() =>
  props.snapshots.some((s) => Number.isFinite(s.draw)),
);

const chartData = computed(() => {
  const labels = props.snapshots.map((s) => s.timestamp);
  const singlePoint = props.snapshots.length === 1;
  const ptRadius = singlePoint ? 4 : props.compact ? 0 : 2;

  const datasets = [
    {
      label: props.homeTeam,
      data: props.snapshots.map((s) => s.home),
      borderColor: "#22c55e",
      backgroundColor: "rgba(34, 197, 94, 0.1)",
      borderWidth: props.compact ? 1.5 : 2,
      pointRadius: ptRadius,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    },
    ...(hasDrawOdds.value
      ? [
          {
            label: "Unentschieden",
            data: props.snapshots.map((s) => s.draw),
            borderColor: "#f59e0b",
            backgroundColor: "rgba(245, 158, 11, 0.1)",
            borderWidth: props.compact ? 1.5 : 2,
            pointRadius: ptRadius,
            pointHoverRadius: 4,
            tension: 0.3,
            fill: false,
          },
        ]
      : []),
    {
      label: props.awayTeam,
      data: props.snapshots.map((s) => s.away),
      borderColor: "#ef4444",
      backgroundColor: "rgba(239, 68, 68, 0.1)",
      borderWidth: props.compact ? 1.5 : 2,
      pointRadius: ptRadius,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    },
  ];

  return { labels, datasets };
});

const chartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  interaction: {
    mode: "index" as const,
    intersect: false,
  },
  plugins: {
    legend: {
      display: !props.compact,
      labels: {
        color: "#94a3b8",
        boxWidth: 12,
        padding: 8,
        font: { size: 11 },
      },
    },
    tooltip: {
      backgroundColor: "#1e293b",
      titleColor: "#e2e8f0",
      bodyColor: "#cbd5e1",
      borderColor: "#334155",
      borderWidth: 1,
      padding: 8,
      titleFont: { size: 11 },
      bodyFont: { size: 11 },
      callbacks: {
        title: (items: { parsed: { x: number | null } }[]) => {
          if (!items.length || items[0].parsed.x == null) return "";
          const d = new Date(items[0].parsed.x);
          return d.toLocaleString("de-DE", {
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          });
        },
        label: (item: { dataset: { label?: string }; parsed: { y: number | null } }) =>
          `${item.dataset.label}: ${(item.parsed.y ?? 0).toFixed(2)}`,
      },
    },
  },
  scales: {
    x: {
      type: "time" as const,
      time: {
        unit: "hour" as const,
        displayFormats: { hour: "dd.MM HH:mm" },
        tooltipFormat: "dd.MM.yyyy HH:mm",
      },
      grid: { color: "rgba(51, 65, 85, 0.3)" },
      ticks: {
        color: "#64748b",
        font: { size: 10 },
        maxTicksLimit: props.compact ? 3 : 8,
      },
    },
    y: {
      grid: { color: "rgba(51, 65, 85, 0.3)" },
      ticks: {
        color: "#64748b",
        font: { size: 10 },
      },
    },
  },
}));

const chartHeight = computed(() => (props.compact ? "120px" : "240px"));
</script>

<template>
  <div v-if="snapshots.length >= 1" :style="{ height: chartHeight }">
    <Line :data="chartData" :options="chartOptions" />
  </div>
  <div v-else class="text-xs text-text-muted py-2 text-center">
    Noch keine Quotenentwicklung verfügbar
  </div>
</template>
