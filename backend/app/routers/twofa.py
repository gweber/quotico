import io
import base64
import logging

import pyotp
import qrcode
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database import get_db
from app.services.auth_service import get_current_user
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
    if user.get("is_2fa_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA ist bereits aktiviert.",
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
    encrypted_secret = encrypt(secret)
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
        "message": "QR-Code scannen und Code eingeben, um 2FA zu aktivieren.",
    }


@router.post("/verify")
async def verify_2fa(body: TwoFAVerify, user=Depends(get_current_user), db=Depends(get_db)):
    """Verify a TOTP code and activate 2FA."""
    encrypted_secret = user.get("encrypted_2fa_secret")
    if not encrypted_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bitte zuerst /setup aufrufen.",
        )

    key_version = user.get("encryption_key_version", 1)

    # Lazy re-encryption if key was rotated
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
    totp = pyotp.TOTP(secret)

    # Verify with +/- 1 window tolerance (handles clock skew)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ungültiger Code. Bitte erneut versuchen.",
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"is_2fa_enabled": True}},
    )

    logger.info("2FA activated for user %s", str(user["_id"]))
    return {"message": "2FA erfolgreich aktiviert."}


@router.post("/disable")
async def disable_2fa(body: TwoFAVerify, user=Depends(get_current_user), db=Depends(get_db)):
    """Disable 2FA. Requires a valid TOTP code for security."""
    if not user.get("is_2fa_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA ist nicht aktiviert.",
        )

    encrypted_secret = user.get("encrypted_2fa_secret")
    key_version = user.get("encryption_key_version", 1)
    secret = decrypt(encrypted_secret, key_version=key_version)
    totp = pyotp.TOTP(secret)

    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ungültiger Code.",
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

    logger.info("2FA disabled for user %s", str(user["_id"]))
    return {"message": "2FA deaktiviert."}
