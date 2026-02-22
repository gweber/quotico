import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

import app.database as _db
from app.providers.odds_api import SUPPORTED_SPORTS, odds_provider
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
    teams_match,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.providers.espn import espn_provider, SPORT_TO_ESPN

logger = logging.getLogger("quotico.match_resolver")

BUNDESLIGA = "soccer_germany_bundesliga"


async def resolve_matches() -> None:
    """Check for completed matches and resolve pending tips.

    Provider routing:
    - Bundesliga: OpenLigaDB (primary) + football-data.org (cross-validate)
    - Other soccer: football-data.org
    - NFL/NBA: ESPN
    - Tennis/other: TheOddsAPI scores (costs credits)
    """
    for sport_key in SUPPORTED_SPORTS:
        try:
            if sport_key == BUNDESLIGA:
                await _resolve_bundesliga()
            elif sport_key in SPORT_TO_COMPETITION:
                await _resolve_via_football_data(sport_key)
            elif sport_key in SPORT_TO_ESPN:
                await _resolve_via_espn(sport_key)
            else:
                # Fallback: TheOddsAPI scores (tennis etc.)
                await _resolve_via_odds_api(sport_key)
        except Exception as e:
            logger.error("Resolution failed for %s: %s", sport_key, e)


# ---------- Shared resolution logic ----------

async def _resolve_match(
    match: dict, result: str, home_score: int, away_score: int
) -> None:
    """Resolve a single match: update status, resolve tips, award points."""
    now = datetime.now(timezone.utc)
    match_id = str(match["_id"])

    await _db.db.matches.update_one(
        {"_id": match["_id"]},
        {
            "$set": {
                "status": "completed",
                "result": result,
                "home_score": home_score,
                "away_score": away_score,
                "updated_at": now,
            }
        },
    )

    pending_tips = await _db.db.tips.find({
        "match_id": match_id,
        "status": "pending",
    }).to_list(length=10000)

    if not pending_tips:
        return

    tip_updates = []
    points_ops = []

    for tip in pending_tips:
        prediction = tip["selection"]["value"]
        is_won = prediction == result
        new_status = "won" if is_won else "lost"
        points_earned = tip["locked_odds"] if is_won else 0.0

        tip_updates.append({
            "filter": {"_id": tip["_id"]},
            "update": {
                "$set": {
                    "status": new_status,
                    "points_earned": points_earned,
                    "resolved_at": now,
                }
            },
        })

        if is_won:
            points_ops.append({
                "user_id": tip["user_id"],
                "delta": points_earned,
                "tip_id": str(tip["_id"]),
            })

    for update in tip_updates:
        await _db.db.tips.update_one(update["filter"], update["update"])

    for op in points_ops:
        await _db.db.users.update_one(
            {"_id": ObjectId(op["user_id"])},
            {"$inc": {"points": op["delta"]}},
        )
        await _db.db.points_transactions.insert_one({
            "user_id": op["user_id"],
            "tip_id": op["tip_id"],
            "delta": op["delta"],
            "scoring_version": 1,
            "created_at": now,
        })

    logger.info(
        "Resolved %s (%s): %s %d-%d | %d tips, %d winners",
        match_id, match.get("teams", {}), result,
        home_score, away_score, len(pending_tips), len(points_ops),
    )


async def _find_match_by_team(
    sport_key: str, score_data: dict
) -> Optional[dict]:
    """Find an unresolved match in our DB by team name + date."""
    utc_date = score_data["utc_date"]
    if isinstance(utc_date, str):
        try:
            match_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        match_time = utc_date

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "status": {"$ne": "completed"},
        "commence_time": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=100)

    for candidate in candidates:
        home = candidate.get("teams", {}).get("home", "")
        if teams_match(home, score_data["home_team"]):
            return candidate

    return None


# ---------- Bundesliga: OpenLigaDB + football-data.org cross-validation ----------

async def _resolve_bundesliga() -> None:
    """Resolve Bundesliga with cross-validation between two free providers."""
    primary = await openligadb_provider.get_finished_scores(BUNDESLIGA)
    secondary = await football_data_provider.get_finished_scores(BUNDESLIGA)

    if not primary:
        # Fallback to football-data.org alone
        if secondary:
            logger.info("Bundesliga: OpenLigaDB empty, using football-data.org alone")
            for score in secondary:
                match = await _find_match_by_team(BUNDESLIGA, score)
                if match:
                    await _resolve_match(
                        match, score["result"],
                        score["home_score"], score["away_score"],
                    )
        return

    for p_score in primary:
        match = await _find_match_by_team(BUNDESLIGA, p_score)
        if not match:
            continue

        # Cross-validate against football-data.org
        validated = _cross_validate(p_score, secondary)
        if validated is False:
            logger.warning(
                "RESULT MISMATCH for %s vs %s: OpenLigaDB=%s, football-data=%s — SKIPPING",
                p_score["home_team"], p_score["away_team"],
                f"{p_score['home_score']}-{p_score['away_score']}",
                "see logs",
            )
            continue

        if validated is True:
            logger.info(
                "VALIDATED %s vs %s: %d-%d (both providers agree)",
                p_score["home_team"], p_score["away_team"],
                p_score["home_score"], p_score["away_score"],
            )

        await _resolve_match(
            match, p_score["result"],
            p_score["home_score"], p_score["away_score"],
        )


def _cross_validate(
    primary_score: dict, secondary_scores: list[dict]
) -> Optional[bool]:
    """Cross-validate a result against secondary provider.

    Returns:
        True  — both providers agree
        False — providers disagree (DO NOT resolve)
        None  — secondary has no data for this match (resolve anyway)
    """
    for s in secondary_scores:
        if not teams_match(primary_score["home_team"], s["home_team"]):
            continue

        # Found matching match — compare results
        if (
            primary_score["home_score"] == s["home_score"]
            and primary_score["away_score"] == s["away_score"]
        ):
            return True

        # Scores differ
        logger.warning(
            "Score mismatch: %s vs %s — OpenLigaDB: %d-%d, football-data: %d-%d",
            primary_score["home_team"], primary_score["away_team"],
            primary_score["home_score"], primary_score["away_score"],
            s["home_score"], s["away_score"],
        )
        return False

    # No matching match in secondary — that's OK, resolve with primary
    return None


# ---------- Other soccer: football-data.org ----------

async def _resolve_via_football_data(sport_key: str) -> None:
    scores = await football_data_provider.get_finished_scores(sport_key)
    for score in scores:
        match = await _find_match_by_team(sport_key, score)
        if match:
            await _resolve_match(
                match, score["result"],
                score["home_score"], score["away_score"],
            )


# ---------- NFL/NBA: ESPN ----------

async def _resolve_via_espn(sport_key: str) -> None:
    scores = await espn_provider.get_finished_scores(sport_key)
    for score in scores:
        match = await _find_match_by_team(sport_key, score)
        if match:
            await _resolve_match(
                match, score["result"],
                score["home_score"], score["away_score"],
            )


# ---------- Fallback: TheOddsAPI (costs credits) ----------

async def _resolve_via_odds_api(sport_key: str) -> None:
    scores = await odds_provider.get_scores(sport_key)
    for score_data in scores:
        if not score_data.get("completed"):
            continue

        external_id = score_data["external_id"]
        match = await _db.db.matches.find_one({
            "external_id": external_id,
            "status": {"$ne": "completed"},
        })
        if not match:
            continue

        await _resolve_match(
            match, score_data["result"],
            score_data.get("home_score", 0),
            score_data.get("away_score", 0),
        )
