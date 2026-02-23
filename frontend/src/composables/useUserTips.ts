import { reactive } from "vue";
import { useApi } from "./useApi";

export interface UserTip {
  id: string;
  match_id: string;
  selection: { type: string; value: string };
  locked_odds: number;
  points_earned: number | null;
  status: string; // "pending" | "won" | "lost" | "void"
  created_at: string;
}

// Shared reactive cache keyed by match_id (reactive so computed() tracks changes)
const tipCache = reactive(new Map<string, UserTip>());

/**
 * Prefetch user tips for the given match IDs in a single request.
 * Populates the shared cache so getCachedUserTip() returns instantly.
 */
export async function prefetchUserTips(matchIds: string[]): Promise<void> {
  if (matchIds.length === 0) return;
  const api = useApi();
  try {
    const tips = await api.get<UserTip[]>("/tips/mine", {
      match_ids: matchIds.join(","),
    });
    for (const tip of tips) {
      tipCache.set(tip.match_id, tip);
    }
  } catch {
    // Prefetch failed silently — cards will just show normal state
  }
}

/**
 * Get a cached user tip by match_id (no network call).
 * Returns undefined if no tip is cached for this match.
 */
export function getCachedUserTip(matchId: string): UserTip | undefined {
  return tipCache.get(matchId);
}

/**
 * Manually add a tip to the cache (called after betslip submission
 * to avoid an extra API round-trip).
 */
export function cacheUserTip(tip: UserTip): void {
  tipCache.set(tip.match_id, tip);
}

/**
 * Clear the tip cache (on logout or sport change).
 */
export function clearUserTipCache(): void {
  tipCache.clear();
}
