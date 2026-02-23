"""Squad War Room service — per-match squad tip visibility with Shadow Logic."""

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.utils import utcnow, ensure_utc


async def get_war_room(
    squad_id: str, match_id: str, requesting_user_id: str
) -> dict:
    """Build the war room payload for a squad + match.

    Shadow Logic: before kickoff, other members' selections are hidden.
    After kickoff, all selections are revealed with consensus data.
    """
    # 1. Fetch squad
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if requesting_user_id not in squad.get("members", []):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Du bist kein Mitglied dieses Squads."
        )

    # 2. Fetch match
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spiel nicht gefunden.")

    member_ids = squad["members"]
    now = utcnow()
    commence = ensure_utc(match["commence_time"])
    is_post_kickoff = now >= commence

    # 3. Bulk alias lookup
    member_docs = await _db.db.users.find(
        {"_id": {"$in": [ObjectId(uid) for uid in member_ids]}},
        {"alias": 1},
    ).to_list(length=len(member_ids))
    alias_map = {str(d["_id"]): d.get("alias", "Anonymous") for d in member_docs}

    # 4. Squad tips for this match
    tips = await _db.db.tips.find(
        {"match_id": match_id, "user_id": {"$in": member_ids}}
    ).to_list(length=len(member_ids))
    tip_map = {t["user_id"]: t for t in tips}

    # 5. Build member list with Shadow Logic
    members = _build_members(
        member_ids, alias_map, tip_map, requesting_user_id,
        is_post_kickoff, match,
    )

    # 6. Build match payload
    match_payload = {
        "id": str(match["_id"]),
        "sport_key": match["sport_key"],
        "teams": match["teams"],
        "commence_time": match["commence_time"],
        "status": match["status"],
        "current_odds": match.get("current_odds", {}),
        "result": match.get("result"),
        "home_score": match.get("home_score"),
        "away_score": match.get("away_score"),
    }

    # 7. Consensus + mavericks (post-kickoff only)
    consensus = None
    mavericks = None
    if is_post_kickoff:
        consensus, mavericks = _compute_consensus(tip_map)

    return {
        "match": match_payload,
        "members": members,
        "consensus": consensus,
        "mavericks": mavericks,
        "is_post_kickoff": is_post_kickoff,
    }


def _build_members(
    member_ids: list[str],
    alias_map: dict[str, str],
    tip_map: dict[str, dict],
    requesting_user_id: str,
    is_post_kickoff: bool,
    match: dict,
) -> list[dict]:
    result = []
    for uid in member_ids:
        tip = tip_map.get(uid)
        has_tipped = tip is not None
        alias = alias_map.get(uid, "Anonymous")
        is_self = uid == requesting_user_id

        if not has_tipped:
            result.append({
                "user_id": uid,
                "alias": alias,
                "has_tipped": False,
                "is_self": is_self,
            })
            continue

        selection = tip["selection"]
        locked_odds = tip["locked_odds"]
        tip_status = tip["status"]
        points_earned = tip.get("points_earned")

        # Shadow Logic gate
        reveal_tip = is_post_kickoff or is_self

        is_currently_winning = None
        if is_post_kickoff and has_tipped:
            is_currently_winning = _infer_winning(selection["value"], match)

        result.append({
            "user_id": uid,
            "alias": alias,
            "has_tipped": True,
            "is_self": is_self,
            "selection": selection if reveal_tip else None,
            "locked_odds": locked_odds if reveal_tip else None,
            "tip_status": tip_status if reveal_tip else None,
            "points_earned": points_earned if reveal_tip else None,
            "is_currently_winning": is_currently_winning if reveal_tip else None,
        })

    return result


def _infer_winning(selection_value: str, match: dict) -> bool | None:
    """Determine if a selection is currently winning based on score."""
    home_score = match.get("home_score")
    away_score = match.get("away_score")
    match_status = match.get("status", "upcoming")

    if match_status == "upcoming":
        return None
    if home_score is None or away_score is None:
        return None

    # For completed matches, use authoritative result
    if match_status == "completed" and match.get("result"):
        return selection_value == match["result"]

    # For live matches, derive from current score
    if home_score > away_score:
        current_result = "1"
    elif away_score > home_score:
        current_result = "2"
    else:
        current_result = "X"

    return selection_value == current_result


def _compute_consensus(
    tip_map: dict[str, dict],
) -> tuple[dict | None, list[str]]:
    """Compute selection breakdown and identify mavericks."""
    if not tip_map:
        return None, []

    counts: dict[str, int] = {}
    picker_map: dict[str, list[str]] = {}

    for uid, tip in tip_map.items():
        val = tip["selection"]["value"]
        counts[val] = counts.get(val, 0) + 1
        picker_map.setdefault(val, []).append(uid)

    total = sum(counts.values())
    if total == 0:
        return None, []

    percentages = {k: round(v / total * 100, 1) for k, v in counts.items()}
    majority_pick = max(counts, key=counts.get)

    mavericks = []
    for val, uids in picker_map.items():
        if val != majority_pick:
            mavericks.extend(uids)

    consensus = {"percentages": percentages, "total_tippers": total}
    return consensus, mavericks
