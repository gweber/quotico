/** Central sport key → display label mapping. Import this everywhere instead of duplicating. */
export const SPORT_LABELS: Record<string, string> = {
  soccer_germany_bundesliga: "Bundesliga",
  soccer_germany_bundesliga2: "2. Bundesliga",
  soccer_epl: "Premier League",
  soccer_spain_la_liga: "La Liga",
  soccer_italy_serie_a: "Serie A",
  soccer_uefa_champs_league: "Champions League",
  soccer_france_ligue_one: "Ligue 1",
  soccer_netherlands_eredivisie: "Eredivisie",
  soccer_portugal_primeira_liga: "Primeira Liga",
  americanfootball_nfl: "NFL",
  basketball_nba: "NBA",
  tennis_atp_french_open: "Tennis ATP",
};

/** Central sport key → country flag mapping. */
export const SPORT_FLAGS: Record<string, string> = {
  soccer_germany_bundesliga: "🇩🇪",
  soccer_germany_bundesliga2: "🇩🇪",
  soccer_epl: "🇬🇧",
  soccer_spain_la_liga: "🇪🇸",
  soccer_italy_serie_a: "🇮🇹",
  soccer_france_ligue_one: "🇫🇷",
  soccer_netherlands_eredivisie: "🇳🇱",
  soccer_portugal_primeira_liga: "🇵🇹",
};

/** Resolve a sport_key to its display label, falling back to the raw key. */
export function sportLabel(key: string): string {
  return SPORT_LABELS[key] || key;
}

/** True for basketball sports where scores are in the hundreds. */
export function isBasketball(sportKey: string): boolean {
  return sportKey.startsWith("basketball_");
}

/** Sport-aware label for score totals: "Punkte" for basketball, "Tore" for football. */
export function scoreUnitLabel(sportKey: string): string {
  return isBasketball(sportKey) ? "Punkte" : "Tore";
}

/** Resolve a sport_key to its flag, falling back to white flag. */
export function sportFlag(key: string): string {
  return SPORT_FLAGS[key] || "🏳️";
}
