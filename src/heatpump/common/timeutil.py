"""The one place timestamps come from. Everything stored in the database is UTC."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
