<!--
frontend/src/components/layout/SportNav.vue

Purpose:
    Dynamic sport navigation sourced from League Tower instead of hardcoded keys.
    Renders active leagues in backend-defined order for sidebar/bar use.
-->
<script setup lang="ts">
/**
 * Script section for SportNav runtime behavior.
 */
import { computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useMatchesStore } from "@/stores/matches";
import { useLeagueStore } from "@/stores/leagues";
import { sportFlag } from "@/types/sports";

const props = withDefaults(defineProps<{
  variant?: "sidebar" | "bar";
}>(), {
  variant: "bar",
});

const matches = useMatchesStore();
const leagues = useLeagueStore();
const { t } = useI18n();

const SPORT_ICONS: Record<string, string> = {
  americanfootball_nfl: "🏈",
  basketball_nba: "🏀",
  tennis_atp_french_open: "🎾",
};

function getFlagFromCountryCode(countryCode: string | null): string | null {
  if (!countryCode || countryCode.length !== 2) return null;
  const upper = countryCode.toUpperCase();
  const base = 127397;
  return String.fromCodePoint(
    base + upper.charCodeAt(0),
    base + upper.charCodeAt(1),
  );
}

function leagueIcon(sportKey: string, countryCode: string | null): string {
  if (sportKey.startsWith("soccer_")) {
    const mapped = sportFlag(sportKey);
    if (mapped !== "🏳️") return mapped;
  }
  const byCountry = getFlagFromCountryCode(countryCode);
  if (byCountry) return byCountry;
  return SPORT_ICONS[sportKey] || "⚽";
}

const sports = computed(() => [
  { key: null, label: t("nav.allSports"), icon: "🎯" },
  ...leagues.navigation.map((league) => ({
    key: league.sport_key,
    label: league.name,
    icon: leagueIcon(league.sport_key, league.country_code),
  })),
]);

onMounted(() => {
  void leagues.fetchNavigation();
});

function selectSport(key: string | null, event?: Event) {
  matches.setSport(key);

  // On mobile bar, scroll the active button into view
  if (props.variant === "bar" && event) {
    (event.currentTarget as HTMLElement).scrollIntoView({
      behavior: "smooth",
      inline: "center",
      block: "nearest",
    });
  }
}
</script>

<template>
  <!-- Desktop: Sidebar -->
  <aside
    v-if="props.variant === 'sidebar'"
    class="hidden lg:block w-56 shrink-0"
    :aria-label="t('nav.sportsLabel')"
  >
    <nav class="sticky top-[3.75rem] p-3 space-y-0.5">
      <h2 class="px-3 py-2 text-xs font-semibold text-text-muted uppercase tracking-wider">
        {{ t("nav.sportsLabel") }}
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
  <nav
    v-if="props.variant === 'bar'"
    class="lg:hidden overflow-x-auto border-b border-surface-3 bg-surface-1 scrollbar-hide"
    :aria-label="t('nav.sportsLabel')"
  >
    <div class="flex gap-1 px-3 py-2 min-w-max">
      <button
        v-for="sport in sports"
        :key="sport.key ?? 'all'"
        :aria-pressed="matches.activeSport === sport.key"
        class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors shrink-0"
        :class="
          matches.activeSport === sport.key
            ? 'bg-primary-muted/20 text-primary font-medium'
            : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
        "
        @click="selectSport(sport.key, $event)"
      >
        <span class="text-base" aria-hidden="true">{{ sport.icon }}</span>
        <span>{{ sport.label }}</span>
      </button>
    </div>
  </nav>
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
