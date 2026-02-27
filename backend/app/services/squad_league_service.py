"""Shared utilities for squad league configuration lookups."""

from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.models.game_mode import GAME_MODE_LABELS


def get_active_league_config(
    squad: dict, league_id: int, expected_mode: str | None = None,
) -> dict | None:
    """Find the active league config for a sport within a squad.

    Returns the matching league config dict, or None if not found.
    Raises HTTPException 400 if expected_mode doesn't match.
    """
    for config in squad.get("league_configs", []):
        if not isinstance(config.get("league_id"), int):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Invalid squad league configuration: league_id must be int.",
            )
        if config["league_id"] == league_id and config.get("deactivated_at") is None:
            if expected_mode and config["game_mode"] != expected_mode:
                label = GAME_MODE_LABELS.get(expected_mode, expected_mode)
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"This league is not configured in {label} mode.",
                )
            return config
    return None


def require_active_league_config(
    squad: dict, league_id: int, expected_mode: str,
) -> dict:
    """Like get_active_league_config but raises 400 if not found."""
    config = get_active_league_config(squad, league_id, expected_mode)
    if not config:
        label = GAME_MODE_LABELS.get(expected_mode, expected_mode)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Squad has no active {label} configuration for this league.",
        )
    return config


async def get_squad_mode_for_sport(
    squad_id: str, league_id: int,
) -> str | None:
    """Quick lookup: what game_mode does this squad use for this sport?"""
    squad = await _db.db.squads.find_one(
        {
            "_id": ObjectId(squad_id),
            "league_configs": {
                "$elemMatch": {
                    "league_id": league_id,
                    "deactivated_at": None,
                }
            },
        },
        {"league_configs.$": 1},
    )
    if squad and squad.get("league_configs"):
        return squad["league_configs"][0]["game_mode"]

    # Fallback: legacy game_mode field
    squad = await _db.db.squads.find_one(
        {"_id": ObjectId(squad_id)},
        {"game_mode": 1},
    )
    if squad:
        return squad.get("game_mode", "classic")
    return None
