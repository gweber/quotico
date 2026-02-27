import logging
from datetime import datetime

from app.utils import utcnow

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo.errors import DuplicateKeyError

from app.database import get_db
from app.models.user import AliasUpdate, ChangePasswordRequest, SetPasswordRequest, TipPersonaUpdate, UnlinkGoogleRequest
from app.services.alias_service import validate_alias, normalize_slug
from app.services.auth_service import get_current_user, hash_password, verify_password
from app.services.audit_service import log_audit
from app.services.encryption import decrypt

logger = logging.getLogger("quotico.user")
router = APIRouter(prefix="/api/user", tags=["user"])


@router.patch("/alias")
async def update_alias(
    body: AliasUpdate,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Set or change the user's alias.

    Validates format, checks blacklist, and enforces uniqueness via DB index.
    """
    error = validate_alias(body.alias)
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    slug = normalize_slug(body.alias)
    now = utcnow()

    try:
        result = await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "alias": body.alias,
                    "alias_slug": slug,
                    "has_custom_alias": True,
                    "updated_at": now,
                }
            },
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This name is already taken.",
        )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alias could not be changed.",
        )

    user_id = str(user["_id"])
    old_alias = user.get("alias", "")
    await log_audit(
        actor_id=user_id, target_id=user_id, action="ALIAS_CHANGED",
        metadata={"old_alias": old_alias, "new_alias": body.alias}, request=request,
    )
    logger.info("User %s changed alias to: %s", user_id, body.alias)
    return {"message": "Alias changed successfully.", "alias": body.alias, "alias_slug": slug}


@router.patch("/tip-persona")
async def update_tip_persona(
    body: TipPersonaUpdate,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Update preferred tip persona for the current user."""
    now = utcnow()
    previous = str(user.get("tip_persona") or "casual")
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "tip_persona": str(body.tip_persona),
                "tip_persona_updated_at": now,
                "updated_at": now,
            }
        },
    )
    user_id = str(user["_id"])
    await log_audit(
        actor_id=user_id,
        target_id=user_id,
        action="TIP_PERSONA_CHANGED",
        metadata={"old_value": previous, "new_value": str(body.tip_persona)},
        request=request,
    )
    return {
        "message": "Tip persona updated.",
        "tip_persona": str(body.tip_persona),
        "tip_persona_updated_at": now.isoformat(),
    }


@router.post("/set-password")
async def set_password(
    body: SetPasswordRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Set a password on a Google-only account.

    Only available for users who signed in via Google and have no password yet.
    """
    if not user.get("google_sub"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This action is only available for Google-linked accounts.",
        )
    if user.get("hashed_password"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account already has a password.",
        )

    hashed = hash_password(body.password)
    now = utcnow()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"hashed_password": hashed, "updated_at": now}},
    )

    user_id = str(user["_id"])
    await log_audit(
        actor_id=user_id, target_id=user_id, action="PASSWORD_SET",
        metadata={"method": "google_account"}, request=request,
    )
    logger.info("User %s set a password on Google-only account", user_id)
    return {"message": "Password set successfully."}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Change the password for a user who already has one."""
    if not user.get("hashed_password"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No password set on this account.",
        )

    if not verify_password(body.current_password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect password.",
        )

    if user.get("is_2fa_enabled"):
        if not body.totp_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="2FA code required.",
            )
        import pyotp
        secret = decrypt(
            user["encrypted_2fa_secret"],
            key_version=user.get("encryption_key_version", 1),
        )
        if not pyotp.TOTP(secret).verify(body.totp_code, valid_window=1):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid 2FA code.",
            )

    hashed = hash_password(body.new_password)
    now = utcnow()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"hashed_password": hashed, "updated_at": now}},
    )

    user_id = str(user["_id"])
    await log_audit(
        actor_id=user_id, target_id=user_id, action="PASSWORD_CHANGED",
        request=request,
    )
    logger.info("User %s changed password", user_id)
    return {"message": "Password changed successfully."}


@router.post("/unlink-google")
async def unlink_google(
    body: UnlinkGoogleRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Unlink Google from the account.

    Requires a valid password as proof (user must have set one first).
    """
    if not user.get("google_sub"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google account linked.",
        )
    if not user.get("hashed_password"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set a password before unlinking Google.",
        )

    if not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect password.",
        )

    now = utcnow()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"updated_at": now}, "$unset": {"google_sub": ""}},
    )

    user_id = str(user["_id"])
    await log_audit(
        actor_id=user_id, target_id=user_id, action="GOOGLE_UNLINKED",
        request=request,
    )
    logger.info("User %s unlinked Google account", user_id)
    return {"message": "Google account unlinked."}
