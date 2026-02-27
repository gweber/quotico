<!--
frontend/src/components/layout/SportNav.vue

Purpose:
    Dynamic sport navigation sourced from League Tower.
    Renders a sport -> league hierarchy and applies league_id filtering.
-->
<script setup lang="ts">
/**
 * Script section for SportNav runtime behavior.
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useMatchesStore } from "@/stores/matches";
import { useLeagueStore, type NavigationLeague } from "@/stores/leagues";

const props = withDefaults(defineProps<{
  variant?: "sidebar" | "bar";
}>(), {
  variant: "bar",
});

const matches = useMatchesStore();
const leagues = useLeagueStore();
const { t } = useI18n();

const selectedSportFamily = ref<string | null>(null);

const SPORT_ICONS: Record<string, string> = {
  americanfootball: "🏈",
  basketball: "🏀",
  tennis: "🎾",
  soccer: "⚽",
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

function leagueIcon(leagueId: number | string, countryCode: string | null): string {
  const byCountry = getFlagFromCountryCode(countryCode);
  if (byCountry) return byCountry;
  const family = sportFamilyFromKey(leagueId);
  return SPORT_ICONS[family] || "🎯";
}

function sportFamilyFromKey(leagueId: number | string): string {
  const numericId = Number(leagueId);
  if (!Number.isFinite(numericId)) return "other";
  if (numericId >= 5000) return "basketball";
  if (numericId >= 3000) return "tennis";
  if (numericId >= 1000) return "americanfootball";
  return "soccer";
}

function sportFamilyLabel(family: string): string {
  const key = `nav.sportFamilies.${family}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return family.replace(/_/g, " ");
}

interface SportGroup {
  family: string;
  label: string;
  icon: string;
  leagues: NavigationLeague[];
}

const sportGroups = computed<SportGroup[]>(() => {
  const grouped = new Map<string, NavigationLeague[]>();
  for (const league of leagues.navigation) {
    const family = sportFamilyFromKey(league.league_id);
    if (!grouped.has(family)) grouped.set(family, []);
    grouped.get(family)!.push(league);
  }

  return [...grouped.entries()]
    .map(([family, rows]) => ({
      family,
      label: sportFamilyLabel(family),
      icon: leagueIcon(rows[0]?.league_id || family, rows[0]?.country_code || null),
      leagues: [...rows].sort((a, b) => (a.ui_order - b.ui_order) || a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
});

const activeSportFamily = computed(() => {
  if (selectedSportFamily.value) return selectedSportFamily.value;
  return sportGroups.value[0]?.family || null;
});

const activeBarLeagues = computed(() => {
  if (!activeSportFamily.value) return [];
  return sportGroups.value.find((group) => group.family === activeSportFamily.value)?.leagues || [];
});

onMounted(async () => {
  await leagues.fetchNavigation();
  if (!selectedSportFamily.value && sportGroups.value.length > 0) {
    selectedSportFamily.value = sportGroups.value[0].family;
  }
});

function selectSportFamily(family: string, event?: Event) {
  selectedSportFamily.value = family;
  if (props.variant === "bar" && event) {
    (event.currentTarget as HTMLElement).scrollIntoView({
      behavior: "smooth",
      inline: "center",
      block: "nearest",
    });
  }
}

function selectLeague(leagueId: number | null, event?: Event) {
  matches.setLeague(leagueId);
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
    class="hidden lg:block w-64 shrink-0"
    :aria-label="t('nav.sportsLabel')"
  >
    <nav class="sticky top-[3.75rem] p-3 space-y-2">
      <h2 class="px-3 py-2 text-xs font-semibold text-text-muted uppercase tracking-wider">
        {{ t("nav.sportsLabel") }}
      </h2>

      <div v-if="sportGroups.length === 0" class="px-3 py-2 rounded-lg bg-warning/10 text-warning text-xs">
        <p class="font-medium">{{ t("nav.noTippableLeagues") }}</p>
        <p>{{ t("nav.enableInAdmin") }}</p>
      </div>

      <template v-else>
        <button
          class="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition-colors text-left"
          :class="
            matches.activeLeagueId === null
              ? 'bg-primary-muted/20 text-primary font-medium'
              : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
          "
          @click="selectLeague(null)"
        >
          <span class="text-base" aria-hidden="true">🎯</span>
          <span>{{ t("nav.allLeagues") }}</span>
        </button>

        <section
          v-for="group in sportGroups"
          :key="group.family"
          class="rounded-lg border border-surface-3/50 bg-surface-1/40"
        >
          <div class="px-3 py-2 text-xs font-semibold text-text-muted uppercase tracking-wide flex items-center gap-2">
            <span aria-hidden="true">{{ group.icon }}</span>
            <span>{{ group.label }}</span>
          </div>
          <div class="pb-2">
            <button
              v-for="league in group.leagues"
              :key="league.league_id"
              class="w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition-colors"
              :class="
                matches.activeLeagueId === league.league_id
                  ? 'bg-primary-muted/20 text-primary font-medium'
                  : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
              "
              @click="selectLeague(league.league_id)"
            >
              <span class="text-sm" aria-hidden="true">{{ leagueIcon(league.league_id, league.country_code) }}</span>
              <span>{{ league.name }}</span>
            </button>
          </div>
        </section>
      </template>
    </nav>
  </aside>

  <!-- Mobile: Sport tabs + league row -->
  <div
    v-if="props.variant === 'bar'"
    class="lg:hidden border-b border-surface-3 bg-surface-1"
    :aria-label="t('nav.sportsLabel')"
  >
    <div v-if="sportGroups.length === 0" class="px-3 py-2 text-xs text-warning bg-warning/10">
      <p class="font-medium">{{ t("nav.noTippableLeagues") }}</p>
      <p>{{ t("nav.enableInAdmin") }}</p>
    </div>

    <template v-else>
      <nav class="overflow-x-auto scrollbar-hide" :aria-label="t('nav.sportsLabel')">
        <div class="flex gap-1 px-3 pt-2 pb-1 min-w-max">
          <button
            v-for="group in sportGroups"
            :key="group.family"
            class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors shrink-0"
            :class="
              activeSportFamily === group.family
                ? 'bg-primary-muted/20 text-primary font-medium'
                : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
            "
            @click="selectSportFamily(group.family, $event)"
          >
            <span class="text-base" aria-hidden="true">{{ group.icon }}</span>
            <span>{{ group.label }}</span>
          </button>
        </div>
      </nav>

      <nav class="overflow-x-auto scrollbar-hide pb-2" :aria-label="t('nav.allLeagues')">
        <div class="flex gap-1 px-3 min-w-max">
          <button
            class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors shrink-0"
            :class="
              matches.activeLeagueId === null
                ? 'bg-primary-muted/20 text-primary font-medium'
                : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
            "
            @click="selectLeague(null, $event)"
          >
            <span class="text-base" aria-hidden="true">🎯</span>
            <span>{{ t("nav.allLeagues") }}</span>
          </button>

          <button
            v-for="league in activeBarLeagues"
            :key="league.league_id"
            class="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors shrink-0"
            :class="
              matches.activeLeagueId === league.league_id
                ? 'bg-primary-muted/20 text-primary font-medium'
                : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'
            "
            @click="selectLeague(league.league_id, $event)"
          >
            <span class="text-sm" aria-hidden="true">{{ leagueIcon(league.league_id, league.country_code) }}</span>
            <span>{{ league.name }}</span>
          </button>
        </div>
      </nav>
    </template>
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
