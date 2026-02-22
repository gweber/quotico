<script setup lang="ts">
import { onMounted } from "vue";
import { useFantasyStore } from "@/stores/fantasy";

const props = defineProps<{
  squadId: string;
  sportKey: string;
  season?: number;
}>();

const fantasy = useFantasyStore();

onMounted(() => {
  fantasy.fetchStandings(props.squadId, props.sportKey, props.season);
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 class="text-sm font-semibold text-text-primary mb-3">Fantasy-Ranking</h3>

    <div v-if="fantasy.standings.length === 0" class="text-sm text-text-muted text-center py-4">
      Noch keine Daten.
    </div>

    <table v-else class="w-full text-sm">
      <thead>
        <tr class="text-xs text-text-muted border-b border-surface-3">
          <th class="text-left py-1 w-8">#</th>
          <th class="text-left py-1">Alias</th>
          <th class="text-right py-1">Punkte</th>
          <th class="text-right py-1">Avg</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(entry, i) in fantasy.standings"
          :key="entry.user_id"
          class="border-b border-surface-3/50 last:border-0"
        >
          <td class="py-1.5 text-text-muted">{{ i + 1 }}</td>
          <td class="py-1.5 font-medium text-text-primary">{{ entry.alias }}</td>
          <td class="py-1.5 text-right font-bold text-text-primary">
            {{ entry.total_points }}
          </td>
          <td class="py-1.5 text-right text-text-muted">
            {{ entry.avg_points }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
