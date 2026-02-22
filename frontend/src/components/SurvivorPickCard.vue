<script setup lang="ts">
import { computed, ref } from "vue";
import type { SpieltagMatch } from "@/stores/spieltag";
import { useSurvivorStore } from "@/stores/survivor";
import { useToast } from "@/composables/useToast";

const props = defineProps<{
  match: SpieltagMatch;
  squadId: string;
}>();

const survivor = useSurvivorStore();
const toast = useToast();
const submitting = ref(false);

const isEliminated = computed(() => survivor.entry?.status === "eliminated");

const usedTeams = computed(() => survivor.entry?.used_teams || []);

const homeUsed = computed(() => usedTeams.value.includes(props.match.teams.home));
const awayUsed = computed(() => usedTeams.value.includes(props.match.teams.away));

const currentPick = computed(() => {
  if (!survivor.entry?.picks) return null;
  return survivor.entry.picks.find((p) => p.match_id === props.match.id);
});

const kickoffLabel = computed(() => {
  const d = new Date(props.match.commence_time);
  return d.toLocaleString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
});

async function pickTeam(team: string) {
  if (props.match.is_locked || isEliminated.value) return;
  submitting.value = true;
  try {
    await survivor.makePick(props.squadId, props.match.id, team);
    toast.success(`${team} gewählt!`);
  } catch (e: unknown) {
    toast.error(e instanceof Error ? e.message : "Fehler.");
  } finally {
    submitting.value = false;
  }
}

function pickResultClass(result: string) {
  switch (result) {
    case "won": return "text-emerald-500";
    case "lost": return "text-red-500";
    case "draw": return "text-amber-500";
    default: return "text-text-muted";
  }
}
</script>

<template>
  <div
    class="bg-surface-1 rounded-card p-4 border border-surface-3/50"
    :class="{ 'opacity-60': match.is_locked && !currentPick }"
  >
    <div class="flex items-center justify-between mb-3">
      <span class="text-xs text-text-muted">{{ kickoffLabel }}</span>
      <span
        v-if="currentPick"
        class="text-xs font-bold px-2 py-0.5 rounded-full"
        :class="pickResultClass(currentPick.result)"
      >
        {{ currentPick.result === "pending" ? "Gewählt" : currentPick.result }}
      </span>
    </div>

    <!-- Score display if completed -->
    <div v-if="match.home_score !== null" class="text-center mb-2">
      <span class="text-lg font-bold text-text-primary">
        {{ match.home_score }} : {{ match.away_score }}
      </span>
    </div>

    <!-- Team buttons -->
    <div class="grid grid-cols-2 gap-3">
      <button
        class="py-3 rounded-lg text-sm font-medium transition-colors border"
        :class="[
          currentPick?.team === match.teams.home
            ? 'bg-primary text-surface-0 border-primary'
            : homeUsed
              ? 'bg-surface-3/50 text-text-muted border-surface-3 cursor-not-allowed line-through'
              : 'bg-surface-2 text-text-primary border-surface-3 hover:border-primary',
        ]"
        :disabled="match.is_locked || isEliminated || homeUsed || submitting"
        @click="pickTeam(match.teams.home)"
      >
        {{ match.teams.home }}
      </button>
      <button
        class="py-3 rounded-lg text-sm font-medium transition-colors border"
        :class="[
          currentPick?.team === match.teams.away
            ? 'bg-primary text-surface-0 border-primary'
            : awayUsed
              ? 'bg-surface-3/50 text-text-muted border-surface-3 cursor-not-allowed line-through'
              : 'bg-surface-2 text-text-primary border-surface-3 hover:border-primary',
        ]"
        :disabled="match.is_locked || isEliminated || awayUsed || submitting"
        @click="pickTeam(match.teams.away)"
      >
        {{ match.teams.away }}
      </button>
    </div>
  </div>
</template>
