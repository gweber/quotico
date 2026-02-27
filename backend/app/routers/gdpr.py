import hashlib
import logging
import secrets
from datetime import datetime

from app.utils import ensure_utc, utcnow

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
            "timestamp": ensure_utc(entry["timestamp"]).isoformat(),
            "action": entry["action"],
            "ip_truncated": entry.get("ip_truncated", ""),
        }
        for entry in logs
    ]


@router.get("/export")
async def export_data(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """DSGVO Art. 20: Export all personal data as JSON.

    Returns all user data, bets, squad memberships, and battle participations.
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
            ensure_utc(user["wallet_disclaimer_accepted_at"]).isoformat()
            if user.get("wallet_disclaimer_accepted_at") else None
        ),
        "created_at": ensure_utc(user["created_at"]).isoformat(),
        "updated_at": ensure_utc(user["updated_at"]).isoformat(),
    }

    # All betting slips (unified: singles, parlays, matchday, survivor, etc.)
    slips = await db.betting_slips.find({"user_id": user_id}).to_list(length=10000)
    slips_export = [
        {
            "slip_id": str(s["_id"]),
            "type": s["type"],
            "selections": [
                {
                    "match_id": sel.get("match_id"),
                    "market": sel.get("market"),
                    "pick": sel.get("pick"),
                    "locked_odds": sel.get("locked_odds"),
                    "points_earned": sel.get("points_earned"),
                    "status": sel.get("status"),
                }
                for sel in s.get("selections", [])
            ],
            "total_odds": s.get("total_odds"),
            "stake": s.get("stake"),
            "potential_payout": s.get("potential_payout"),
            "funding": s.get("funding"),
            "status": s["status"],
            "submitted_at": ensure_utc(s["submitted_at"]).isoformat() if s.get("submitted_at") else None,
            "resolved_at": ensure_utc(s["resolved_at"]).isoformat() if s.get("resolved_at") else None,
            "created_at": ensure_utc(s["created_at"]).isoformat(),
        }
        for s in slips
    ]

    # Points transactions
    transactions = await db.points_transactions.find(
        {"user_id": user_id}
    ).to_list(length=10000)
    transactions_export = [
        {
            "bet_id": t.get("bet_id", t.get("tip_id")),
            "delta": t["delta"],
            "scoring_version": t["scoring_version"],
            "created_at": ensure_utc(t["created_at"]).isoformat(),
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
            "joined_at": ensure_utc(p["joined_at"]).isoformat(),
        }
        for p in participations
    ]

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
            "created_at": ensure_utc(w["created_at"]).isoformat(),
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
            "created_at": ensure_utc(t["created_at"]).isoformat(),
        }
        for t in wallet_txns
    ]

    # Device fingerprints (hash-only, no raw data)
    fingerprints = await db.device_fingerprints.find(
        {"user_id": user_id}
    ).to_list(length=50)
    fingerprints_export = [
        {
            "fingerprint_hash": fp["fingerprint_hash"],
            "ip_truncated": fp.get("ip_truncated", ""),
            "created_at": ensure_utc(fp["created_at"]).isoformat(),
            "last_seen_at": ensure_utc(fp["last_seen_at"]).isoformat(),
        }
        for fp in fingerprints
    ]

    await log_audit(actor_id=user_id, target_id=user_id, action="DATA_EXPORTED", request=request)

    return {
        "export_date": utcnow().isoformat(),
        "profile": profile,
        "betting_slips": slips_export,
        "points_transactions": transactions_export,
        "squads": squads_export,
        "battle_participations": battles_export,
        "wallets": wallets_export,
        "wallet_transactions": wallet_txns_export,
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
    # Google-only users have no password — they re-authenticate via Google
    if not user.get("hashed_password"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google-linked accounts must use the Google re-authentication flow to delete their account.",
        )

    # Verify password
    if not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password.",
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
        "betting_slips", "wallets", "wallet_transactions",
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
        "message": "Your account has been anonymized. Your bets remain for platform integrity but are no longer linked to your identity.",
    }
