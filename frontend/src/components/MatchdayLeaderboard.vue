<script setup lang="ts">
import { ref, watch } from "vue";
import { useApi } from "@/composables/useApi";

const props = defineProps<{
  sportKey: string;
  season?: number;
  matchdayId?: string;
  mode: "season" | "matchday";
}>();

interface LeaderboardEntry {
  rank: number;
  user_id: string;
  alias: string;
  total_points: number;
  matchdays_played?: number;
  exact_count: number;
  diff_count: number;
  tendency_count: number;
}

const api = useApi();
const entries = ref<LeaderboardEntry[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    if (props.mode === "matchday" && props.matchdayId) {
      entries.value = await api.get<LeaderboardEntry[]>(
        `/matchday/matchdays/${props.matchdayId}/leaderboard`
      );
    } else {
      const params: Record<string, string> = { sport: props.sportKey };
      if (props.season) params.season = String(props.season);
      entries.value = await api.get<LeaderboardEntry[]>(
        "/matchday/leaderboard",
        params
      );
    }
  } catch {
    entries.value = [];
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.sportKey, props.season, props.matchdayId, props.mode],
  () => load(),
  { immediate: true }
);
</script>

<template>
  <div class="bg-surface-1 rounded-card p-4 border border-surface-3/50">
    <h3 class="text-sm font-semibold text-text-primary mb-3">
      {{ mode === "matchday" ? $t('matchday.matchdayLeaderboard') : $t('matchday.seasonLeaderboard') }}
    </h3>

    <!-- Loading -->
    <div v-if="loading" class="space-y-2">
      <div v-for="i in 5" :key="i" class="h-8 bg-surface-2 rounded animate-pulse" />
    </div>

    <!-- Empty -->
    <p v-else-if="entries.length === 0" class="text-sm text-text-muted">
      Noch keine Ergebnisse vorhanden.
    </p>

    <!-- Table -->
    <div v-else class="overflow-x-auto -mx-4 px-4">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-text-muted border-b border-surface-3">
            <th class="pb-2 pr-2 w-8 font-medium">#</th>
            <th class="pb-2 pr-2 font-medium">{{ $t('match.players') }}</th>
            <th class="pb-2 pr-2 text-right font-medium">Pkt</th>
            <th v-if="mode === 'season'" class="pb-2 pr-2 text-right font-medium">ST</th>
            <th class="pb-2 text-right font-medium" title="Exakt / Differenz / Tendenz">
              E/D/T
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in entries"
            :key="entry.user_id"
            class="border-b border-surface-3/50 last:border-0"
          >
            <td class="py-2 pr-2 text-text-muted font-mono">{{ entry.rank }}</td>
            <td class="py-2 pr-2 text-text-primary font-medium truncate max-w-[140px]">
              {{ entry.alias }}
            </td>
            <td class="py-2 pr-2 text-right font-bold text-primary">
              {{ entry.total_points }}
            </td>
            <td v-if="mode === 'season'" class="py-2 pr-2 text-right text-text-muted">
              {{ entry.matchdays_played }}
            </td>
            <td class="py-2 text-right text-text-muted font-mono text-xs">
              {{ entry.exact_count }}/{{ entry.diff_count }}/{{ entry.tendency_count }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
