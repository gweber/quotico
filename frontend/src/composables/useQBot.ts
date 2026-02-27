import { ref, type Ref } from "vue";
import { useApi } from "./useApi";

export interface QBotStreak {
  type: "won" | "lost" | null;
  count: number;
}

export interface QBotHero {
  total_bets: number;
  won: number;
  lost: number;
  win_rate: number;
  total_points: number;
  rank: number;
  streak: QBotStreak;
}

export interface QBotBet {
  match_id: string;
  home_team: string;
  away_team: string;
  league_id: number;
  match_date: string;
  selection: string;
  locked_odds: number;
  status: string;
  points_earned: number | null;
  confidence: number | null;
  edge_pct: number | null;
  created_at: string;
}

export interface QBotSportPerformance {
  league_id: number;
  total: number;
  won: number;
  win_rate: number;
  total_points: number;
}

export interface QBotTrendPoint {
  date: string;
  win_rate: number;
  bet_number: number;
}

export interface QBotCandidate {
  match_id: string;
  home_team: string;
  away_team: string;
  league_id: number;
  match_date: string;
  recommended_selection: string;
  confidence: number;
  edge_pct: number;
  true_probability: number;
  implied_probability: number;
  justification: string;
  justification_full: string;
  signals: {
    h2h_meetings: number | null;
    sharp_movement: boolean;
    momentum_gap: number | null;
  };
  generated_at: string;
}

export interface QBotCalibrationBucket {
  bucket: string;
  total: number;
  correct: number;
  win_rate: number;
  avg_confidence: number;
}

export interface QBotDashboard {
  hero: QBotHero;
  active_bets: QBotBet[];
  candidates: QBotCandidate[];
  recent_bets: QBotBet[];
  by_sport: QBotSportPerformance[];
  win_rate_trend: QBotTrendPoint[];
  calibration: QBotCalibrationBucket[];
}

export function useQBot() {
  const api = useApi();
  const data: Ref<QBotDashboard | null> = ref(null);
  const loading = ref(false);
  const error = ref(false);

  async function fetch() {
    loading.value = true;
    error.value = false;

    try {
      data.value = await api.get<QBotDashboard>("/qbot/dashboard");
    } catch {
      error.value = true;
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, fetch };
}
