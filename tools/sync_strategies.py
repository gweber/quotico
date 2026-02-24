import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
# Wir importieren motor direkt, um die 'NoneType' Falle zu umgehen
from motor.motor_asyncio import AsyncIOMotorClient

# Pfad-Fix
backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.append(str(backend_path))

STRATEGIES = [
    {"key": "soccer_germany_bundesliga2", "roi": 0.086, "active": True},
    {"key": "soccer_portugal_primeira_liga", "roi": 0.030, "active": True},
    {"key": "soccer_epl", "roi": 0.027, "active": True},
    {"key": "soccer_france_ligue_one", "roi": -0.004, "active": False},
    {"key": "soccer_germany_bundesliga", "roi": -0.067, "active": False},
    {"key": "soccer_italy_serie_a", "roi": -0.127, "active": False},
    {"key": "soccer_netherlands_eredivisie", "roi": -0.042, "active": False},
    {"key": "soccer_spain_la_liga", "roi": -0.058, "active": False},
]

async def sync():
    # Wir bauen die Verbindung hier direkt und manuell auf
    mongo_url = "mongodb://localhost:27017"
    client = AsyncIOMotorClient(mongo_url)
    db = client["quotico"] # Hier den Namen deiner DB eintragen, falls nicht 'quotico'
    
    print(f"🔄 Synchronisiere 8 Ligen direkt via {mongo_url}...")
    
    for s in STRATEGIES:
        now = datetime.now(timezone.utc)
        await db.qbot_strategies.update_one(
            {"sport_key": s["key"]},
            {
                "$set": {
                    "is_active": s["active"],
                    "is_shadow": not s["active"],
                    "roi_validation": s["roi"],
                    "updated_at": now,
                    "status": "calibrated" if s["active"] else "shadow_testing"
                }
            },
            upsert=True
        )
        print(f"  -> {s['key']}: {'ACTIVE' if s['active'] else 'SHADOW'}")

    print("\n✨ Erfolg! Alle Strategien sind in der Datenbank.")

if __name__ == "__main__":
    asyncio.run(sync())
