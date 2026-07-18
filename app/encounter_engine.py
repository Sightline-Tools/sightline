from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .charm import (
    CHARM_STATE_CATEGORIES,
    active_charm_windows,
    is_charm_signal_line,
    is_probable_npc_actor,
    is_recent_charm_signal,
    is_tagged_charmed_pet,
    observe_charm_event,
)
from .db import Encounter, Event, SessionLogLine
from .time_utils import utcnow

COMBAT_ACTIVITY_CATEGORIES = {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}
COMBAT_START_CATEGORIES = COMBAT_ACTIVITY_CATEGORIES
DAMAGE_CATEGORIES = {"damage_out", "damage_in", "damage"}


@dataclass(frozen=True)
class EncounterEngineConfig:
    inactivity_timeout_s: int = 5
    filter_mode: str = "none"
    party_members: list[dict[str, str | None] | str] | None = None


@dataclass(frozen=True)
class _FilterDecision:
    keep: bool
    may_start_encounter: bool = True
    counts_as_activity: bool = True


class EncounterEngine:
    """Build encounters exclusively from committed SessionLogLine entries."""

    def reconcile_timeouts(
        self,
        db: Session,
        *,
        now: datetime,
        timeout_s: int,
    ) -> None:
        timeout_delta = timedelta(seconds=max(timeout_s, 1))
        changed = False
        for active in self._active_encounters(db):
            if active.last_activity_at is None:
                continue
            if (now - active.last_activity_at) >= timeout_delta:
                self._end_encounter(active, ended_at=active.last_activity_at + timeout_delta, reason="timeout")
                changed = True
        if changed:
            db.flush()

    def process_committed_line(self, db: Session, line: SessionLogLine, *, config: EncounterEngineConfig) -> None:
        if line.parse_outcome != "accepted" or not line.parse_category:
            return

        now = line.received_at
        observe_charm_event(db, line)
        self.reconcile_timeouts(
            db,
            now=now,
            timeout_s=config.inactivity_timeout_s,
        )
        active = self._active_encounter(db, parser_session_id=line.parser_session_id)
        decision = self._filter_decision(
            db,
            line,
            config,
            has_active_encounter=active is not None,
        )
        if not decision.keep:
            return

        category = line.parse_category
        is_activity = category in COMBAT_ACTIVITY_CATEGORIES and decision.counts_as_activity
        starts_encounter = category in COMBAT_START_CATEGORIES and decision.may_start_encounter

        if active is None and starts_encounter:
            active = self._start_encounter(db, line, config=config)

        if active is None:
            return

        if is_activity:
            active.last_activity_at = now

        self._attach_event(db, active, line)

        if category == "death_self":
            self._end_encounter(active, ended_at=now, reason="death")
            db.flush()

    def end_active_encounter(
        self,
        db: Session,
        *,
        ended_at: datetime | None = None,
        reason: str = "manual",
        parser_session_id: int | None = None,
    ) -> bool:
        active = self._active_encounter(db, parser_session_id=parser_session_id)
        if active is None:
            return False
        self._end_encounter(active, ended_at=ended_at or utcnow(), reason=reason)
        db.flush()
        return True

    @staticmethod
    def _active_encounter(db: Session, *, parser_session_id: int | None = None) -> Encounter | None:
        stmt = select(Encounter).where(Encounter.active.is_(True))
        if parser_session_id is not None:
            stmt = stmt.where(
                select(Event.id)
                .where(Event.encounter_id == Encounter.id, Event.parser_session_id == parser_session_id)
                .exists()
            )
        return db.scalars(stmt.order_by(Encounter.id.desc())).first()

    @staticmethod
    def _active_encounters(db: Session) -> list[Encounter]:
        stmt = select(Encounter).where(Encounter.active.is_(True)).order_by(Encounter.id.asc())
        return list(db.scalars(stmt).all())

    @staticmethod
    def _filter_decision(
        db: Session,
        line: SessionLogLine,
        config: EncounterEngineConfig,
        *,
        has_active_encounter: bool,
    ) -> _FilterDecision:
        mode = (config.filter_mode or "none").lower()
        if mode == "none":
            return _FilterDecision(True)

        if line.parse_category in CHARM_STATE_CATEGORIES:
            return _FilterDecision(True, may_start_encounter=False, counts_as_activity=False)
        if is_charm_signal_line(line):
            return _FilterDecision(True, may_start_encounter=False, counts_as_activity=False)

        participants = {
            (line.source_actor or "").strip().lower(),
            (line.target_actor or "").strip().lower(),
        }
        participants.discard("")

        if mode == "solo" and (
            line.parse_category == "death_self"
            or "you" in participants
            or any(_is_owned_pet_actor_name(participant) for participant in participants)
        ):
            return _FilterDecision(True)

        if mode == "party":
            whitelist = _party_whitelist(config)
            if line.parse_category == "death_self" or bool(participants.intersection(whitelist)):
                return _FilterDecision(True)

        has_active_charm = any(
            active_charm_windows(
                db,
                parser_session_id=line.parser_session_id,
                actor_name=participant,
                at=line.received_at,
                session_log_line_id=line.id,
            )
            for participant in participants
        )
        recent_charm_signal = is_recent_charm_signal(
            db,
            parser_session_id=line.parser_session_id,
            at=line.received_at,
            session_log_line_id=line.id,
            lookback_seconds=15,
        )
        is_npc_damage = (
            line.parse_category in DAMAGE_CATEGORIES
            and is_probable_npc_actor(line.source_actor)
            and is_probable_npc_actor(line.target_actor)
        )

        if mode == "solo":
            keep = has_active_charm or (recent_charm_signal and is_npc_damage)
            return _FilterDecision(keep)

        if mode == "party":
            if has_active_charm or (recent_charm_signal and is_npc_damage):
                return _FilterDecision(True)
            if has_active_encounter and is_npc_damage:
                # Retain weak candidates for review, but do not let them create or prolong combat.
                return _FilterDecision(True, may_start_encounter=False, counts_as_activity=False)
            return _FilterDecision(False)

        return _FilterDecision(True)

    @staticmethod
    def _label_for_line(line: SessionLogLine) -> str:
        target = (line.target_actor or "").strip()
        if target and target.lower() != "you":
            return target
        return line.received_at.strftime('%Y-%m-%d %H:%M:%S')

    def _start_encounter(
        self,
        db: Session,
        line: SessionLogLine,
        *,
        config: EncounterEngineConfig,
    ) -> Encounter:
        encounter = Encounter(
            started_at=line.received_at,
            last_activity_at=line.received_at,
            active=True,
            label=self._label_for_line(line),
            capture_filter_mode=(config.filter_mode or "none").lower(),
        )
        db.add(encounter)
        db.flush()
        return encounter

    @staticmethod
    def _attach_event(db: Session, encounter: Encounter, line: SessionLogLine) -> None:
        existing = db.scalars(select(Event).where(Event.session_log_line_id == line.id)).first()
        if existing:
            return
        db.add(
            Event(
                encounter_id=encounter.id,
                parser_session_id=line.parser_session_id,
                session_log_line_id=line.id,
                received_at=line.received_at,
                raw_ocr_line=line.raw_text,
                normalized_text=line.normalized_text,
                category=line.parse_category,
                source_actor=line.source_actor,
                target_actor=line.target_actor,
                ability_name=line.ability_name,
                amount=line.amount,
                absorbed_amount=line.absorbed_amount,
                blocked_amount=line.blocked_amount,
                result=line.result,
                attack_verb=line.attack_verb,
                effect_name=line.effect_name,
                ocr_confidence=line.ocr_confidence,
                parse_confidence=line.parse_confidence,
            )
        )

    @staticmethod
    def _end_encounter(encounter: Encounter, *, ended_at: datetime, reason: str) -> None:
        encounter.active = False
        encounter.ended_at = ended_at
        encounter.ended_reason = reason


encounter_engine = EncounterEngine()


def _is_owned_pet_actor_name(actor_name: str | None) -> bool:
    normalized = (actor_name or "").strip().lower()
    return normalized.startswith("your pet ") or is_tagged_charmed_pet(actor_name)


def _party_whitelist(config: EncounterEngineConfig) -> set[str]:
    whitelist = {"you"}
    for member in config.party_members or []:
        if isinstance(member, dict):
            name = str(member.get("name") or "").strip().lower()
            pet_name = str(member.get("pet_name") or "").strip().lower()
            if name:
                whitelist.add(name)
            if pet_name:
                whitelist.add(pet_name)
            continue
        normalized = str(member or "").strip().lower()
        if normalized:
            whitelist.add(normalized)
    return whitelist
