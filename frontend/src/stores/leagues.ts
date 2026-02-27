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
  league_id: number;
  name: string;
  country: string | null;
  country_code: string | null;
  ui_order: number;
}

interface NavigationApiItem {
  id: number;
  league_id: number;
  name: string;
  country: string | null;
  country_code: string | null;
  ui_order: number;
}

interface NavigationResponse {
  items: NavigationApiItem[];
}

export const useLeagueStore = defineStore("league-navigation", () => {
  const api = useApi();
  const navigation = ref<NavigationLeague[]>([]);
  const loading = ref(false);
  const fetched = ref(false);

  const LS_KEY = "nav_leagues";
  const hydrated = ref(false);

function normalizeNavigation(items: NavigationApiItem[] | NavigationLeague[]): NavigationLeague[] {
  return (items || []).map((item) => ({
    league_id: Number((item as NavigationApiItem).id ?? (item as NavigationLeague).league_id),
    name: String(item.name || "").trim(),
    country: item.country ?? null,
    country_code: item.country_code ?? null,
    ui_order: Number(item.ui_order ?? 999),
  })).filter((item) => Number.isInteger(item.league_id) && item.league_id > 0 && !!item.name);
}

  // Hydrate from localStorage immediately (sync, no flash)
  try {
    const cached = localStorage.getItem(LS_KEY);
    if (cached) {
      navigation.value = normalizeNavigation(JSON.parse(cached));
      hydrated.value = true;
    }
  } catch { /* corrupt localStorage — ignore */ }

  async function fetchNavigation(force = false): Promise<void> {
    if (fetched.value && !force) return;
    loading.value = !hydrated.value;
    try {
      const result = await api.get<NavigationResponse>("/leagues/navigation");
      navigation.value = normalizeNavigation(result.items);
      fetched.value = true;
      localStorage.setItem(LS_KEY, JSON.stringify(navigation.value));
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
