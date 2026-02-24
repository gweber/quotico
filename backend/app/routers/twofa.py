import io
import base64
import logging

import pyotp
import qrcode
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.audit_service import log_audit
from app.services.encryption import (
    encrypt,
    decrypt,
    needs_reencryption,
    reencrypt,
    CURRENT_KEY_VERSION,
)

logger = logging.getLogger("quotico.2fa")
router = APIRouter(prefix="/api/2fa", tags=["2fa"])


class TwoFAVerify(BaseModel):
    code: str


@router.post("/setup")
async def setup_2fa(user=Depends(get_current_user), db=Depends(get_db)):
    """Generate a new 2FA secret and return the QR code as base64 PNG.

    The secret is encrypted with Fernet before storing in the DB.
    2FA is NOT active until verified with /verify.
    """
    if not user.get("hashed_password"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not available for Google sign-in accounts.",
        )

    if user.get("is_2fa_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is already enabled.",
        )

    # Generate TOTP secret
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)

    # Generate QR code
    provisioning_uri = totp.provisioning_uri(
        name=user["email"],
        issuer_name="Quotico.de",
    )
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()

    # Encrypt and store (not yet active)
    try:
        encrypted_secret = encrypt(secret)
    except Exception:
        logger.exception("2FA encrypt failed for user %s", str(user["_id"]))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="2FA setup failed. Please try again.",
        )
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "encrypted_2fa_secret": encrypted_secret,
                "encryption_key_version": CURRENT_KEY_VERSION,
            }
        },
    )

    logger.info("2FA setup initiated for user %s", str(user["_id"]))
    return {
        "qr_code": f"data:image/png;base64,{qr_base64}",
        "message": "Scan the QR code and enter the code to activate 2FA.",
    }


@router.post("/verify")
async def verify_2fa(body: TwoFAVerify, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Verify a TOTP code and activate 2FA."""
    encrypted_secret = user.get("encrypted_2fa_secret")
    if not encrypted_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please call /setup first.",
        )

    key_version = user.get("encryption_key_version", 1)

    # Lazy re-encryption if key was rotated
    try:
        if needs_reencryption(key_version):
            new_encrypted, new_version = reencrypt(encrypted_secret, key_version)
            await db.users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "encrypted_2fa_secret": new_encrypted,
                        "encryption_key_version": new_version,
                    }
                },
            )
            encrypted_secret = new_encrypted
            key_version = new_version

        secret = decrypt(encrypted_secret, key_version=key_version)
    except Exception:
        logger.exception("2FA decrypt failed for user %s", str(user["_id"]))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="2FA configuration error. Please disable and re-enable 2FA.",
        )
    totp = pyotp.TOTP(secret)

    # Verify with +/- 1 window tolerance (handles clock skew)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid code. Please try again.",
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"is_2fa_enabled": True}},
    )

    user_id = str(user["_id"])
    await log_audit(actor_id=user_id, target_id=user_id, action="2FA_ENABLED", request=request)
    logger.info("2FA activated for user %s", user_id)
    return {"message": "2FA successfully activated."}


@router.post("/disable")
async def disable_2fa(body: TwoFAVerify, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Disable 2FA. Requires a valid TOTP code for security."""
    if not user.get("is_2fa_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled.",
        )

    encrypted_secret = user.get("encrypted_2fa_secret")
    key_version = user.get("encryption_key_version", 1)
    try:
        secret = decrypt(encrypted_secret, key_version=key_version)
    except Exception:
        logger.exception("2FA decrypt failed for user %s", str(user["_id"]))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="2FA configuration error. Please contact support.",
        )
    totp = pyotp.TOTP(secret)

    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid code.",
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "is_2fa_enabled": False,
                "encrypted_2fa_secret": None,
            }
        },
    )

    user_id = str(user["_id"])
    await log_audit(actor_id=user_id, target_id=user_id, action="2FA_DISABLED", request=request)
    logger.info("2FA disabled for user %s", user_id)
    return {"message": "2FA disabled."}
