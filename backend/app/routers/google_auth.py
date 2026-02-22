import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.config import settings
from app.database import get_db
from app.services.alias_service import generate_default_alias
from app.services.auth_service import create_access_token, create_refresh_token, set_auth_cookies

logger = logging.getLogger("quotico.google_auth")
router = APIRouter(prefix="/api/auth/google", tags=["google-auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _redirect_uri(request: Request) -> str:
    """Build the OAuth callback URL from the incoming request."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.hostname)
    return f"{scheme}://{host}/api/auth/google/callback"


@router.get("")
async def google_login(request: Request):
    """Redirect to Google OAuth consent screen."""
    if not settings.GOOGLE_CLIENT_ID:
        return RedirectResponse("/login?error=google_not_configured")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Google OAuth callback."""
    if error:
        logger.warning("Google OAuth error: %s", error)
        return RedirectResponse("/login?error=google_denied")

    # Verify state
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        logger.warning("Google OAuth state mismatch")
        return RedirectResponse("/login?error=invalid_state")

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(request),
        })

    if token_resp.status_code != 200:
        logger.error("Google token exchange failed: %s", token_resp.text)
        return RedirectResponse("/login?error=google_failed")

    tokens = token_resp.json()
    access_token = tokens.get("access_token")

    # Fetch user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        logger.error("Google userinfo failed: %s", userinfo_resp.text)
        return RedirectResponse("/login?error=google_failed")

    userinfo = userinfo_resp.json()
    google_email = userinfo.get("email", "").lower().strip()
    google_sub = userinfo.get("sub", "")

    if not google_email:
        return RedirectResponse("/login?error=google_no_email")

    # Find or create user
    db = await get_db()
    user = await db.users.find_one({"email": google_email, "is_deleted": False})

    now = datetime.now(timezone.utc)

    if user:
        # Link Google sub if not already linked
        if not user.get("google_sub"):
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"google_sub": google_sub, "updated_at": now}},
            )
        if user.get("is_banned"):
            return RedirectResponse("/login?error=banned")
    else:
        # Create new user (no password — Google-only)
        alias, alias_slug = await generate_default_alias(db)
        user_doc = {
            "email": google_email,
            "hashed_password": "",
            "google_sub": google_sub,
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
        user = {**user_doc, "_id": result.inserted_id}
        logger.info("Google user registered: %s", str(user["_id"]))

    # Issue JWT cookies
    user_id = str(user["_id"])
    access = create_access_token(user_id)
    refresh = await create_refresh_token(user_id)

    response = RedirectResponse("/", status_code=302)
    set_auth_cookies(response, access, refresh)

    logger.info("Google login: %s", user_id)
    return response
