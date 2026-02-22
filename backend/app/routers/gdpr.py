import hashlib
import logging
import secrets
from datetime import datetime, timezone

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
from fastapi import Response, Request

logger = logging.getLogger("quotico.gdpr")
router = APIRouter(prefix="/api/gdpr", tags=["gdpr"])


class DeleteAccountRequest(BaseModel):
    password: str  # Require password confirmation for deletion


@router.get("/export")
async def export_data(user=Depends(get_current_user), db=Depends(get_db)):
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

    return {
        "export_date": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "tips": tips_export,
        "points_transactions": transactions_export,
        "squads": squads_export,
        "battle_participations": battles_export,
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
    now = datetime.now(timezone.utc)

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

    # Invalidate all tokens
    await invalidate_user_tokens(user_id)
    clear_auth_cookies(response)

    logger.info("Account anonymized: %s -> %s", user_id, anon_email)
    return {
        "message": "Dein Konto wurde anonymisiert. Deine Tipps bleiben für die Plattform-Integrität erhalten, sind aber nicht mehr mit deiner Person verknüpft.",
    }
