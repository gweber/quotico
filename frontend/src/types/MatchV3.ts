/**
 * frontend/src/types/MatchV3.ts
 *
 * Purpose:
 * Canonical frontend types for Sportmonks v3.1 match cards, odds summaries,
 * xG justice hints, and referee display metadata.
 */

export interface OddSummary {
  min: number;
  max: number;
  avg: number;
  count: number;
}

export interface OddsMetaV3 {
  summary_1x2?: {
    home?: OddSummary;
    draw?: OddSummary;
    away?: OddSummary;
  };
  source?: string;
  updated_at?: string;
}

export interface MatchTeamV3 {
  sm_id?: number | null;
  name?: string | null;
  score?: number | null;
  xg?: number | null;
}

export interface MatchV3 {
  id: string;
  status?: string;
  start_at?: string;
  match_date?: string;
  has_advanced_stats?: boolean;
  teams?: {
    home?: MatchTeamV3;
    away?: MatchTeamV3;
  };
  odds_meta?: OddsMetaV3;
  referee_id?: number | string | null;
  result?: {
    home_score?: number | null;
    away_score?: number | null;
    outcome?: string | null;
  };
  odds?: {
    h2h?: Record<string, number>;
    updated_at?: string | null;
  };
  [key: string]: unknown;
}

export type OddsButtonKey = "1" | "X" | "2";
export type OddsBadge = "live" | "closing" | "none";
export type JusticeState = "unlucky" | "overperformed" | "none";

export interface OddsButtonVM {
  key: OddsButtonKey;
  avg: number | null;
  min: number | null;
  max: number | null;
  count: number | null;
}

export interface MatchCardVM {
  oddsButtons: OddsButtonVM[];
  oddsBadge: OddsBadge;
  justice: {
    home: JusticeState;
    away: JusticeState;
    enabled: boolean;
  };
  refereeId?: number;
  refereeName?: string;
}

