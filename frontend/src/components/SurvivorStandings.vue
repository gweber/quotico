<script setup lang="ts">
import { onMounted } from "vue";
import { useSurvivorStore } from "@/stores/survivor";

const props = defineProps<{
  squadId: string;
  leagueId: number;
  season?: number;
}>();

const survivor = useSurvivorStore();

onMounted(() => {
  survivor.fetchStandings(props.squadId, props.leagueId, props.season);
});
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 class="text-sm font-semibold text-text-primary mb-3">Survivor-Tabelle</h3>

    <div v-if="survivor.standings.length === 0" class="text-sm text-text-muted text-center py-4">
      Noch keine Teilnehmer.
    </div>

    <table v-else class="w-full text-sm">
      <thead>
        <tr class="text-xs text-text-muted border-b border-surface-3">
          <th class="text-left py-1">Alias</th>
          <th class="text-center py-1">Status</th>
          <th class="text-right py-1">Streak</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="entry in survivor.standings"
          :key="entry.user_id"
          class="border-b border-surface-3/50 last:border-0"
          :class="{ 'opacity-50': entry.status === 'eliminated' }"
        >
          <td class="py-1.5 font-medium text-text-primary">{{ entry.alias }}</td>
          <td class="py-1.5 text-center">
            <span
              class="text-xs font-bold px-2 py-0.5 rounded-full"
              :class="
                entry.status === 'alive'
                  ? 'text-emerald-500 bg-emerald-500/10'
                  : 'text-red-500 bg-red-500/10'
              "
            >
              {{ entry.status === "alive" ? "Alive" : "Out" }}
            </span>
          </td>
          <td class="py-1.5 text-right font-bold text-text-primary">
            {{ entry.streak }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
