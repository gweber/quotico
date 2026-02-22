import { defineStore } from "pinia";
import { ref, computed, watch } from "vue";
import { useApi } from "@/composables/useApi";
import type { Match } from "./matches";

export interface BetSlipItem {
  matchId: string;
  teams: { home: string; away: string };
  prediction: string; // "1", "X", or "2"
  predictionLabel: string; // "Bayern München" or "Unentschieden"
  odds: number;
  sportKey: string;
}

const STORAGE_KEY = "quotico-betslip";

export const useBetSlipStore = defineStore("betslip", () => {
  const api = useApi();
  const items = ref<BetSlipItem[]>(loadFromStorage());
  const submitting = ref(false);
  const isOpen = ref(false); // Mobile bottom sheet toggle

  const totalOdds = computed(() =>
    items.value.reduce((sum, item) => sum + item.odds, 0)
  );

  const itemCount = computed(() => items.value.length);

  function addItem(match: Match, prediction: string) {
    // Remove existing tip for same match
    items.value = items.value.filter((i) => i.matchId !== match.id);

    const labels: Record<string, string> = {
      "1": match.teams.home,
      "X": "Unentschieden",
      "2": match.teams.away,
    };

    items.value.push({
      matchId: match.id,
      teams: match.teams,
      prediction,
      predictionLabel: labels[prediction] || prediction,
      odds: match.current_odds[prediction],
      sportKey: match.sport_key,
    });

    isOpen.value = true; // Auto-open on mobile
  }

  function removeItem(matchId: string) {
    items.value = items.value.filter((i) => i.matchId !== matchId);
  }

  function clear() {
    items.value = [];
  }

  async function submitAll(): Promise<{ success: string[]; errors: string[] }> {
    submitting.value = true;
    const success: string[] = [];
    const errors: string[] = [];

    for (const item of items.value) {
      try {
        await api.post("/tips/", {
          match_id: item.matchId,
          prediction: item.prediction,
          displayed_odds: item.odds,
        });
        success.push(`${item.teams.home} vs ${item.teams.away}`);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Unbekannter Fehler";
        errors.push(`${item.teams.home} vs ${item.teams.away}: ${msg}`);
      }
    }

    // Remove successfully submitted items
    const successMatchIds = new Set(
      items.value
        .filter((_, i) => i < success.length)
        .map((item) => item.matchId)
    );
    items.value = items.value.filter((i) => !successMatchIds.has(i.matchId));

    submitting.value = false;
    return { success, errors };
  }

  // Persist to localStorage
  watch(items, (val) => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(val));
  }, { deep: true });

  function loadFromStorage(): BetSlipItem[] {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  }

  return {
    items,
    submitting,
    isOpen,
    totalOdds,
    itemCount,
    addItem,
    removeItem,
    clear,
    submitAll,
  };
});
