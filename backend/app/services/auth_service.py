import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
import jwt
from jwt.exceptions import InvalidTokenError as JWTError

from app.config import settings
import app.database as _db
from app.database import get_db
from app.utils import utcnow

logger = logging.getLogger("quotico.auth")
ph = PasswordHasher()

ALGORITHM = "HS256"


def decode_jwt(token: str) -> dict:
    """Decode a JWT, trying the current secret first, then the old one.

    This allows zero-downtime rotation of JWT_SECRET:
    1. Set JWT_SECRET to the new value and JWT_SECRET_OLD to the previous one.
    2. After REFRESH_TOKEN_EXPIRE_DAYS (7 days), all old tokens have expired.
    3. Remove JWT_SECRET_OLD from .env.
    """
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        if settings.JWT_SECRET_OLD:
            return jwt.decode(token, settings.JWT_SECRET_OLD, algorithms=[ALGORITHM])
        raise


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return ph.verify(hashed, password)
    except VerifyMismatchError:
        return False


def create_access_token(user_id: str) -> str:
    expire = utcnow() + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": user_id,
        "exp": expire,
        "type": "access",
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


async def create_refresh_token(user_id: str, family: Optional[str] = None) -> str:
    """Create a refresh token with rotation support.

    Each refresh token belongs to a 'family'. If a token is reused
    (replay attack), the entire family is invalidated.
    """
    expire = utcnow() + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    jti = secrets.token_hex(16)
    token_family = family or secrets.token_hex(8)

    payload = {
        "sub": user_id,
        "exp": expire,
        "type": "refresh",
        "jti": jti,
        "family": token_family,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)

    await _db.db.refresh_tokens.insert_one({
        "jti": jti,
        "user_id": user_id,
        "family": token_family,
        "created_at": utcnow(),
        "expires_at": expire,
    })

    return token


async def rotate_refresh_token(old_jti: str, user_id: str, family: str) -> str:
    """Issue a new refresh token and invalidate the old one."""
    await _db.db.refresh_tokens.delete_one({"jti": old_jti})
    return await create_refresh_token(user_id, family=family)


async def invalidate_token_family(family: str) -> None:
    """Invalidate all tokens in a family (replay detection)."""
    result = await _db.db.refresh_tokens.delete_many({"family": family})
    logger.warning(
        "Token family invalidated (possible replay): %s (%d removed)",
        family, result.deleted_count,
    )


async def invalidate_user_tokens(user_id: str) -> None:
    """Invalidate all refresh tokens for a user (password change, logout-all)."""
    await _db.db.refresh_tokens.delete_many({"user_id": user_id})


async def blocklist_access_token(jti: str, expires_at: datetime) -> None:
    """Add an access token JTI to the blocklist until it expires."""
    await _db.db.access_blocklist.insert_one({
        "jti": jti,
        "expires_at": expires_at,
    })


async def is_refresh_token_valid(jti: str) -> bool:
    """Check if a refresh token JTI exists (not yet used/revoked)."""
    doc = await _db.db.refresh_tokens.find_one({"jti": jti}, {"_id": 1})
    return doc is not None


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth/refresh",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth/refresh")


async def get_current_user(request: Request, db=Depends(get_db)) -> dict:
    """FastAPI dependency: extract and validate user from access token cookie."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht angemeldet",
        )

    try:
        payload = decode_jwt(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiges Token",
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiger Token-Typ",
        )

    jti = payload.get("jti")
    if jti:
        blocked = await _db.db.access_blocklist.find_one({"jti": jti}, {"_id": 1})
        if blocked:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token widerrufen",
            )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiges Token",
        )

    from bson import ObjectId
    user = await db.users.find_one(
        {"_id": ObjectId(user_id), "is_deleted": False}
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nutzer nicht gefunden",
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dein Konto wurde gesperrt.",
        )

    return user


async def get_admin_user(request: Request, db=Depends(get_db)) -> dict:
    """FastAPI dependency: requires an authenticated admin user."""
    user = await get_current_user(request, db)
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nur für Administratoren.",
        )
    return user
