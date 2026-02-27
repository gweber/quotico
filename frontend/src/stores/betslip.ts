/**
 * frontend/src/stores/betslip.ts
 *
 * Purpose:
 * Client-side bet slip state with draft-sync, odds drift confirmation, and
 * automatic odds synchronization from the matches store.
 */

import { defineStore } from "pinia";
import { ref, computed, onUnmounted, watch } from "vue";
import { useApi, HttpError } from "@/composables/useApi";
import { useAuthStore } from "./auth";
import { useMatchesStore, type Match } from "./matches";
import { oddsValueBySelection } from "@/composables/useMatchV3Adapter";
import type { MatchV3, OddsButtonKey } from "@/types/MatchV3";

type BetSlipItemState = "valid" | "expired" | "invalid_missing";
type InvalidReason = "match_missing" | "match_not_scheduled" | "match_locked";

export interface BetSlipItem {
  matchId: string;
  homeTeam: string;
  awayTeam: string;
  prediction: string; // "1", "X", or "2"
  predictionLabel: string; // "Bayern München" or "Unentschieden"
  odds: number;
  sportKey: number;
  matchDate: string; // ISO date string for expiry check
  addedAt: number; // Timestamp when added (for staleness warning)
  state: BetSlipItemState;
  invalidReason?: InvalidReason | null;
  invalidAt?: number | null;
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
  invalid_reason?: InvalidReason | null;
  invalid_at?: string | null;
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

export const useBetSlipStore = defineStore("betslip", () => {
  const api = useApi();
  const matchesStore = useMatchesStore();

  // Local display state (optimistic mirror of server draft)
  const items = ref<BetSlipItem[]>(loadItemsFromStorage());
  const draftSlipId = ref<string | null>(localStorage.getItem(DRAFT_KEY));
  const submitting = ref(false);
  const syncing = ref(false);
  const isOpen = ref(false); // Mobile bottom sheet toggle

  // Pre-submit odds change confirmation
  const pendingOddsChanges = ref<OddsChange[]>([]);
  const showOddsChangeDialog = ref(false);
  const lastOddsSyncAt = ref<number | null>(null);
  const recentlyUpdatedItemIds = ref<Set<string>>(new Set());
  const totalOddsRecentlyUpdated = ref(false);
  const lastReconcileFailedAt = ref<number | null>(null);

  // Tick every 10s to detect expired items
  const now = ref(Date.now());
  const _ticker = setInterval(() => { now.value = Date.now(); }, 10_000);
  onUnmounted(() => clearInterval(_ticker));

  const activeItems = computed(() =>
    items.value.filter((i) => i.state === "valid" && i.matchDate && new Date(i.matchDate).getTime() > now.value)
  );

  const validItems = computed(() => activeItems.value);

  const expiredItems = computed(() =>
    items.value.filter((i) =>
      i.state === "expired" || (i.state === "valid" && (!i.matchDate || new Date(i.matchDate).getTime() <= now.value))
    )
  );

  const invalidItems = computed(() =>
    items.value.filter((i) => i.state === "invalid_missing")
  );

  const totalOdds = computed(() =>
    activeItems.value.reduce((sum, item) => sum + item.odds, 0)
  );

  const itemCount = computed(() => items.value.length);
  let _recentlyUpdatedClearTimer: ReturnType<typeof setTimeout> | null = null;
  let _totalPulseTimer: ReturnType<typeof setTimeout> | null = null;
  let _draftSyncTimer: ReturnType<typeof setTimeout> | null = null;
  const _pendingDraftUpdates = new Map<string, { prediction: string; odds: number }>();

  function toMatchCompat(row: any): Match {
    if (row && typeof row.home_team === "string" && typeof row.away_team === "string") {
      return row as Match;
    }
    const homeId = Number(row?.teams?.home?.sm_id || 0);
    const awayId = Number(row?.teams?.away?.sm_id || 0);
    // FIXME: ODDS_V3_BREAK — reads odds_meta.summary_1x2 for bet placement odds which is no longer produced by connector
    const summary = row?.odds_meta?.summary_1x2 || {};
    const h2h: Record<string, number> = {};
    const homeAvg = Number(summary?.home?.avg);
    const drawAvg = Number(summary?.draw?.avg);
    const awayAvg = Number(summary?.away?.avg);
    if (Number.isFinite(homeAvg)) h2h["1"] = homeAvg;
    if (Number.isFinite(drawAvg)) h2h["X"] = drawAvg;
    if (Number.isFinite(awayAvg)) h2h["2"] = awayAvg;
    return {
      id: String(row?._id ?? row?.id ?? ""),
      league_id: typeof row?.league_id === "number" ? row.league_id : 0,
      home_team: `Team ${homeId || "?"}`,
      away_team: `Team ${awayId || "?"}`,
      match_date: String(row?.start_at || ""),
      start_at: String(row?.start_at || ""),
      status: String(row?.status || "SCHEDULED").toLowerCase(),
      odds: { h2h, updated_at: row?.odds_meta?.updated_at || null },
      result: { home_score: null, away_score: null, outcome: null },
      odds_meta: row?.odds_meta,
      has_advanced_stats: Boolean(row?.has_advanced_stats),
      referee_id: row?.referee_id ?? null,
      teams: row?.teams,
    } as Match;
  }

  function _isLockedMatch(match: Match): boolean {
    const kickoff = new Date(match.match_date || match.start_at || "").getTime();
    return Number.isFinite(kickoff) && kickoff <= Date.now();
  }

  async function _voidDraftSelection(matchId: string, reason: InvalidReason): Promise<void> {
    if (!draftSlipId.value) return;
    const auth = useAuthStore();
    if (!auth.isLoggedIn) return;
    try {
      await api.patch(`/betting-slips/${draftSlipId.value}/selections`, {
        action: "invalidate",
        match_id: matchId,
        reason,
      });
    } catch {
      // Best effort: draft reconciliation retries later.
    }
  }

  async function reconcileOnLifecycle(): Promise<boolean> {
    const uniqueIds = [...new Set(items.value.map((item) => Number(item.matchId)).filter((id) => Number.isFinite(id)))];
    if (uniqueIds.length === 0) return true;
    try {
      const query = await api.post<{ items: Match[] }>("/v3/matches/query", {
        ids: uniqueIds,
        statuses: ["SCHEDULED"],
        limit: Math.min(200, Math.max(1, uniqueIds.length)),
      });
      const freshMatches = (query.items || []).map(toMatchCompat);
      const freshMap = new Map(freshMatches.map((m) => [m.id, m]));
      const toInvalidate: Array<{ matchId: string; reason: InvalidReason }> = [];
      let changed = false;
      items.value = items.value.map((item) => {
        const fresh = freshMap.get(item.matchId);
        if (!fresh) {
          if (item.state !== "invalid_missing" || item.invalidReason !== "match_missing") {
            changed = true;
            toInvalidate.push({ matchId: item.matchId, reason: "match_missing" });
          }
          return {
            ...item,
            state: "invalid_missing",
            invalidReason: "match_missing",
            invalidAt: item.invalidAt ?? Date.now(),
          };
        }

        const status = String(fresh.status || "").toLowerCase();
        const reason: InvalidReason | null = status !== "scheduled"
          ? "match_not_scheduled"
          : _isLockedMatch(fresh)
            ? "match_locked"
            : null;
        if (reason) {
          if (item.state !== "invalid_missing" || item.invalidReason !== reason) {
            changed = true;
            toInvalidate.push({ matchId: item.matchId, reason });
          }
          return {
            ...item,
            homeTeam: fresh.home_team,
            awayTeam: fresh.away_team,
            sportKey: fresh.league_id,
            matchDate: fresh.match_date,
            state: "invalid_missing",
            invalidReason: reason,
            invalidAt: item.invalidAt ?? Date.now(),
          };
        }

        const nextState: BetSlipItemState = _isLockedMatch(fresh) ? "expired" : "valid";
        if (item.state !== nextState || item.homeTeam !== fresh.home_team || item.awayTeam !== fresh.away_team || item.sportKey !== fresh.league_id || item.matchDate !== fresh.match_date) {
          changed = true;
        }
        return {
          ...item,
          homeTeam: fresh.home_team,
          awayTeam: fresh.away_team,
          sportKey: fresh.league_id,
          matchDate: fresh.match_date,
          state: nextState,
          invalidReason: null,
          invalidAt: null,
        };
      });
      if (changed) persistItems();
      if (toInvalidate.length > 0) {
        for (const row of toInvalidate) {
          await _voidDraftSelection(row.matchId, row.reason);
        }
      }
      lastReconcileFailedAt.value = null;
      return true;
    } catch {
      // Fail-safe: never mutate leg state on query failures.
      lastReconcileFailedAt.value = Date.now();
      return false;
    }
  }

  // ---- Server draft helpers ----

  async function ensureDraft(): Promise<string> {
    if (draftSlipId.value) return draftSlipId.value;
    const draft = await api.post<SlipResponse>("/betting-slips/draft", { type: "single" });
    draftSlipId.value = draft.id;
    localStorage.setItem(DRAFT_KEY, draft.id);
    return draft.id;
  }

  /** Clear cached draft ID (e.g. when the server says it's no longer a draft). */
  function invalidateDraft() {
    draftSlipId.value = null;
    localStorage.removeItem(DRAFT_KEY);
  }

  function isStaleSlipError(e: unknown): boolean {
    return e instanceof HttpError && (e.status === 400 || e.status === 404);
  }

  function persistItems() {
    localStorage.setItem(ITEMS_KEY, JSON.stringify(items.value));
  }

  function _queueDraftOddsUpdate(item: BetSlipItem) {
    if (!draftSlipId.value) return;
    const auth = useAuthStore();
    if (!auth.isLoggedIn) return;
    _pendingDraftUpdates.set(item.matchId, { prediction: item.prediction, odds: item.odds });
    if (_draftSyncTimer) return;
    _draftSyncTimer = setTimeout(async () => {
      _draftSyncTimer = null;
      if (!draftSlipId.value || _pendingDraftUpdates.size === 0) return;
      const updates = [..._pendingDraftUpdates.entries()];
      _pendingDraftUpdates.clear();
      for (const [matchId, payload] of updates) {
        try {
          await api.patch(`/betting-slips/${draftSlipId.value}/selections`, {
            action: "update",
            match_id: matchId,
            market: "h2h",
            pick: payload.prediction,
            displayed_odds: payload.odds,
          });
        } catch {
          // Best effort — submit flow revalidates odds again.
        }
      }
    }, 300);
  }

  function syncOddsFromMatchMap(matchMap: Map<string, Match>): number {
    if (items.value.length === 0) return 0;
    const changedIds: string[] = [];
    for (const item of validItems.value) {
      const match = matchMap.get(item.matchId);
      if (!match) continue;
      const currentOdds = oddsValueBySelection(
        match as unknown as MatchV3,
        item.prediction as OddsButtonKey,
      );
      if (currentOdds == null || currentOdds === item.odds) continue;
      item.odds = currentOdds;
      item.addedAt = Date.now();
      changedIds.push(item.matchId);
      _queueDraftOddsUpdate(item);
    }
    if (changedIds.length === 0) return 0;
    persistItems();
    lastOddsSyncAt.value = Date.now();
    const nextSet = new Set(recentlyUpdatedItemIds.value);
    for (const id of changedIds) nextSet.add(id);
    recentlyUpdatedItemIds.value = nextSet;
    totalOddsRecentlyUpdated.value = true;
    if (_recentlyUpdatedClearTimer) clearTimeout(_recentlyUpdatedClearTimer);
    _recentlyUpdatedClearTimer = setTimeout(() => {
      recentlyUpdatedItemIds.value = new Set();
    }, 3000);
    if (_totalPulseTimer) clearTimeout(_totalPulseTimer);
    _totalPulseTimer = setTimeout(() => {
      totalOddsRecentlyUpdated.value = false;
    }, 1200);
    return changedIds.length;
  }

  watch(
    () => matchesStore.matches,
    (rows) => {
      if (!rows || rows.length === 0) return;
      syncOddsFromMatchMap(new Map(rows.map((m) => [m.id, m])));
    },
    { deep: false },
  );

  // ---- Public actions ----

  function addItem(match: Match, prediction: string) {
    const selectedOdds = oddsValueBySelection(
      match as unknown as MatchV3,
      prediction as OddsButtonKey
    );
    if (selectedOdds == null) return;

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
      odds: selectedOdds,
      sportKey: match.league_id,
      matchDate: match.match_date,
      addedAt: Date.now(),
      state: _isLockedMatch(match) ? "expired" : "valid",
      invalidReason: null,
      invalidAt: null,
    });
    persistItems();
    isOpen.value = true; // Auto-open on mobile

    // Background server sync — only for authenticated users
    const auth = useAuthStore();
    if (auth.isLoggedIn) {
      syncAddSelection(match.id, prediction, selectedOdds);
    }
  }

  async function syncAddSelection(matchId: string, pick: string, displayedOdds: number) {
    const payload = {
      action: "add",
      match_id: matchId,
      market: "h2h",
      pick,
      displayed_odds: displayedOdds,
    };
    try {
      syncing.value = true;
      const id = await ensureDraft();
      await api.patch(`/betting-slips/${id}/selections`, payload);
    } catch (e) {
      // Stale draft (already submitted / deleted) — clear and retry once
      if (draftSlipId.value && isStaleSlipError(e)) {
        invalidateDraft();
        try {
          const newId = await ensureDraft();
          await api.patch(`/betting-slips/${newId}/selections`, payload);
          return;
        } catch {
          // Retry also failed — keep item locally, submitAll() will re-sync
        }
      }
      // Keep item in local state — submitAll() re-syncs all items before submission.
      // Previously this removed the item, causing bets to vanish silently.
    } finally {
      syncing.value = false;
    }
  }

  function removeItem(matchId: string) {
    items.value = items.value.filter((i) => i.matchId !== matchId);
    persistItems();

    // Background server sync — only for authenticated users with an active draft
    const auth = useAuthStore();
    if (auth.isLoggedIn && draftSlipId.value) {
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

  function removeInvalid() {
    const removed = invalidItems.value.length;
    if (removed > 0) {
      const removedIds = invalidItems.value.map((i) => i.matchId);
      items.value = items.value.filter((i) => i.state !== "invalid_missing");
      persistItems();
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

    await reconcileOnLifecycle();

    // Drop expired items before submitting
    const expired = removeExpired();
    if (expired > 0) {
      errors.push(`${expired} expired bet${expired > 1 ? "s" : ""} removed.`);
    }

    if (activeItems.value.length === 0) {
      submitting.value = false;
      return { success, errors, bets };
    }

    // Pre-submit: fetch fresh match data and check for odds drift
    const oddsChanges: OddsChange[] = [];
    try {
      const queryIds = [...new Set(activeItems.value.map((item) => Number(item.matchId)).filter((id) => Number.isFinite(id)))];
      const query = await api.post<{ items: Match[] }>("/v3/matches/query", {
        ids: queryIds,
        statuses: ["SCHEDULED"],
        limit: Math.min(200, Math.max(1, queryIds.length)),
      });
      const freshMatches = (query.items || []).map(toMatchCompat);
      const freshMap = new Map(freshMatches.map((m) => [m.id, m]));

      for (const item of activeItems.value) {
        const fresh = freshMap.get(item.matchId);
        if (!fresh) continue;
        const currentOdds = oddsValueBySelection(
          fresh as unknown as MatchV3,
          item.prediction as OddsButtonKey
        );
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
      let slipId = await ensureDraft();

      // Sync any items that might not be on the server yet
      for (const item of activeItems.value) {
        try {
          await api.patch(`/betting-slips/${slipId}/selections`, {
            action: "add",
            match_id: item.matchId,
            market: "h2h",
            pick: item.prediction,
            displayed_odds: item.odds,
          });
        } catch (e) {
          // Stale draft — get a fresh one and re-sync remaining items
          if (isStaleSlipError(e)) {
            invalidateDraft();
            slipId = await ensureDraft();
            await api.patch(`/betting-slips/${slipId}/selections`, {
              action: "add",
              match_id: item.matchId,
              market: "h2h",
              pick: item.prediction,
              displayed_odds: item.odds,
            });
          }
          // 409 (already exists) is fine — ignore
        }
      }

      // Submit the draft → locks odds, transitions to pending
      const slip = await api.post<SlipResponse>(`/betting-slips/${slipId}/submit`);

      // Map response to flat BetResponseData for cacheUserBet compatibility
      for (const sel of slip.selections) {
        const item = activeItems.value.find((i) => i.matchId === sel.match_id);
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
          const response = await api.post<{ items: Match[] }>("/v3/matches/query", {
            ids: matchIds.map((id) => Number(id)).filter((id) => Number.isFinite(id)),
            limit: 200,
          });
          const matches = (response.items || []).map(toMatchCompat);
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
              sportKey: match?.league_id ?? 0,
              matchDate: match?.match_date ?? "",
              addedAt: Date.now(),
              state: sel.status === "void"
                ? "invalid_missing"
                : ((match && _isLockedMatch(match)) ? "expired" : "valid"),
              invalidReason: sel.invalid_reason ?? (sel.status === "void" ? "match_missing" : null),
              invalidAt: sel.invalid_at ? new Date(sel.invalid_at).getTime() : null,
            };
          });
          persistItems();
        } catch {
          // Can't fetch match data — keep items empty
        }
      }
    } catch {
      // Not logged in or no draft — use local state as-is
    } finally {
      if (items.value.length > 0) {
        await reconcileOnLifecycle();
      }
    }
  }

  function loadItemsFromStorage(): BetSlipItem[] {
    try {
      const stored = localStorage.getItem(ITEMS_KEY);
      const rows = stored ? JSON.parse(stored) : [];
      if (!Array.isArray(rows)) return [];
      return rows.map((row: any) => ({
        ...row,
        state: row?.state === "invalid_missing" ? "invalid_missing" : (row?.state === "expired" ? "expired" : "valid"),
        invalidReason: row?.invalidReason ?? null,
        invalidAt: typeof row?.invalidAt === "number" ? row.invalidAt : null,
      }));
    } catch {
      return [];
    }
  }

  onUnmounted(() => {
    if (_recentlyUpdatedClearTimer) clearTimeout(_recentlyUpdatedClearTimer);
    if (_totalPulseTimer) clearTimeout(_totalPulseTimer);
    if (_draftSyncTimer) clearTimeout(_draftSyncTimer);
  });

  return {
    items,
    submitting,
    syncing,
    isOpen,
    totalOdds,
    itemCount,
    validItems,
    invalidItems,
    expiredItems,
    lastOddsSyncAt,
    recentlyUpdatedItemIds,
    totalOddsRecentlyUpdated,
    lastReconcileFailedAt,
    pendingOddsChanges,
    showOddsChangeDialog,
    draftSlipId,
    addItem,
    removeItem,
    removeExpired,
    removeInvalid,
    acceptOddsChanges,
    dismissOddsChanges,
    clear,
    submitAll,
    loadDraft,
    reconcileOnLifecycle,
    syncOddsFromMatchMap,
  };
});
