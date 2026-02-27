"""
backend/app/workers/match_resolver.py

Purpose:
    Match resolution worker. Picks up matches finalized by Sportmonks ingest,
    resolves betting slips, and awards points.

Dependencies:
    - app.database
    - app.services.match_service
"""

import logging
from datetime import datetime, timedelta
from bson import ObjectId

import app.database as _db
from app.services.match_service import _MAX_DURATION, _DEFAULT_DURATION
from app.services.matchday_service import calculate_points, is_match_locked
from app.services.fantasy_service import calculate_fantasy_points
from app.services.odds_meta_service import get_current_market
from app.utils import ensure_utc, utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.match_resolver")


def _match_team_id(match: dict, side: str) -> int | None:
    teams = (match or {}).get("teams")
    if not isinstance(teams, dict):
        return None
    node = teams.get(side)
    if not isinstance(node, dict):
        return None
    value = node.get("sm_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _match_team_name(match: dict, side: str) -> str:
    teams = (match or {}).get("teams")
    if not isinstance(teams, dict):
        return "?"
    node = teams.get(side)
    if not isinstance(node, dict):
        return "?"
    return str(node.get("name") or "?")


def _extract_scores(match: dict) -> tuple[int | None, int | None]:
    teams = (match or {}).get("teams")
    home_score = None
    away_score = None
    if isinstance(teams, dict):
        home_node = teams.get("home")
        away_node = teams.get("away")
        if isinstance(home_node, dict):
            home_score = home_node.get("score")
        if isinstance(away_node, dict):
            away_score = away_node.get("score")
    scores = (match or {}).get("scores")
    if (home_score is None or away_score is None) and isinstance(scores, dict):
        full_time = scores.get("full_time")
        if isinstance(full_time, dict):
            if home_score is None:
                home_score = full_time.get("home")
            if away_score is None:
                away_score = full_time.get("away")
    try:
        parsed_home = int(home_score) if home_score is not None else None
    except (TypeError, ValueError):
        parsed_home = None
    try:
        parsed_away = int(away_score) if away_score is not None else None
    except (TypeError, ValueError):
        parsed_away = None
    return parsed_home, parsed_away



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
        team_id = sel.get("team_id")
        home_team_id = _match_team_id(match, "home")
        away_team_id = _match_team_id(match, "away")
        if not team_id or not home_team_id or not away_team_id:
            logger.error("Survivor selection identity missing for match %s", match.get("_id"))
            sel["match_result"] = "lost"
            sel["status"] = "lost"
            return sel
        if team_id == home_team_id:
            team_won = result == "1"
            team_draw = result == "X"
        elif team_id == away_team_id:
            team_won = result == "2"
            team_draw = result == "X"
        else:
            logger.error("Survivor selection team_id %s not in match %s", team_id, match.get("_id"))
            sel["match_result"] = "lost"
            sel["status"] = "lost"
            return sel

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
        team_id = sel.get("team_id")
        home_team_id = _match_team_id(match, "home")
        away_team_id = _match_team_id(match, "away")
        if not team_id or not home_team_id or not away_team_id:
            logger.error("Fantasy selection identity missing for match %s", match.get("_id"))
            return sel
        if team_id == home_team_id:
            gs, gc = home_score, away_score
        elif team_id == away_team_id:
            gs, gc = away_score, home_score
        else:
            logger.error("Fantasy selection team_id %s not in match %s", team_id, match.get("_id"))
            return sel

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
                odds = get_current_market(match, "h2h")
                pick = sel.get("pick")
                if pick and pick in odds:
                    sel["locked_odds"] = odds[pick]
            elif market == "totals":
                totals = get_current_market(match, "totals")
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

    Legacy provider-based resolution has been removed. Match resolution now
    relies on Sportmonks ingest updating match status to 'final' with scores.
    This worker picks up those finalized matches and resolves slips.
    """
    now = utcnow()

    active_leagues = await _db.db.league_registry_v3.find(
        {"is_active": True}, {"_id": 1, "league_id": 1}
    ).to_list(100)
    active_league_ids = [int(l["_id"]) for l in active_leagues if l.get("_id") is not None]

    for league_id in active_league_ids:
        # Find matches marked FINISHED by Sportmonks ingest but with unresolved slips.
        unresolved = await _db.db.matches_v3.find({
            "league_id": int(league_id),
            "status": "FINISHED",
            "teams.home.score": {"$ne": None},
            "teams.away.score": {"$ne": None},
        }).to_list(length=200)

        for match in unresolved:
            home_score, away_score = _extract_scores(match)
            if home_score is None or away_score is None:
                continue
            if home_score > away_score:
                outcome = "1"
            elif away_score > home_score:
                outcome = "2"
            else:
                outcome = "X"
            try:
                await _resolve_match(match, outcome, home_score, away_score)
            except Exception as e:
                logger.error("Resolution failed for match %s: %s", str(match["_id"]), e)

        await set_synced(f"resolver:{int(league_id)}")

    await _auto_close_stale_matches(now)
    await cleanup_stale_drafts()

    from app.services.battle_service import expire_stale_challenges
    await expire_stale_challenges()


async def _auto_close_stale_matches(now: datetime) -> None:
    """Auto-close matches stuck as 'live' or 'scheduled' past their expected duration."""
    active_leagues = await _db.db.league_registry_v3.find(
        {"is_active": True}, {"_id": 1, "league_id": 1}
    ).to_list(100)
    active_leagues_by_id = {
        int(l["_id"]): str(l.get("league_id") or "")
        for l in active_leagues
        if l.get("_id") is not None
    }

    for league_id, league_id in active_leagues_by_id.items():
        max_dur = _MAX_DURATION.get(league_id, _DEFAULT_DURATION)
        cutoff = now - max_dur

        stale = await _db.db.matches_v3.find({
            "league_id": int(league_id),
            "status": {"$in": ["SCHEDULED", "LIVE"]},
            "start_at": {"$lte": cutoff},
        }).to_list(length=100)

        for match in stale:
            update: dict = {"status": "FINISHED", "updated_at": now}
            await _db.db.matches_v3.update_one(
                {"_id": match["_id"]},
                {"$set": update},
            )
            match_id = str(match["_id"])
            logger.warning(
                "Auto-closed stale match %s (%s vs %s, %s) — no provider result, needs admin review",
                match_id, _match_team_name(match, "home"), _match_team_name(match, "away"), league_id,
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

    await _db.db.matches_v3.update_one(
        {"_id": match["_id"]},
        {
            "$set": {
                "status": "FINISHED",
                "teams.home.score": home_score,
                "teams.away.score": away_score,
                "scores.full_time.home": home_score,
                "scores.full_time.away": away_score,
                "resolution.outcome_1x2": result,
                "updated_at": now,
                "updated_at_utc": now,
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
        match_id, _match_team_name(match, "home"), _match_team_name(match, "away"),
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


async def resolve_single_match(match_id: int | str) -> None:
    """Resolve one finalized match on demand from event handlers.

    Idempotent behavior:
    - skips if match does not exist
    - skips if score/result is incomplete
    - skips if already resolved and no unresolved dependent slips remain
    """
    try:
        fixture_id = int(match_id)
    except (TypeError, ValueError):
        return

    match = await _db.db.matches_v3.find_one({"_id": fixture_id})
    if not match:
        return

    match_id_str = str(fixture_id)
    unresolved = await _db.db.betting_slips.find_one(
        {
            "selections.match_id": match_id_str,
            "status": {"$in": ["pending", "partial", "draft"]},
        },
        {"_id": 1},
    )
    if not unresolved and (match.get("resolution") or {}).get("outcome_1x2"):
        return

    home_score, away_score = _extract_scores(match)
    if home_score is None or away_score is None:
        return

    outcome = ((match.get("resolution") or {}).get("outcome_1x2"))
    if not outcome:
        if int(home_score) > int(away_score):
            outcome = "1"
        elif int(away_score) > int(home_score):
            outcome = "2"
        else:
            outcome = "X"

    await _resolve_match(match, str(outcome), int(home_score), int(away_score))
