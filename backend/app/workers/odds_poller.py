import logging
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.config import settings
from app.providers.odds_api import SUPPORTED_SPORTS, odds_provider
from app.services.match_service import sync_matches_for_sport
from app.utils import ensure_utc, utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.odds_poller")


async def poll_odds() -> None:
    """Schedule-aware odds polling.

    Only polls sports that have matches in the next 48 hours,
    avoiding unnecessary API calls at 3 AM for matches at 3 PM.
    """
    now = utcnow()
    window = now + timedelta(hours=48)
    any_polled = False
    total_matches = 0
    total_odds_changed = 0

    for sport_key in SUPPORTED_SPORTS:
        has_upcoming = await _db.db.matches.find_one({
            "sport_key": sport_key,
            "status": {"$in": ["scheduled", "live"]},
            "match_date": {"$lte": window},
        })

        should_poll = has_upcoming is not None or await _is_initial_load(sport_key)

        if not should_poll:
            continue

        state_key = f"odds:{sport_key}"
        if await recently_synced(state_key, timedelta(minutes=12)):
            logger.debug("Smart sleep: %s odds polled recently, skipping", sport_key)
            continue

        try:
            result = await sync_matches_for_sport(sport_key)
            matches_count = result["matches"]
            odds_changed = result["odds_changed"]
            await set_synced(state_key, metrics={"matches": matches_count, "odds_changed": odds_changed})
            any_polled = True
            total_matches += matches_count
            total_odds_changed += odds_changed
            if matches_count > 0:
                logger.info("Polled %s: %d matches, %d odds changed", sport_key, matches_count, odds_changed)
                await _snapshot_odds(sport_key)
            if odds_changed > 0:
                try:
                    from app.routers.ws import live_manager
                    await live_manager.broadcast_odds_updated(sport_key, odds_changed)
                except Exception:
                    logger.warning("WS broadcast failed for %s", sport_key, exc_info=True)
                # Chain: regenerate QuoticoTip candidates for this sport
                await _generate_candidates(sport_key)
        except Exception as e:
            logger.error("Poll failed for %s: %s", sport_key, e)

    if any_polled:
        await set_synced("odds_poller", metrics={
            "matches": total_matches,
            "odds_changed": total_odds_changed,
        })
        usage = odds_provider.api_usage
        logger.info(
            "API usage: %s used, %s remaining",
            usage.get("requests_used", "?"),
            usage.get("requests_remaining", "?"),
        )


async def _snapshot_odds(sport_key: str) -> None:
    """Record current odds as a point-in-time snapshot for line movement tracking."""
    now = utcnow()
    matches = await _db.db.matches.find(
        {"sport_key": sport_key, "status": "scheduled"},
        {"_id": 1, "metadata.theoddsapi_id": 1, "sport_key": 1, "odds": 1},
    ).to_list(length=200)

    if not matches:
        return

    docs = [
        {
            "match_id": str(m["_id"]),
            "external_id": m.get("metadata", {}).get("theoddsapi_id"),
            "sport_key": m["sport_key"],
            "odds": m.get("odds", {}).get("h2h", {}),
            "totals": m.get("odds", {}).get("totals", {}),
            "spreads": m.get("odds", {}).get("spreads", {}),
            "snapshot_at": now,
        }
        for m in matches
    ]
    await _db.db.odds_snapshots.insert_many(docs, ordered=False)
    logger.debug("Snapshotted odds for %d %s matches", len(docs), sport_key)


async def _is_initial_load(sport_key: str) -> bool:
    """Check if we have any matches at all for this sport (first run)."""
    existing = await _db.db.matches.find_one({"sport_key": sport_key})
    return existing is None


async def _generate_candidates(sport_key: str) -> None:
    """Generate/refresh QuoticoTip candidates for a sport after odds change.

    Phase 1 of the Q-Bot pipeline: EV analysis runs immediately when odds
    arrive so candidates are always fresh. Actual betting happens at T-15min
    in run_qbot_bets().
    """
    from app.services.quotico_tip_service import generate_quotico_tip

    now = utcnow()
    matches = await _db.db.matches.find(
        {"sport_key": sport_key, "status": "scheduled", "match_date": {"$gt": now}},
    ).to_list(length=200)

    if not matches:
        return

    generated = 0
    no_signal = 0

    for match in matches:
        match_id = str(match["_id"])

        # Skip if existing QuoticoTip is still fresh (generated after last odds update)
        existing = await _db.db.quotico_tips.find_one(
            {"match_id": match_id}, {"generated_at": 1},
        )
        if existing:
            odds_updated = match.get("odds", {}).get("updated_at")
            if odds_updated:
                bet_generated = ensure_utc(existing["generated_at"])
                if bet_generated >= ensure_utc(odds_updated):
                    continue

        try:
            qbet = await generate_quotico_tip(match)
            await _db.db.quotico_tips.update_one(
                {"match_id": match_id}, {"$set": qbet}, upsert=True,
            )
            if qbet.get("status") == "active":
                generated += 1
            else:
                no_signal += 1
        except Exception as e:
            logger.error("QuoticoTip generation failed for %s: %s", match_id, e)

    # Expire stale candidates (match started)
    expired = await _db.db.quotico_tips.update_many(
        {"status": "active", "match_date": {"$lte": now}},
        {"$set": {"status": "expired"}},
    )

    if generated or no_signal or expired.modified_count:
        logger.info(
            "QuoticoTips for %s: %d generated, %d no_signal, %d expired",
            sport_key, generated, no_signal, expired.modified_count,
        )


# ---------------------------------------------------------------------------
# Phase 2: Auto-bet placement — runs every 5 min
#
# Places classic moneyline bets at T-lock_minutes before kickoff for:
#   1. Q-Bot system user (uses QuoticoTip candidates above confidence threshold)
#   2. Regular users who opted into auto-bet (q_bot / favorite / draw strategy)
# ---------------------------------------------------------------------------

_DEFAULT_LOCK_MINUTES = 15


def _resolve_auto_pick(
    strategy: str, match: dict, qbet: dict | None,
) -> str | None:
    """Resolve an auto-bet strategy to a moneyline pick ("1"/"X"/"2").

    Returns None if no pick can be determined.
    """
    if strategy == "none":
        return None

    if strategy == "draw":
        return "X"

    if strategy == "favorite":
        return _favorite_pick(match)

    if strategy == "q_bot":
        # Chain: QuoticoTip recommendation → odds favorite
        if qbet and qbet.get("recommended_selection"):
            return qbet["recommended_selection"]
        return _favorite_pick(match)

    return None


def _favorite_pick(match: dict) -> str | None:
    """Pick the moneyline favorite based on odds."""
    odds = match.get("odds", {}).get("h2h", {})
    home_odds = odds.get("1", 0)
    away_odds = odds.get("2", 0)
    if not home_odds or not away_odds:
        return None
    if home_odds < away_odds:
        return "1"
    elif away_odds < home_odds:
        return "2"
    else:
        return "X"


async def run_qbot_bets() -> None:
    """Place auto-bets for Q-Bot and opted-in users at T-lock before kickoff.

    Runs every 5 min. For each match approaching kickoff:
    1. Q-Bot system user: bets if QuoticoTip confidence >= threshold
    2. Users with auto_bet_strategy != "none" in their matchday predictions:
       places a classic moneyline bet based on their chosen strategy,
       respecting the squad's lock_minutes setting.

    Dedup: unique index on (user_id, match_id) prevents double bets.
    """
    from app.services.betting_slip_service import create_slip_internal

    now = utcnow()
    min_conf = settings.QBOT_MIN_CONFIDENCE

    # --- 1. Q-Bot system user ---
    qbot_user = await _db.db.users.find_one(
        {"email": "qbot@quotico.de", "is_bot": True}, {"_id": 1},
    )
    qbot_id = str(qbot_user["_id"]) if qbot_user else None

    qbot_window = now + timedelta(minutes=_DEFAULT_LOCK_MINUTES)
    qbot_placed = 0

    if qbot_id:
        candidates = await _db.db.quotico_tips.find({
            "status": "active",
            "confidence": {"$gte": min_conf},
            "match_date": {"$gt": now, "$lte": qbot_window},
        }).to_list(length=100)

        for qbet in candidates:
            try:
                result = await create_slip_internal(
                    user_id=qbot_id,
                    match_id=qbet["match_id"],
                    prediction=qbet["recommended_selection"],
                )
                if result:
                    qbot_placed += 1
            except DuplicateKeyError:
                pass
            except Exception as e:
                logger.warning("Q-Bot bet failed for %s: %s", qbet["match_id"], e)

    # --- 2. Users with auto-bet enabled ---
    # Find matchday predictions with auto-bet strategies that haven't been resolved
    auto_preds = await _db.db.matchday_predictions.find({
        "auto_bet_strategy": {"$ne": "none"},
        "status": {"$ne": "resolved"},
    }).to_list(length=5000)

    if not auto_preds:
        if qbot_placed:
            logger.info("Q-Bot placed %d bets", qbot_placed)
        return

    # Collect unique matchday IDs to fetch their match lists
    matchday_ids = list({p["matchday_id"] for p in auto_preds})
    matchdays = await _db.db.matchdays.find(
        {"_id": {"$in": [__import__("bson").ObjectId(mid) for mid in matchday_ids]}},
        {"match_ids": 1},
    ).to_list(length=len(matchday_ids))
    matchday_match_ids: dict[str, list[str]] = {
        str(md["_id"]): md.get("match_ids", []) for md in matchdays
    }

    # Collect all match IDs across all matchdays
    all_match_ids: set[str] = set()
    for mids in matchday_match_ids.values():
        all_match_ids.update(mids)

    if not all_match_ids:
        if qbot_placed:
            logger.info("Q-Bot placed %d bets", qbot_placed)
        return

    # Fetch matches that are scheduled and approaching kickoff
    # Use the widest possible window (some squads may have larger lock_minutes)
    max_window = now + timedelta(minutes=60)
    matches = await _db.db.matches.find({
        "_id": {"$in": [__import__("bson").ObjectId(mid) for mid in all_match_ids]},
        "status": "scheduled",
        "match_date": {"$gt": now, "$lte": max_window},
    }).to_list(length=500)
    matches_by_id: dict[str, dict] = {str(m["_id"]): m for m in matches}

    if not matches_by_id:
        if qbot_placed:
            logger.info("Q-Bot placed %d bets", qbot_placed)
        return

    # Fetch squad lock_minutes + auto_bet_blocked settings
    squad_ids = list({p["squad_id"] for p in auto_preds if p.get("squad_id")})
    squads_by_id: dict[str, dict] = {}
    if squad_ids:
        squads = await _db.db.squads.find(
            {"_id": {"$in": [__import__("bson").ObjectId(sid) for sid in squad_ids]}},
            {"lock_minutes": 1, "auto_bet_blocked": 1},
        ).to_list(length=len(squad_ids))
        squads_by_id = {str(s["_id"]): s for s in squads}

    # Pre-fetch QuoticoTips for q_bot strategy
    qbets_by_match: dict[str, dict] = {}
    has_qbot_strategy = any(p.get("auto_bet_strategy") == "q_bot" for p in auto_preds)
    if has_qbot_strategy:
        match_id_list = list(matches_by_id.keys())
        qbets = await _db.db.quotico_tips.find(
            {"match_id": {"$in": match_id_list}, "status": "active"},
            {"match_id": 1, "recommended_selection": 1},
        ).to_list(length=len(match_id_list))
        qbets_by_match = {q["match_id"]: q for q in qbets}

    # Pre-fetch existing classic bets — skip matches where user already bet
    auto_user_ids = list({p["user_id"] for p in auto_preds})
    existing_slips = await _db.db.betting_slips.find(
        {
            "user_id": {"$in": auto_user_ids},
            "type": "single",
            "selections.match_id": {"$in": list(matches_by_id.keys())},
            "status": {"$ne": "void"},
        },
        {"user_id": 1, "selections.match_id": 1},
    ).to_list(length=10000)
    already_bet: set[tuple[str, str]] = set()
    for slip in existing_slips:
        for sel in slip.get("selections", []):
            already_bet.add((slip["user_id"], sel["match_id"]))

    user_placed = 0
    user_skipped = 0

    for pred_doc in auto_preds:
        strategy = pred_doc["auto_bet_strategy"]
        user_id = pred_doc["user_id"]
        squad_id = pred_doc.get("squad_id")
        matchday_id = pred_doc["matchday_id"]

        # Check squad settings
        lock_mins = _DEFAULT_LOCK_MINUTES
        if squad_id:
            squad = squads_by_id.get(squad_id)
            if squad:
                if squad.get("auto_bet_blocked", False):
                    continue
                lock_mins = squad.get("lock_minutes", _DEFAULT_LOCK_MINUTES)

        lock_window = now + timedelta(minutes=lock_mins)

        # Get match IDs for this matchday
        match_ids = matchday_match_ids.get(matchday_id, [])

        # Existing manual predictions — don't auto-bet on those
        manual_match_ids = {
            p["match_id"] for p in pred_doc.get("predictions", [])
        }

        for match_id in match_ids:
            if match_id in manual_match_ids:
                continue

            # Skip if user already has a classic bet on this match
            if (user_id, match_id) in already_bet:
                continue

            match = matches_by_id.get(match_id)
            if not match:
                continue

            # Check if within this squad's lock window
            match_date = ensure_utc(match["match_date"])
            if match_date > lock_window:
                continue  # not yet time

            # Resolve strategy to moneyline pick
            qbet = qbets_by_match.get(match_id)
            pick = _resolve_auto_pick(strategy, match, qbet)
            if not pick:
                continue

            # Validate pick exists in odds
            h2h = match.get("odds", {}).get("h2h", {})
            if pick not in h2h:
                continue

            try:
                result = await create_slip_internal(
                    user_id=user_id,
                    match_id=match_id,
                    prediction=pick,
                )
                if result:
                    user_placed += 1
            except DuplicateKeyError:
                user_skipped += 1
            except Exception as e:
                logger.warning(
                    "Auto-bet failed: user=%s match=%s strategy=%s: %s",
                    user_id, match_id, strategy, e,
                )

    total = qbot_placed + user_placed
    if total:
        logger.info(
            "Auto-bets placed: %d Q-Bot, %d users (%d already existed)",
            qbot_placed, user_placed, user_skipped,
        )
