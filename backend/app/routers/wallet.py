"""Wallet & Bankroll endpoints — balance, bets, transactions, disclaimer."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.models.wallet import (
    BankrollBetCreate, BankrollBetResponse,
    OverUnderBetCreate, OverUnderBetResponse,
    TransactionResponse, WalletResponse,
)
from app.services import bankroll_service, over_under_service, wallet_service
from app.services.auth_service import get_current_user
import app.database as _db
from app.utils import utcnow

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


# ---------- Disclaimer ----------

@router.post("/accept-disclaimer", status_code=status.HTTP_200_OK)
async def accept_disclaimer(user=Depends(get_current_user)):
    """Accept the virtual currency disclaimer (required before wallet use)."""
    user_id = str(user["_id"])
    if user.get("wallet_disclaimer_accepted_at"):
        return {"message": "Already accepted."}

    await _db.db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"wallet_disclaimer_accepted_at": utcnow()}},
    )
    return {"message": "Disclaimer accepted."}


# ---------- Wallet ----------

@router.get("/{squad_id}", response_model=WalletResponse)
async def get_wallet(
    squad_id: str,
    league_id: int = Query(..., description="League id"),
    season: int = Query(None, description="Season year"),
    user=Depends(get_current_user),
):
    """Get current wallet for a squad (lazy-creates if needed)."""
    user_id = str(user["_id"])
    _check_disclaimer(user)
    _check_adult(user)

    if not season:
        season = utcnow().year

    wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, int(league_id), season)
    return WalletResponse(
        id=str(wallet["_id"]),
        squad_id=wallet["squad_id"],
        league_id=wallet["league_id"],
        season=wallet["season"],
        balance=wallet["balance"],
        initial_balance=wallet["initial_balance"],
        total_wagered=wallet["total_wagered"],
        total_won=wallet["total_won"],
        status=wallet["status"],
        bankrupt_since=wallet.get("bankrupt_since"),
        consecutive_bonus_days=wallet.get("consecutive_bonus_days", 0),
    )


@router.get("/{squad_id}/transactions")
async def get_transactions(
    squad_id: str,
    league_id: int = Query(...),
    season: int = Query(None),
    limit: int = Query(50, le=200),
    skip: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    """Get wallet transaction history."""
    user_id = str(user["_id"])
    if not season:
        season = utcnow().year

    wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, int(league_id), season)
    txns = await wallet_service.get_wallet_transactions(str(wallet["_id"]), limit, skip)
    return [
        TransactionResponse(
            id=str(t["_id"]),
            type=t["type"],
            amount=t["amount"],
            balance_after=t["balance_after"],
            description=t["description"],
            created_at=t["created_at"],
        )
        for t in txns
    ]


# ---------- Bankroll Bets ----------

@router.post("/{squad_id}/bet", status_code=status.HTTP_201_CREATED)
async def place_bankroll_bet(
    squad_id: str,
    body: BankrollBetCreate,
    user=Depends(get_current_user),
):
    """Place a bankroll bet (atomic wallet deduction)."""
    user_id = str(user["_id"])
    _check_disclaimer(user)
    _check_adult(user)

    bet = await bankroll_service.place_bet(
        user_id=user_id,
        squad_id=squad_id,
        match_id=body.match_id,
        prediction=body.prediction,
        stake=body.stake,
        displayed_odds=body.displayed_odds,
    )
    return BankrollBetResponse(
        id=str(bet["_id"]),
        match_id=bet["match_id"],
        prediction=bet["prediction"],
        stake=bet["stake"],
        locked_odds=bet["locked_odds"],
        potential_win=bet["potential_win"],
        status=bet["status"],
        points_earned=bet.get("points_earned"),
        resolved_at=bet.get("resolved_at"),
        created_at=bet["created_at"],
    )


@router.get("/{squad_id}/bets")
async def get_bankroll_bets(
    squad_id: str,
    matchday_id: str = Query(""),
    user=Depends(get_current_user),
):
    """Get user's bankroll bets for a squad."""
    user_id = str(user["_id"])
    bets = await bankroll_service.get_user_bets(user_id, squad_id, matchday_id)
    return [
        BankrollBetResponse(
            id=str(b["_id"]),
            match_id=b["match_id"],
            prediction=b["prediction"],
            stake=b["stake"],
            locked_odds=b["locked_odds"],
            potential_win=b["potential_win"],
            status=b["status"],
            points_earned=b.get("points_earned"),
            resolved_at=b.get("resolved_at"),
            created_at=b["created_at"],
        )
        for b in bets
    ]


# ---------- Over/Under Bets ----------

@router.post("/{squad_id}/over-under", status_code=status.HTTP_201_CREATED)
async def place_over_under_bet(
    squad_id: str,
    body: OverUnderBetCreate,
    user=Depends(get_current_user),
):
    """Place an over/under bet."""
    user_id = str(user["_id"])
    _check_disclaimer(user)
    _check_adult(user)

    bet = await over_under_service.place_bet(
        user_id=user_id,
        squad_id=squad_id,
        match_id=body.match_id,
        prediction=body.prediction,
        stake=body.stake,
        displayed_odds=body.displayed_odds,
    )
    return OverUnderBetResponse(
        id=str(bet["_id"]),
        match_id=bet["match_id"],
        prediction=bet["prediction"],
        line=bet["line"],
        locked_odds=bet["locked_odds"],
        stake=bet.get("stake"),
        status=bet["status"],
        points_earned=bet.get("points_earned"),
        created_at=bet["created_at"],
    )


@router.get("/{squad_id}/over-under")
async def get_over_under_bets(
    squad_id: str,
    matchday_id: str = Query(""),
    user=Depends(get_current_user),
):
    """Get user's over/under bets."""
    user_id = str(user["_id"])
    bets = await over_under_service.get_user_bets(user_id, squad_id, matchday_id)
    return [
        OverUnderBetResponse(
            id=str(b["_id"]),
            match_id=b["match_id"],
            prediction=b["prediction"],
            line=b["line"],
            locked_odds=b["locked_odds"],
            stake=b.get("stake"),
            status=b["status"],
            points_earned=b.get("points_earned"),
            created_at=b["created_at"],
        )
        for b in bets
    ]


# ---------- Helpers ----------

def _check_disclaimer(user: dict) -> None:
    if not user.get("wallet_disclaimer_accepted_at"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please accept the coin disclaimer first.",
        )


def _check_adult(user: dict) -> None:
    if not user.get("is_adult"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Age verification required.",
        )
