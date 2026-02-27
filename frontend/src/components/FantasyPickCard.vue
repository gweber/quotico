<script setup lang="ts">
import { ref, computed } from "vue";
import type { MatchdayMatch } from "@/stores/matchday";
import { useFantasyStore } from "@/stores/fantasy";
import { useToast } from "@/composables/useToast";

const props = defineProps<{
  match: MatchdayMatch;
  squadId: string;
}>();

const fantasy = useFantasyStore();
const toast = useToast();
const submitting = ref(false);

const existingPick = computed(() => {
  if (!fantasy.pick) return null;
  if (fantasy.pick.match_id === props.match.id) return fantasy.pick;
  return null;
});

const kickoffLabel = computed(() => {
  const d = new Date(props.match.match_date);
  if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) {
    return d.toLocaleString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" }) + " · Uhrzeit offen";
  }
  return d.toLocaleString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

async function pickTeam(team: string) {
  if (props.match.is_locked) return;
  submitting.value = true;
  try {
    await fantasy.makePick(props.squadId, props.match.id, team);
    toast.success(`${team} gewählt!`);
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler.");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div
    class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
    :class="{ 'opacity-60': match.is_locked && !existingPick }"
  >
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <span
        v-if="existingPick?.fantasy_points !== null && existingPick?.fantasy_points !== undefined"
        class="text-xs font-bold px-2 py-0.5 rounded-full bg-primary/10 text-primary"
      >
        {{ existingPick.fantasy_points }}P
      </span>
    </div>

    <!-- Score if completed -->
    <div v-if="(match.result as any)?.home_score != null" class="text-center mb-2">
      <span class="text-lg font-bold text-text-primary">
        {{ (match.result as any)?.home_score }} : {{ (match.result as any)?.away_score }}
      </span>
    </div>

    <!-- Team pick buttons -->
    <div class="grid grid-cols-2 gap-3">
      <button
        class="py-3 rounded-lg text-sm font-medium transition-colors border"
        :class="
          existingPick?.team === match.home_team
            ? 'bg-primary text-surface-0 border-primary'
            : 'bg-surface-2 text-text-primary border-surface-3 hover:border-primary'
        "
        :disabled="match.is_locked || submitting"
        @click="pickTeam(match.home_team)"
      >
        {{ match.home_team }}
      </button>
      <button
        class="py-3 rounded-lg text-sm font-medium transition-colors border"
        :class="
          existingPick?.team === match.away_team
            ? 'bg-primary text-surface-0 border-primary'
            : 'bg-surface-2 text-text-primary border-surface-3 hover:border-primary'
        "
        :disabled="match.is_locked || submitting"
        @click="pickTeam(match.away_team)"
      >
        {{ match.away_team }}
      </button>
    </div>

    <!-- Result details -->
    <div v-if="existingPick?.status === 'resolved'" class="mt-2 text-xs text-text-muted text-center">
      {{ existingPick.goals_scored }} Tore geschossen, {{ existingPick.goals_conceded }} kassiert
    </div>
  </div>
</template>
