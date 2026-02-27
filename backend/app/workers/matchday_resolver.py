"""Matchday mode: auto-bet injection after matchday completes.

Scoring of individual predictions is handled by the Universal Resolver
in match_resolver._resolve_match() — this worker only fills in missing
predictions via auto-bet strategies when all matches in a matchday are done.
"""

import logging

from bson import ObjectId

import app.database as _db
from app.utils import utcnow
from app.services.matchday_service import generate_auto_prediction

logger = logging.getLogger("quotico.matchday_resolver")


async def resolve_matchday_predictions() -> None:
    """Check completed matchdays and inject auto-bet predictions.

    For each matchday where all matches are completed:
    1. Find betting_slips with type=matchday_round that have auto_bet_strategy
    2. Fill in missing predictions via auto-bet
    3. The universal resolver will score them on next match_resolver cycle
    """
    matchdays = await _db.db.matchdays.find({
        "status": {"$in": ["in_progress", "completed"]},
    }).to_list(length=100)

    for matchday in matchdays:
        try:
            await _inject_auto_bets(matchday)
        except Exception as e:
            logger.error(
                "Matchday auto-bet error for %s: %s",
                matchday.get("label", "?"), e,
            )


async def _inject_auto_bets(matchday: dict) -> None:
    """Inject auto-bet predictions for a matchday where all matches are done."""
    matchday_id = str(matchday["_id"])
    match_ids = matchday.get("match_ids", [])

    if not match_ids:
        return

    # Get all matches
    matches = await _db.db.matches_v3.find(
        {"_id": {"$in": [int(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))

    matches_by_id = {str(m["_id"]): m for m in matches}

    # Check if entire matchday is done
    completed = sum(
        1 for m in matches
        if m.get("status") == "final"
        and m.get("result", {}).get("home_score") is not None
    )
    if completed < len(match_ids):
        return  # Not all done yet, wait

    # Find matchday_round slips for this matchday that have an auto_bet_strategy
    slips = await _db.db.betting_slips.find({
        "matchday_id": matchday_id,
        "type": "matchday_round",
        "auto_bet_strategy": {"$nin": [None, "none"]},
        "status": {"$in": ["draft", "pending", "partial"]},
    }).to_list(length=10000)

    if not slips:
        # Update matchday status if all done
        await _db.db.matchdays.update_one(
            {"_id": matchday["_id"]},
            {"$set": {"status": "completed", "all_resolved": True, "updated_at": utcnow()}},
        )
        return

    # Pre-fetch squads for auto-bet blocking check
    squad_ids = list({s["squad_id"] for s in slips if s.get("squad_id")})
    squads_by_id: dict[str, dict] = {}
    if squad_ids:
        _squads = await _db.db.squads.find(
            {"_id": {"$in": [ObjectId(sid) for sid in squad_ids]}},
            {"auto_bet_blocked": 1},
        ).to_list(length=len(squad_ids))
        squads_by_id = {str(s["_id"]): s for s in _squads}

    # Pre-fetch QuoticoTips for Q-Bot strategy
    match_id_strs = [str(m["_id"]) for m in matches]
    qbets_by_match: dict[str, dict] = {}
    has_qbot = any(s.get("auto_bet_strategy") == "q_bot" for s in slips)
    if has_qbot and match_id_strs:
        _qbets = await _db.db.quotico_tips.find(
            {"match_id": {"$in": match_id_strs}},
            {"match_id": 1, "recommended_selection": 1, "confidence": 1, "qbot_logic": 1},
        ).to_list(length=len(match_id_strs))
        qbets_by_match = {t["match_id"]: t for t in _qbets}

    now = utcnow()
    injected_count = 0

    for slip in slips:
        auto_strategy = slip.get("auto_bet_strategy", "none")
        if auto_strategy == "none":
            continue

        # Check squad auto-bet block
        squad_id = slip.get("squad_id")
        if squad_id:
            squad = squads_by_id.get(squad_id)
            if squad and squad.get("auto_bet_blocked", False):
                continue

        # Find which matches are missing predictions
        existing_match_ids = {
            sel["match_id"] for sel in slip.get("selections", [])
        }
        new_selections = []

        for match_id, match in matches_by_id.items():
            if match_id in existing_match_ids:
                continue

            qbet = qbets_by_match.get(match_id)
            auto = generate_auto_prediction(auto_strategy, match, quotico_tip=qbet)
            if auto:
                new_selections.append({
                    "match_id": match_id,
                    "market": "exact_score",
                    "pick": {"home": auto[0], "away": auto[1]},
                    "is_auto": True,
                    "status": "pending",  # Auto-bets go directly to pending
                    "locked_at": now,
                    "points_earned": None,
                })

        if new_selections:
            await _db.db.betting_slips.update_one(
                {"_id": slip["_id"]},
                {
                    "$push": {"selections": {"$each": new_selections}},
                    "$set": {"updated_at": now},
                },
            )
            injected_count += len(new_selections)

    # Update matchday status
    await _db.db.matchdays.update_one(
        {"_id": matchday["_id"]},
        {"$set": {"status": "completed", "all_resolved": True, "updated_at": now}},
    )

    if injected_count > 0:
        logger.info(
            "Auto-bet injected %d predictions for %s (%s)",
            injected_count, matchday.get("label"), matchday.get("league_id"),
        )
