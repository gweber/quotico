"""Betting slips API — unified endpoints for all game modes."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

import app.database as _db
from app.models.betting_slip import (
    BankrollBetRequest,
    CreateDraftRequest,
    CreateSlipRequest,
    FantasyPickRequest,
    OverUnderBetRequest,
    ParlayRequest,
    PatchSelectionRequest,
    SurvivorPickRequest,
)
from app.services.auth_service import get_current_user
from app.services.betting_slip_service import (
    create_bankroll_bet,
    create_or_get_draft,
    create_over_under_bet,
    create_parlay,
    create_slip,
    discard_draft,
    get_slip_by_id,
    get_user_slips,
    make_fantasy_pick,
    make_survivor_pick,
    patch_selection,
    slip_to_response,
    submit_slip,
)

router = APIRouter(prefix="/api/betting-slips", tags=["betting-slips"])


# ---------- Classic direct-submit ----------

@router.post("/", status_code=status.HTTP_201_CREATED)
async def submit_classic_slip(
    body: CreateSlipRequest,
    user=Depends(get_current_user),
):
    """Submit a betting slip with one or more selections (direct, no draft)."""
    user_id = str(user["_id"])
    selections = [
        {
            "match_id": s.match_id,
            "market": s.market,
            "pick": s.pick,
            "displayed_odds": s.displayed_odds,
        }
        for s in body.selections
    ]
    slip = await create_slip(user_id=user_id, selections=selections)
    return slip_to_response(slip)


# ---------- Draft lifecycle ----------

@router.post("/draft", status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: CreateDraftRequest,
    user=Depends(get_current_user),
):
    """Create a new draft slip or return existing active draft."""
    user_id = str(user["_id"])
    slip = await create_or_get_draft(
        user_id=user_id,
        slip_type=body.type.value,
        squad_id=body.squad_id,
        matchday_id=body.matchday_id,
        sport_key=body.sport_key,
        funding=body.funding,
    )
    return slip_to_response(slip)


@router.patch("/{slip_id}/selections")
async def edit_selection(
    slip_id: str,
    body: PatchSelectionRequest,
    user=Depends(get_current_user),
):
    """Add, update, or remove a leg on a draft slip."""
    user_id = str(user["_id"])
    slip = await patch_selection(
        slip_id=slip_id,
        user_id=user_id,
        action=body.action,
        match_id=body.match_id,
        market=body.market,
        pick=body.pick,
        displayed_odds=body.displayed_odds,
    )
    return slip_to_response(slip)


@router.post("/{slip_id}/submit")
async def submit_draft(
    slip_id: str,
    user=Depends(get_current_user),
):
    """Submit a draft slip: freeze odds, deduct wallet if applicable."""
    user_id = str(user["_id"])
    slip = await submit_slip(slip_id=slip_id, user_id=user_id)
    return slip_to_response(slip)


@router.delete("/{slip_id}")
async def delete_draft(
    slip_id: str,
    user=Depends(get_current_user),
):
    """Discard a draft slip. Only drafts can be deleted."""
    user_id = str(user["_id"])
    deleted = await discard_draft(slip_id=slip_id, user_id=user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found or not deletable.",
        )
    return {"deleted": True}


# ---------- Mode-specific creation ----------

@router.post("/bankroll", status_code=status.HTTP_201_CREATED)
async def bankroll_bet(
    body: BankrollBetRequest,
    user=Depends(get_current_user),
):
    """Place a bankroll-funded bet."""
    user_id = str(user["_id"])
    slip = await create_bankroll_bet(
        user_id=user_id,
        squad_id=body.squad_id,
        match_id=body.match_id,
        prediction=body.prediction,
        stake=body.stake,
        displayed_odds=body.displayed_odds,
    )
    return slip_to_response(slip)


@router.post("/over-under", status_code=status.HTTP_201_CREATED)
async def over_under_bet(
    body: OverUnderBetRequest,
    user=Depends(get_current_user),
):
    """Place an over/under bet."""
    user_id = str(user["_id"])
    slip = await create_over_under_bet(
        user_id=user_id,
        squad_id=body.squad_id,
        match_id=body.match_id,
        prediction=body.prediction,
        displayed_odds=body.displayed_odds,
        stake=body.stake,
    )
    return slip_to_response(slip)


@router.post("/parlay", status_code=status.HTTP_201_CREATED)
async def parlay_bet(
    body: ParlayRequest,
    user=Depends(get_current_user),
):
    """Create a parlay (combo bet) with exactly 3 legs."""
    user_id = str(user["_id"])
    slip = await create_parlay(
        user_id=user_id,
        squad_id=body.squad_id,
        matchday_id=body.matchday_id,
        legs=[leg.model_dump() for leg in body.legs],
        stake=body.stake,
    )
    return slip_to_response(slip)


@router.post("/survivor", status_code=status.HTTP_201_CREATED)
async def survivor_pick_endpoint(
    body: SurvivorPickRequest,
    user=Depends(get_current_user),
):
    """Make a survivor pick."""
    user_id = str(user["_id"])
    slip = await make_survivor_pick(
        user_id=user_id,
        squad_id=body.squad_id,
        match_id=body.match_id,
        team=body.team,
    )
    return slip_to_response(slip)


@router.post("/fantasy", status_code=status.HTTP_201_CREATED)
async def fantasy_pick_endpoint(
    body: FantasyPickRequest,
    user=Depends(get_current_user),
):
    """Make a fantasy team pick."""
    user_id = str(user["_id"])
    slip = await make_fantasy_pick(
        user_id=user_id,
        squad_id=body.squad_id,
        match_id=body.match_id,
        team=body.team,
    )
    return slip_to_response(slip)


# ---------- Query ----------

@router.get("/mine")
async def my_slips(
    user=Depends(get_current_user),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    slip_type: str | None = Query(None, alias="type", description="Filter by type"),
    match_ids: str | None = Query(None, description="Comma-separated match IDs to filter by"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get the current user's betting slips."""
    user_id = str(user["_id"])
    match_id_list = [m.strip() for m in match_ids.split(",") if m.strip()] if match_ids else None
    slips = await get_user_slips(
        user_id, status_filter=status_filter, slip_type=slip_type,
        match_ids=match_id_list, limit=limit,
    )
    return [slip_to_response(s) for s in slips]


@router.get("/draft")
async def get_active_draft(
    user=Depends(get_current_user),
    slip_type: str = Query("single", alias="type"),
):
    """Get the user's active draft slip (for cross-device restore)."""
    user_id = str(user["_id"])
    slip = await _db.db.betting_slips.find_one({
        "user_id": user_id,
        "status": "draft",
        "type": slip_type,
    })
    if not slip:
        return None
    return slip_to_response(slip)


@router.get("/{slip_id}")
async def get_slip(
    slip_id: str,
    user=Depends(get_current_user),
):
    """Get a single betting slip by ID (must belong to the current user)."""
    user_id = str(user["_id"])
    slip = await get_slip_by_id(slip_id, user_id=user_id)
    if not slip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Slip not found.",
        )
    return slip_to_response(slip)
