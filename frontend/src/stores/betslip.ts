import { defineStore } from "pinia";
import { ref, computed, onUnmounted } from "vue";
import { useApi } from "@/composables/useApi";
import type { Match } from "./matches";

export interface BetSlipItem {
  matchId: string;
  homeTeam: string;
  awayTeam: string;
  prediction: string; // "1", "X", or "2"
  predictionLabel: string; // "Bayern München" or "Unentschieden"
  odds: number;
  sportKey: string;
  matchDate: string; // ISO date string for expiry check
  addedAt: number; // Timestamp when added (for staleness warning)
}

export interface OddsChange {
  matchId: string;
  label: string;
  prediction: string;
  oldOdds: number;
  newOdds: number;
}

/** Flat bet shape returned to BetSlip.vue for cacheUserBet() compatibility. */
export interface BetResponseData {
  id: string;
  match_id: string;
  selection: { type: string; value: string };
  locked_odds: number;
  points_earned: number | null;
  status: string;
  created_at: string;
}

interface SlipSelection {
  match_id: string;
  market: string;
  pick: string;
  displayed_odds?: number;
  locked_odds?: number;
  points_earned?: number | null;
  status: string;
}

interface SlipResponse {
  id: string;
  type: string;
  selections: SlipSelection[];
  total_odds: number | null;
  stake: number;
  potential_payout: number | null;
  status: string;
  submitted_at: string | null;
  created_at: string;
}

const DRAFT_KEY = "quotico-draft-id";
const ITEMS_KEY = "quotico-betslip";
const STALE_THRESHOLD_MS = 10 * 60 * 1000; // 10 minutes → show warning

export const useBetSlipStore = defineStore("betslip", () => {
  const api = useApi();

  // Local display state (optimistic mirror of server draft)
  const items = ref<BetSlipItem[]>(loadItemsFromStorage());
  const draftSlipId = ref<string | null>(localStorage.getItem(DRAFT_KEY));
  const submitting = ref(false);
  const syncing = ref(false);
  const isOpen = ref(false); // Mobile bottom sheet toggle

  // Pre-submit odds change confirmation
  const pendingOddsChanges = ref<OddsChange[]>([]);
  const showOddsChangeDialog = ref(false);

  // Tick every 10s to detect expired items
  const now = ref(Date.now());
  const _ticker = setInterval(() => { now.value = Date.now(); }, 10_000);
  onUnmounted(() => clearInterval(_ticker));

  const validItems = computed(() =>
    items.value.filter((i) => i.matchDate && new Date(i.matchDate).getTime() > now.value)
  );

  const expiredItems = computed(() =>
    items.value.filter((i) => !i.matchDate || new Date(i.matchDate).getTime() <= now.value)
  );

  /** Items added more than 10 minutes ago (odds may be stale). */
  const staleItems = computed(() =>
    validItems.value.filter((i) => i.addedAt && (now.value - i.addedAt) > STALE_THRESHOLD_MS)
  );

  const totalOdds = computed(() =>
    validItems.value.reduce((sum, item) => sum + item.odds, 0)
  );

  const itemCount = computed(() => items.value.length);

  // ---- Server draft helpers ----

  async function ensureDraft(): Promise<string> {
    if (draftSlipId.value) return draftSlipId.value;
    const draft = await api.post<SlipResponse>("/betting-slips/draft", { type: "single" });
    draftSlipId.value = draft.id;
    localStorage.setItem(DRAFT_KEY, draft.id);
    return draft.id;
  }

  function persistItems() {
    localStorage.setItem(ITEMS_KEY, JSON.stringify(items.value));
  }

  // ---- Public actions ----

  function addItem(match: Match, prediction: string) {
    // Remove existing bet for same match
    items.value = items.value.filter((i) => i.matchId !== match.id);

    const labels: Record<string, string> = {
      "1": match.home_team,
      "X": "Unentschieden",
      "2": match.away_team,
    };

    items.value.push({
      matchId: match.id,
      homeTeam: match.home_team,
      awayTeam: match.away_team,
      prediction,
      predictionLabel: labels[prediction] || prediction,
      odds: match.odds.h2h[prediction],
      sportKey: match.sport_key,
      matchDate: match.match_date,
      addedAt: Date.now(),
    });
    persistItems();
    isOpen.value = true; // Auto-open on mobile

    // Background server sync (fire-and-forget)
    syncAddSelection(match.id, prediction, match.odds.h2h[prediction]);
  }

  async function syncAddSelection(matchId: string, pick: string, displayedOdds: number) {
    try {
      syncing.value = true;
      const id = await ensureDraft();
      await api.patch(`/betting-slips/${id}/selections`, {
        action: "add",
        match_id: matchId,
        market: "h2h",
        pick,
        displayed_odds: displayedOdds,
      });
    } catch {
      // Server rejection — remove from local state
      items.value = items.value.filter((i) => i.matchId !== matchId);
      persistItems();
    } finally {
      syncing.value = false;
    }
  }

  function removeItem(matchId: string) {
    items.value = items.value.filter((i) => i.matchId !== matchId);
    persistItems();

    // Background server sync
    if (draftSlipId.value) {
      syncRemoveSelection(matchId);
    }
  }

  async function syncRemoveSelection(matchId: string) {
    if (!draftSlipId.value) return;
    try {
      syncing.value = true;
      await api.patch(`/betting-slips/${draftSlipId.value}/selections`, {
        action: "remove",
        match_id: matchId,
      });
      // If no items left, discard the empty draft
      if (items.value.length === 0) {
        await api.del(`/betting-slips/${draftSlipId.value}`);
        draftSlipId.value = null;
        localStorage.removeItem(DRAFT_KEY);
      }
    } catch {
      // Best effort — server will auto-cleanup stale drafts
    } finally {
      syncing.value = false;
    }
  }

  function clear() {
    const oldDraftId = draftSlipId.value;
    items.value = [];
    draftSlipId.value = null;
    localStorage.removeItem(DRAFT_KEY);
    persistItems();

    // Background discard
    if (oldDraftId) {
      api.del(`/betting-slips/${oldDraftId}`).catch(() => {});
    }
  }

  function removeExpired() {
    const removed = expiredItems.value.length;
    if (removed > 0) {
      const removedIds = expiredItems.value.map((i) => i.matchId);
      items.value = validItems.value.slice();
      persistItems();
      // Sync removals to server
      for (const matchId of removedIds) {
        if (draftSlipId.value) {
          api.patch(`/betting-slips/${draftSlipId.value}/selections`, {
            action: "remove",
            match_id: matchId,
          }).catch(() => {});
        }
      }
    }
    return removed;
  }

  /** Update betslip item odds to current server values and clear the dialog. */
  function acceptOddsChanges() {
    for (const change of pendingOddsChanges.value) {
      const item = items.value.find((i) => i.matchId === change.matchId);
      if (item) {
        item.odds = change.newOdds;
        item.addedAt = Date.now();
        // Sync updated odds to server draft
        if (draftSlipId.value) {
          api.patch(`/betting-slips/${draftSlipId.value}/selections`, {
            action: "update",
            match_id: change.matchId,
            market: "h2h",
            pick: change.prediction,
            displayed_odds: change.newOdds,
          }).catch(() => {});
        }
      }
    }
    persistItems();
    pendingOddsChanges.value = [];
    showOddsChangeDialog.value = false;
  }

  function dismissOddsChanges() {
    pendingOddsChanges.value = [];
    showOddsChangeDialog.value = false;
  }

  async function submitAll(): Promise<{ success: string[]; errors: string[]; bets: BetResponseData[] }> {
    submitting.value = true;
    const success: string[] = [];
    const errors: string[] = [];
    const bets: BetResponseData[] = [];

    // Drop expired items before submitting
    const expired = removeExpired();
    if (expired > 0) {
      errors.push(`${expired} expired bet${expired > 1 ? "s" : ""} removed.`);
    }

    if (validItems.value.length === 0) {
      submitting.value = false;
      return { success, errors, bets };
    }

    // Pre-submit: fetch fresh match data and check for odds drift
    const oddsChanges: OddsChange[] = [];
    try {
      const freshMatches = await api.get<Match[]>("/matches/", { status: "scheduled" });
      const freshMap = new Map(freshMatches.map((m) => [m.id, m]));

      for (const item of validItems.value) {
        const fresh = freshMap.get(item.matchId);
        if (!fresh) continue;
        const currentOdds = fresh.odds.h2h[item.prediction];
        if (currentOdds != null && currentOdds !== item.odds) {
          oddsChanges.push({
            matchId: item.matchId,
            label: `${item.homeTeam} vs ${item.awayTeam}`,
            prediction: item.prediction,
            oldOdds: item.odds,
            newOdds: currentOdds,
          });
        }
      }
    } catch {
      // If pre-check fails, proceed with submission anyway
    }

    // If odds changed, show confirmation dialog instead of submitting
    if (oddsChanges.length > 0) {
      pendingOddsChanges.value = oddsChanges;
      showOddsChangeDialog.value = true;
      submitting.value = false;
      return { success, errors: ["Quoten haben sich geändert. Bitte bestätigen."], bets };
    }

    try {
      // Ensure server draft exists with all selections
      const slipId = await ensureDraft();

      // Sync any items that might not be on the server yet
      for (const item of validItems.value) {
        try {
          await api.patch(`/betting-slips/${slipId}/selections`, {
            action: "add",
            match_id: item.matchId,
            market: "h2h",
            pick: item.prediction,
            displayed_odds: item.odds,
          });
        } catch {
          // Already exists — that's fine
        }
      }

      // Submit the draft → locks odds, transitions to pending
      const slip = await api.post<SlipResponse>(`/betting-slips/${slipId}/submit`);

      // Map response to flat BetResponseData for cacheUserBet compatibility
      for (const sel of slip.selections) {
        const item = validItems.value.find((i) => i.matchId === sel.match_id);
        if (item) success.push(`${item.homeTeam} vs ${item.awayTeam}`);
        bets.push({
          id: slip.id,
          match_id: sel.match_id,
          selection: { type: sel.market, value: sel.pick },
          locked_odds: sel.locked_odds ?? sel.displayed_odds ?? 0,
          points_earned: null,
          status: slip.status,
          created_at: slip.submitted_at ?? slip.created_at,
        });
      }

      // Clear local state
      items.value = [];
      draftSlipId.value = null;
      localStorage.removeItem(DRAFT_KEY);
      persistItems();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Unknown error";
      errors.push(msg);
    }

    submitting.value = false;
    return { success, errors, bets };
  }

  /** Load existing server draft and rebuild local items. */
  async function loadDraft() {
    try {
      const draft = await api.get<SlipResponse | null>("/betting-slips/draft", { type: "single" });
      if (!draft) {
        // No server draft — if we have local items without a draftSlipId, keep them
        if (draftSlipId.value) {
          draftSlipId.value = null;
          localStorage.removeItem(DRAFT_KEY);
        }
        return;
      }

      draftSlipId.value = draft.id;
      localStorage.setItem(DRAFT_KEY, draft.id);

      // If server has selections but local items are empty, rebuild from server
      if (draft.selections.length > 0 && items.value.length === 0) {
        const matchIds = draft.selections.map((s) => s.match_id);
        try {
          const matches = await api.get<Match[]>("/matches/", { ids: matchIds.join(",") });
          const matchMap = new Map(matches.map((m) => [m.id, m]));

          items.value = draft.selections.map((sel) => {
            const match = matchMap.get(sel.match_id);
            const labels: Record<string, string> = match
              ? { "1": match.home_team, "X": "Unentschieden", "2": match.away_team }
              : {};
            return {
              matchId: sel.match_id,
              homeTeam: match?.home_team ?? "",
              awayTeam: match?.away_team ?? "",
              prediction: sel.pick,
              predictionLabel: labels[sel.pick] ?? sel.pick,
              odds: sel.displayed_odds ?? sel.locked_odds ?? 0,
              sportKey: match?.sport_key ?? "",
              matchDate: match?.match_date ?? "",
              addedAt: Date.now(),
            };
          });
          persistItems();
        } catch {
          // Can't fetch match data — keep items empty
        }
      }
    } catch {
      // Not logged in or no draft — use local state as-is
    }
  }

  function loadItemsFromStorage(): BetSlipItem[] {
    try {
      const stored = localStorage.getItem(ITEMS_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  }

  return {
    items,
    submitting,
    syncing,
    isOpen,
    totalOdds,
    itemCount,
    validItems,
    expiredItems,
    staleItems,
    pendingOddsChanges,
    showOddsChangeDialog,
    draftSlipId,
    addItem,
    removeItem,
    removeExpired,
    acceptOddsChanges,
    dismissOddsChanges,
    clear,
    submitAll,
    loadDraft,
  };
});
