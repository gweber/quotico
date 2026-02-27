export type GameModeType =
  | "classic"
  | "bankroll"
  | "survivor"
  | "over_under"
  | "fantasy"
  | "moneyline";

export interface LeagueConfig {
  league_id: number;
  game_mode: GameModeType;
  config: Record<string, unknown>;
  activated_at: string;
  deactivated_at: string | null;
}

/** i18n key mapping for game mode labels — resolve with t() at call sites */
export const GAME_MODE_I18N_KEYS: Record<GameModeType, string> = {
  classic: "gameModes.classic",
  bankroll: "gameModes.bankroll",
  survivor: "gameModes.survivor",
  over_under: "gameModes.overUnder",
  fantasy: "gameModes.fantasy",
  moneyline: "gameModes.moneyline",
};
