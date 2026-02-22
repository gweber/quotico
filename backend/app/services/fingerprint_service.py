"""Device fingerprint service — DSGVO-compliant, hash-only storage."""

import logging
from typing import Optional

from bson import ObjectId

import app.database as _db
from app.services.audit_service import _truncate_ip
from app.utils import utcnow

logger = logging.getLogger("quotico.fingerprint_service")

# Verification escalation thresholds
SOFT_LIMIT = 4   # accounts per IP before extra verification
HARD_LIMIT = 6   # accounts per IP triggering captcha on registration


async def record_fingerprint(
    user_id: str, fingerprint_hash: str, ip_address: str,
) -> None:
    """Record a device fingerprint (hash-only, DSGVO-compliant).

    Raw components (user-agent, screen resolution etc.) are hashed client-side
    and never transmitted or stored.  The IP address is truncated (last octet
    replaced with "xxx") before persistence — full IPs are never written to
    the database.
    """
    if not fingerprint_hash or not ip_address:
        return

    truncated_ip = _truncate_ip(ip_address)
    now = utcnow()
    await _db.db.device_fingerprints.update_one(
        {"user_id": user_id, "fingerprint_hash": fingerprint_hash},
        {
            "$set": {"ip_truncated": truncated_ip, "last_seen_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    # Check for household clustering (uses truncated IP — same /24 = same household)
    await _update_household_group(user_id, fingerprint_hash, truncated_ip)


async def check_registration_limit(ip_address: str) -> dict:
    """Check if IP has too many accounts — returns verification level needed.

    Uses the truncated IP for matching (same /24 subnet).

    Returns:
        {"level": "none"} — no extra verification
        {"level": "email_code"} — require email verification code
        {"level": "captcha"} — require captcha + email code
    """
    if not ip_address:
        return {"level": "none"}

    truncated_ip = _truncate_ip(ip_address)

    # Count distinct users from this IP subnet
    distinct_users = await _db.db.device_fingerprints.distinct(
        "user_id",
        {"ip_truncated": truncated_ip},
    )
    count = len(distinct_users)

    if count >= HARD_LIMIT:
        return {"level": "captcha"}
    elif count >= SOFT_LIMIT:
        return {"level": "email_code"}
    return {"level": "none"}


async def check_bet_verification_needed(user_id: str, ip_address: str) -> bool:
    """Check if extra verification is needed for placing bets.

    Returns True if user is in a cluster with 5+ accounts sharing same IP subnet.
    """
    if not ip_address:
        return False

    truncated_ip = _truncate_ip(ip_address)
    distinct_users = await _db.db.device_fingerprints.distinct(
        "user_id",
        {"ip_truncated": truncated_ip},
    )
    return len(distinct_users) >= SOFT_LIMIT


async def get_household_clusters() -> list[dict]:
    """Admin: get all household clusters grouped by truncated IP + fingerprint overlap."""
    pipeline = [
        {"$group": {
            "_id": "$ip_truncated",
            "users": {"$addToSet": "$user_id"},
            "fingerprints": {"$addToSet": "$fingerprint_hash"},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 100},
    ]
    clusters = await _db.db.device_fingerprints.aggregate(pipeline).to_list(length=100)

    # Enrich with user aliases
    result = []
    for cluster in clusters:
        user_ids = cluster["users"]
        users = await _db.db.users.find(
            {"_id": {"$in": [ObjectId(uid) for uid in user_ids]}},
            {"alias": 1},
        ).to_list(length=50)
        alias_map = {str(u["_id"]): u["alias"] for u in users}

        result.append({
            "ip_truncated": cluster["_id"],
            "users": [
                {"user_id": uid, "alias": alias_map.get(uid, "Unknown")}
                for uid in user_ids
            ],
            "fingerprint_count": len(cluster["fingerprints"]),
            "account_count": len(user_ids),
        })

    return result


async def detect_suspicious_activity() -> list[dict]:
    """Admin: detect suspicious patterns (identical bets, simultaneous registrations)."""
    now = utcnow()
    alerts = []

    # Find users with same fingerprint on multiple accounts
    pipeline = [
        {"$group": {
            "_id": "$fingerprint_hash",
            "users": {"$addToSet": "$user_id"},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ]
    same_device = await _db.db.device_fingerprints.aggregate(pipeline).to_list(length=50)
    for entry in same_device:
        if len(entry["users"]) > 1:
            alerts.append({
                "type": "same_device_multiple_accounts",
                "fingerprint_hash": entry["_id"][:12] + "...",
                "user_ids": entry["users"],
                "severity": "high" if len(entry["users"]) > 2 else "medium",
            })

    return alerts


async def _update_household_group(
    user_id: str, fingerprint_hash: str, ip_truncated: str,
) -> None:
    """Automatically group users into households based on truncated IP + fingerprint overlap."""
    # Find other users with same IP subnet
    same_ip_users = await _db.db.device_fingerprints.distinct(
        "user_id",
        {"ip_truncated": ip_truncated, "user_id": {"$ne": user_id}},
    )
    if not same_ip_users:
        return

    # Check if any of them share the same fingerprint (same device = suspicious)
    same_fingerprint = await _db.db.device_fingerprints.distinct(
        "user_id",
        {"fingerprint_hash": fingerprint_hash, "user_id": {"$ne": user_id}},
    )

    if same_fingerprint:
        # Same device = definite household
        group_members = set(same_fingerprint + [user_id])
    elif len(same_ip_users) >= 2:
        # Same IP subnet, different devices = likely household
        group_members = set(same_ip_users + [user_id])
    else:
        return

    # Find existing household group or create new one
    existing = await _db.db.users.find_one(
        {"_id": {"$in": [ObjectId(uid) for uid in group_members]}, "household_group_id": {"$ne": None}},
        {"household_group_id": 1},
    )

    group_id = existing["household_group_id"] if existing else str(ObjectId())

    # Update all members
    await _db.db.users.update_many(
        {"_id": {"$in": [ObjectId(uid) for uid in group_members]}},
        {"$set": {"household_group_id": group_id}},
    )

    logger.info(
        "Household group %s updated: %d members (IP subnet=%s)",
        group_id, len(group_members), ip_truncated,
    )
