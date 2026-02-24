"""Match resolution worker.

Fetches final scores from providers, resolves matches → status=final,
resolves betting slips, and awards points. No separate archive step —
the unified matches collection IS the archive.
"""

import logging
from datetime import datetime, timedelta
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
from app.services.match_service import _MAX_DURATION, _DEFAULT_DURATION
from app.services.matchday_service import calculate_points, is_match_locked
from app.services.fantasy_service import calculate_fantasy_points
from app.utils import ensure_utc, parse_utc, utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.match_resolver")

BUNDESLIGA = "soccer_germany_bundesliga"
BUNDESLIGA2 = "soccer_germany_bundesliga2"
GERMAN_LEAGUES = {BUNDESLIGA, BUNDESLIGA2}


# ---------- Universal Resolver Functions ----------

def resolve_selection(
    sel: dict, match: dict, result: str,
    home_score: int, away_score: int,
    *, squad_config: dict | None = None,
) -> dict:
    """Resolve a single selection based on its market type.

    Polymorphic: dispatches on sel["market"] to determine outcome.
    Mutates and returns sel dict with updated status + audit fields.
    """
    market = sel.get("market", "h2h")

    if market == "h2h":
        sel["status"] = "won" if sel["pick"] == result else "lost"

    elif market == "totals":
        total = home_score + away_score
        line = sel.get("line", 2.5)
        if total == line:
            sel["status"] = "void"  # Push → void + refund
        elif (sel["pick"] == "over" and total > line) or \
             (sel["pick"] == "under" and total < line):
            sel["status"] = "won"
        else:
            sel["status"] = "lost"

    elif market == "exact_score":
        pick = sel["pick"]
        weights = None
        if squad_config:
            weights = squad_config.get("point_weights")
        pts = calculate_points(
            pick["home"], pick["away"],
            home_score, away_score,
            weights=weights,
        )
        sel["points_earned"] = pts
        sel["status"] = "scored"
        sel["actual_score"] = {"home": home_score, "away": away_score}

    elif market == "survivor_pick":
        team = sel["pick"]
        home_team = match.get("home_team", "")
        if team == home_team:
            team_won = result == "1"
            team_draw = result == "X"
        else:
            team_won = result == "2"
            team_draw = result == "X"

        if team_won:
            sel["match_result"] = "won"
            sel["status"] = "won"
        elif team_draw:
            sel["match_result"] = "draw"
            # Leave as pending — slip-level logic handles draw_eliminates
            sel["status"] = "pending"
        else:
            sel["match_result"] = "lost"
            sel["status"] = "lost"

    elif market == "fantasy_pick":
        team = sel["pick"]
        if team == match.get("home_team", ""):
            gs, gc = home_score, away_score
        else:
            gs, gc = away_score, home_score

        sel["goals_scored"] = gs
        sel["goals_conceded"] = gc
        sel["match_result"] = (
            "won" if gs > gc else ("draw" if gs == gc else "lost")
        )
        pure_stats = True
        if squad_config:
            pure_stats = squad_config.get("pure_stats_only", True)
        sel["fantasy_points"] = calculate_fantasy_points(gs, gc, pure_stats)
        sel["points_earned"] = sel["fantasy_points"]
        sel["status"] = "scored"

    return sel


def recalculate_slip(
    slip: dict, now: datetime,
    *, squad_config: dict | None = None,
) -> dict:
    """Recalculate slip-level status from selection statuses.

    Mutates and returns slip dict with updated status + aggregates.
    """
    slip_type = slip.get("type", "single")
    selections = slip.get("selections", [])
    statuses = [s.get("status", "pending") for s in selections]

    if slip_type in ("single", "parlay"):
        if any(s == "lost" for s in statuses):
            slip["status"] = "lost"
        elif all(s == "won" for s in statuses):
            slip["status"] = "won"
        elif all(s in ("won", "void") for s in statuses):
            # Recalculate total_odds excluding void legs
            active_odds = [
                sel.get("locked_odds", 1.0)
                for sel, st in zip(selections, statuses) if st == "won"
            ]
            if active_odds:
                total = 1.0
                for o in active_odds:
                    total *= o
                slip["total_odds"] = round(total, 4)
                slip["potential_payout"] = round(slip.get("stake", 10.0) * total, 2)
            slip["status"] = "won"
        elif all(s == "void" for s in statuses):
            slip["status"] = "void"
        elif any(s == "pending" for s in statuses):
            slip["status"] = "partial"
        else:
            slip["status"] = "partial"

        if slip["status"] in ("won", "lost", "void"):
            slip["resolved_at"] = now

    elif slip_type == "matchday_round":
        all_scored = all(
            s in ("scored", "void") for s in statuses
        )
        if all_scored and selections:
            slip["status"] = "resolved"
            slip["total_points"] = sum(
                sel.get("points_earned", 0) or 0 for sel in selections
            )
            slip["resolved_at"] = now
        elif any(s in ("scored", "void") for s in statuses):
            slip["status"] = "partial"

    elif slip_type == "survivor":
        # Find the latest selection (current matchday pick)
        if selections:
            latest_sel = selections[-1]
            match_result = latest_sel.get("match_result")
            draw_eliminates = True
            if squad_config:
                draw_eliminates = squad_config.get("draw_eliminates", True)

            if match_result == "lost":
                latest_sel["status"] = "lost"
                slip["status"] = "lost"
                slip["eliminated_at"] = now
            elif match_result == "draw":
                if draw_eliminates:
                    latest_sel["status"] = "lost"
                    slip["status"] = "lost"
                    slip["eliminated_at"] = now
                else:
                    latest_sel["status"] = "won"
                    slip["streak"] = slip.get("streak", 0) + 1
            elif match_result == "won":
                latest_sel["status"] = "won"
                slip["streak"] = slip.get("streak", 0) + 1
            # Slip stays "partial" until season ends or eliminated

    elif slip_type == "fantasy":
        all_scored = all(s == "scored" for s in statuses)
        if all_scored and selections:
            slip["status"] = "resolved"
            slip["total_points"] = sum(
                sel.get("fantasy_points", 0) or 0 for sel in selections
            )
            slip["resolved_at"] = now

    slip["updated_at"] = now
    return slip


async def calculate_points_award(slip: dict, now: datetime) -> None:
    """Award points or wallet credits based on resolved slip status."""
    slip_type = slip.get("type", "single")
    slip_status = slip.get("status")
    slip_id = str(slip["_id"])
    user_id = slip["user_id"]
    funding = slip.get("funding", "virtual")
    wallet_id = slip.get("wallet_id")

    if slip_type in ("single", "parlay"):
        if slip_status == "won":
            payout = slip.get("potential_payout", 0)
            if payout <= 0:
                return

            if funding == "wallet" and wallet_id:
                from app.services import wallet_service
                squad_id = slip.get("squad_id", "")
                await wallet_service.credit_win(
                    wallet_id=wallet_id,
                    user_id=user_id,
                    squad_id=squad_id,
                    amount=payout,
                    reference_type="betting_slip",
                    reference_id=slip_id,
                    description=f"Won slip {slip_id}: {payout:.2f} coins",
                )
            else:
                # Virtual: award points to user
                await _db.db.users.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$inc": {"points": payout}},
                )
                await _db.db.points_transactions.insert_one({
                    "user_id": user_id,
                    "bet_id": slip_id,
                    "delta": payout,
                    "scoring_version": 2,
                    "created_at": now,
                })

        elif slip_status == "void" and funding == "wallet" and wallet_id:
            # Refund the stake for fully voided wallet-funded slips
            from app.services import wallet_service
            stake = slip.get("stake", 0)
            squad_id = slip.get("squad_id", "")
            if stake > 0:
                await wallet_service.credit_win(
                    wallet_id=wallet_id,
                    user_id=user_id,
                    squad_id=squad_id,
                    amount=stake,
                    reference_type="betting_slip_refund",
                    reference_id=slip_id,
                    description=f"Void refund slip {slip_id}: {stake:.2f} coins",
                )

    # matchday_round, fantasy, survivor — no immediate payout
    # Leaderboards/standings derived from slip data separately


def auto_lock_selections(
    slip: dict, matches_by_id: dict[str, dict],
    lock_mins: int, now: datetime,
) -> bool:
    """Transition draft selections to locked if their match is within lock window.

    For h2h/totals markets, freezes current server odds at lock time.
    Returns True if any selection was changed.
    """
    changed = False
    for sel in slip.get("selections", []):
        if sel.get("status") != "draft":
            continue
        match = matches_by_id.get(sel["match_id"])
        if not match:
            continue
        if is_match_locked(match, lock_mins):
            sel["status"] = "locked"
            sel["locked_at"] = now
            # Freeze odds at lock time for market bets
            market = sel.get("market", "h2h")
            if market == "h2h":
                odds = match.get("odds", {}).get("h2h", {})
                pick = sel.get("pick")
                if pick and pick in odds:
                    sel["locked_odds"] = odds[pick]
            elif market == "totals":
                totals = match.get("odds", {}).get("totals", {})
                pick = sel.get("pick")
                if pick and pick in totals:
                    sel["locked_odds"] = totals[pick]
            changed = True
    return changed


async def cleanup_stale_drafts() -> None:
    """Delete draft slips where updated_at is more than 24 hours ago."""
    cutoff = utcnow() - timedelta(hours=24)
    result = await _db.db.betting_slips.delete_many({
        "status": "draft",
        "updated_at": {"$lte": cutoff},
    })
    if result.deleted_count > 0:
        logger.info("Cleaned up %d stale draft slips", result.deleted_count)


async def resolve_matches() -> None:
    """Check for completed matches and resolve pending bets.

    Smart sleep: skips sports with no started-but-unresolved matches.
    Safety margin: polls each sport at least once every 6 hours.
    """
    now = utcnow()

    for sport_key in SUPPORTED_SPORTS:
        has_work = await _db.db.matches.find_one({
            "sport_key": sport_key,
            "$or": [
                {"status": {"$in": ["scheduled", "live"]}, "match_date": {"$lte": now}},
                {"status": "final", "result.outcome": None},
            ],
        })

        if not has_work:
            state_key = f"resolver:{sport_key}"
            if await recently_synced(state_key, timedelta(hours=6)):
                logger.debug("Smart sleep: %s has no unresolved matches, skipping", sport_key)
                continue
            logger.info("Smart sleep safety: %s >6h since last resolve, polling anyway", sport_key)

        try:
            if sport_key in GERMAN_LEAGUES:
                await _resolve_german_league(sport_key)
            elif sport_key in SPORT_TO_COMPETITION:
                await _resolve_via_football_data(sport_key)
            else:
                await _resolve_via_odds_api(sport_key)
            await set_synced(f"resolver:{sport_key}")
        except Exception as e:
            logger.error("Resolution failed for %s: %s", sport_key, e)

    await _auto_close_stale_matches(now)
    await cleanup_stale_drafts()

    from app.services.battle_service import expire_stale_challenges
    await expire_stale_challenges()


async def _auto_close_stale_matches(now: datetime) -> None:
    """Auto-close matches stuck as 'live' or 'scheduled' past their expected duration."""
    for sport_key in SUPPORTED_SPORTS:
        max_dur = _MAX_DURATION.get(sport_key, _DEFAULT_DURATION)
        cutoff = now - max_dur

        stale = await _db.db.matches.find({
            "sport_key": sport_key,
            "status": {"$in": ["scheduled", "live"]},
            "match_date": {"$lte": cutoff},
        }).to_list(length=100)

        for match in stale:
            update: dict = {"status": "final", "updated_at": now}
            # Freeze closing line if transitioning from scheduled (no prior capture)
            odds = match.get("odds", {})
            if match.get("status") == "scheduled" and not odds.get("closing_line"):
                update["odds.closing_line"] = {
                    "h2h": odds.get("h2h", {}),
                    "totals": odds.get("totals", {}),
                    "spreads": odds.get("spreads", {}),
                    "frozen_at": now,
                }
            await _db.db.matches.update_one(
                {"_id": match["_id"]},
                {"$set": update},
            )
            match_id = str(match["_id"])
            logger.warning(
                "Auto-closed stale match %s (%s vs %s, %s) — no provider result, needs admin review",
                match_id, match.get("home_team"), match.get("away_team"), sport_key,
            )


# ---------- Shared resolution logic ----------

async def _resolve_match(
    match: dict, result: str, home_score: int, away_score: int
) -> None:
    """Resolve a single match: update status, resolve all betting slips, award points.

    Universal resolver: handles all slip types (single, parlay, matchday_round,
    survivor, fantasy) via resolve_selection() + recalculate_slip() dispatch.
    """
    now = utcnow()
    match_id = str(match["_id"])

    await _db.db.matches.update_one(
        {"_id": match["_id"]},
        {
            "$set": {
                "status": "final",
                "result.outcome": result,
                "result.home_score": home_score,
                "result.away_score": away_score,
                "updated_at": now,
            }
        },
    )

    # Find all slips with selections on this match (pending, partial, or draft with locked legs)
    affected_slips = await _db.db.betting_slips.find({
        "selections.match_id": match_id,
        "status": {"$in": ["pending", "partial", "draft"]},
    }).to_list(length=10000)

    # Pre-fetch squad configs for all affected slips (batch, not N+1)
    squad_ids = list({s["squad_id"] for s in affected_slips if s.get("squad_id")})
    squad_config_map: dict[str, dict] = {}
    if squad_ids:
        squads = await _db.db.squads.find(
            {"_id": {"$in": [ObjectId(sid) for sid in squad_ids]}},
            {"league_configs": 1, "game_mode_config": 1},
        ).to_list(length=len(squad_ids))
        for sq in squads:
            # Extract mode-relevant config from league_configs
            config: dict = {}
            for lc in sq.get("league_configs", []):
                if not lc.get("deactivated_at"):
                    lc_config = lc.get("config", {})
                    config.update(lc_config)
            # Fallback to legacy game_mode_config
            if not config:
                config = sq.get("game_mode_config", {})
            squad_config_map[str(sq["_id"])] = config

    resolved_count = 0
    awarded_count = 0

    for slip in affected_slips:
        # Skip pure drafts — only process if the slip has locked/pending selections
        if slip["status"] == "draft":
            has_resolvable = any(
                sel["match_id"] == match_id and sel.get("status") in ("locked", "pending")
                for sel in slip["selections"]
            )
            if not has_resolvable:
                continue

        squad_config = squad_config_map.get(slip.get("squad_id", ""))
        # Use slip-level point_weights if stored (frozen at creation time)
        if slip.get("point_weights") and squad_config is not None:
            squad_config = {**squad_config, "point_weights": slip["point_weights"]}
        elif slip.get("point_weights"):
            squad_config = {"point_weights": slip["point_weights"]}

        slip_changed = False
        for sel in slip["selections"]:
            if sel["match_id"] != match_id:
                continue
            if sel.get("status") not in ("pending", "locked"):
                continue

            # Resolve the selection using polymorphic dispatch
            resolve_selection(sel, match, result, home_score, away_score,
                              squad_config=squad_config)
            slip_changed = True

        if not slip_changed:
            continue

        # Recalculate slip-level status
        old_status = slip["status"]
        recalculate_slip(slip, now, squad_config=squad_config)

        # Persist updated slip
        update_fields: dict = {
            "selections": slip["selections"],
            "status": slip["status"],
            "updated_at": now,
        }
        if slip.get("resolved_at"):
            update_fields["resolved_at"] = slip["resolved_at"]
        if slip.get("total_points") is not None:
            update_fields["total_points"] = slip["total_points"]
        if slip.get("total_odds") is not None:
            update_fields["total_odds"] = slip["total_odds"]
        if slip.get("potential_payout") is not None:
            update_fields["potential_payout"] = slip["potential_payout"]
        if slip.get("eliminated_at"):
            update_fields["eliminated_at"] = slip["eliminated_at"]
        if slip.get("streak") is not None and slip.get("type") == "survivor":
            update_fields["streak"] = slip["streak"]

        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]},
            {"$set": update_fields},
        )
        resolved_count += 1

        # Award points/credits for terminal states
        if slip["status"] in ("won", "lost", "void", "resolved"):
            try:
                await calculate_points_award(slip, now)
                if slip["status"] == "won":
                    awarded_count += 1
            except Exception as e:
                logger.error(
                    "Points award failed for slip %s: %s", str(slip["_id"]), e,
                )

    logger.info(
        "Resolved %s (%s vs %s): %s %d-%d | %d slips affected, %d awarded",
        match_id, match.get("home_team", "?"), match.get("away_team", "?"),
        result, home_score, away_score,
        resolved_count, awarded_count,
    )

    # Broadcast to connected WebSocket clients
    from app.routers.ws import live_manager
    await live_manager.broadcast_match_resolved(match_id, result)

    # Update QuoticoTip for backtesting
    bet_doc = await _db.db.quotico_tips.find_one({"match_id": match_id})
    if bet_doc:
        await _db.db.quotico_tips.update_one(
            {"match_id": match_id},
            {"$set": {
                "status": "resolved",
                "actual_result": result,
                "was_correct": result == bet_doc.get("recommended_selection"),
            }},
        )


async def _find_match_by_team(
    sport_key: str, score_data: dict
) -> Optional[dict]:
    """Find an unresolved match in our DB by team name + date."""
    utc_date = score_data["utc_date"]
    try:
        match_time = parse_utc(utc_date)
    except (ValueError, TypeError, AttributeError):
        return None

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "$or": [
            {"status": {"$ne": "final"}},
            {"status": "final", "result.outcome": None},
        ],
        "match_date": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=100)

    for candidate in candidates:
        if teams_match(candidate.get("home_team", ""), score_data["home_team"]):
            return candidate

    return None


# ---------- German leagues: OpenLigaDB + football-data.org cross-validation ----------

async def _resolve_german_league(sport_key: str) -> None:
    primary = await openligadb_provider.get_finished_scores(sport_key)
    secondary = await football_data_provider.get_finished_scores(sport_key)

    if not primary:
        if secondary:
            logger.info("%s: OpenLigaDB empty, using football-data.org alone", sport_key)
            for score in secondary:
                match = await _find_match_by_team(sport_key, score)
                if match:
                    await _resolve_match(
                        match, score["result"],
                        score["home_score"], score["away_score"],
                    )
        return

    for p_score in primary:
        match = await _find_match_by_team(sport_key, p_score)
        if not match:
            continue

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
    for s in secondary_scores:
        if not teams_match(primary_score["home_team"], s["home_team"]):
            continue

        if (
            primary_score["home_score"] == s["home_score"]
            and primary_score["away_score"] == s["away_score"]
        ):
            return True

        logger.warning(
            "Score mismatch: %s vs %s — OpenLigaDB: %d-%d, football-data: %d-%d",
            primary_score["home_team"], primary_score["away_team"],
            primary_score["home_score"], primary_score["away_score"],
            s["home_score"], s["away_score"],
        )
        return False

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


# ---------- Fallback: TheOddsAPI (costs credits) ----------

async def _resolve_via_odds_api(sport_key: str) -> None:
    scores = await odds_provider.get_scores(sport_key)
    for score_data in scores:
        if not score_data.get("completed"):
            continue

        theoddsapi_id = score_data["external_id"]
        match = await _db.db.matches.find_one({
            "metadata.theoddsapi_id": theoddsapi_id,
            "$or": [
                {"status": {"$ne": "final"}},
                {"status": "final", "result.outcome": None},
            ],
        })
        if not match:
            continue

        await _resolve_match(
            match, score_data["result"],
            score_data.get("home_score", 0),
            score_data.get("away_score", 0),
        )
