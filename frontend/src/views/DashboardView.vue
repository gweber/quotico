<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { useMatchesStore } from "@/stores/matches";
import { useAuthStore } from "@/stores/auth";
import SportNav from "@/components/layout/SportNav.vue";
import BetSlip from "@/components/layout/BetSlip.vue";
import MatchCard from "@/components/MatchCard.vue";

const matches = useMatchesStore();
const auth = useAuthStore();
const aliasBannerDismissed = ref(false);

onMounted(async () => {
  await matches.fetchMatches();
  matches.connectLive();
});

onUnmounted(() => {
  matches.disconnectLive();
});
</script>

<template>
  <div class="min-h-[calc(100vh-3.5rem)] flex flex-col">
    <!-- Mobile: horizontal sport bar -->
    <SportNav variant="bar" />

    <!-- 3-col layout -->
    <div class="flex-1 max-w-screen-2xl mx-auto w-full flex">
      <!-- Desktop: left sidebar -->
      <SportNav variant="sidebar" />

      <!-- Match feed (center) -->
      <main id="main-content" class="flex-1 min-w-0 p-4">
        <!-- Alias banner -->
        <div
          v-if="auth.isLoggedIn && !auth.user?.has_custom_alias && !aliasBannerDismissed"
          class="mb-4 flex items-center justify-between gap-3 p-3 bg-primary-muted/10 border border-primary/20 rounded-lg"
          role="status"
        >
          <div class="flex items-center gap-2 min-w-0">
            <span class="text-sm text-text-primary">
              Du tippst noch als <strong class="text-primary">{{ auth.user?.alias }}</strong>.
            </span>
            <RouterLink
              to="/settings"
              class="shrink-0 text-sm font-medium text-primary hover:underline"
            >
              Spielername wählen
            </RouterLink>
          </div>
          <button
            class="shrink-0 text-text-muted hover:text-text-primary transition-colors"
            aria-label="Banner schließen"
            @click="aliasBannerDismissed = true"
          >
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <!-- Loading skeleton -->
        <div v-if="matches.loading" class="space-y-4">
          <div
            v-for="n in 5"
            :key="n"
            class="bg-surface-1 rounded-card h-32 animate-pulse"
          />
        </div>

        <!-- Empty state -->
        <div
          v-else-if="matches.matches.length === 0"
          class="flex flex-col items-center justify-center py-20"
        >
          <span class="text-4xl mb-4" aria-hidden="true">⚽</span>
          <h2 class="text-lg font-semibold text-text-primary mb-2">
            Keine Spiele verfügbar
          </h2>
          <p class="text-sm text-text-secondary text-center max-w-xs">
            Aktuell sind keine Spiele für diese Sportart geplant. Schau später noch einmal vorbei.
          </p>
        </div>

        <!-- Match cards -->
        <div v-else class="space-y-3" aria-label="Spielübersicht">
          <MatchCard
            v-for="match in matches.matches"
            :key="match.id"
            :match="match"
          />
        </div>
      </main>

      <!-- Desktop: right sidebar with bet slip -->
      <BetSlip />
    </div>
  </div>
</template>
