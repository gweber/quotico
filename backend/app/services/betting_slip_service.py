"""
backend/app/services/betting_slip_service.py

Purpose:
    Unified betting slip service for all game modes. Handles draft lifecycle,
    validation, lock-in, and submission using aggregated odds from odds_meta.

Dependencies:
    - app.database
    - app.services.matchday_service
    - app.services.odds_meta_service
"""

import logging
from datetime import timedelta
from functools import reduce
from operator import mul
from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.config import settings
import app.database as _db
from app.services.matchday_service import LOCK_MINUTES, is_match_locked
from app.services.odds_meta_service import build_legacy_like_odds
from app.services.team_registry_service import TeamRegistry
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.betting_slip_service")


# ---------- Classic slip creation (direct submit, no draft) ----------

async def create_slip(
    user_id: str,
    selections: list[dict],
) -> dict:
    """Create a betting slip from one or more selections.

    Each selection dict: {match_id, market, pick, displayed_odds}

    Validates:
    - All matches exist and are scheduled
    - No duplicate match in selections
    - Picks are valid for the market
    - Odds aren't stale
    - Displayed odds within 20% of current (drift guard)
    """
    if not selections:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one selection is required.",
        )

    now = utcnow()

    # Deduplicate match IDs
    match_ids_seen: set[str] = set()
    for sel in selections:
        if sel["match_id"] in match_ids_seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate match in selections.",
            )
        match_ids_seen.add(sel["match_id"])

    # Fetch all matches in one query
    oid_list = [ObjectId(s["match_id"]) for s in selections]
    matches_cursor = _db.db.matches.find(
        {"_id": {"$in": oid_list}},
        {"status": 1, "match_date": 1, "odds": 1},
    )
    matches_by_id = {str(m["_id"]): m async for m in matches_cursor}

    # Check for existing bets on these matches by this user
    existing_slips = await _db.db.betting_slips.find(
        {
            "user_id": user_id,
            "type": {"$in": ["single", "parlay"]},
            "selections.match_id": {"$in": list(match_ids_seen)},
            "status": {"$ne": "void"},
        },
        {"selections.match_id": 1},
    ).to_list(length=100)

    already_bet_matches: set[str] = set()
    for slip in existing_slips:
        for sel in slip.get("selections", []):
            if sel["match_id"] in match_ids_seen:
                already_bet_matches.add(sel["match_id"])

    if already_bet_matches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a bet on one or more of these matches.",
        )

    # Validate each selection
    locked_selections: list[dict] = []

    from app.providers.odds_api import odds_provider
    staleness_limit = settings.ODDS_STALENESS_MAX_SECONDS
    if odds_provider.circuit_open:
        staleness_limit = staleness_limit * 4

    for sel in selections:
        match = matches_by_id.get(sel["match_id"])
        if not match:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Match {sel['match_id']} not found.",
            )

        # Check match hasn't started
        commence = ensure_utc(match["match_date"])
        if commence <= now or match["status"] != "scheduled":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Match {sel['match_id']} is not available for betting.",
            )

        # Get odds for the market
        odds_data = build_legacy_like_odds(match)
        market = sel.get("market", "h2h")

        if market == "h2h":
            market_odds = odds_data.get("h2h", {})
        elif market == "totals":
            market_odds = odds_data.get("totals", {})
        elif market == "spreads":
            market_odds = odds_data.get("spreads", {})
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid market: {market}",
            )

        if not market_odds:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No odds available for match {sel['match_id']}.",
            )

        pick = sel["pick"]
        if market == "h2h" and pick not in market_odds:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pick '{pick}' for match {sel['match_id']}.",
            )

        # Staleness check
        odds_updated_at = odds_data.get("updated_at")
        if not odds_updated_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No odds available for match {sel['match_id']}.",
            )
        odds_age = (now - ensure_utc(odds_updated_at)).total_seconds()
        if odds_age > staleness_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Odds are stale. Please reload the page.",
            )

        # Lock odds server-side
        if market == "h2h":
            locked_odds = market_odds[pick]
        elif market == "totals":
            locked_odds = market_odds.get(f"{pick}_odds", market_odds.get(pick, 0))
        else:
            locked_odds = market_odds.get(f"{pick}_odds", market_odds.get(pick, 0))

        # Drift guard
        displayed = sel["displayed_odds"]
        if abs(displayed - locked_odds) / max(locked_odds, 0.01) > 0.2:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Odds have changed.",
                    "code": "ODDS_CHANGED",
                    "match_id": sel["match_id"],
                },
            )

        locked_selections.append({
            "match_id": sel["match_id"],
            "market": market,
            "pick": pick,
            "locked_odds": locked_odds,
            "points_earned": None,
            "is_auto": False,
            "status": "pending",
        })

    # Calculate slip-level values
    all_odds = [s["locked_odds"] for s in locked_selections]
    total_odds = reduce(mul, all_odds, 1.0)
    stake = 10.0
    potential_payout = round(stake * total_odds, 2)
    slip_type = "single" if len(locked_selections) == 1 else "parlay"

    slip_doc = {
        "user_id": user_id,
        "squad_id": None,
        "type": slip_type,
        "selections": locked_selections,
        "total_odds": round(total_odds, 4),
        "stake": stake,
        "potential_payout": potential_payout,
        "funding": "virtual",
        "status": "pending",
        "submitted_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.betting_slips.insert_one(slip_doc)
    slip_doc["_id"] = result.inserted_id

    logger.info(
        "Slip created: user=%s type=%s selections=%d total_odds=%.2f payout=%.2f",
        user_id, slip_type, len(locked_selections), total_odds, potential_payout,
    )

    return slip_doc


# ---------- Internal slip creation (no HTTP exceptions, for Q-Bot auto-bet) ----------

async def create_slip_internal(
    user_id: str, match_id: str, prediction: str,
) -> dict | None:
    """Create a single h2h bet without HTTP validation. Used by Q-Bot auto-bet."""
    now = utcnow()
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match or match.get("status") != "scheduled":
        return None

    odds = build_legacy_like_odds(match).get("h2h", {})
    locked_odds = odds.get(prediction)
    if not locked_odds:
        return None

    slip_doc = {
        "user_id": user_id,
        "squad_id": None,
        "type": "single",
        "selections": [{
            "match_id": match_id,
            "market": "h2h",
            "pick": prediction,
            "locked_odds": locked_odds,
            "points_earned": None,
            "is_auto": True,
            "status": "pending",
        }],
        "total_odds": locked_odds,
        "stake": 10.0,
        "potential_payout": round(10.0 * locked_odds, 2),
        "funding": "virtual",
        "status": "pending",
        "submitted_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.betting_slips.insert_one(slip_doc)
    slip_doc["_id"] = result.inserted_id
    return slip_doc


# ---------- Draft lifecycle ----------

async def create_or_get_draft(
    user_id: str,
    slip_type: str = "single",
    squad_id: str | None = None,
    matchday_id: str | None = None,
    sport_key: str | None = None,
    funding: str = "virtual",
) -> dict:
    """Create a new draft slip or return the user's existing active draft.

    Only one active draft per (user, type) for single/parlay.
    Matchday rounds keyed by (user, matchday_id, squad_id).
    """
    now = utcnow()

    # For matchday rounds, key by matchday_id + squad_id
    if slip_type == "matchday_round" and matchday_id:
        existing = await _db.db.betting_slips.find_one({
            "user_id": user_id,
            "matchday_id": matchday_id,
            "squad_id": squad_id,
            "type": "matchday_round",
            "status": {"$in": ["draft", "pending"]},
        })
        if existing:
            return existing
    else:
        # For market slips: find existing draft of same type
        existing = await _db.db.betting_slips.find_one({
            "user_id": user_id,
            "status": "draft",
            "type": slip_type,
        })
        if existing:
            return existing

    slip_doc: dict = {
        "user_id": user_id,
        "type": slip_type,
        "squad_id": squad_id,
        "selections": [],
        "status": "draft",
        "funding": funding,
        "submitted_at": None,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    # Add matchday-specific fields
    if slip_type == "matchday_round" and matchday_id:
        # v3 matchday IDs are strings like "v3:sport:season:round" — resolve context
        from app.services.matchday_service import _resolve_v3_matchday_context
        context, _ = await _resolve_v3_matchday_context(matchday_id)
        slip_doc["matchday_id"] = matchday_id
        slip_doc["matchday_number"] = context.get("matchday_number")
        slip_doc["sport_key"] = context.get("sport_key")
        slip_doc["season"] = context.get("season")
        # Freeze point_weights from squad config at creation
        if squad_id:
            squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
            if squad:
                from app.services.squad_league_service import get_active_league_config
                lc = get_active_league_config(squad, context.get("sport_key", ""))
                if lc:
                    slip_doc["point_weights"] = lc.get("config", {}).get("point_weights")

    if sport_key:
        slip_doc["sport_key"] = sport_key

    result = await _db.db.betting_slips.insert_one(slip_doc)
    slip_doc["_id"] = result.inserted_id
    return slip_doc


def _get_current_odds(match: dict, market: str, pick: str) -> float | None:
    """Get current server-side odds for a match + market + pick."""
    odds = build_legacy_like_odds(match)
    if market == "h2h":
        return odds.get("h2h", {}).get(pick)
    elif market == "totals":
        totals = odds.get("totals", {})
        return totals.get(f"{pick}_odds", totals.get(pick))
    return None


def is_leg_editable(
    sel: dict, match: dict | None, lock_mins: int = LOCK_MINUTES,
) -> bool:
    """Check if a selection can still be modified.

    Used by PATCH endpoint to guard edits AND by GET endpoints
    to set `editable` flag in response.
    """
    if sel.get("status") not in ("draft",):
        return False
    if not match:
        return False
    return not is_match_locked(match, lock_mins)


async def _get_squad_lock_minutes(squad_id: str | None) -> int:
    """Get lock_minutes from squad config, defaulting to LOCK_MINUTES."""
    if not squad_id:
        return LOCK_MINUTES
    squad = await _db.db.squads.find_one(
        {"_id": ObjectId(squad_id)}, {"league_configs": 1, "game_mode_config": 1},
    )
    if not squad:
        return LOCK_MINUTES
    # Check league_configs for lock_minutes override
    for lc in squad.get("league_configs", []):
        if not lc.get("deactivated_at"):
            lock = lc.get("config", {}).get("lock_minutes")
            if lock is not None:
                return int(lock)
    return squad.get("game_mode_config", {}).get("lock_minutes", LOCK_MINUTES)


async def patch_selection(
    slip_id: str, user_id: str,
    action: str, match_id: str,
    market: str = "h2h",
    pick: Any | None = None,
    displayed_odds: float | None = None,
) -> dict:
    """Add, update, or remove a leg on a draft/pending slip.

    Guards:
    - Slip must be in 'draft' status (or 'pending' for matchday if leg not locked)
    - Match must exist and not be started
    - Kickoff minus lock_minutes must be in the future
    """
    slip = await _db.db.betting_slips.find_one({
        "_id": ObjectId(slip_id), "user_id": user_id,
    })
    if not slip:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slip not found.")

    # Guard: slip is editable
    if slip["status"] not in ("draft", "pending"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Slip is not editable.")

    # For pending non-matchday slips, reject edits
    if slip["status"] == "pending" and slip.get("type") != "matchday_round":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Slip is already submitted.")

    now = utcnow()
    lock_mins = await _get_squad_lock_minutes(slip.get("squad_id"))
    is_matchday = slip.get("type") == "matchday_round"

    # exact_score validation: both home and away scores required
    if market == "exact_score" and action in ("add", "update"):
        if not isinstance(pick, dict) or pick.get("home") is None or pick.get("away") is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "exact_score pick requires both home and away scores.",
            )

    # Validate match exists — matchday_round uses matches_v3 (integer IDs)
    if is_matchday:
        match = await _db.db.matches_v3.find_one({"_id": int(match_id)})
    else:
        match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Match {match_id} not found.")

    if action == "add":
        if pick is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick is required.")

        if is_match_locked(match, lock_mins):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is locked.")

        # Check not already in selections
        for existing in slip.get("selections", []):
            if existing["match_id"] == match_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "Match already in selections. Use 'update'.",
                )

        sel: dict = {
            "match_id": match_id,
            "market": market,
            "pick": pick,
            "points_earned": None,
            "is_auto": False,
            "status": "draft",
        }
        # Only set odds fields for market bets (not exact_score)
        if market != "exact_score":
            sel["displayed_odds"] = displayed_odds
            sel["locked_odds"] = None
        if market == "totals":
            totals = build_legacy_like_odds(match).get("totals", {})
            sel["line"] = totals.get("line", 2.5)

        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]},
            {"$push": {"selections": sel}, "$set": {"updated_at": now}},
        )

    elif action == "update":
        if pick is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick is required.")

        # Find the selection for this match
        sel_idx = None
        for i, s in enumerate(slip.get("selections", [])):
            if s["match_id"] == match_id:
                sel_idx = i
                break

        if sel_idx is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Selection not found for this match.")

        existing_sel = slip["selections"][sel_idx]
        # Check editability
        if existing_sel.get("status") not in ("draft",):
            # Admin unlock can bypass
            if match_id not in slip.get("admin_unlocked_matches", []):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selection is locked.")

        if is_match_locked(match, lock_mins):
            if match_id not in slip.get("admin_unlocked_matches", []):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is locked.")

        update_fields: dict = {
            f"selections.{sel_idx}.pick": pick,
            "updated_at": now,
        }
        # Only update odds for market bets (not exact_score)
        if displayed_odds is not None and market != "exact_score":
            update_fields[f"selections.{sel_idx}.displayed_odds"] = displayed_odds

        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]}, {"$set": update_fields},
        )

    elif action == "remove":
        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]},
            {
                "$pull": {"selections": {"match_id": match_id}},
                "$set": {"updated_at": now},
            },
        )

    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid action: {action}")

    # Return updated slip
    return await _db.db.betting_slips.find_one({"_id": slip["_id"]})


async def submit_slip(slip_id: str, user_id: str) -> dict:
    """Transition draft -> pending. Freezes odds server-side.

    For wallet-funded slips: deducts stake atomically.
    For market slips: locks odds (20% drift guard from displayed_odds).
    """
    slip = await _db.db.betting_slips.find_one({
        "_id": ObjectId(slip_id), "user_id": user_id,
    })
    if not slip:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slip not found.")
    if slip["status"] != "draft":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Slip is not a draft.")
    if not slip.get("selections"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Slip has no selections.")

    now = utcnow()
    selections = slip["selections"]

    # Lock odds for each selection
    for sel in selections:
        if sel.get("status") != "draft":
            continue  # Already locked (e.g. per-leg auto-lock)

        match = await _db.db.matches.find_one({"_id": ObjectId(sel["match_id"])})
        if not match:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Match {sel['match_id']} not found.",
            )

        lock_mins = await _get_squad_lock_minutes(slip.get("squad_id"))
        if is_match_locked(match, lock_mins):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Match {sel['match_id']} is locked.",
            )

        market = sel.get("market", "h2h")
        if market in ("h2h", "totals"):
            current_odds = _get_current_odds(match, market, sel["pick"])
            if current_odds is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"No odds available for match {sel['match_id']}.",
                )

            # Drift guard
            displayed = sel.get("displayed_odds")
            if displayed and abs(displayed - current_odds) / max(current_odds, 0.01) > 0.2:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {"message": "Odds have changed.", "code": "ODDS_CHANGED",
                     "match_id": sel["match_id"]},
                )
            sel["locked_odds"] = current_odds

        sel["status"] = "pending"
        sel["locked_at"] = now

    # Calculate total_odds and potential_payout for market slips
    if slip["type"] in ("single", "parlay"):
        active_odds = [
            sel.get("locked_odds", 1.0)
            for sel in selections if sel.get("locked_odds")
        ]
        if active_odds:
            total_odds = reduce(mul, active_odds, 1.0)
            slip["total_odds"] = round(total_odds, 4)
            slip["potential_payout"] = round(slip.get("stake", 10.0) * total_odds, 2)

    # Wallet deduction for funded slips
    if slip.get("funding") == "wallet" and slip.get("wallet_id"):
        from app.services import wallet_service
        await wallet_service.deduct_stake(
            wallet_id=slip["wallet_id"],
            user_id=user_id,
            squad_id=slip.get("squad_id", ""),
            stake=slip.get("stake", 0),
            reference_type="betting_slip",
            reference_id=str(slip["_id"]),
            description=f"Slip submitted: {len(selections)} selections",
        )

    update: dict = {
        "selections": selections,
        "status": "pending",
        "submitted_at": now,
        "updated_at": now,
    }
    if slip.get("total_odds") is not None:
        update["total_odds"] = slip["total_odds"]
    if slip.get("potential_payout") is not None:
        update["potential_payout"] = slip["potential_payout"]

    await _db.db.betting_slips.update_one(
        {"_id": slip["_id"]}, {"$set": update},
    )

    slip["status"] = "pending"
    slip["submitted_at"] = now
    logger.info("Slip submitted: user=%s slip=%s type=%s", user_id, slip_id, slip["type"])
    return slip


async def discard_draft(slip_id: str, user_id: str) -> bool:
    """Delete a draft slip. Only drafts can be deleted."""
    result = await _db.db.betting_slips.delete_one({
        "_id": ObjectId(slip_id), "user_id": user_id, "status": "draft",
    })
    return result.deleted_count > 0


# ---------- Mode-specific creation ----------

async def create_bankroll_bet(
    user_id: str, squad_id: str, match_id: str,
    prediction: str, stake: float, displayed_odds: float,
) -> dict:
    """Create a bankroll-funded single bet. Deducts stake from wallet atomically."""
    now = utcnow()

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this squad.")

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    commence = ensure_utc(match["match_date"])
    if commence <= now or match["status"] != "scheduled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is not available for betting.")

    # Get squad bankroll config
    from app.services.squad_league_service import get_active_league_config
    lc = get_active_league_config(squad, match.get("sport_key", ""))
    config = lc.get("config", {}) if lc else {}
    min_bet = config.get("min_bet", 10)
    max_bet_pct = config.get("max_bet_pct", 50)

    if stake < min_bet:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Minimum bet is {min_bet}.")

    # Lock odds + drift guard
    odds = build_legacy_like_odds(match).get("h2h", {})
    locked_odds = odds.get(prediction)
    if not locked_odds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid prediction '{prediction}'.")
    if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
        raise HTTPException(status.HTTP_409_CONFLICT, "Odds have changed.")

    # Get or create wallet, check max bet
    from app.services import wallet_service
    sport_key = match.get("sport_key", "")
    season = match.get("matchday_season") or match.get("season", 0)
    wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, sport_key, season)
    wallet_id = str(wallet["_id"])

    max_stake = wallet["balance"] * (max_bet_pct / 100)
    if stake > max_stake:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Maximum bet is {max_bet_pct}% of balance ({max_stake:.0f} coins).",
        )

    potential_payout = round(stake * locked_odds, 2)

    slip_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "type": "single",
        "selections": [{
            "match_id": match_id,
            "market": "h2h",
            "pick": prediction,
            "displayed_odds": displayed_odds,
            "locked_odds": locked_odds,
            "status": "pending",
            "locked_at": now,
        }],
        "total_odds": locked_odds,
        "stake": stake,
        "potential_payout": potential_payout,
        "funding": "wallet",
        "wallet_id": wallet_id,
        "sport_key": sport_key,
        "status": "pending",
        "submitted_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.betting_slips.insert_one(slip_doc)
    slip_doc["_id"] = result.inserted_id
    slip_id = str(result.inserted_id)

    # Deduct stake from wallet
    await wallet_service.deduct_stake(
        wallet_id=wallet_id,
        user_id=user_id,
        squad_id=squad_id,
        stake=stake,
        reference_type="betting_slip",
        reference_id=slip_id,
        description=f"Bankroll bet: {prediction} @ {locked_odds:.2f} ({stake:.0f} coins)",
    )

    logger.info(
        "Bankroll bet created: user=%s squad=%s match=%s odds=%.2f stake=%.0f",
        user_id, squad_id, match_id, locked_odds, stake,
    )
    return slip_doc


async def create_over_under_bet(
    user_id: str, squad_id: str, match_id: str,
    prediction: str, displayed_odds: float,
    stake: float | None = None,
) -> dict:
    """Create an over/under bet. Optionally wallet-funded."""
    now = utcnow()

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this squad.")

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    commence = ensure_utc(match["match_date"])
    if commence <= now or match["status"] != "scheduled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is not available for betting.")

    if prediction not in ("over", "under"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prediction must be 'over' or 'under'.")

    totals = build_legacy_like_odds(match).get("totals", {})
    locked_odds = totals.get(prediction)
    if not locked_odds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No over/under odds available.")
    line = totals.get("line", 2.5)

    if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
        raise HTTPException(status.HTTP_409_CONFLICT, "Odds have changed.")

    funding = "virtual"
    wallet_id = None
    actual_stake = stake or 10.0
    potential_payout = round(actual_stake * locked_odds, 2)

    # Handle wallet funding if stake provided
    if stake and stake > 0:
        from app.services import wallet_service
        sport_key = match.get("sport_key", "")
        season = match.get("matchday_season") or match.get("season", 0)
        wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, sport_key, season)
        wallet_id = str(wallet["_id"])
        funding = "wallet"

    slip_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "type": "single",
        "selections": [{
            "match_id": match_id,
            "market": "totals",
            "pick": prediction,
            "displayed_odds": displayed_odds,
            "locked_odds": locked_odds,
            "line": line,
            "status": "pending",
            "locked_at": now,
        }],
        "total_odds": locked_odds,
        "stake": actual_stake,
        "potential_payout": potential_payout,
        "funding": funding,
        "wallet_id": wallet_id,
        "sport_key": match.get("sport_key"),
        "status": "pending",
        "submitted_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.betting_slips.insert_one(slip_doc)
    slip_doc["_id"] = result.inserted_id
    slip_id = str(result.inserted_id)

    if funding == "wallet" and wallet_id:
        from app.services import wallet_service
        await wallet_service.deduct_stake(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            stake=actual_stake,
            reference_type="betting_slip",
            reference_id=slip_id,
            description=f"O/U bet: {prediction} @ {locked_odds:.2f} (line {line})",
        )

    logger.info(
        "O/U bet created: user=%s squad=%s match=%s %s line=%.1f odds=%.2f",
        user_id, squad_id, match_id, prediction, line, locked_odds,
    )
    return slip_doc


async def create_parlay(
    user_id: str, squad_id: str, matchday_id: str,
    legs: list[dict], stake: float | None = None,
) -> dict:
    """Create a parlay (combo bet) with exactly 3 legs."""
    from app.services.parlay_service import REQUIRED_LEGS
    now = utcnow()

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this squad.")

    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Matchday not found.")

    if len(legs) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Exactly {REQUIRED_LEGS} matches required.")

    match_ids = [leg["match_id"] for leg in legs]
    if len(set(match_ids)) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each match may only appear once.")

    # Validate legs and lock odds
    validated_legs = []
    combined_odds = 1.0

    for leg in legs:
        match = await _db.db.matches.find_one({"_id": ObjectId(leg["match_id"])})
        if not match:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Match {leg['match_id']} not found.")

        commence = ensure_utc(match["match_date"])
        if commence <= now or match["status"] != "scheduled":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One of the matches has already started.")

        prediction = leg["prediction"]
        displayed = leg["displayed_odds"]

        if prediction in ("over", "under"):
            totals = build_legacy_like_odds(match).get("totals", {})
            locked_odds = totals.get(prediction)
            if not locked_odds:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No O/U odds for match {leg['match_id']}.")
            market = "totals"
            line = totals.get("line", 2.5)
        else:
            h2h = build_legacy_like_odds(match).get("h2h", {})
            locked_odds = h2h.get(prediction)
            if not locked_odds:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid prediction '{prediction}'.")
            market = "h2h"
            line = None

        if abs(displayed - locked_odds) / max(locked_odds, 0.01) > 0.2:
            raise HTTPException(status.HTTP_409_CONFLICT, "Odds have changed.")

        combined_odds *= locked_odds
        sel: dict = {
            "match_id": leg["match_id"],
            "market": market,
            "pick": prediction,
            "displayed_odds": displayed,
            "locked_odds": locked_odds,
            "status": "pending",
            "locked_at": now,
        }
        if line is not None:
            sel["line"] = line
        validated_legs.append(sel)

    combined_odds = round(combined_odds, 3)
    game_mode = squad.get("game_mode", "classic")
    funding = "virtual"
    wallet_id = None
    actual_stake = stake if (game_mode == "bankroll" and stake and stake > 0) else None

    if actual_stake:
        potential_payout = round(actual_stake * combined_odds, 2)
        funding = "wallet"
        sport_key = matchday["sport_key"]
        season = matchday["season"]
        from app.services import wallet_service
        wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, sport_key, season)
        wallet_id = str(wallet["_id"])
    else:
        actual_stake = 10.0
        potential_payout = round(combined_odds * 10, 2)

    slip_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "type": "parlay",
        "matchday_id": matchday_id,
        "sport_key": matchday["sport_key"],
        "season": matchday["season"],
        "matchday_number": matchday["matchday_number"],
        "selections": validated_legs,
        "total_odds": combined_odds,
        "stake": actual_stake,
        "potential_payout": potential_payout,
        "funding": funding,
        "wallet_id": wallet_id,
        "status": "pending",
        "submitted_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = await _db.db.betting_slips.insert_one(slip_doc)
        slip_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Parlay for this matchday already exists.")

    slip_id = str(slip_doc["_id"])

    if funding == "wallet" and wallet_id:
        from app.services import wallet_service
        await wallet_service.deduct_stake(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            stake=actual_stake,
            reference_type="betting_slip",
            reference_id=slip_id,
            description=f"Parlay: {REQUIRED_LEGS} matches, odds {combined_odds:.2f}",
        )

    logger.info(
        "Parlay created: user=%s squad=%s matchday=%s odds=%.2f",
        user_id, squad_id, matchday_id, combined_odds,
    )
    return slip_doc


async def make_survivor_pick(
    user_id: str, squad_id: str, match_id: str, team: str,
) -> dict:
    """Make a survivor pick. Find-or-create season-long slip, append pick."""
    now = utcnow()

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this squad.")

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    if is_match_locked(match):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is locked.")

    sport_key = match.get("sport_key", "")
    season = match.get("matchday_season") or match.get("season", 0)
    matchday_number = match.get("matchday_number")
    if matchday_number is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match has no matchday number (survivor requires matchday sports).")

    home_team_id = match.get("home_team_id")
    away_team_id = match.get("away_team_id")
    if not home_team_id or not away_team_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Match team identity not initialized yet.")

    registry = TeamRegistry.get()
    team_id = await registry.resolve(team, sport_key)
    if team_id not in (home_team_id, away_team_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team not in this match.")

    # Find or create season-long survivor slip
    slip = await _db.db.betting_slips.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
        "type": "survivor",
    })

    if slip:
        # Check not eliminated
        if slip.get("status") == "lost":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You have been eliminated.")

        # Check team not already used
        if team_id in slip.get("used_team_ids", []) or team in slip.get("used_teams", []):
            raise HTTPException(status.HTTP_409_CONFLICT, "You already used this team this season.")

        # Check one pick per matchday
        for sel in slip.get("selections", []):
            if sel.get("matchday_number") == matchday_number:
                raise HTTPException(status.HTTP_409_CONFLICT, "You already have a pick for this matchday.")

        # Append pick
        new_sel = {
            "match_id": match_id,
            "market": "survivor_pick",
            "pick": team,
            "team_name": team,
            "team_id": team_id,
            "status": "pending",
            "matchday_number": matchday_number,
        }
        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]},
            {
                "$push": {"selections": new_sel},
                "$addToSet": {
                    "used_team_ids": team_id,
                    "used_teams": team,
                },
                "$set": {"updated_at": now},
            },
        )
        slip = await _db.db.betting_slips.find_one({"_id": slip["_id"]})
    else:
        # Create new survivor slip
        slip_doc = {
            "user_id": user_id,
            "squad_id": squad_id,
            "type": "survivor",
            "sport_key": sport_key,
            "season": season,
            "selections": [{
                "match_id": match_id,
                "market": "survivor_pick",
                "pick": team,
                "team_name": team,
                "team_id": team_id,
                "status": "pending",
                "matchday_number": matchday_number,
            }],
            "used_team_ids": [team_id],
            "used_teams": [team],
            "streak": 0,
            "status": "pending",  # Survivor: immediate commit, no draft
            "submitted_at": now,
            "resolved_at": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = await _db.db.betting_slips.insert_one(slip_doc)
            slip_doc["_id"] = result.inserted_id
            slip = slip_doc
        except DuplicateKeyError:
            raise HTTPException(status.HTTP_409_CONFLICT, "Survivor entry already exists.")

    logger.info(
        "Survivor pick: user=%s squad=%s team=%s matchday=%s",
        user_id, squad_id, team, matchday_number,
    )
    return slip


async def make_fantasy_pick(
    user_id: str, squad_id: str, match_id: str, team: str,
) -> dict:
    """Make a fantasy team pick for a matchday."""
    now = utcnow()

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this squad.")

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    if is_match_locked(match):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is locked.")

    sport_key = match.get("sport_key", "")
    season = match.get("matchday_season") or match.get("season", 0)
    matchday_number = match.get("matchday_number")
    if matchday_number is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match has no matchday number (fantasy requires matchday sports).")

    home_team_id = match.get("home_team_id")
    away_team_id = match.get("away_team_id")
    if not home_team_id or not away_team_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Match team identity not initialized yet.")

    registry = TeamRegistry.get()
    team_id = await registry.resolve(team, sport_key)
    if team_id not in (home_team_id, away_team_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team not in this match.")

    # Upsert: find existing pick for this matchday or create new
    existing = await _db.db.betting_slips.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
        "matchday_number": matchday_number,
        "type": "fantasy",
    })

    if existing:
        # Update existing pick
        await _db.db.betting_slips.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "selections": [{
                    "match_id": match_id,
                    "market": "fantasy_pick",
                    "pick": team,
                    "team_name": team,
                    "team_id": team_id,
                    "status": "draft",
                    "matchday_number": matchday_number,
                }],
                "updated_at": now,
            }},
        )
        return await _db.db.betting_slips.find_one({"_id": existing["_id"]})

    slip_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "type": "fantasy",
        "sport_key": sport_key,
        "season": season,
        "matchday_number": matchday_number,
        "selections": [{
            "match_id": match_id,
            "market": "fantasy_pick",
            "pick": team,
            "team_name": team,
            "team_id": team_id,
            "status": "draft",
            "matchday_number": matchday_number,
        }],
        "status": "draft",
        "submitted_at": None,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = await _db.db.betting_slips.insert_one(slip_doc)
        slip_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Fantasy pick already exists for this matchday.")

    logger.info(
        "Fantasy pick: user=%s squad=%s team=%s matchday=%s",
        user_id, squad_id, team, matchday_number,
    )
    return slip_doc


# ---------- Query + Serialization ----------

async def get_user_slips(
    user_id: str,
    status_filter: Optional[str] = None,
    slip_type: Optional[str] = None,
    match_ids: Optional[list[str]] = None,
    limit: int = 50,
) -> list[dict]:
    """Get betting slips for a user."""
    query: dict = {"user_id": user_id}
    if status_filter:
        query["status"] = status_filter
    if slip_type:
        query["type"] = slip_type
    if match_ids:
        query["selections.match_id"] = {"$in": match_ids}

    return await _db.db.betting_slips.find(query).sort(
        "updated_at", -1
    ).to_list(length=limit)


async def get_slip_by_id(slip_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    """Get a single betting slip by ID, optionally scoped to a user."""
    query: dict = {"_id": ObjectId(slip_id)}
    if user_id:
        query["user_id"] = user_id
    return await _db.db.betting_slips.find_one(query)


def slip_to_response(doc: dict) -> dict:
    """Convert a betting_slips document to a response dict."""
    selections = []
    for sel in doc.get("selections", []):
        row = dict(sel)
        if row.get("team_id") is not None:
            row["team_id"] = str(row["team_id"])
        selections.append(row)

    used_team_ids = [str(tid) for tid in doc.get("used_team_ids", [])] if doc.get("used_team_ids") else None
    resp = {
        "id": str(doc["_id"]),
        "user_id": doc["user_id"],
        "squad_id": doc.get("squad_id"),
        "type": doc["type"],
        "selections": selections,
        "total_odds": doc.get("total_odds"),
        "stake": doc.get("stake", 10.0),
        "potential_payout": doc.get("potential_payout"),
        "funding": doc.get("funding", "virtual"),
        "status": doc["status"],
        "submitted_at": doc.get("submitted_at"),
        "resolved_at": doc.get("resolved_at"),
        # Matchday round
        "matchday_id": doc.get("matchday_id"),
        "matchday_number": doc.get("matchday_number"),
        "total_points": doc.get("total_points"),
        "point_weights": doc.get("point_weights"),
        "auto_bet_strategy": doc.get("auto_bet_strategy"),
        # Survivor
        "streak": doc.get("streak"),
        "eliminated_at": doc.get("eliminated_at"),
        "used_teams": doc.get("used_teams"),
        "used_team_ids": used_team_ids,
        # Common
        "sport_key": doc.get("sport_key"),
        "season": doc.get("season"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
    return resp
