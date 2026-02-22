from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AuditLog(BaseModel):
    """Immutable audit log entry for compliance and regulatory purposes.

    Insert-only. No updates or deletes permitted on this collection.
    """

    timestamp: datetime
    actor_id: str  # Who did it? (User-ID or "SYSTEM")
    target_id: str  # Who was affected? (User-ID, Match-ID, etc.)
    action: str  # e.g. "LOGIN_SUCCESS", "ADMIN_BAN_USER"
    metadata: dict = Field(default_factory=dict)  # Previous/new values
    ip_truncated: str = ""  # e.g. "192.168.1.xxx" (GDPR-compliant)
