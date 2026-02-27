<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";

const props = defineProps<{
  squadId: string;
  leagueId: number;
  season?: number;
}>();

interface BankrollEntry {
  user_id: string;
  alias: string;
  balance: number;
  profit: number;
  total_wagered: number;
  win_rate: number;
  bets_count: number;
}

const api = useApi();
const entries = ref<BankrollEntry[]>([]);
const loading = ref(false);

onMounted(async () => {
  loading.value = true;
  try {
    // Use matchday leaderboard endpoint with bankroll context
    const params: Record<string, string> = {
      league_id: String(props.leagueId),
      mode: "bankroll",
    };
    if (props.season) params.season = String(props.season);
    entries.value = await api.get<BankrollEntry[]>(
      `/squads/${props.squadId}/leaderboard`,
      params,
    );
  } catch {
    entries.value = [];
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 class="text-sm font-semibold text-text-primary mb-3">Bankroll-Ranking</h3>

    <div v-if="loading" class="animate-pulse space-y-2">
      <div v-for="n in 3" :key="n" class="h-8 bg-surface-2 rounded" />
    </div>

    <div v-else-if="entries.length === 0" class="text-sm text-text-muted text-center py-4">
      Noch keine Daten.
    </div>

    <table v-else class="w-full text-sm">
      <thead>
        <tr class="text-xs text-text-muted border-b border-surface-3">
          <th class="text-left py-1 w-8">#</th>
          <th class="text-left py-1">Alias</th>
          <th class="text-right py-1">Balance</th>
          <th class="text-right py-1">Profit</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(entry, i) in entries"
          :key="entry.user_id"
          class="border-b border-surface-3/50 last:border-0"
        >
          <td class="py-1.5 text-text-muted">{{ i + 1 }}</td>
          <td class="py-1.5 font-medium text-text-primary">{{ entry.alias }}</td>
          <td class="py-1.5 text-right font-bold text-text-primary">
            {{ Math.round(entry.balance) }}
          </td>
          <td
            class="py-1.5 text-right"
            :class="entry.profit >= 0 ? 'text-emerald-500' : 'text-red-500'"
          >
            {{ entry.profit >= 0 ? "+" : "" }}{{ Math.round(entry.profit) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
