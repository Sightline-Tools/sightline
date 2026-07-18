from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .actor_constants import CHARMED_ACTOR_PREFIX, NPC_ARTICLES, YOUR_PET_PREFIX

if TYPE_CHECKING:
    from .db import CharmWindow, SessionLogLine


CHARM_STATE_CATEGORIES = frozenset({"charm_start", "charm_end"})
CHARM_SIGNAL_ABILITIES = frozenset({"charm", "siren's call", "enthrall"})
RECENT_CHARM_SIGNAL_LOOKBACK_SECONDS = 15


def canonical_actor_key(actor_name: str | None) -> str:
    """Return a case-insensitive key shared by tagged and untagged Charm names."""
    normalized = re.sub(r"\s+", " ", (actor_name or "").strip()).casefold()
    prefix = CHARMED_ACTOR_PREFIX.casefold()
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix) :].strip()
    return normalized


def is_tagged_charmed_pet(actor_name: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", (actor_name or "").strip()).casefold()
    prefix = CHARMED_ACTOR_PREFIX.casefold()
    return normalized.startswith(prefix) and bool(normalized[len(prefix) :].strip())


def tagged_charmed_actor_name(actor_name: str) -> str:
    stripped = re.sub(r"\s+", " ", actor_name.strip())
    if is_tagged_charmed_pet(stripped):
        return stripped
    return f"{CHARMED_ACTOR_PREFIX}{stripped}"


def is_probable_npc_actor(actor_name: str | None) -> bool:
    """Identify NPC-like actor labels without treating every multiword name as an NPC."""
    normalized = re.sub(r"\s+", " ", (actor_name or "").strip()).casefold()
    if not normalized or normalized == "you" or normalized.startswith(YOUR_PET_PREFIX):
        return False
    if is_tagged_charmed_pet(normalized):
        return True
    return normalized.split(maxsplit=1)[0] in NPC_ARTICLES


def is_charm_signal_line(line: Any) -> bool:
    return (
        getattr(line, "parse_outcome", None) == "accepted"
        and getattr(line, "parse_category", None) == "cast_start"
        and (getattr(line, "ability_name", None) or "").strip().casefold() in CHARM_SIGNAL_ABILITIES
    )


def is_recent_charm_signal(
    db: Session,
    *,
    parser_session_id: int,
    at: datetime,
    session_log_line_id: int | None = None,
    lookback_seconds: float = RECENT_CHARM_SIGNAL_LOOKBACK_SECONDS,
) -> bool:
    """Return whether this parser session has a current/prior Charm cast signal."""
    from .db import SessionLogLine

    cutoff = at - timedelta(seconds=max(float(lookback_seconds), 0.0))
    preceding = SessionLogLine.received_at <= at
    if session_log_line_id is not None:
        preceding = or_(
            SessionLogLine.received_at < at,
            and_(
                SessionLogLine.received_at == at,
                SessionLogLine.id <= session_log_line_id,
            ),
        )

    stmt = (
        select(SessionLogLine.id)
        .where(
            SessionLogLine.parser_session_id == parser_session_id,
            SessionLogLine.parse_outcome == "accepted",
            SessionLogLine.parse_category == "cast_start",
            func.lower(func.trim(SessionLogLine.ability_name)).in_(CHARM_SIGNAL_ABILITIES),
            SessionLogLine.received_at >= cutoff,
            preceding,
        )
        .limit(1)
    )
    return db.scalar(stmt) is not None


def window_is_active_at(
    window: CharmWindow,
    *,
    at: datetime,
    session_log_line_id: int | None = None,
) -> bool:
    """Check a half-open Charm window, using line order for equal timestamps."""
    if window.started_at > at:
        return False
    if (
        window.started_at == at
        and session_log_line_id is not None
        and window.start_line_id > session_log_line_id
    ):
        return False

    if window.ended_at is None:
        return True
    if window.ended_at > at:
        return True
    if window.ended_at < at:
        return False
    if session_log_line_id is None or window.end_line_id is None:
        return False
    return window.end_line_id > session_log_line_id


def window_is_active_for_event(window: CharmWindow, event: Any) -> bool:
    line_id = getattr(event, "session_log_line_id", None)
    if line_id is None and hasattr(event, "parse_category"):
        line_id = getattr(event, "id", None)
    return window_is_active_at(
        window,
        at=event.received_at,
        session_log_line_id=line_id,
    )


def active_charm_windows(
    db: Session,
    *,
    parser_session_id: int,
    actor_name: str,
    at: datetime,
    session_log_line_id: int | None = None,
) -> list[CharmWindow]:
    """Load active windows for one actor in one parser session."""
    from .db import CharmWindow

    started = CharmWindow.started_at <= at
    not_ended = or_(CharmWindow.ended_at.is_(None), CharmWindow.ended_at > at)
    if session_log_line_id is not None:
        started = or_(
            CharmWindow.started_at < at,
            and_(
                CharmWindow.started_at == at,
                CharmWindow.start_line_id <= session_log_line_id,
            ),
        )
        not_ended = or_(
            CharmWindow.ended_at.is_(None),
            CharmWindow.ended_at > at,
            and_(
                CharmWindow.ended_at == at,
                CharmWindow.end_line_id > session_log_line_id,
            ),
        )

    stmt = (
        select(CharmWindow)
        .where(
            CharmWindow.parser_session_id == parser_session_id,
            CharmWindow.actor_key == canonical_actor_key(actor_name),
            started,
            not_ended,
        )
        .order_by(CharmWindow.started_at.asc(), CharmWindow.start_line_id.asc())
    )
    return list(db.scalars(stmt).all())


def observe_charm_event(db: Session, line: SessionLogLine) -> CharmWindow | None:
    """Apply one accepted charm_start/end line to persistent parser-session state."""
    from .db import CharmWindow

    if (
        getattr(line, "parse_outcome", None) != "accepted"
        or getattr(line, "parse_category", None) not in CHARM_STATE_CATEGORIES
        or not getattr(line, "target_actor", None)
    ):
        return None

    if line.id is None:
        db.flush()

    if line.parse_category == "charm_start":
        existing = db.scalar(select(CharmWindow).where(CharmWindow.start_line_id == line.id))
        if existing is not None:
            return existing
        window = CharmWindow(
            parser_session_id=line.parser_session_id,
            actor_name=line.target_actor.strip(),
            actor_key=canonical_actor_key(line.target_actor),
            started_at=line.received_at,
            start_line_id=line.id,
        )
        db.add(window)
        db.flush()
        return window

    existing = db.scalar(select(CharmWindow).where(CharmWindow.end_line_id == line.id))
    if existing is not None:
        return existing

    windows = active_charm_windows(
        db,
        parser_session_id=line.parser_session_id,
        actor_name=line.target_actor,
        at=line.received_at,
        session_log_line_id=line.id,
    )
    if not windows:
        return None

    window = windows[0]
    window.ended_at = line.received_at
    window.end_line_id = line.id
    db.flush()
    return window
