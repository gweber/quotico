"""Squad War Room service — per-match squad bet visibility with Shadow Logic."""

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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if requesting_user_id not in squad.get("members", []):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You are not a member of this squad."
        )

    # 2. Fetch match
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    member_ids = squad["members"]
    now = utcnow()
    commence = ensure_utc(match["match_date"])
    is_post_kickoff = now >= commence

    # 3. Bulk alias lookup
    member_docs = await _db.db.users.find(
        {"_id": {"$in": [ObjectId(uid) for uid in member_ids]}},
        {"alias": 1},
    ).to_list(length=len(member_ids))
    alias_map = {str(d["_id"]): d.get("alias", "Anonymous") for d in member_docs}

    # 4. Squad bets for this match (from unified betting_slips)
    slips = await _db.db.betting_slips.find(
        {
            "selections.match_id": match_id,
            "user_id": {"$in": member_ids},
            "type": {"$in": ["single", "parlay"]},
        }
    ).to_list(length=len(member_ids))
    # Build bet_map: extract the matching selection for this match from each slip
    bet_map: dict[str, dict] = {}
    for slip in slips:
        for sel in slip.get("selections", []):
            if sel.get("match_id") == match_id:
                bet_map[slip["user_id"]] = {
                    "selection": {"type": sel.get("market", "h2h"), "value": sel["pick"]},
                    "locked_odds": sel.get("locked_odds"),
                    "status": slip["status"],
                    "points_earned": sel.get("points_earned"),
                }
                break

    # 5. Build member list with Shadow Logic
    members = _build_members(
        member_ids, alias_map, bet_map, requesting_user_id,
        is_post_kickoff, match,
    )

    # 6. Build match payload
    match_payload = {
        "id": str(match["_id"]),
        "sport_key": match["sport_key"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "match_date": match["match_date"],
        "status": match["status"],
        "odds": match.get("odds", {}),
        "result": match.get("result", {}),
    }

    # 7. Consensus + mavericks (post-kickoff only)
    consensus = None
    mavericks = None
    if is_post_kickoff:
        consensus, mavericks = _compute_consensus(bet_map)

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
    bet_map: dict[str, dict],
    requesting_user_id: str,
    is_post_kickoff: bool,
    match: dict,
) -> list[dict]:
    result = []
    for uid in member_ids:
        bet = bet_map.get(uid)
        has_bet = bet is not None
        alias = alias_map.get(uid, "Anonymous")
        is_self = uid == requesting_user_id

        if not has_bet:
            result.append({
                "user_id": uid,
                "alias": alias,
                "has_bet": False,
                "is_self": is_self,
            })
            continue

        selection = bet["selection"]
        locked_odds = bet["locked_odds"]
        bet_status = bet["status"]
        points_earned = bet.get("points_earned")

        # Shadow Logic gate
        reveal_bet = is_post_kickoff or is_self

        is_currently_winning = None
        if is_post_kickoff and has_bet:
            is_currently_winning = _infer_winning(selection["value"], match)

        result.append({
            "user_id": uid,
            "alias": alias,
            "has_bet": True,
            "is_self": is_self,
            "selection": selection if reveal_bet else None,
            "locked_odds": locked_odds if reveal_bet else None,
            "bet_status": bet_status if reveal_bet else None,
            "points_earned": points_earned if reveal_bet else None,
            "is_currently_winning": is_currently_winning if reveal_bet else None,
        })

    return result


def _infer_winning(selection_value: str, match: dict) -> bool | None:
    """Determine if a selection is currently winning based on score."""
    home_score = match.get("result", {}).get("home_score")
    away_score = match.get("result", {}).get("away_score")
    match_status = match.get("status", "scheduled")

    if match_status == "scheduled":
        return None
    if home_score is None or away_score is None:
        return None

    # For final matches, use authoritative result
    if match_status == "final" and match.get("result", {}).get("outcome"):
        return selection_value == match["result"]["outcome"]

    # For live matches, derive from current score
    if home_score > away_score:
        current_result = "1"
    elif away_score > home_score:
        current_result = "2"
    else:
        current_result = "X"

    return selection_value == current_result


def _compute_consensus(
    bet_map: dict[str, dict],
) -> tuple[dict | None, list[str]]:
    """Compute selection breakdown and identify mavericks."""
    if not bet_map:
        return None, []

    counts: dict[str, int] = {}
    picker_map: dict[str, list[str]] = {}

    for uid, bet in bet_map.items():
        val = bet["selection"]["value"]
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

    consensus = {"percentages": percentages, "total_bettors": total}
    return consensus, mavericks
