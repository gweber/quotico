import logging
from datetime import datetime, timezone

import pyotp
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from pydantic import BaseModel, EmailStr

from fastapi import Query as QueryParam

from app.database import get_db
from app.models.user import UserCreate, UserLogin, UserResponse
from app.services.alias_service import generate_default_alias, validate_alias, normalize_slug
from app.services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    rotate_refresh_token,
    invalidate_token_family,
    invalidate_user_tokens,
    is_refresh_token_valid,
    decode_jwt,
    set_auth_cookies,
    clear_auth_cookies,
    get_current_user,
)
from app.services.encryption import decrypt, needs_reencryption, reencrypt
from jwt.exceptions import InvalidTokenError as JWTError


class TwoFALogin(BaseModel):
    """Request body for the second step of a 2FA login."""
    email: EmailStr
    password: str
    code: str

logger = logging.getLogger("quotico.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, response: Response, db=Depends(get_db)):
    """Register a new user."""
    existing = await db.users.find_one({"email": body.email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Diese E-Mail-Adresse ist bereits registriert.",
        )

    now = datetime.now(timezone.utc)
    alias, alias_slug = await generate_default_alias(db)
    user_doc = {
        "email": body.email,
        "hashed_password": hash_password(body.password),
        "alias": alias,
        "alias_slug": alias_slug,
        "has_custom_alias": False,
        "points": 0.0,
        "is_admin": False,
        "is_banned": False,
        "is_2fa_enabled": False,
        "encrypted_2fa_secret": None,
        "encryption_key_version": 1,
        "is_deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    logger.info("User registered: %s", user_id)
    return {"message": "Registrierung erfolgreich."}


@router.post("/login")
async def login(body: UserLogin, response: Response, db=Depends(get_db)):
    """Login with email and password."""
    user = await db.users.find_one({"email": body.email, "is_deleted": False})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-Mail oder Passwort ist falsch.",
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dein Konto wurde gesperrt.",
        )

    if not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-Mail oder Passwort ist falsch.",
        )

    user_id = str(user["_id"])

    # If 2FA is enabled, require verification (handled in Phase 4)
    if user.get("is_2fa_enabled"):
        return {
            "requires_2fa": True,
            "message": "Bitte 2FA-Code eingeben.",
        }

    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    logger.info("User logged in: %s", user_id)
    return {"message": "Anmeldung erfolgreich."}


@router.post("/login/2fa")
async def login_2fa(body: TwoFALogin, response: Response, db=Depends(get_db)):
    """Complete login for users with 2FA enabled.

    Step 1: /login returns { requires_2fa: true }.
    Step 2: Client calls this endpoint with email + password + TOTP code.
    """
    user = await db.users.find_one({"email": body.email, "is_deleted": False})
    if not user or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-Mail oder Passwort ist falsch.",
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dein Konto wurde gesperrt.",
        )

    if not user.get("is_2fa_enabled") or not user.get("encrypted_2fa_secret"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA ist für dieses Konto nicht aktiviert.",
        )

    # Decrypt the stored TOTP secret and verify the code
    key_version = user.get("encryption_key_version", 1)
    encrypted_secret = user["encrypted_2fa_secret"]

    # Lazy re-encryption if key was rotated
    if needs_reencryption(key_version):
        new_encrypted, new_version = reencrypt(encrypted_secret, key_version)
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "encrypted_2fa_secret": new_encrypted,
                "encryption_key_version": new_version,
            }},
        )
        encrypted_secret = new_encrypted
        key_version = new_version

    secret = decrypt(encrypted_secret, key_version=key_version)
    totp = pyotp.TOTP(secret)

    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiger 2FA-Code.",
        )

    user_id = str(user["_id"])
    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    logger.info("User logged in (2FA): %s", user_id)
    return {"message": "Anmeldung erfolgreich."}


@router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    """Refresh access token using refresh token cookie (with rotation)."""
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kein Refresh-Token vorhanden.",
        )

    try:
        payload = decode_jwt(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiges Refresh-Token.",
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiger Token-Typ.",
        )

    jti = payload.get("jti")
    family = payload.get("family")
    user_id = payload.get("sub")

    # Check if token was already used (replay detection)
    if not await is_refresh_token_valid(jti):
        # Token reuse detected — invalidate entire family
        if family:
            await invalidate_token_family(family)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token wurde bereits verwendet. Bitte neu anmelden.",
        )

    # Rotate: invalidate old, issue new
    new_access = create_access_token(user_id)
    new_refresh = await rotate_refresh_token(jti, user_id, family)
    set_auth_cookies(response, new_access, new_refresh)

    return {"message": "Token erneuert."}


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout — clear cookies and invalidate refresh token."""
    token = request.cookies.get("refresh_token")
    if token:
        try:
            payload = decode_jwt(token)
            family = payload.get("family")
            if family:
                await invalidate_token_family(family)
        except JWTError:
            pass

    clear_auth_cookies(response)
    return {"message": "Abgemeldet."}


@router.get("/me", response_model=UserResponse)
async def get_me(user=Depends(get_current_user)):
    """Get current user profile."""
    return UserResponse(
        email=user["email"],
        alias=user.get("alias", ""),
        alias_slug=user.get("alias_slug", ""),
        has_custom_alias=user.get("has_custom_alias", False),
        points=user["points"],
        is_admin=user.get("is_admin", False),
        is_2fa_enabled=user.get("is_2fa_enabled", False),
        created_at=user["created_at"],
    )


@router.get("/check-alias")
async def check_alias(name: str = QueryParam(..., min_length=1), db=Depends(get_db)):
    """Real-time alias availability check. Should be debounced by the client."""
    error = validate_alias(name)
    if error:
        return {"available": False, "reason": error}

    slug = normalize_slug(name)
    existing = await db.users.find_one({"alias_slug": slug}, {"_id": 1})
    if existing:
        return {"available": False, "reason": "Dieser Name ist bereits vergeben."}

    return {"available": True, "reason": ""}
