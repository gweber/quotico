"""
backend/app/checks/tower_check.py

Purpose:
    Permanent self-check module for Tower architecture integrity. Validates DB
    connectivity, core league seeding, TeamRegistry readiness, and smoke-team
    resolution.

Dependencies:
    - app.database
    - app.services.league_service.LeagueService
    - app.services.team_registry_service
"""

import logging
import traceback

import app.database as _db
from app.services.league_service import LeagueService
from app.services.team_registry_service import TeamRegistry

logger = logging.getLogger("quotico.tower_check")


class TowerHealthCheck:
    """Central health check for DB + League Tower + Team Tower."""

    @staticmethod
    async def run() -> dict:
        report: dict = {
            "status": "UNKNOWN",
            "steps": {
                "database": "PENDING",
                "leagues_seeded": "PENDING",
                "team_registry": "PENDING",
                "resolution_test": "PENDING",
            },
            "details": {},
            "error": None,
        }

        try:
            if _db.db is None:
                raise RuntimeError("Database is not initialized. Call connect_db() first.")

            collections = await _db.db.list_collection_names()
            report["steps"]["database"] = "OK"
            report["details"]["collections_count"] = len(collections)

            seed_result = await LeagueService.seed_core_leagues()
            report["steps"]["leagues_seeded"] = "OK"
            report["details"]["seed_result"] = seed_result

            registry = TeamRegistry.get()
            await registry.initialize()
            stats = registry.stats()
            report["steps"]["team_registry"] = "OK"
            report["details"]["teams_in_ram"] = stats.get("sport_entries", 0)
            report["details"]["global_entries"] = stats.get("global_entries", 0)

            test_name = "Bayern Munich"
            test_sport_key = "soccer_germany_bundesliga"
            team_id = await registry.resolve(test_name, test_sport_key)

            if team_id:
                team_doc = await _db.db.teams.find_one(
                    {"_id": team_id},
                    {"display_name": 1},
                )
                report["steps"]["resolution_test"] = "PASSED"
                report["details"]["resolved_team"] = {
                    "id": str(team_id),
                    "name": (team_doc or {}).get("display_name", test_name),
                }
                report["status"] = "HEALTHY"
            else:
                report["steps"]["resolution_test"] = "FAILED"
                report["status"] = "DEGRADED"
                report["error"] = (
                    f"Could not resolve '{test_name}'. Check if a needs_review entry was created."
                )

        except Exception as e:
            report["status"] = "CRITICAL"
            report["error"] = str(e)
            report["traceback"] = traceback.format_exc()
            logger.error("Tower check failed: %s", e, exc_info=True)

        return report
