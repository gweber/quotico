import { reactive } from "vue";
import { useApi } from "./useApi";

export interface UserBet {
  id: string;
  match_id: string;
  selection: { type: string; value: string };
  locked_odds: number;
  points_earned: number | null;
  status: string; // "pending" | "won" | "lost" | "void"
  created_at: string;
}

/** Shape returned by GET /api/betting-slips/mine */
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
  status: string;
  submitted_at: string | null;
  created_at: string;
}

// Shared reactive cache keyed by match_id (reactive so computed() tracks changes)
const betCache = reactive(new Map<string, UserBet>());

/**
 * Prefetch user bets for the given match IDs in a single request.
 * Populates the shared cache so getCachedUserBet() returns instantly.
 */
export async function prefetchUserBets(matchIds: string[]): Promise<void> {
  if (matchIds.length === 0) return;
  const api = useApi();
  try {
    const slips = await api.get<SlipResponse[]>("/betting-slips/mine", {
      type: "single",
      match_ids: matchIds.join(","),
    });
    for (const slip of slips) {
      const sel = slip.selections[0];
      if (!sel) continue;
      betCache.set(sel.match_id, {
        id: slip.id,
        match_id: sel.match_id,
        selection: { type: sel.market || "h2h", value: sel.pick },
        locked_odds: sel.locked_odds ?? sel.displayed_odds ?? 0,
        points_earned: sel.points_earned ?? null,
        status: slip.status,
        created_at: slip.submitted_at ?? slip.created_at,
      });
    }
  } catch {
    // Prefetch failed silently — cards will just show normal state
  }
}

/**
 * Get a cached user bet by match_id (no network call).
 * Returns undefined if no bet is cached for this match.
 */
export function getCachedUserBet(matchId: string): UserBet | undefined {
  return betCache.get(matchId);
}

/**
 * Manually add a bet to the cache (called after betslip submission
 * to avoid an extra API round-trip).
 */
export function cacheUserBet(bet: UserBet): void {
  betCache.set(bet.match_id, bet);
}

/**
 * Clear the bet cache (on logout or sport change).
 */
export function clearUserBetCache(): void {
  betCache.clear();
}
