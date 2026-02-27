/**
 * frontend/src/types/MatchV3.ts
 *
 * Purpose:
 * Canonical frontend types for Sportmonks v3.1 match cards, odds summaries,
 * xG justice hints, events, period scores, and referee display metadata.
 */

export interface OddSummary {
  min: number;
  max: number;
  avg: number;
  count: number;
}

export interface FixedSnapshot {
  h: number;
  d: number;
  a: number;
  ts_utc: string;
}

export interface MarketEntropy {
  current_spread_pct: number;
  drift_velocity_3h: number;
}

// FIXME: ODDS_V3_BREAK — type still valid but data no longer produced by connector
export interface OddsMetaV3 {
  summary_1x2?: {
    home?: OddSummary;
    draw?: OddSummary;
    away?: OddSummary;
  };
  fixed_snapshots?: {
    opening?: FixedSnapshot;
    alpha_24h?: FixedSnapshot;
    beta_6h?: FixedSnapshot;
    omega_1h?: FixedSnapshot;
    closing?: FixedSnapshot;
  };
  market_entropy?: MarketEntropy;
  source?: string;
  updated_at?: string;
}

export interface MatchTeamV3 {
  sm_id?: number | null;
  name?: string | null;
  short_code?: string | null;
  image_path?: string | null;
  score?: number | null;
  xg?: number | null;
}

export interface MatchEventV3 {
  type: 'goal' | 'card' | 'var' | 'missed_penalty';
  minute: number | null;
  extra_minute: number | null;
  player_name: string;
  player_id: number | null;
  team_id: number | null;
  detail: string;
  sort_order: number | null;
}

export interface PeriodScoreV3 {
  home?: number | null;
  away?: number | null;
}

export interface PeriodScoresV3 {
  half_time?: PeriodScoreV3;
  full_time?: PeriodScoreV3;
}

export interface MatchV3 {
  id: string;
  status?: string;
  finish_type?: string | null;
  start_at?: string;
  match_date?: string;
  has_advanced_stats?: boolean;
  teams?: {
    home?: MatchTeamV3;
    away?: MatchTeamV3;
  };
  events?: MatchEventV3[];
  scores?: PeriodScoresV3;
  odds_meta?: OddsMetaV3;
  manual_check_required?: boolean;
  referee_id?: number | null;
  referee_name?: string | null;
  qtip?: MatchQtipPayload | null;
  qtip_output_level?: OutputLevel;
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

export type OddsButtonKey = '1' | 'X' | '2';
export type OddsBadge = 'live' | 'closing' | 'none';
export type JusticeState = 'unlucky' | 'overperformed' | 'none';

export interface OddsButtonVM {
  key: OddsButtonKey;
  avg: number | null;
  min: number | null;
  max: number | null;
  count: number | null;
}

export interface JusticeDiff {
  home: number | null;
  away: number | null;
}

export interface MatchCardVM {
  oddsButtons: OddsButtonVM[];
  oddsBadge: OddsBadge;
  justice: {
    home: JusticeState;
    away: JusticeState;
    enabled: boolean;
  };
  justiceDiff: JusticeDiff;
  refereeId?: number;
  refereeName?: string;
}
import type { MatchQtipPayload, OutputLevel } from "./persona";
