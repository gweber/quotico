"""Immutable audit logging service for compliance and regulatory purposes.

All audit entries are insert-only. This module intentionally exposes NO
update or delete operations on the audit_logs collection.
"""

import logging
from typing import Optional

from fastapi import Request

import app.database as _db
from app.utils import utcnow

logger = logging.getLogger("quotico.audit")


def _truncate_ip(ip: str) -> str:
    """Anonymize an IP address by replacing the last segment (GDPR-compliant).

    IPv4: 192.168.1.42  -> 192.168.1.xxx
    IPv6: 2001:db8::1   -> 2001:db8::xxx
    """
    if not ip:
        return ""

    # IPv4
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            parts[-1] = "xxx"
            return ".".join(parts)
        return ip

    # IPv6
    if ":" in ip:
        parts = ip.rsplit(":", 1)
        if len(parts) == 2:
            return f"{parts[0]}:xxx"
        return ip

    return ip


def _get_client_ip(request: Optional[Request]) -> str:
    """Extract client IP from request, preferring X-Forwarded-For (behind nginx)."""
    if request is None:
        return ""

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP (original client)
        return forwarded.split(",")[0].strip()

    if request.client:
        return request.client.host

    return ""


async def log_audit(
    *,
    actor_id: str,
    target_id: str,
    action: str,
    metadata: Optional[dict] = None,
    request: Optional[Request] = None,
) -> None:
    """Write an immutable audit record to the audit_logs collection.

    Args:
        actor_id: Who performed the action (User-ID or "SYSTEM").
        target_id: Who/what was affected (User-ID, Match-ID, etc.).
        action: Action identifier, e.g. "LOGIN_SUCCESS", "ADMIN_BAN_USER".
        metadata: Optional dict with before/after values or extra context.
        request: Optional FastAPI request for IP extraction.
    """
    raw_ip = _get_client_ip(request)
    truncated_ip = _truncate_ip(raw_ip)

    doc = {
        "timestamp": utcnow(),
        "actor_id": actor_id,
        "target_id": target_id,
        "action": action,
        "metadata": metadata or {},
        "ip_truncated": truncated_ip,
    }

    try:
        await _db.db.audit_logs.insert_one(doc)
    except Exception:
        # Audit logging must never crash the request
        logger.exception("Failed to write audit log: action=%s actor=%s", action, actor_id)
