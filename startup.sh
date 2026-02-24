#!/bin/bash

# Konfiguration
API_KEY="some-random-secret-key"
LEAGUES=(
    "soccer_france_ligue_one"
    "soccer_netherlands_eredivisie"
    "soccer_portugal_primeira_liga"
    "soccer_epl"
    "soccer_germany_bundesliga"
    "soccer_germany_bundesliga2"
    "soccer_spain_la_liga"
    "soccer_italy_serie_a"
)

# xG Support (Understat Top 5)
XG_LEAGUES=("soccer_epl" "soccer_germany_bundesliga" "soccer_spain_la_liga" "soccer_italy_serie_a" "soccer_france_ligue_one")

source backend/.venv/bin/activate
mkdir -p logs

echo "🧹 Räume alte Logs auf..."
rm -f logs/backfill_*.log

echo "🚀 Phase 1: Download der Fußball-Daten (CSV-Import)"
python tools/football_history_backfiller.py --api-key "$API_KEY"

echo "🚀 Phase 2: xG-Veredelung (Sequenziell)"
for XG in "${XG_LEAGUES[@]}"; do
    echo "  -> Lade xG-Daten für $XG..."
    # Falls das Tool den Key via Flag erwartet:
    python -m tools.enrich_matches_xg --sport "$XG" 
done

echo "🚀 Phase 2.1: time maschine"
python -m tools.engine_time_maschine --mode auto --interval-days 30 --concurrency 2 

echo "🚀 Phase 3: Paralleler Backfill auf allen 8 P-Cores"
for LG in "${LEAGUES[@]}"; do
    echo "  -> Zünde Backfill-Kern für $LG..."
    # Rerun sorgt für frische Tabellen pro Liga
    python -m tools.qtip_backfill --sport "$LG" --batch-size 1500 --rerun > "logs/backfill_${LG}.log" 2>&1 &
done

echo "----------------------------------------------------------"
echo "✅ Alle 8 Prozesse wurden an die P-Cores übergeben."
echo "📊 Überwachung: 'asitop' für Hardware, 'tail -f logs/backfill_*.log' für Fortschritt."
echo "----------------------------------------------------------"