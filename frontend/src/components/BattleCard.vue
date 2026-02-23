<script setup lang="ts">
import { computed, ref } from "vue";
import type { Battle } from "@/stores/battles";
import type { Squad } from "@/stores/squads";

const props = defineProps<{
  battle: Battle;
  acceptableSquads?: Squad[];
}>();

const emit = defineEmits<{
  commit: [squadId: string];
  accept: [squadId: string];
  decline: [];
}>();

const isActive = computed(() => props.battle.status === "active");
const isChallenge = computed(() => props.battle.status === "open" || props.battle.status === "pending");
const hasCommitted = computed(() => props.battle.my_commitment != null);
const needsCommitment = computed(() => props.battle.needs_commitment ?? (!hasCommitted.value && !isChallenge.value));
const hasSquadB = computed(() => props.battle.squad_b != null);

// Accept flow: if user has multiple admin squads, show selector
const showAcceptSelector = ref(false);
const selectedAcceptSquad = ref("");

// Score bar calculation (only for accepted battles)
const scoreA = computed(() => props.battle.result?.squad_a_avg ?? props.battle.squad_a.avg_points ?? 0);
const scoreB = computed(() => props.battle.result?.squad_b_avg ?? props.battle.squad_b?.avg_points ?? 0);
const total = computed(() => scoreA.value + scoreB.value || 1);
const percentA = computed(() => {
  if (scoreA.value === 0 && scoreB.value === 0) return 50;
  return Math.round((scoreA.value / total.value) * 100);
});
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
  if (props.battle.squad_b && props.battle.my_commitment === props.battle.squad_b.id) return props.battle.squad_b.name;
  return null;
});

const statusBadge = computed(() => {
  switch (props.battle.status) {
    case "active": return { label: "Live", cls: "bg-danger-muted/20 text-danger animate-pulse" };
    case "upcoming": return { label: "Geplant", cls: "bg-primary-muted/20 text-primary" };
    case "open": return { label: "Offen", cls: "bg-emerald-500/20 text-emerald-400" };
    case "pending": return { label: "Ausstehend", cls: "bg-amber-500/20 text-amber-400" };
    default: return { label: props.battle.status, cls: "bg-surface-3 text-text-muted" };
  }
});

function handleAcceptClick() {
  const squads = props.acceptableSquads ?? [];
  // Filter out the challenging squad
  const eligible = squads.filter((s) => s.id !== props.battle.squad_a.id);
  if (eligible.length === 1) {
    emit("accept", eligible[0].id);
  } else if (eligible.length > 1) {
    showAcceptSelector.value = true;
    selectedAcceptSquad.value = eligible[0].id;
  }
}

function confirmAccept() {
  if (selectedAcceptSquad.value) {
    emit("accept", selectedAcceptSquad.value);
    showAcceptSelector.value = false;
  }
}
</script>

<template>
  <article class="bg-surface-1 rounded-card border border-surface-3/50 overflow-hidden">
    <!-- Header -->
    <div class="px-5 py-3 flex items-center justify-between border-b border-surface-3/30">
      <div class="flex items-center gap-2">
        <span
          class="text-xs px-2 py-0.5 rounded-full font-medium"
          :class="statusBadge.cls"
        >
          {{ statusBadge.label }}
        </span>
        <span
          v-if="battle.challenge_type && battle.challenge_type !== 'classic'"
          class="text-xs text-text-muted"
        >
          {{ battle.challenge_type === "direct" ? "Direkt" : "Lobby" }}
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
          <p v-if="hasSquadB" class="text-xs text-text-muted mt-0.5">
            {{ battle.squad_a.committed_count ?? battle.squad_a.member_count ?? "?" }} Spieler
          </p>
        </div>
        <span class="text-lg font-bold text-text-muted px-4">vs</span>
        <div class="text-center flex-1">
          <template v-if="hasSquadB">
            <p class="text-sm font-semibold text-text-primary">{{ battle.squad_b!.name }}</p>
            <p class="text-xs text-text-muted mt-0.5">
              {{ battle.squad_b!.committed_count ?? battle.squad_b!.member_count ?? "?" }} Spieler
            </p>
          </template>
          <template v-else>
            <p class="text-sm font-medium text-text-muted italic">Offen</p>
            <p class="text-xs text-text-muted mt-0.5">Wartet auf Gegner</p>
          </template>
        </div>
      </div>

      <!-- Score Bar (only for accepted battles with activity) -->
      <div v-if="!isChallenge && (isActive || scoreA || scoreB)" class="mb-4">
        <div class="flex justify-between text-xs font-mono tabular-nums text-text-muted mb-1">
          <span>{{ scoreA.toFixed(1) }} Pkt</span>
          <span>{{ scoreB.toFixed(1) }} Pkt</span>
        </div>
        <div
          class="h-3 rounded-full bg-surface-3 overflow-hidden flex"
          role="progressbar"
          :aria-valuenow="percentA"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-label="`${battle.squad_a.name} ${percentA}%, ${battle.squad_b?.name ?? '?'} ${percentB}%`"
        >
          <div
            class="h-full bg-primary transition-all duration-700 ease-out"
            :style="{ width: `${percentA}%` }"
          />
          <div
            class="h-full bg-danger transition-all duration-700 ease-out"
            :style="{ width: `${percentB}%` }"
          />
        </div>
        <div class="flex justify-between text-xs text-text-muted mt-1">
          <span>{{ percentA }}%</span>
          <span>{{ percentB }}%</span>
        </div>
      </div>

      <!-- Challenge actions: Accept / Decline -->
      <div v-if="isChallenge && acceptableSquads && acceptableSquads.length > 0" class="space-y-2">
        <!-- Squad selector (when multiple eligible squads) -->
        <div v-if="showAcceptSelector" class="space-y-2">
          <label class="text-xs text-text-muted">Mit welchem Squad annehmen?</label>
          <select
            v-model="selectedAcceptSquad"
            class="w-full bg-surface-2 border border-surface-3 rounded-lg px-3 py-2 text-sm text-text-primary"
          >
            <option
              v-for="s in acceptableSquads.filter((s) => s.id !== battle.squad_a.id)"
              :key="s.id"
              :value="s.id"
            >
              {{ s.name }}
            </option>
          </select>
          <div class="flex gap-2">
            <button
              class="flex-1 py-2 text-sm rounded-lg bg-emerald-600 text-white font-medium hover:bg-emerald-500 transition-colors"
              @click="confirmAccept"
            >
              Bestätigen
            </button>
            <button
              class="py-2 px-3 text-sm rounded-lg bg-surface-2 text-text-muted border border-surface-3"
              @click="showAcceptSelector = false"
            >
              Abbrechen
            </button>
          </div>
        </div>

        <div v-else class="flex gap-2">
          <button
            class="flex-1 py-2.5 text-sm rounded-lg bg-emerald-600/15 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-600/25 transition-colors font-medium"
            @click="handleAcceptClick"
          >
            Annehmen
          </button>
          <button
            v-if="battle.status === 'pending'"
            class="flex-1 py-2.5 text-sm rounded-lg bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20 transition-colors font-medium"
            @click="emit('decline')"
          >
            Ablehnen
          </button>
        </div>
      </div>

      <!-- Commitment Status / Buttons (for accepted battles) -->
      <template v-else-if="!isChallenge">
        <div v-if="hasCommitted" class="text-center py-2 bg-surface-2 rounded-lg">
          <p class="text-xs text-text-muted">Dein Team:</p>
          <p class="text-sm font-semibold text-primary">{{ committedSquadName }}</p>
        </div>
        <div v-else-if="needsCommitment && hasSquadB" class="space-y-2">
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
              @click="emit('commit', battle.squad_b!.id)"
            >
              {{ battle.squad_b!.name }}
            </button>
          </div>
        </div>
      </template>
    </div>
  </article>
</template>
