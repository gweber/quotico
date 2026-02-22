from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Make a naive datetime timezone-aware (UTC). Already-aware datetimes pass through."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
