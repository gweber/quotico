<script setup lang="ts">
import { computed } from "vue";
import type { Battle } from "@/stores/battles";

const props = defineProps<{
  battle: Battle;
}>();

const emit = defineEmits<{
  commit: [squadId: string];
}>();

const isActive = computed(() => props.battle.status === "active");
const hasCommitted = computed(() => props.battle.my_commitment != null);
const needsCommitment = computed(() => props.battle.needs_commitment ?? !hasCommitted.value);

// Score bar calculation
const scoreA = computed(() => props.battle.result?.squad_a_avg ?? props.battle.squad_a.avg_points ?? 0);
const scoreB = computed(() => props.battle.result?.squad_b_avg ?? props.battle.squad_b.avg_points ?? 0);
const total = computed(() => scoreA.value + scoreB.value || 1);
const percentA = computed(() => Math.round((scoreA.value / total.value) * 100));
const percentB = computed(() => 100 - percentA.value);

const formatDate = (iso: string) =>
  new Date(iso).toLocaleDateString("de-DE", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

const committedSquadName = computed(() => {
  if (!props.battle.my_commitment) return null;
  if (props.battle.my_commitment === props.battle.squad_a.id) return props.battle.squad_a.name;
  if (props.battle.my_commitment === props.battle.squad_b.id) return props.battle.squad_b.name;
  return null;
});
</script>

<template>
  <article class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden">
    <!-- Header -->
    <div class="px-5 py-3 flex items-center justify-between border-b border-surface-3/30">
      <div class="flex items-center gap-2">
        <span
          class="text-xs px-2 py-0.5 rounded-full font-medium"
          :class="isActive ? 'bg-danger-muted/20 text-danger animate-pulse' : 'bg-primary-muted/20 text-primary'"
        >
          {{ isActive ? "Live" : "Geplant" }}
        </span>
      </div>
      <span class="text-xs text-text-muted">
        {{ formatDate(battle.start_time) }} &mdash; {{ formatDate(battle.end_time) }}
      </span>
    </div>

    <!-- Squads vs -->
    <div class="px-5 py-4">
      <div class="flex items-center justify-between mb-4">
        <div class="text-center flex-1">
          <p class="text-sm font-semibold text-text-primary">{{ battle.squad_a.name }}</p>
          <p class="text-xs text-text-muted mt-0.5">
            {{ battle.squad_a.committed_count ?? battle.squad_a.member_count ?? "?" }} Spieler
          </p>
        </div>
        <span class="text-lg font-bold text-text-muted px-4">vs</span>
        <div class="text-center flex-1">
          <p class="text-sm font-semibold text-text-primary">{{ battle.squad_b.name }}</p>
          <p class="text-xs text-text-muted mt-0.5">
            {{ battle.squad_b.committed_count ?? battle.squad_b.member_count ?? "?" }} Spieler
          </p>
        </div>
      </div>

      <!-- Score Bar -->
      <div v-if="isActive || scoreA || scoreB" class="mb-4">
        <div class="flex justify-between text-xs font-mono tabular-nums text-text-muted mb-1">
          <span>{{ scoreA.toFixed(1) }} Pkt</span>
          <span>{{ scoreB.toFixed(1) }} Pkt</span>
        </div>
        <div class="h-3 rounded-full bg-surface-3 overflow-hidden flex">
          <div
            class="h-full bg-primary transition-all duration-700 ease-out rounded-l-full"
            :style="{ width: `${percentA}%` }"
          />
          <div
            class="h-full bg-danger transition-all duration-700 ease-out rounded-r-full"
            :style="{ width: `${percentB}%` }"
          />
        </div>
        <div class="flex justify-between text-xs text-text-muted mt-1">
          <span>{{ percentA }}%</span>
          <span>{{ percentB }}%</span>
        </div>
      </div>

      <!-- Commitment Status / Buttons -->
      <div v-if="hasCommitted" class="text-center py-2 bg-surface-2 rounded-lg">
        <p class="text-xs text-text-muted">Dein Team:</p>
        <p class="text-sm font-semibold text-primary">{{ committedSquadName }}</p>
      </div>
      <div v-else-if="needsCommitment" class="space-y-2">
        <p class="text-xs text-text-muted text-center mb-2">Wähle dein Team:</p>
        <div class="flex gap-2">
          <button
            class="flex-1 py-2.5 text-sm rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 transition-colors font-medium"
            @click="emit('commit', battle.squad_a.id)"
          >
            {{ battle.squad_a.name }}
          </button>
          <button
            class="flex-1 py-2.5 text-sm rounded-lg bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20 transition-colors font-medium"
            @click="emit('commit', battle.squad_b.id)"
          >
            {{ battle.squad_b.name }}
          </button>
        </div>
      </div>
    </div>
  </article>
</template>
