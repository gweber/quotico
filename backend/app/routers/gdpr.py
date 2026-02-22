import hashlib
import logging
import secrets
from datetime import datetime

from app.utils import utcnow

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database import get_db
from app.services.auth_service import (
    get_current_user,
    invalidate_user_tokens,
    clear_auth_cookies,
    verify_password,
)
from app.services.audit_service import log_audit
from fastapi import Response, Request

import app.database as _db

logger = logging.getLogger("quotico.gdpr")
router = APIRouter(prefix="/api/gdpr", tags=["gdpr"])

# Actions visible to users in their security log
USER_VISIBLE_ACTIONS = {
    "LOGIN_SUCCESS", "LOGIN_FAILED", "REGISTER", "AGE_VERIFIED",
    "2FA_ENABLED", "2FA_DISABLED", "ALIAS_CHANGED",
    "DATA_EXPORTED", "ACCOUNT_DELETED", "TERMS_ACCEPTED",
}


class DeleteAccountRequest(BaseModel):
    password: str  # Require password confirmation for deletion


@router.get("/security-log")
async def security_log(request: Request, user=Depends(get_current_user)):
    """GDPR self-service: view own login and security events."""
    user_id = str(user["_id"])
    logs = await _db.db.audit_logs.find(
        {"actor_id": user_id, "action": {"$in": list(USER_VISIBLE_ACTIONS)}},
    ).sort("timestamp", -1).limit(50).to_list(length=50)

    return [
        {
            "timestamp": entry["timestamp"].isoformat(),
            "action": entry["action"],
            "ip_truncated": entry.get("ip_truncated", ""),
        }
        for entry in logs
    ]


@router.get("/export")
async def export_data(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """DSGVO Art. 20: Export all personal data as JSON.

    Returns all user data, tips, squad memberships, and battle participations.
    """
    user_id = str(user["_id"])

    # User profile (without sensitive fields)
    profile = {
        "email": user["email"],
        "alias": user.get("alias", ""),
        "points": user["points"],
        "is_2fa_enabled": user.get("is_2fa_enabled", False),
        "household_group_id": user.get("household_group_id"),
        "wallet_disclaimer_accepted_at": (
            user["wallet_disclaimer_accepted_at"].isoformat()
            if user.get("wallet_disclaimer_accepted_at") else None
        ),
        "created_at": user["created_at"].isoformat(),
        "updated_at": user["updated_at"].isoformat(),
    }

    # All tips
    tips = await db.tips.find({"user_id": user_id}).to_list(length=10000)
    tips_export = [
        {
            "match_id": t["match_id"],
            "selection": t["selection"],
            "locked_odds": t["locked_odds"],
            "points_earned": t.get("points_earned"),
            "status": t["status"],
            "created_at": t["created_at"].isoformat(),
        }
        for t in tips
    ]

    # Points transactions
    transactions = await db.points_transactions.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    transactions_export = [
        {
            "tip_id": t["tip_id"],
            "delta": t["delta"],
            "scoring_version": t["scoring_version"],
            "created_at": t["created_at"].isoformat(),
        }
        for t in transactions
    ]

    # Squad memberships
    squads = await db.squads.find({"members": user_id}).to_list(length=50)
    squads_export = [
        {
            "name": s["name"],
            "role": "admin" if s["admin_id"] == user_id else "member",
            "joined": "unknown",  # Not tracked separately
        }
        for s in squads
    ]

    # Battle participations
    participations = await db.battle_participations.find(
        {"user_id": user_id}
    ).to_list(length=100)
    battles_export = [
        {
            "battle_id": p["battle_id"],
            "squad_id": p["squad_id"],
            "joined_at": p["joined_at"].isoformat(),
        }
        for p in participations
    ]

    # --- New game mode data ---

    # Wallets
    wallets = await db.wallets.find({"user_id": user_id}).to_list(length=50)
    wallets_export = [
        {
            "squad_id": w["squad_id"],
            "sport_key": w.get("sport_key"),
            "season": w.get("season"),
            "balance": w["balance"],
            "initial_balance": w.get("initial_balance"),
            "total_wagered": w.get("total_wagered", 0),
            "total_won": w.get("total_won", 0),
            "status": w.get("status"),
            "created_at": w["created_at"].isoformat(),
        }
        for w in wallets
    ]

    # Wallet transactions
    wallet_txns = await db.wallet_transactions.find(
        {"user_id": user_id}
    ).sort("created_at", 1).to_list(length=10000)
    wallet_txns_export = [
        {
            "type": t["type"],
            "amount": t["amount"],
            "balance_after": t.get("balance_after"),
            "reference_type": t.get("reference_type"),
            "description": t.get("description", ""),
            "created_at": t["created_at"].isoformat(),
        }
        for t in wallet_txns
    ]

    # Bankroll bets
    bankroll_bets = await db.bankroll_bets.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    bankroll_export = [
        {
            "match_id": b["match_id"],
            "prediction": b["prediction"],
            "stake": b["stake"],
            "locked_odds": b["locked_odds"],
            "potential_win": b.get("potential_win"),
            "status": b["status"],
            "points_earned": b.get("points_earned"),
            "created_at": b["created_at"].isoformat(),
        }
        for b in bankroll_bets
    ]

    # Survivor entries
    survivor = await db.survivor_entries.find(
        {"user_id": user_id}
    ).to_list(length=50)
    survivor_export = [
        {
            "squad_id": s["squad_id"],
            "sport_key": s.get("sport_key"),
            "season": s.get("season"),
            "status": s["status"],
            "picks": s.get("picks", []),
            "streak": s.get("streak", 0),
            "created_at": s["created_at"].isoformat(),
        }
        for s in survivor
    ]

    # Over/Under bets
    ou_bets = await db.over_under_bets.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    ou_export = [
        {
            "match_id": b["match_id"],
            "prediction": b["prediction"],
            "line": b.get("line"),
            "locked_odds": b["locked_odds"],
            "stake": b.get("stake"),
            "status": b["status"],
            "points_earned": b.get("points_earned"),
            "created_at": b["created_at"].isoformat(),
        }
        for b in ou_bets
    ]

    # Fantasy picks
    fantasy = await db.fantasy_picks.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    fantasy_export = [
        {
            "squad_id": f["squad_id"],
            "team": f["team"],
            "match_id": f["match_id"],
            "fantasy_points": f.get("fantasy_points"),
            "status": f["status"],
            "created_at": f["created_at"].isoformat(),
        }
        for f in fantasy
    ]

    # Parlays
    parlays = await db.parlays.find(
        {"user_id": user_id}
    ).to_list(length=1000)
    parlays_export = [
        {
            "squad_id": p["squad_id"],
            "matchday_id": p.get("matchday_id"),
            "legs": p.get("legs", []),
            "combined_odds": p.get("combined_odds"),
            "stake": p.get("stake"),
            "potential_win": p.get("potential_win"),
            "status": p["status"],
            "created_at": p["created_at"].isoformat(),
        }
        for p in parlays
    ]

    # Spieltag predictions
    spieltag = await db.spieltag_predictions.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    spieltag_export = [
        {
            "match_id": s["match_id"],
            "home_score": s.get("home_score"),
            "away_score": s.get("away_score"),
            "points_earned": s.get("points_earned"),
            "status": s.get("status"),
            "created_at": s["created_at"].isoformat(),
        }
        for s in spieltag
    ]

    # Device fingerprints (hash-only, no raw data)
    fingerprints = await db.device_fingerprints.find(
        {"user_id": user_id}
    ).to_list(length=50)
    fingerprints_export = [
        {
            "fingerprint_hash": fp["fingerprint_hash"],
            "ip_truncated": fp.get("ip_truncated", ""),
            "created_at": fp["created_at"].isoformat(),
            "last_seen_at": fp["last_seen_at"].isoformat(),
        }
        for fp in fingerprints
    ]

    await log_audit(actor_id=user_id, target_id=user_id, action="DATA_EXPORTED", request=request)

    return {
        "export_date": utcnow().isoformat(),
        "profile": profile,
        "tips": tips_export,
        "points_transactions": transactions_export,
        "squads": squads_export,
        "battle_participations": battles_export,
        "wallets": wallets_export,
        "wallet_transactions": wallet_txns_export,
        "bankroll_bets": bankroll_export,
        "survivor_entries": survivor_export,
        "over_under_bets": ou_export,
        "fantasy_picks": fantasy_export,
        "parlays": parlays_export,
        "spieltag_predictions": spieltag_export,
        "device_fingerprints": fingerprints_export,
    }


@router.delete("/account", status_code=status.HTTP_200_OK)
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """DSGVO Art. 17: Right to erasure.

    Anonymizes the account:
    - Email replaced with sha256 hash (leaderboard stays correct)
    - Password hash deleted
    - 2FA secret deleted
    - is_deleted flag set
    - All refresh tokens invalidated

    Tips are retained with anonymized user_id for platform integrity.
    """
    # Verify password
    if not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falsches Passwort.",
        )

    user_id = str(user["_id"])
    now = utcnow()

    # Generate anonymized email hash
    salt = secrets.token_hex(8)
    anon_hash = hashlib.sha256(f"{user['email']}{salt}".encode()).hexdigest()[:16]
    anon_email = f"deleted-{anon_hash}@anonymized.quotico.de"

    # Anonymize user document (including alias)
    anon_alias = f"Deleted#{anon_hash[:6]}"
    anon_alias_slug = f"deleted{anon_hash[:6]}"
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "email": anon_email,
                "hashed_password": "",
                "alias": anon_alias,
                "alias_slug": anon_alias_slug,
                "has_custom_alias": False,
                "encrypted_2fa_secret": None,
                "is_2fa_enabled": False,
                "is_deleted": True,
                "household_group_id": None,
                "wallet_disclaimer_accepted_at": None,
                "updated_at": now,
            }
        },
    )

    # Remove from all squads
    await db.squads.update_many(
        {"members": user_id},
        {"$pull": {"members": user_id}},
    )

    # Transfer admin role or delete squad if user was admin and sole member
    admin_squads = await db.squads.find({"admin_id": user_id}).to_list(length=50)
    for squad in admin_squads:
        remaining = [m for m in squad["members"] if m != user_id]
        if remaining:
            await db.squads.update_one(
                {"_id": squad["_id"]},
                {"$set": {"admin_id": remaining[0]}},
            )
        else:
            await db.squads.delete_one({"_id": squad["_id"]})

    # Delete device fingerprints (personal data — full deletion)
    await db.device_fingerprints.delete_many({"user_id": user_id})

    # Anonymize game mode data (keep for platform integrity, anonymize user_id)
    anon_user_id = f"anon-{anon_hash}"
    game_collections = [
        "wallets", "wallet_transactions", "bankroll_bets",
        "survivor_entries", "over_under_bets", "fantasy_picks",
        "parlays", "spieltag_predictions",
    ]
    for coll_name in game_collections:
        coll = db[coll_name]
        await coll.update_many(
            {"user_id": user_id},
            {"$set": {"user_id": anon_user_id}},
        )

    # Invalidate all tokens
    await invalidate_user_tokens(user_id)
    clear_auth_cookies(response)

    await log_audit(actor_id=user_id, target_id=user_id, action="ACCOUNT_DELETED", request=request)
    logger.info("Account anonymized: %s -> %s", user_id, anon_email)
    return {
        "message": "Dein Konto wurde anonymisiert. Deine Tipps bleiben für die Plattform-Integrität erhalten, sind aber nicht mehr mit deiner Person verknüpft.",
    }
