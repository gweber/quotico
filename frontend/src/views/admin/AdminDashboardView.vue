<!--
frontend/src/views/admin/AdminDashboardView.vue

Purpose:
    Main admin overview with system stats and quick links into admin modules.
-->
<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";

const api = useApi();

interface Stats {
  users: { total: number; banned: number };
  bets: { total: number; today: number };
  matches: { total: number; pending: number; completed: number };
  squads: number;
  battles: number;
  api_usage: { requests_used: number | string; requests_remaining: number | string | null };
  circuit_open: boolean;
}

const stats = ref<Stats | null>(null);
const loading = ref(true);
const error = ref(false);

async function fetchStats() {
  loading.value = true;
  error.value = false;
  try {
    stats.value = await api.get<Stats>("/admin/stats");
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

onMounted(fetchStats);

const cards = [
  { to: "/admin/users", label: "User Management", icon: "\uD83D\uDC65" },
  { to: "/admin/matches", label: "Match Management", icon: "\u26BD" },
  { to: "/admin/battles", label: "Battle Management", icon: "\u2694\uFE0F" },
  { to: "/admin/audit", label: "Audit Log", icon: "\uD83D\uDCDC" },
  { to: "/admin/providers", label: "Provider Status", icon: "\uD83D\uDCE1" },
  { to: "/admin/event-bus", label: "QBus Monitor", icon: "\uD83D\uDE9A" },
  { to: "/admin/time-machine-justice", label: "Time Machine Justice", icon: "\u23F2\uFE0F" },
  { to: "/admin/leagues", label: "League Tower", icon: "\uD83D\uDCCA" },
  { to: "/admin/team-tower", label: "Team Tower", icon: "\uD83C\uDFAF" },
  { to: "/admin/ingest", label: "Sportmonks Ingest", icon: "\u2699\uFE0F" },
  { to: "/admin/qbot-lab", label: "Qbot Lab", icon: "\uD83E\uDDEC" },
];
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <h1 class="text-xl font-bold text-text-primary mb-6">Admin Dashboard</h1>

    <div v-if="loading" class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div v-for="n in 8" :key="n" class="bg-surface-1 rounded-card h-20 animate-pulse" />
    </div>

    <div v-else-if="error" class="text-center py-12">
      <p class="text-text-muted mb-3">Error loading.</p>
      <button class="text-sm text-primary hover:underline" @click="fetchStats">Try again</button>
    </div>

    <template v-else-if="stats">
      <!-- Stats Grid -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">Nutzer</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.users.total }}</p>
          <p v-if="stats.users.banned" class="text-xs text-danger">{{ stats.users.banned }} gesperrt</p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">Bets</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.bets.total }}</p>
          <p class="text-xs text-primary">{{ stats.bets.today }} today</p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">Spiele</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.matches.total }}</p>
          <p class="text-xs text-text-muted">{{ stats.matches.pending }} offen / {{ stats.matches.completed }} beendet</p>
        </div>
        <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
          <p class="text-xs text-text-muted">Squads / Battles</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.squads }} / {{ stats.battles }}</p>
        </div>
      </div>

      <!-- API Usage -->
      <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50 mb-6">
        <h2 class="text-sm font-semibold text-text-primary mb-2">TheOddsAPI Status</h2>
        <div class="flex items-center gap-4 text-sm">
          <span class="text-text-muted">
            Verbraucht: <span class="text-text-primary font-mono">{{ stats.api_usage.requests_used }}</span>
          </span>
          <span class="text-text-muted">
            Verbleibend: <span class="text-text-primary font-mono">{{ stats.api_usage.requests_remaining ?? "?" }}</span>
          </span>
          <span
            class="px-2 py-0.5 text-xs rounded-full font-medium"
            :class="stats.circuit_open ? 'bg-danger-muted/20 text-danger' : 'bg-primary-muted/20 text-primary'"
          >
            Circuit {{ stats.circuit_open ? "OPEN" : "OK" }}
          </span>
        </div>
      </div>

      <!-- Quick Links -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <RouterLink
          v-for="card in cards"
          :key="card.to"
          :to="card.to"
          class="bg-surface-1 rounded-card p-5 border border-surface-3/50 hover:border-primary/50 transition-colors text-center"
        >
          <span class="text-2xl" aria-hidden="true">{{ card.icon }}</span>
          <p class="text-sm font-medium text-text-primary mt-2">{{ card.label }}</p>
        </RouterLink>
      </div>
    </template>
  </div>
</template>
