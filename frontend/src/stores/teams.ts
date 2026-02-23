import { defineStore } from "pinia";
import { ref } from "vue";
import { useApi } from "@/composables/useApi";

export interface TeamSearchResult {
  team_key: string;
  display_name: string;
  slug: string;
  sport_keys: string[];
  current_league: string | null;
}

export const useTeamsStore = defineStore("teams", () => {
  const api = useApi();

  const searchResults = ref<TeamSearchResult[]>([]);
  const searchLoading = ref(false);

  async function search(query: string, sportKey?: string) {
    if (query.length < 2) {
      searchResults.value = [];
      return;
    }
    searchLoading.value = true;
    try {
      const params: Record<string, string> = { q: query };
      if (sportKey) params.sport_key = sportKey;
      searchResults.value = await api.get<TeamSearchResult[]>("/teams/search", params);
    } catch {
      searchResults.value = [];
    } finally {
      searchLoading.value = false;
    }
  }

  return { searchResults, searchLoading, search };
});
