/**
 * frontend/src/types/persona.ts
 *
 * Purpose:
 * Shared strict enums/types for persona governance and qtip output levels.
 */

export type TipPersona = "casual" | "pro" | "silent" | "experimental";
export type TipPersonaSource = "user" | "override" | "policy" | "default";
export type OutputLevel = "none" | "summary" | "full" | "experimental";

export interface MatchQtipSummary {
  match_id: number;
  recommended_selection: string;
  confidence: number;
  status: string;
  source_output_level: "summary";
}

export interface MatchQtipFull {
  match_id: number;
  league_id: number;
  home_team: string;
  away_team: string;
  match_date: string | null;
  recommended_selection: string;
  confidence: number;
  raw_confidence?: number | null;
  edge_pct: number;
  true_probability: number;
  implied_probability: number;
  expected_goals_home: number;
  expected_goals_away: number;
  tier_signals: Record<string, unknown>;
  justification: string;
  skip_reason: string | null;
  qbot_logic?: Record<string, unknown> | null;
  generated_at: string | null;
  source_output_level: "full" | "experimental";
  decision_trace?: Record<string, unknown> | null;
  arena_metrics?: Record<string, unknown> | null;
}

export type MatchQtipPayload = MatchQtipSummary | MatchQtipFull;
