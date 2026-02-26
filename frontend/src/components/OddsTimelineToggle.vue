<script setup lang="ts">
import { ref, watch } from "vue";
import { useOddsTimeline } from "@/composables/useOddsTimeline";
import OddsTimelineChart from "./OddsTimelineChart.vue";

const props = defineProps<{
  matchId: string;
  homeTeam?: string;
  awayTeam?: string;
}>();

const expanded = ref(false);
const { data, loading, fetchForMatch } = useOddsTimeline();

// Lazy-fetch: only load data when first expanded
watch(expanded, (val) => {
  if (val && !data.value) {
    fetchForMatch(props.matchId);
  }
});
</script>

<template>
  <div class="mt-2 border-t border-surface-3/30 pt-2">
    <button
      class="w-full flex items-center gap-2 text-xs py-1.5 group transition-colors"
      @click="expanded = !expanded"
      :aria-expanded="expanded"
      aria-label="Quotenverlauf"
    >
      <!-- Expand chevron -->
      <svg
        class="w-3.5 h-3.5 shrink-0 text-text-muted transition-transform duration-200"
        :class="{ 'rotate-90': expanded }"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" />
      </svg>

      <span class="text-text-secondary font-medium">Quotenverlauf</span>

      <!-- Snapshot count badge -->
      <span
        v-if="data?.items?.length"
        class="text-text-muted/60 ml-auto tabular-nums"
      >
        {{ data.items.length }} Punkte
      </span>
    </button>

    <Transition name="expand">
      <div v-if="expanded" class="overflow-hidden">
        <div class="py-2">
          <div v-if="loading" class="text-xs text-text-muted text-center py-4">
            Lade Quotenverlauf...
          </div>
          <OddsTimelineChart
            v-else-if="data?.items"
            :snapshots="data.items"
            :home-team="homeTeam"
            :away-team="awayTeam"
            compact
          />
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.expand-enter-active,
.expand-leave-active {
  transition: all 0.2s ease;
  max-height: 200px;
}
.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
