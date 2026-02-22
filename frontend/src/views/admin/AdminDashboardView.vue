<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";

const api = useApi();

interface Stats {
  users: { total: number; banned: number };
  tips: { total: number; today: number };
  matches: { total: number; pending: number; completed: number };
  squads: number;
  battles: number;
  api_usage: { requests_used: number | string; requests_remaining: number | string | null };
  circuit_open: boolean;
}

const stats = ref<Stats | null>(null);
const loading = ref(true);

onMounted(async () => {
  try {
    stats.value = await api.get<Stats>("/admin/stats");
  } finally {
    loading.value = false;
  }
});

const cards = [
  { to: "/admin/users", label: "User Management", icon: "\uD83D\uDC65" },
  { to: "/admin/matches", label: "Match Management", icon: "\u26BD" },
  { to: "/admin/battles", label: "Battle Management", icon: "\u2694\uFE0F" },
];
</script>

<template>
  <div class="max-w-4xl mx-auto p-4">
    <h1 class="text-xl font-bold text-text-primary mb-6">Admin Dashboard</h1>

    <div v-if="loading" class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div v-for="n in 8" :key="n" class="bg-surface-1 rounded-card h-20 animate-pulse" />
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
          <p class="text-xs text-text-muted">Tipps gesamt</p>
          <p class="text-2xl font-bold text-text-primary tabular-nums">{{ stats.tips.total }}</p>
          <p class="text-xs text-primary">{{ stats.tips.today }} heute</p>
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
