<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { useI18n } from "vue-i18n";
import { useMatchesStore } from "@/stores/matches";
import { useAuthStore } from "@/stores/auth";
import { prefetchMatchHistory } from "@/composables/useMatchHistory";
import { prefetchUserBets } from "@/composables/useUserBets";
import SportNav from "@/components/layout/SportNav.vue";
import BetSlip from "@/components/layout/BetSlip.vue";
import MatchCard from "@/components/MatchCard.vue";
const { t } = useI18n();
const matches = useMatchesStore();
const auth = useAuthStore();
const aliasBannerDismissed = ref(false);
const error = ref(false);

async function reload() {
  error.value = false;
  try {
    await matches.fetchMatches();

    // Prefetch historical data, QuoticoTips, and user bets for all visible matches
    if (matches.matches.length > 0) {
      const prefetches: Promise<void>[] = [
        prefetchMatchHistory(
          matches.matches.map((m) => ({
            home_team: m.home_team,
            away_team: m.away_team,
            sport_key: m.sport_key,
          })),
        ),
      ];

      if (auth.isLoggedIn) {
        prefetches.push(prefetchUserBets(matches.matches.map((m) => m.id)));
      }

      await Promise.all(prefetches);
    }

    matches.connectLive();
  } catch {
    error.value = true;
  }
}

onMounted(() => reload());

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
              {{ t('dashboard.playingAs', { alias: auth.user?.alias }) }}
            </span>
            <RouterLink
              to="/settings"
              class="shrink-0 text-sm font-medium text-primary hover:underline"
            >
              {{ $t('dashboard.choosePlayerName') }}
            </RouterLink>
          </div>
          <button
            class="shrink-0 text-text-muted hover:text-text-primary transition-colors"
            :aria-label="$t('dashboard.closeBanner')"
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

        <!-- Error state -->
        <div v-else-if="error" class="text-center py-12">
          <p class="text-text-muted mb-3">{{ $t('common.loadError') }}</p>
          <button class="text-sm text-primary hover:underline" @click="reload">{{ $t('common.retry') }}</button>
        </div>

        <!-- Empty state -->
        <div
          v-else-if="matches.matches.length === 0"
          class="flex flex-col items-center justify-center py-20"
        >
          <span class="text-4xl mb-4" aria-hidden="true">⚽</span>
          <h2 class="text-lg font-semibold text-text-primary mb-2">
            {{ $t('dashboard.noMatches') }}
          </h2>
          <p class="text-sm text-text-secondary text-center max-w-xs">
            {{ $t('dashboard.noMatchesMessage') }}
          </p>
        </div>

        <!-- Match cards -->
        <div v-else class="space-y-3" :aria-label="$t('dashboard.matchOverview')">
          <!-- Refresh countdown -->
          <div class="flex items-center justify-end gap-2 text-xs text-text-muted">
            <span
              class="inline-flex items-center gap-1 tabular-nums"
              :title="$t('dashboard.oddsRefreshInfo')"
            >
              <svg class="w-3 h-3 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              {{ Math.floor(matches.refreshCountdown / 60) }}:{{ String(matches.refreshCountdown % 60).padStart(2, '0') }}
            </span>
          </div>
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
