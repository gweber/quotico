<script setup lang="ts">
import { useMatchesStore } from "@/stores/matches";

const props = withDefaults(defineProps<{
  variant?: "sidebar" | "bar";
}>(), {
  variant: "bar",
});

const matches = useMatchesStore();

const sports = [
  { key: null, label: "Alle", icon: "\uD83C\uDFAF" },
  { key: "soccer_germany_bundesliga", label: "Bundesliga", icon: "\u26BD" },
  { key: "soccer_epl", label: "Premier League", icon: "\u26BD" },
  { key: "soccer_spain_la_liga", label: "La Liga", icon: "\u26BD" },
  { key: "soccer_italy_serie_a", label: "Serie A", icon: "\u26BD" },
  { key: "soccer_uefa_champs_league", label: "Champions League", icon: "\u26BD" },
  { key: "americanfootball_nfl", label: "NFL", icon: "\uD83C\uDFC8" },
  { key: "basketball_nba", label: "NBA", icon: "\uD83C\uDFC0" },
  { key: "tennis_atp_french_open", label: "Tennis ATP", icon: "\uD83C\uDFBE" },
];

function selectSport(key: string | null) {
  matches.setSport(key);
}
</script>

<template>
  <!-- Desktop: Sidebar -->
  <aside
    v-if="props.variant === 'sidebar'"
    class="hidden lg:block w-56 shrink-0"
    aria-label="Sportarten"
  >
    <nav class="sticky top-[3.75rem] p-3 space-y-0.5">
      <h2 class="px-3 py-2 text-xs font-semibold text-text-muted uppercase tracking-wider">
        Sportarten
      </h2>
      <button
        v-for="sport in sports"
        :key="sport.key ?? 'all'"
        class="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition-colors text-left"
        :class="
          matches.activeSport === sport.key
            ? 'bg-primary-muted/20 text-primary font-medium'
            : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
        "
        @click="selectSport(sport.key)"
      >
        <span class="text-base" aria-hidden="true">{{ sport.icon }}</span>
        <span>{{ sport.label }}</span>
      </button>
    </nav>
  </aside>

  <!-- Mobile: Horizontal scroll bar -->
  <div
    v-if="props.variant === 'bar'"
    class="lg:hidden overflow-x-auto border-b border-surface-3 bg-surface-1 scrollbar-hide"
    role="tablist"
    aria-label="Sportarten"
  >
    <div class="flex gap-1 px-3 py-2 min-w-max">
      <button
        v-for="sport in sports"
        :key="sport.key ?? 'all'"
        role="tab"
        :aria-selected="matches.activeSport === sport.key"
        class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors shrink-0"
        :class="
          matches.activeSport === sport.key
            ? 'bg-primary-muted/20 text-primary font-medium'
            : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
        "
        @click="selectSport(sport.key)"
      >
        <span class="text-base" aria-hidden="true">{{ sport.icon }}</span>
        <span>{{ sport.label }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.scrollbar-hide {
  -ms-overflow-style: none;
  scrollbar-width: none;
}
.scrollbar-hide::-webkit-scrollbar {
  display: none;
}
</style>
