/**
 * frontend/src/stores/leagues.ts
 *
 * Purpose:
 * Runtime store for public league navigation rendered in SportNav. Fetches
 * backend-ordered active+tippable leagues from League Tower.
 *
 * Dependencies:
 * - pinia
 * - @/composables/useApi
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface NavigationLeague {
  id: string;
  sport_key: string;
  name: string;
  country: string | null;
  country_code: string | null;
  ui_order: number;
}

interface NavigationResponse {
  items: NavigationLeague[];
}

export const useLeagueStore = defineStore("league-navigation", () => {
  const api = useApi();
  const navigation = ref<NavigationLeague[]>([]);
  const loading = ref(false);
  const fetched = ref(false);

  const LS_KEY = "nav_leagues";
  const hydrated = ref(false);

  // Hydrate from localStorage immediately (sync, no flash)
  try {
    const cached = localStorage.getItem(LS_KEY);
    if (cached) {
      navigation.value = JSON.parse(cached);
      hydrated.value = true;
    }
  } catch { /* corrupt localStorage — ignore */ }

  async function fetchNavigation(force = false): Promise<void> {
    if (fetched.value && !force) return;
    loading.value = !hydrated.value;
    try {
      const result = await api.get<NavigationResponse>("/leagues/navigation");
      navigation.value = result.items;
      fetched.value = true;
      localStorage.setItem(LS_KEY, JSON.stringify(result.items));
    } catch {
      if (!hydrated.value) navigation.value = [];
    } finally {
      loading.value = false;
    }
  }

  return {
    navigation,
    loading,
    fetched,
    fetchNavigation,
  };
});
