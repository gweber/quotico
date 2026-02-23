#!/usr/bin/env python3
"""Standalone EVD backtest — runs directly against MongoDB, no HTTP server needed.

Usage:
    cd backend && python -m tools.backtest_evd          # dry-run (read-only)
    cd backend && python -m tools.backtest_evd --backfill  # also write btb into resolved tips

Or from project root:
    PYTHONPATH=backend python tools/backtest_evd.py
"""

import asyncio
import sys
from pathlib import Path

# Ensure backend is on sys.path so `app.*` imports work
_backend = Path(__file__).resolve().parent.parent / "backend"
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

import app.database as _db
from app.services.quotico_tip_service import (
    compute_team_evd,
    EVD_BOOST_THRESHOLD,
    EVD_DAMPEN_THRESHOLD,
)
from app.services.historical_service import resolve_team_key, sport_keys_for


async def run_backtest(*, backfill: bool = False) -> None:
    await _db.connect_db()

    resolved = await _db.db.quotico_tips.find(
        {"status": "resolved", "was_correct": {"$ne": None}},
    ).to_list(length=5000)

    if not resolved:
        print("Keine resolved Tips vorhanden.")
        return

    buckets: dict[str, dict] = {
        "evd_positive": {"total": 0, "correct": 0},
        "evd_negative": {"total": 0, "correct": 0},
        "evd_neutral":  {"total": 0, "correct": 0},
        "no_data":      {"total": 0, "correct": 0},
    }
    backfill_count = 0
    processed = 0

    print(f"Analysiere {len(resolved)} resolved Tips …\n")

    for tip in resolved:
        teams = tip.get("teams", {})
        sport_key = tip.get("sport_key", "")
        selection = tip.get("recommended_selection", "-")
        was_correct = tip.get("was_correct", False)
        match_date = tip.get("match_commence_time")

        if selection == "-" or not match_date:
            continue

        related_keys = sport_keys_for(sport_key)
        home_key = await resolve_team_key(teams.get("home", ""), related_keys)
        away_key = await resolve_team_key(teams.get("away", ""), related_keys)

        if not home_key or not away_key:
            buckets["no_data"]["total"] += 1
            if was_correct:
                buckets["no_data"]["correct"] += 1
            processed += 1
            continue

        # Compute EVD as-of match date (temporal correctness)
        evd_home = await compute_team_evd(home_key, related_keys, before_date=match_date)
        evd_away = await compute_team_evd(away_key, related_keys, before_date=match_date)

        picked_evd = evd_home if selection == "1" else (evd_away if selection == "2" else None)

        if picked_evd and picked_evd["contributes"]:
            evd_val = picked_evd["evd"]
            if evd_val > EVD_BOOST_THRESHOLD:
                bucket = "evd_positive"
            elif evd_val < EVD_DAMPEN_THRESHOLD:
                bucket = "evd_negative"
            else:
                bucket = "evd_neutral"
        else:
            bucket = "no_data"

        buckets[bucket]["total"] += 1
        if was_correct:
            buckets[bucket]["correct"] += 1

        # Backfill
        if backfill:
            await _db.db.quotico_tips.update_one(
                {"_id": tip["_id"]},
                {"$set": {"tier_signals.btb": {"home": evd_home, "away": evd_away}}},
            )
            backfill_count += 1

        processed += 1
        if processed % 25 == 0:
            print(f"  … {processed}/{len(resolved)} verarbeitet")

    # Print results
    total_tips = sum(b["total"] for b in buckets.values())
    total_correct = sum(b["correct"] for b in buckets.values())

    print("\n" + "=" * 60)
    print("EVD BACKTEST ERGEBNISSE")
    print("=" * 60)
    print(f"\nGesamt analysiert: {total_tips}")
    if total_tips > 0:
        print(f"Gesamt Win Rate:   {total_correct/total_tips:.1%}")
    print()

    labels = {
        "evd_positive": "EVD positiv  (unterschätzt, > +10%)",
        "evd_negative": "EVD negativ  (überschätzt, < -10%)",
        "evd_neutral":  "EVD neutral  (zwischen Schwellwerten)",
        "no_data":      "Keine Daten  (< 5 Spiele mit Odds)  ",
    }

    for name, data in buckets.items():
        t = data["total"]
        c = data["correct"]
        wr = f"{c/t:.1%}" if t > 0 else "n/a"
        bar = "█" * int((c / t) * 20) if t > 0 else ""
        print(f"  {labels[name]}  {t:>4} Tips  {c:>4} richtig  WR {wr:>6}  {bar}")

    # Highlight the key comparison
    pos = buckets["evd_positive"]
    neg = buckets["evd_negative"]
    if pos["total"] >= 3 and neg["total"] >= 3:
        pos_wr = pos["correct"] / pos["total"]
        neg_wr = neg["correct"] / neg["total"]
        delta = pos_wr - neg_wr
        print(f"\n  Delta (positiv vs negativ): {delta:+.1%}")
        if delta > 0.05:
            print("  → Signal hat prädiktiven Wert!")
        elif delta > 0:
            print("  → Leichter Trend, aber noch nicht signifikant.")
        else:
            print("  → Kein klarer Vorteil erkennbar.")

    if backfill:
        print(f"\nBackfill: {backfill_count} Tips mit tier_signals.btb aktualisiert.")
    print()

    await _db.close_db()


def main():
    backfill = "--backfill" in sys.argv
    if backfill:
        print("Modus: Backtest + Backfill (schreibt btb in resolved Tips)\n")
    else:
        print("Modus: Backtest (read-only, --backfill zum Schreiben)\n")
    asyncio.run(run_backtest(backfill=backfill))


if __name__ == "__main__":
    main()
