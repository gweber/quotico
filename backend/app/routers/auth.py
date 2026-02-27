import logging
from datetime import datetime

from app.utils import utcnow

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
from app.services.audit_service import log_audit
from app.services.encryption import decrypt, needs_reencryption, reencrypt
from app.config_legal import TERMS_VERSION
from app.services.persona_policy_service import get_persona_policy_service
from jwt.exceptions import InvalidTokenError as JWTError


class TwoFALogin(BaseModel):
    """Request body for the second step of a 2FA login."""
    email: EmailStr
    password: str
    code: str

logger = logging.getLogger("quotico.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, request: Request, response: Response, db=Depends(get_db)):
    """Register a new user with age verification and disclaimer."""
    existing = await db.users.find_one({"email": body.email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email address is already registered.",
        )

    # Age verification (server-side)
    try:
        birth = datetime.strptime(body.birth_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date of birth. Format: YYYY-MM-DD.",
        )

    today = utcnow().date()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if age < 18:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be at least 18 years old to use Quotico.de.",
        )

    if not body.disclaimer_accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please accept the disclaimer.",
        )

    now = utcnow()
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
        "is_adult": True,
        "birth_date_verified_at": now,
        "terms_accepted_version": TERMS_VERSION if body.disclaimer_accepted else None,
        "terms_accepted_at": now if body.disclaimer_accepted else None,
        "tip_persona": "casual",
        "tip_persona_updated_at": now,
        "tip_override_persona": None,
        "tip_override_updated_at": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    await log_audit(actor_id=user_id, target_id=user_id, action="REGISTER", request=request)
    logger.info("User registered: %s", user_id)
    return {"message": "Registration successful."}


@router.post("/login")
async def login(body: UserLogin, request: Request, response: Response, db=Depends(get_db)):
    """Login with email and password."""
    user = await db.users.find_one({"email": body.email, "is_deleted": False})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended.",
        )

    user_id = str(user["_id"])

    if not verify_password(body.password, user["hashed_password"]):
        await log_audit(
            actor_id=user_id, target_id=user_id, action="LOGIN_FAILED", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # If 2FA is enabled, require verification
    if user.get("is_2fa_enabled"):
        return {
            "requires_2fa": True,
            "message": "Please enter your 2FA code.",
        }

    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    await log_audit(actor_id=user_id, target_id=user_id, action="LOGIN_SUCCESS", request=request)
    logger.info("User logged in: %s", user_id)
    return {"message": "Login successful."}


@router.post("/login/2fa")
async def login_2fa(body: TwoFALogin, request: Request, response: Response, db=Depends(get_db)):
    """Complete login for users with 2FA enabled.

    Step 1: /login returns { requires_2fa: true }.
    Step 2: Client calls this endpoint with email + password + TOTP code.
    """
    user = await db.users.find_one({"email": body.email, "is_deleted": False})
    if not user or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended.",
        )

    if not user.get("is_2fa_enabled") or not user.get("encrypted_2fa_secret"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled for this account.",
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
            detail="Invalid 2FA code.",
        )

    user_id = str(user["_id"])
    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)

    await log_audit(actor_id=user_id, target_id=user_id, action="LOGIN_SUCCESS", request=request)
    logger.info("User logged in (2FA): %s", user_id)
    return {"message": "Login successful."}


@router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    """Refresh access token using refresh token cookie (with rotation)."""
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token provided.",
        )

    try:
        payload = decode_jwt(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type.",
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
            detail="Token has already been used. Please log in again.",
        )

    # Rotate: invalidate old, issue new
    new_access = create_access_token(user_id)
    new_refresh = await rotate_refresh_token(jti, user_id, family)
    set_auth_cookies(response, new_access, new_refresh)

    return {"message": "Token refreshed."}


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
    return {"message": "Logged out."}


class CompleteProfileRequest(BaseModel):
    """Request body for completing profile after Google OAuth."""
    birth_date: str
    disclaimer_accepted: bool


@router.post("/complete-profile")
async def complete_profile(
    body: CompleteProfileRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Complete age verification for Google OAuth users."""
    if user.get("is_adult"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile already complete.",
        )

    try:
        birth = datetime.strptime(body.birth_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date of birth. Format: YYYY-MM-DD.",
        )

    today = utcnow().date()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if age < 18:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be at least 18 years old to use Quotico.de.",
        )

    if not body.disclaimer_accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please accept the disclaimer.",
        )

    now = utcnow()
    update_fields = {"is_adult": True, "birth_date_verified_at": now, "updated_at": now}
    if body.disclaimer_accepted:
        update_fields["terms_accepted_version"] = TERMS_VERSION
        update_fields["terms_accepted_at"] = now
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": update_fields},
    )

    user_id = str(user["_id"])
    await log_audit(
        actor_id=user_id, target_id=user_id, action="AGE_VERIFIED", request=request,
    )
    return {"message": "Profile completed."}


@router.get("/me", response_model=UserResponse)
async def get_me(user=Depends(get_current_user)):
    """Get current user profile."""
    policy = get_persona_policy_service()
    effective, source = await policy.resolve_effective_persona(user)
    return UserResponse(
        email=user["email"],
        alias=user.get("alias", ""),
        alias_slug=user.get("alias_slug", ""),
        has_custom_alias=user.get("has_custom_alias", False),
        points=user["points"],
        is_admin=user.get("is_admin", False),
        is_2fa_enabled=user.get("is_2fa_enabled", False),
        is_adult=user.get("is_adult", True),
        google_linked=bool(user.get("google_sub")),
        has_password=bool(user.get("hashed_password")),
        terms_accepted_version=user.get("terms_accepted_version"),
        tip_persona=str(user.get("tip_persona") or "casual"),
        tip_persona_effective=effective,
        tip_persona_source=source,
        tip_persona_updated_at=user.get("tip_persona_updated_at"),
        tip_override_persona=user.get("tip_override_persona"),
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
        return {"available": False, "reason": "This name is already taken."}

    return {"available": True, "reason": ""}
