<script setup lang="ts">
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useTeamsStore } from "@/stores/teams";
import { sportLabel } from "@/types/sports";

useI18n();

const teams = useTeamsStore();
const searchQuery = ref("");
let searchTimeout: ReturnType<typeof setTimeout> | null = null;

watch(searchQuery, (q) => {
  if (searchTimeout) clearTimeout(searchTimeout);
  const trimmed = q.trim();
  if (trimmed.length < 2) {
    teams.searchResults = [];
    return;
  }
  searchTimeout = setTimeout(() => {
    teams.search(trimmed);
  }, 300);
});
</script>

<template>
  <div class="max-w-2xl mx-auto p-4">
    <h1 class="text-xl font-bold text-text-primary mb-6">Teams</h1>

    <!-- Search -->
    <label for="team-search" class="sr-only">Team suchen</label>
    <input
      id="team-search"
      v-model="searchQuery"
      type="text"
      placeholder="Team suchen..."
      autocomplete="off"
      class="w-full bg-surface-1 border border-surface-3/50 rounded-lg px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted/50 focus:outline-none focus:ring-1 focus:ring-primary mb-6"
    />

    <!-- Loading -->
    <div v-if="teams.searchLoading" class="space-y-3">
      <div v-for="n in 4" :key="n" class="bg-surface-1 rounded-card h-16 animate-pulse" />
    </div>

    <!-- Results -->
    <div v-else-if="searchQuery.trim().length >= 2 && teams.searchResults.length > 0" class="space-y-3">
      <RouterLink
        v-for="team in teams.searchResults"
        :key="team.team_key"
        :to="`/team/${team.slug}`"
        class="block bg-surface-1 rounded-card p-4 border border-surface-3/50 hover:border-surface-3 transition-colors"
      >
        <div class="flex items-center justify-between gap-3">
          <h3 class="text-sm font-semibold text-text-primary truncate">{{ team.display_name }}</h3>
          <span
            v-if="team.current_league"
            class="text-xs px-2.5 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20 shrink-0 font-medium"
          >
            {{ sportLabel(team.current_league) }}
          </span>
        </div>
        <div v-if="team.sport_keys.filter(sk => sk !== team.current_league).length > 0" class="flex flex-wrap gap-1.5 mt-1.5">
          <span
            v-for="sk in team.sport_keys.filter(sk => sk !== team.current_league)"
            :key="sk"
            class="text-xs px-2 py-0.5 rounded-full bg-surface-2 text-text-muted border border-surface-3/50"
          >
            {{ sportLabel(sk) }}
          </span>
        </div>
      </RouterLink>
    </div>

    <!-- Empty search -->
    <div
      v-else-if="searchQuery.trim().length >= 2 && !teams.searchLoading && teams.searchResults.length === 0"
      class="text-center py-12"
    >
      <p class="text-sm text-text-muted">{{ $t('teams.noTeamFound') }}</p>
    </div>

    <!-- Initial state -->
    <div v-else-if="searchQuery.trim().length < 2" class="text-center py-16">
      <p class="text-4xl mb-4" aria-hidden="true">&#x1F3DF;&#xFE0F;</p>
      <p class="text-sm text-text-secondary">
        Gib mindestens 2 Zeichen ein, um nach einem Team zu suchen.
      </p>
    </div>
  </div>
</template>
