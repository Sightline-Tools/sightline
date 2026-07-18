from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return naive UTC for compatibility with the existing SQLite schema."""
    return datetime.now(UTC).replace(tzinfo=None)
