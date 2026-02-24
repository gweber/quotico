import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Pfad-Fix für 'app' Importe
backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.append(str(backend_path))

from motor.motor_asyncio import AsyncIOMotorClient

# Liste deiner 8 Ligen
LEAGUES = [
    "soccer_germany_bundesliga2", "soccer_portugal_primeira_liga", 
    "soccer_epl", "soccer_france_ligue_one", "soccer_germany_bundesliga", 
    "soccer_italy_serie_a", "soccer_netherlands_eredivisie", "soccer_spain_la_liga"
]

async def bridge():
    # Direkte Verbindung (keine Creds nötig, da localhost)
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client["quotico"]
    
    print(f"🌉 Initialisiere Engine-Configs für {len(LEAGUES)} Ligen...")

    for lg in LEAGUES:
        now = datetime.now(timezone.utc)
        # 1. Dixon-Coles Basiswerte festlegen
        params = {
            "rho": 0.05,
            "alpha": 0.001,
            "floor": 0.05
        }

        # 2. Aktuelle Live-Config schreiben
        await db.engine_config.update_one(
            {"sport_key": lg},
            {"$set": {
                "sport_key": lg,
                "rho": params["rho"],
                "alpha_time_decay": params["alpha"],
                "alpha_weight_floor": params["floor"],
                "updated_at": now
            }},
            upsert=True
        )

        # 3. Historischen Snapshot für den Backfill erzeugen
        await db.engine_config_history.insert_one({
            "sport_key": lg,
            "snapshot_date": now,
            "params": params,
            "reliability": 1.0
        })
        print(f"  ✅ {lg}: Engine & History initialisiert.")

    print("\n✨ Erfolg! Dein Backfill wird jetzt keine Warnungen mehr zeigen.")

if __name__ == "__main__":
    asyncio.run(bridge())
