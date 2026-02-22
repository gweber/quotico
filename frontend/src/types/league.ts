export type GameModeType =
  | "classic"
  | "bankroll"
  | "survivor"
  | "over_under"
  | "fantasy"
  | "spieltag";

export interface LeagueConfig {
  sport_key: string;
  game_mode: GameModeType;
  config: Record<string, unknown>;
  activated_at: string;
  deactivated_at: string | null;
}

export const GAME_MODE_LABELS: Record<GameModeType, string> = {
  classic: "Klassisch",
  bankroll: "Bankroll",
  survivor: "Survivor",
  over_under: "Über/Unter",
  fantasy: "Fantasy",
  spieltag: "Spieltag",
};
