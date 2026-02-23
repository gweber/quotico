import { defineStore } from "pinia";
import { ref, computed, watch, onUnmounted } from "vue";
import { useApi } from "@/composables/useApi";
import type { Match } from "./matches";

export interface BetSlipItem {
  matchId: string;
  teams: { home: string; away: string };
  prediction: string; // "1", "X", or "2"
  predictionLabel: string; // "Bayern München" or "Unentschieden"
  odds: number;
  sportKey: string;
  commenceTime: string; // ISO date string for expiry check
}

const STORAGE_KEY = "quotico-betslip";

export const useBetSlipStore = defineStore("betslip", () => {
  const api = useApi();
  const items = ref<BetSlipItem[]>(loadFromStorage());
  const submitting = ref(false);
  const isOpen = ref(false); // Mobile bottom sheet toggle

  // Tick every 10s to detect expired items
  const now = ref(Date.now());
  const _ticker = setInterval(() => { now.value = Date.now(); }, 10_000);
  onUnmounted(() => clearInterval(_ticker));

  const validItems = computed(() =>
    items.value.filter((i) => i.commenceTime && new Date(i.commenceTime).getTime() > now.value)
  );

  const expiredItems = computed(() =>
    items.value.filter((i) => !i.commenceTime || new Date(i.commenceTime).getTime() <= now.value)
  );

  const totalOdds = computed(() =>
    validItems.value.reduce((sum, item) => sum + item.odds, 0)
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
      commenceTime: match.commence_time,
    });

    isOpen.value = true; // Auto-open on mobile
  }

  function removeItem(matchId: string) {
    items.value = items.value.filter((i) => i.matchId !== matchId);
  }

  function clear() {
    items.value = [];
  }

  function removeExpired() {
    const removed = expiredItems.value.length;
    if (removed > 0) {
      items.value = validItems.value.slice();
    }
    return removed;
  }

  interface TipResponseData {
    id: string;
    match_id: string;
    selection: { type: string; value: string };
    locked_odds: number;
    points_earned: number | null;
    status: string;
    created_at: string;
  }

  async function submitAll(): Promise<{ success: string[]; errors: string[]; tips: TipResponseData[] }> {
    submitting.value = true;
    const success: string[] = [];
    const errors: string[] = [];
    const tips: TipResponseData[] = [];
    const successMatchIds = new Set<string>();

    // Drop expired items before submitting
    const expired = removeExpired();
    if (expired > 0) {
      errors.push(`${expired} abgelaufene${expired > 1 ? " Tipps" : "r Tipp"} entfernt.`);
    }

    for (const item of items.value) {
      try {
        const response = await api.post<TipResponseData>("/tips/", {
          match_id: item.matchId,
          prediction: item.prediction,
          displayed_odds: item.odds,
        });
        success.push(`${item.teams.home} vs ${item.teams.away}`);
        successMatchIds.add(item.matchId);
        tips.push(response);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Unbekannter Fehler";
        errors.push(`${item.teams.home} vs ${item.teams.away}: ${msg}`);
      }
    }

    // Remove successfully submitted items
    items.value = items.value.filter((i) => !successMatchIds.has(i.matchId));

    submitting.value = false;
    return { success, errors, tips };
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
    validItems,
    expiredItems,
    addItem,
    removeItem,
    removeExpired,
    clear,
    submitAll,
  };
});
