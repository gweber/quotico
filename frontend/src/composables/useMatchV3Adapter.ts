/**
 * frontend/src/composables/useMatchV3Adapter.ts
 *
 * Purpose:
 * Central adapter for v3.1 match cards. Maps raw match payloads to normalized
 * odds/xG/referee UI data with strict justice and badge rules.
 */

import type {
  MatchV3,
  MatchCardVM,
  JusticeDiff,
  OddsBadge,
  OddsButtonKey,
  OddsButtonVM,
  JusticeState,
} from "@/types/MatchV3";

const LIVE_ODDS_WINDOW_MS = 60 * 60 * 1000;
const JUSTICE_DELTA = 0.5;

function toNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function toDate(value: unknown): Date | null {
  if (!value) return null;
  const raw = String(value);
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

function statusIsFinal(value: unknown): boolean {
  const status = String(value || "").trim().toUpperCase();
  return status === "FINAL" || status === "FINISHED" || status === "FT";
}

// FIXME: ODDS_V3_BREAK — reads summary_1x2 and updated_at from odds_meta which are no longer produced by connector
export function toOddsSummary(match: MatchV3): OddsButtonVM[] {
  const summary = match.odds_meta?.summary_1x2;
  const mapFromSummary = (key: OddsButtonKey): OddsButtonVM => {
    const node = key === "1" ? summary?.home : key === "X" ? summary?.draw : summary?.away;
    return {
      key,
      avg: toNumber(node?.avg),
      min: toNumber(node?.min),
      max: toNumber(node?.max),
      count: toNumber(node?.count),
    };
  };

  const rows = (["1", "X", "2"] as OddsButtonKey[]).map(mapFromSummary);
  return rows;
}

export function oddsValueBySelection(match: MatchV3, key: OddsButtonKey): number | null {
  const row = toOddsSummary(match).find((entry) => entry.key === key);
  return row?.avg ?? null;
}

export function toOddsBadge(match: MatchV3, now: Date = new Date()): OddsBadge {
  const rows = toOddsSummary(match);
  if (!rows.some((row) => row.avg != null)) return "none";

  const startAt = toDate(match.start_at || match.match_date);
  if (!startAt) return "none";

  if (startAt.getTime() < now.getTime()) {
    return "closing";
  }

  const updatedAt = toDate(match.odds_meta?.updated_at || match.odds?.updated_at || null);
  if (!updatedAt) return "none";
  return now.getTime() - updatedAt.getTime() <= LIVE_ODDS_WINDOW_MS ? "live" : "none";
}

function extractGoals(match: MatchV3): { home: number | null; away: number | null } {
  const homeFromResult = toNumber(match.result?.home_score);
  const awayFromResult = toNumber(match.result?.away_score);
  if (homeFromResult != null && awayFromResult != null) {
    return { home: homeFromResult, away: awayFromResult };
  }
  return {
    home: toNumber(match.teams?.home?.score),
    away: toNumber(match.teams?.away?.score),
  };
}

function extractXg(match: MatchV3): { home: number | null; away: number | null } {
  return {
    home: toNumber(match.teams?.home?.xg),
    away: toNumber(match.teams?.away?.xg),
  };
}

export function computeJustice(match: MatchV3): MatchCardVM["justice"] {
  const base = { home: "none" as JusticeState, away: "none" as JusticeState, enabled: false };
  if (!statusIsFinal(match.status)) return base;
  if (!match.has_advanced_stats) return base;

  const goals = extractGoals(match);
  const xg = extractXg(match);
  if (goals.home == null || goals.away == null || xg.home == null || xg.away == null) {
    return base;
  }

  const homeUnlucky = goals.home <= goals.away && (xg.home - xg.away) > JUSTICE_DELTA;
  const awayUnlucky = goals.away <= goals.home && (xg.away - xg.home) > JUSTICE_DELTA;
  const homeOver = goals.home > goals.away && (xg.away - xg.home) > JUSTICE_DELTA;
  const awayOver = goals.away > goals.home && (xg.home - xg.away) > JUSTICE_DELTA;

  return {
    home: homeUnlucky ? "unlucky" : homeOver ? "overperformed" : "none",
    away: awayUnlucky ? "unlucky" : awayOver ? "overperformed" : "none",
    enabled: true,
  };
}

export function computeJusticeDiff(match: MatchV3): JusticeDiff {
  if (!statusIsFinal(match.status) || !match.has_advanced_stats) {
    return { home: null, away: null };
  }
  const xg = extractXg(match);
  const oddsHome = toNumber(match.odds_meta?.summary_1x2?.home?.avg);
  const oddsAway = toNumber(match.odds_meta?.summary_1x2?.away?.avg);
  if (xg.home == null || xg.away == null) return { home: null, away: null };
  const totalXg = xg.home + xg.away;
  if (totalXg === 0) return { home: null, away: null };

  const homeShare = xg.home / totalXg;
  const awayShare = xg.away / totalXg;
  return {
    home:
      oddsHome && oddsHome > 0
        ? +((homeShare - 1 / oddsHome).toFixed(3))
        : null,
    away:
      oddsAway && oddsAway > 0
        ? +((awayShare - 1 / oddsAway).toFixed(3))
        : null,
  };
}

export function toMatchCardVM(match: MatchV3): MatchCardVM {
  const refereeIdRaw = toNumber(match.referee_id);
  return {
    oddsButtons: toOddsSummary(match),
    oddsBadge: toOddsBadge(match),
    justice: computeJustice(match),
    justiceDiff: computeJusticeDiff(match),
    refereeId: refereeIdRaw == null ? undefined : Math.trunc(refereeIdRaw),
    refereeName: typeof match.referee_name === "string" ? match.referee_name.trim() || undefined : undefined,
  };
}

export function buildLegacyUnavailableMatch(matchId: string | number): MatchV3 {
  return {
    id: String(matchId),
    status: "SCHEDULED",
    start_at: new Date(0).toISOString(),
    has_advanced_stats: false,
    teams: {
      home: { sm_id: 0, name: "Legacy Match Missing" },
      away: { sm_id: 0, name: "Legacy Match Missing" },
    },
    odds_meta: {},
  };
}
