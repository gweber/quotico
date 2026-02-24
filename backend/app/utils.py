from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Make a naive datetime timezone-aware (UTC). Already-aware datetimes pass through.

    MongoDB stores datetimes without tzinfo (naive). When you read a date field
    from a Mongo document and need to do arithmetic or comparison with utcnow()
    (which is tz-aware), wrap it with ensure_utc() first — otherwise Python
    raises "can't subtract offset-naive and offset-aware datetimes".
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def as_utc(dt: datetime | None) -> datetime | None:
    """None-safe UTC conversion for JSON serialization.

    Use at API response boundaries to ensure naive datetimes from MongoDB
    serialize with '+00:00' suffix. Without this, Pydantic serializes naive
    datetimes as "2026-02-23T17:30:00" (no offset), and the browser's
    Date() interprets it as local time — off by the user's UTC offset.

    Returns None as-is for optional datetime fields.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_utc(value: str | datetime) -> datetime:
    """Parse a date string or datetime into a tz-aware UTC datetime.

    Handles ISO 8601 strings (with or without Z/offset) and bare datetimes.
    Always returns a timezone-aware datetime in UTC.
    """
    if isinstance(value, datetime):
        return ensure_utc(value)
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
