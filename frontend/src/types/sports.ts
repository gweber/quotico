/** Central sport key → display label mapping. Import this everywhere instead of duplicating. */
export const SPORT_LABELS: Record<string, string> = {
  soccer_germany_bundesliga: "Bundesliga",
  soccer_germany_bundesliga2: "2. Bundesliga",
  soccer_epl: "Premier League",
  soccer_spain_la_liga: "La Liga",
  soccer_italy_serie_a: "Serie A",
  soccer_uefa_champs_league: "Champions League",
  americanfootball_nfl: "NFL",
  basketball_nba: "NBA",
  tennis_atp_french_open: "Tennis ATP",
};

/** Resolve a sport_key to its display label, falling back to the raw key. */
export function sportLabel(key: string): string {
  return SPORT_LABELS[key] || key;
}
