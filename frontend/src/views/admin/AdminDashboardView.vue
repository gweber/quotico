<!--
frontend/src/views/admin/AdminDashboardView.vue

Purpose:
    Main admin overview with module cards and compatibility health indicators.
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { HttpError, useApi } from "@/composables/useApi";
import { useI18n } from "vue-i18n";

const api = useApi();
const { t } = useI18n();

interface Stats {
  users: { total: number; banned: number };
  bets: { total: number; today: number };
  matches_v3: {
    total: number;
    scheduled: number;
    live: number;
    finished: number;
    postponed: number;
    canceled: number;
    walkover: number;
    with_xg: number;
    with_odds: number;
    last_match_update: string | null;
  };
  ingest_jobs: { queued: number; running: number; paused: number; failed: number; succeeded: number };
  sportmonks_api: { remaining: number | null; reset_at: string | null; reserve_credits: number };
  generated_at: string;
}

const stats = ref<Stats | null>(null);
const loading = ref(true);
const error = ref(false);
let pollTimer: ReturnType<typeof setInterval> | null = null;
const healthChecking = ref(false);

type ModuleStatus = "active" | "deprecated";
type ModuleHealth = "ok" | "missing" | "error";
interface ModuleCard {
  key: string;
  to: string;
  labelKey: string;
  icon: string;
  status: ModuleStatus;
  reasonKey?: string;
  healthEndpoint: string;
  health: ModuleHealth;
}

async function fetchStats() {
  loading.value = true;
  error.value = false;
  try {
    stats.value = await api.get<Stats>("/admin/ingest/overview/stats");
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

const isStale = computed(() => {
  const updatedAt = stats.value?.matches_v3.last_match_update;
  if (!updatedAt) return false;
  const ageMs = Date.now() - new Date(updatedAt).getTime();
  return ageMs > (24 * 60 * 60 * 1000);
});

const cards = ref<ModuleCard[]>([
  {
    key: "dataAudit",
    to: "/admin/data-audit",
    labelKey: "admin.dashboard.modules.dataAudit",
    icon: "🧪",
    status: "active",
    healthEndpoint: "/admin/ingest/discovery",
    health: "ok",
  },
  {
    key: "users",
    to: "/admin/users",
    labelKey: "admin.dashboard.modules.users",
    icon: "👥",
    status: "active",
    healthEndpoint: "/admin/users",
    health: "ok",
  },
  {
    key: "matches",
    to: "/admin/matches",
    labelKey: "admin.dashboard.modules.matches",
    icon: "⚽",
    status: "active",
    healthEndpoint: "/admin/matches",
    health: "ok",
  },
  {
    key: "battles",
    to: "/admin/battles",
    labelKey: "admin.dashboard.modules.battles",
    icon: "⚔️",
    status: "active",
    healthEndpoint: "/admin/squads",
    health: "ok",
  },
  {
    key: "audit",
    to: "/admin/audit",
    labelKey: "admin.dashboard.modules.audit",
    icon: "📜",
    status: "active",
    healthEndpoint: "/admin/audit-logs/actions",
    health: "ok",
  },
  {
    key: "eventBus",
    to: "/admin/event-bus",
    labelKey: "admin.dashboard.modules.eventBus",
    icon: "🚚",
    status: "active",
    healthEndpoint: "/admin/event-bus/status",
    health: "ok",
  },
  {
    key: "justice",
    to: "/admin/time-machine-justice",
    labelKey: "admin.dashboard.modules.timeMachineJustice",
    icon: "⏲️",
    status: "active",
    healthEndpoint: "/admin/time-machine/justice",
    health: "ok",
  },
  {
    key: "teamTower",
    to: "/admin/team-tower",
    labelKey: "admin.dashboard.modules.teamTower",
    icon: "🎯",
    status: "active",
    healthEndpoint: "/admin/teams-v3",
    health: "ok",
  },
  {
    key: "referees",
    to: "/admin/referees",
    labelKey: "admin.dashboard.modules.referees",
    icon: "🧑‍⚖️",
    status: "active",
    healthEndpoint: "/admin/referees",
    health: "ok",
  },
  {
    key: "oddsMonitor",
    to: "/admin/odds-monitor",
    labelKey: "admin.dashboard.modules.oddsMonitor",
    icon: "📈",
    status: "active",
    healthEndpoint: "/admin/odds/anomalies",
    health: "ok",
  },
  {
    key: "qbotLab",
    to: "/admin/qbot-lab",
    labelKey: "admin.dashboard.modules.qbotLab",
    icon: "🧬",
    status: "active",
    healthEndpoint: "/admin/qbot/strategies",
    health: "ok",
  },
  {
    key: "viewsCatalog",
    to: "/admin/views",
    labelKey: "admin.dashboard.modules.viewsCatalog",
    icon: "VC",
    status: "active",
    healthEndpoint: "/admin/views/catalog",
    health: "ok",
  },
  {
    key: "providerStatus",
    to: "/admin/provider-status",
    labelKey: "admin.dashboard.modules.providerStatus",
    icon: "📡",
    status: "deprecated",
    reasonKey: "admin.dashboard.deprecatedReason.providerStatus",
    healthEndpoint: "/admin/provider-status",
    health: "ok",
  },
  {
    key: "leagueControl",
    to: "/admin/league-control",
    labelKey: "admin.dashboard.modules.leagueControl",
    icon: "🏟️",
    status: "active",
    healthEndpoint: "/admin/ingest/metrics/league-dashboard",
    health: "ok",
  },
]);

function isCardEnabled(card: ModuleCard): boolean {
  return card.health !== "missing";
}

async function runCompatibilityAudit(): Promise<void> {
  healthChecking.value = true;
  const updated = [...cards.value];
  await Promise.all(
    updated.map(async (card) => {
      try {
        await api.get(card.healthEndpoint, { limit: "1" });
        card.health = "ok";
      } catch (err) {
        if (err instanceof HttpError && (err.status === 404 || err.status === 410)) {
          card.health = "missing";
          card.status = "deprecated";
          if (!card.reasonKey) {
            card.reasonKey = "admin.dashboard.deprecatedReason.endpointMissing";
          }
          return;
        }
        card.health = "error";
      }
    }),
  );
  cards.value = updated;
  healthChecking.value = false;
}

onMounted(async () => {
  await fetchStats();
  await runCompatibilityAudit();
  pollTimer = setInterval(() => {
    void fetchStats();
  }, 60_000);
});

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <h1 class="text-xl font-bold text-text-primary mb-6">{{ t("admin.dashboard.title") }}</h1>

    <div v-if="loading" class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div v-for="n in 8" :key="n" class="bg-surface-1 rounded-card h-20 animate-pulse" />
    </div>

    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">{{ t("common.loadError") }}</p>
      <button class="text-sm text-primary hover:underline" @click="fetchStats">{{ t("common.retry") }}</button>
    </div>

    <template v-else-if="stats">
      <!-- Stats Grid -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.dashboard.cards.users") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.users.total }}</p>
          <p v-if="stats.users.banned" class="text-xs text-danger">{{ t("admin.dashboard.banned", { count: stats.users.banned }) }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.dashboard.cards.bets") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.bets.total }}</p>
          <p class="text-xs text-primary">{{ t("admin.dashboard.today", { count: stats.bets.today }) }}</p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.dashboard.cards.matchesV3") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.matches_v3.total }}</p>
          <p class="text-xs text-text-muted">
            {{ t("admin.dashboard.matchBreakdown", { scheduled: stats.matches_v3.scheduled, live: stats.matches_v3.live, finished: stats.matches_v3.finished }) }}
          </p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">{{ t("admin.dashboard.cards.jobs") }}</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.ingest_jobs.running }}</p>
          <p class="text-xs text-text-muted">
            {{ t("admin.dashboard.jobBreakdown", { queued: stats.ingest_jobs.queued, paused: stats.ingest_jobs.paused, failed: stats.ingest_jobs.failed }) }}
          </p>
        </div>
      </div>

      <!-- Match health -->
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50 mb-6">
        <h2 class="text-sm font-semibold text-text-primary mb-2">{{ t("admin.dashboard.healthTitle") }}</h2>
        <div class="flex flex-wrap items-center gap-4 text-sm mb-2">
          <span class="text-text-muted">
            {{ t("admin.dashboard.withXg") }}: <span class="text-text-primary font-mono">{{ stats.matches_v3.with_xg }}</span>
          </span>
          <span class="text-text-muted">
            {{ t("admin.dashboard.withOdds") }}: <span class="text-text-primary font-mono">{{ stats.matches_v3.with_odds }}</span>
          </span>
          <span class="text-text-muted">
            {{ t("admin.dashboard.status.postponed") }}: <span class="text-text-primary font-mono">{{ stats.matches_v3.postponed }}</span>
          </span>
          <span class="text-text-muted">
            {{ t("admin.dashboard.status.canceled") }}: <span class="text-text-primary font-mono">{{ stats.matches_v3.canceled }}</span>
          </span>
          <span class="text-text-muted">
            {{ t("admin.dashboard.status.walkover") }}: <span class="text-text-primary font-mono">{{ stats.matches_v3.walkover }}</span>
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <span class="text-text-muted">
            {{ t("admin.dashboard.lastMatchUpdate") }}:
            <span class="text-text-primary">
              {{ stats.matches_v3.last_match_update ? new Date(stats.matches_v3.last_match_update).toLocaleString() : "-" }}
            </span>
          </span>
          <span
            v-if="isStale"
            class="px-2 py-0.5 text-xs rounded-full font-medium bg-warning-muted/20 text-warning"
          >
            {{ t("admin.dashboard.staleWarning") }}
          </span>
        </div>
      </div>

      <!-- API Usage -->
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50 mb-6">
        <h2 class="text-sm font-semibold text-text-primary mb-2">{{ t("admin.dashboard.apiTitle") }}</h2>
        <div class="flex items-center gap-4 text-sm">
          <span class="text-text-muted">
            {{ t("admin.dashboard.apiRemaining") }}:
            <span class="text-text-primary font-mono">{{ stats.sportmonks_api.remaining ?? "?" }}</span>
          </span>
          <span class="text-text-muted">
            {{ t("admin.dashboard.apiResetAt") }}:
            <span class="text-text-primary font-mono">{{ stats.sportmonks_api.reset_at ? new Date(stats.sportmonks_api.reset_at).toLocaleString() : "-" }}</span>
          </span>
          <span
            class="px-2 py-0.5 text-xs rounded-full font-medium"
            :class="(stats.sportmonks_api.remaining ?? 0) <= stats.sportmonks_api.reserve_credits ? 'bg-danger-muted/20 text-danger' : 'bg-primary-muted/20 text-primary'"
          >
            {{ t("admin.dashboard.reserve", { count: stats.sportmonks_api.reserve_credits }) }}
          </span>
        </div>
      </div>

      <!-- Quick Links -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <component
          v-for="card in cards"
          :key="card.to"
          :is="isCardEnabled(card) ? 'RouterLink' : 'div'"
          :to="isCardEnabled(card) ? card.to : undefined"
          class="bg-surface-1 rounded-card p-5 border border-surface-3/50 transition-colors text-center"
          :class="isCardEnabled(card) ? 'hover:border-primary/50' : 'opacity-70 cursor-not-allowed'"
          :title="card.status === 'deprecated' ? t(card.reasonKey || 'admin.dashboard.deprecatedReason.generic') : undefined"
        >
          <div class="flex items-center justify-center gap-2">
            <span class="text-2xl" aria-hidden="true">{{ card.icon }}</span>
            <span
              v-if="card.status === 'deprecated'"
              class="px-2 py-0.5 text-[10px] rounded-full bg-warning-muted/20 text-warning font-medium"
            >
              {{ t("admin.dashboard.deprecated") }}
            </span>
            <span
              v-else-if="card.health === 'error'"
              class="px-2 py-0.5 text-[10px] rounded-full bg-danger-muted/20 text-danger font-medium"
            >
              {{ t("admin.dashboard.unstable") }}
            </span>
          </div>
          <p class="text-sm font-medium text-text-primary mt-2">{{ t(card.labelKey) }}</p>
          <p v-if="card.status === 'deprecated' && card.reasonKey" class="text-xs text-text-muted mt-1">
            {{ t(card.reasonKey) }}
          </p>
          <p v-if="card.health === 'missing'" class="text-xs text-danger mt-1">
            {{ t("admin.dashboard.endpointMissing") }}
          </p>
        </component>
      </div>
      <p v-if="healthChecking" class="text-xs text-text-muted mt-3">{{ t("admin.dashboard.healthChecking") }}</p>
    </template>
  </div>
</template>
