<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useWarRoom } from "@/composables/useWarRoom";
import WarRoomHeader from "@/components/WarRoomHeader.vue";
import WarRoomGrid from "@/components/WarRoomGrid.vue";
import WarRoomConsensusBar from "@/components/WarRoomConsensusBar.vue";

const route = useRoute();

const squadId = computed(() => route.params.id as string);
const matchId = computed(() => route.params.matchId as string);

const {
  data,
  loading,
  error,
  countdown,
  phase,
  liveScore,
  isBetWinning,
} = useWarRoom(squadId.value, matchId.value);

// Per-member winning/losing state (computed from live WS score)
const tipStates = computed(() => {
  const map = new Map<string, { winning: boolean; losing: boolean }>();
  if (!data.value) return map;

  for (const member of data.value.members) {
    if (!member.selection || phase.value !== "live") {
      map.set(member.user_id, { winning: false, losing: false });
      continue;
    }
    const winning = isBetWinning(member.selection.value);
    map.set(member.user_id, { winning, losing: !winning });
  }
  return map;
});
</script>

<template>
  <div class="max-w-2xl mx-auto px-4 py-6 space-y-4">
    <!-- Back link -->
    <RouterLink
      :to="`/squads/${squadId}`"
      class="inline-flex items-center gap-1 text-sm text-text-muted hover:text-text-primary transition-colors"
    >
      &larr; Zurück zum Squad
    </RouterLink>

    <!-- Loading skeleton -->
    <template v-if="loading && !data">
      <div class="bg-surface-1 rounded-card h-20 animate-pulse" />
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <div
          v-for="n in 8"
          :key="n"
          class="bg-surface-1 rounded-card h-32 animate-pulse"
        />
      </div>
    </template>

    <!-- Error state -->
    <div
      v-else-if="error"
      class="bg-surface-1 rounded-card p-6 border border-danger/20 text-center"
    >
      <p class="text-sm text-danger">{{ error }}</p>
    </div>

    <!-- War Room content -->
    <template v-else-if="data">
      <!-- Title -->
      <h1 class="text-xl font-bold text-text-primary">War Room</h1>

      <!-- Header: match info + countdown/score -->
      <WarRoomHeader
        :match="data.match"
        :countdown="countdown"
        :phase="phase"
        :live-score="liveScore"
      />

      <!-- Pre-kickoff hint -->
      <p
        v-if="phase === 'pre_kickoff'"
        class="text-xs text-text-muted text-center"
      >
        Tipps werden beim Anpfiff aufgedeckt.
      </p>

      <!-- Consensus bar (post-kickoff only) -->
      <Transition name="expand">
        <WarRoomConsensusBar
          v-if="phase !== 'pre_kickoff' && data.consensus"
          :consensus="data.consensus"
          :home-team="data.match.home_team"
          :away-team="data.match.away_team"
        />
      </Transition>

      <!-- Member grid -->
      <WarRoomGrid
        :members="data.members"
        :phase="phase"
        :maverick-ids="data.mavericks ?? []"
        :home-team="data.match.home_team"
        :away-team="data.match.away_team"
        :tip-states="tipStates"
      />
    </template>
  </div>
</template>

<style scoped>
.expand-enter-active,
.expand-leave-active {
  transition: all 0.4s ease;
  max-height: 200px;
  overflow: hidden;
}
.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
