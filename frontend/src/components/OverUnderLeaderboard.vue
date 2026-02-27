<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useApi } from "@/composables/useApi";

const props = defineProps<{
  squadId: string;
  leagueId: number;
  season?: number;
}>();

interface OUEntry {
  user_id: string;
  alias: string;
  total_points: number;
  bets_count: number;
  win_rate: number;
}

const api = useApi();
const entries = ref<OUEntry[]>([]);

onMounted(async () => {
  try {
    const params: Record<string, string> = {
      league_id: String(props.leagueId),
      mode: "over_under",
    };
    if (props.season) params.season = String(props.season);
    entries.value = await api.get<OUEntry[]>(
      `/squads/${props.squadId}/leaderboard`,
      params,
    );
  } catch {
    entries.value = [];
  }
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 class="text-sm font-semibold text-text-primary mb-3">Über/Unter-Ranking</h3>

    <div v-if="entries.length === 0" class="text-sm text-text-muted text-center py-4">
      Noch keine Daten.
    </div>

    <table v-else class="w-full text-sm">
      <thead>
        <tr class="text-xs text-text-muted border-b border-surface-3">
          <th class="text-left py-1 w-8">#</th>
          <th class="text-left py-1">Alias</th>
          <th class="text-right py-1">Punkte</th>
          <th class="text-right py-1">Bets</th>
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
            {{ entry.total_points.toFixed(1) }}
          </td>
          <td class="py-1.5 text-right text-text-muted">{{ entry.bets_count }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
