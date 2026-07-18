from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .capture import CaptureRegion, capture_region
from .config import (
    RuntimeSettings,
    normalize_executable_name,
    normalize_executable_names,
    normalize_overlay_font_family,
)
from .combat_events import combat_event_bus
from .db import ParserSession, SessionLocal, SessionLogLine, SettingsModel
from .encounter_engine import EncounterEngineConfig, encounter_engine
from .floating_combat_text import (
    OverlayPositionConfig,
    build_structured_combat_event,
    floating_text_controller,
    map_event_to_floating_kind,
)
from .ocr import OCRResult, run_ocr
from .parser import normalize_text, parse_line
from .time_utils import utcnow
from .window_focus import get_foreground_process_name

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image


@dataclass(frozen=True)
class _FrameLine:
    normalized_text: str
    confidence: float = 1.0
    center_y: float | None = None


@dataclass(frozen=True)
class _OverlayCandidate:
    event: StructuredCombatEvent
    kind: str


class ParserService:
    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame_memory_norm_lines: list[list[_FrameLine]] = []
        self._ignore_next_frame_for_new_lines = False
        self._known_actors: set[str] = {"You"}
        self._active_parser_session_id: int | None = None
        self._overlay_requested_by_parser = False
        self._overlay_requested_manually = False
        self.last_successful_ocr_at: datetime | None = None
        self.last_error: str | None = None
        self.last_skip_reason: str | None = None

    def load_settings(self) -> RuntimeSettings:
        with SessionLocal() as db:
            try:
                model = db.get(SettingsModel, 1)
            except OperationalError:
                return RuntimeSettings()
            if model is None:
                defaults = RuntimeSettings()
                model = SettingsModel(
                    id=1,
                    allowed_focus_executables=defaults.allowed_focus_executables,
                    allowed_focus_executables_configured=True,
                    visible_columns=defaults.visible_columns,
                    ocr_passes_enabled=defaults.ocr_passes_enabled,
                )
                db.add(model)
                db.commit()
                db.refresh(model)
            default_settings = RuntimeSettings()
            try:
                stored_allowlist = normalize_executable_names(
                    model.allowed_focus_executables if isinstance(model.allowed_focus_executables, list) else []
                )
            except ValueError:
                # A manually edited or otherwise malformed persisted list is
                # treated like an unset legacy value. This avoids granting
                # capture access based on corrupted settings.
                stored_allowlist = []
            # Preserve explicit and legacy allowlists exactly. When foreground
            # restriction is enabled, an empty value remains safely paused
            # instead of assuming an application.
            allowed_focus_executables = stored_allowlist
            return RuntimeSettings(
                capture_x=model.capture_x,
                capture_y=model.capture_y,
                capture_width=model.capture_width,
                capture_height=model.capture_height,
                combat_region_configured=model.combat_region_configured,
                onboarding_completed=model.onboarding_completed,
                capture_interval_ms=model.capture_interval_ms,
                inactivity_timeout_s=model.inactivity_timeout_s,
                encounter_inactivity_timeout_s=model.encounter_inactivity_timeout_s,
                debug_mode=model.debug_mode,
                require_game_focus=model.require_game_focus,
                increase_accuracy=model.increase_accuracy,
                allowed_focus_executables=allowed_focus_executables,
                manual_end_hotkey_enabled=model.manual_end_hotkey_enabled,
                manual_end_hotkey_binding=model.manual_end_hotkey_binding,
                encounter_filter_mode=(model.encounter_filter_mode or "none").lower(),
                encounter_party_members=model.encounter_party_members or [],
                floating_combat_text_enabled=model.floating_combat_text_enabled,
                overlay_monitor_index=model.overlay_monitor_index,
                overlay_anchor_x_ratio=model.overlay_anchor_x_ratio,
                overlay_anchor_y_ratio=model.overlay_anchor_y_ratio,
                overlay_offset_x=model.overlay_offset_x,
                overlay_offset_y=model.overlay_offset_y,
                overlay_damage_out_offset_x=model.overlay_damage_out_offset_x,
                overlay_damage_in_offset_x=model.overlay_damage_in_offset_x,
                overlay_heal_in_offset_y=model.overlay_heal_in_offset_y,
                overlay_font_family=normalize_overlay_font_family(model.overlay_font_family),
                overlay_font_size=model.overlay_font_size,
                overlay_time_to_fade_s=model.overlay_time_to_fade_s,
                overlay_event_spacing=model.overlay_event_spacing,
                overlay_combine_multi_hit=model.overlay_combine_multi_hit,
                ocr_passes_enabled=model.ocr_passes_enabled or default_settings.ocr_passes_enabled,
                visible_columns=model.visible_columns or default_settings.visible_columns,
            )

    def save_settings(self, settings: RuntimeSettings) -> RuntimeSettings:
        with SessionLocal() as db:
            model = db.get(SettingsModel, 1)
            if model is None:
                model = SettingsModel(id=1)
                db.add(model)
            model.capture_x = settings.capture_x
            model.capture_y = settings.capture_y
            model.capture_width = settings.capture_width
            model.capture_height = settings.capture_height
            model.combat_region_configured = settings.combat_region_configured
            model.onboarding_completed = settings.onboarding_completed
            model.capture_interval_ms = settings.capture_interval_ms
            model.inactivity_timeout_s = settings.inactivity_timeout_s
            model.encounter_inactivity_timeout_s = settings.encounter_inactivity_timeout_s
            model.debug_mode = settings.debug_mode
            model.require_game_focus = settings.require_game_focus
            model.increase_accuracy = settings.increase_accuracy
            settings.allowed_focus_executables = normalize_executable_names(settings.allowed_focus_executables)
            model.allowed_focus_executables = settings.allowed_focus_executables
            model.allowed_focus_executables_configured = True
            model.manual_end_hotkey_enabled = settings.manual_end_hotkey_enabled
            model.manual_end_hotkey_binding = settings.manual_end_hotkey_binding
            mode = (settings.encounter_filter_mode or "none").lower()
            if mode not in {"none", "solo", "party"}:
                mode = "none"
            model.encounter_filter_mode = mode
            sanitized_party_members: list[dict[str, str | None]] = []
            party_member_keys: set[str] = set()
            for raw_member in settings.encounter_party_members:
                member_name = ""
                pet_name: str | None = None
                if isinstance(raw_member, dict):
                    member_name = str(raw_member.get("name") or "").strip()
                    parsed_pet_name = str(raw_member.get("pet_name") or "").strip()
                    pet_name = parsed_pet_name or None
                else:
                    member_name = str(raw_member or "").strip()
                if not member_name:
                    continue
                member_key = member_name.lower()
                if member_key == "you":
                    continue
                if member_key in party_member_keys:
                    continue
                if pet_name and pet_name.lower() == "you":
                    pet_name = None
                if pet_name and pet_name.lower() in party_member_keys:
                    pet_name = None
                sanitized_party_members.append({"name": member_name, "pet_name": pet_name})
                party_member_keys.add(member_key)
                if pet_name:
                    party_member_keys.add(pet_name.lower())
                if len(sanitized_party_members) >= 5:
                    break
            model.encounter_party_members = sanitized_party_members if mode == "party" else []
            model.floating_combat_text_enabled = settings.floating_combat_text_enabled
            model.overlay_monitor_index = settings.overlay_monitor_index
            model.overlay_anchor_x_ratio = settings.overlay_anchor_x_ratio
            model.overlay_anchor_y_ratio = settings.overlay_anchor_y_ratio
            model.overlay_offset_x = settings.overlay_offset_x
            model.overlay_offset_y = settings.overlay_offset_y
            model.overlay_damage_out_offset_x = settings.overlay_damage_out_offset_x
            model.overlay_damage_in_offset_x = settings.overlay_damage_in_offset_x
            model.overlay_heal_in_offset_y = settings.overlay_heal_in_offset_y
            settings.overlay_font_family = normalize_overlay_font_family(settings.overlay_font_family)
            model.overlay_font_family = settings.overlay_font_family
            model.overlay_font_size = settings.overlay_font_size
            model.overlay_time_to_fade_s = settings.overlay_time_to_fade_s
            model.overlay_event_spacing = settings.overlay_event_spacing
            model.overlay_combine_multi_hit = settings.overlay_combine_multi_hit
            model.ocr_passes_enabled = settings.ocr_passes_enabled
            model.visible_columns = settings.visible_columns
            db.commit()
        with self._lock:
            self._overlay_requested_by_parser = self._running and settings.floating_combat_text_enabled
            self._sync_overlay_state()
        return settings

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._open_parser_session(file_path=None)
            self._overlay_requested_by_parser = self.load_settings().floating_combat_text_enabled
            self._sync_overlay_state()
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _open_parser_session(self, *, file_path: str | None) -> int:
        with SessionLocal() as db:
            active = db.scalars(
                select(ParserSession).where(ParserSession.active.is_(True)).order_by(ParserSession.id.desc())
            ).first()
            if active:
                active.active = False
                active.ended_at = utcnow()
            parser_session = ParserSession(active=True, started_at=utcnow(), file_path=file_path)
            db.add(parser_session)
            db.commit()
            db.refresh(parser_session)
            self._active_parser_session_id = parser_session.id
            return parser_session.id

    def stop(self) -> None:
        thread: threading.Thread | None
        with self._lock:
            self._running = False
            thread = self._thread

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join()

        with self._lock:
            self._thread = None
            self._close_active_parser_session()
            self._overlay_requested_by_parser = False
            self._overlay_requested_manually = False
            self._sync_overlay_state()
            self._known_actors = {"You"}
            self._reset_frame_tracking(skip_next_frame_for_new_lines=False, clear_frame_memory=True)

    def start_overlay(self) -> None:
        with self._lock:
            self._overlay_requested_manually = True
            self._sync_overlay_state()

    def stop_overlay(self) -> None:
        with self._lock:
            self._overlay_requested_manually = False
            self._sync_overlay_state()

    def overlay_status(self) -> dict[str, bool]:
        with self._lock:
            active = self._overlay_requested_by_parser or self._overlay_requested_manually
            return {
                "active": active,
                "parser_requested": self._overlay_requested_by_parser,
                "manual_requested": self._overlay_requested_manually,
            }

    def _sync_overlay_state(self) -> None:
        active = self._overlay_requested_by_parser or self._overlay_requested_manually
        if active:
            settings = self.load_settings()
            floating_text_controller.start(
                OverlayPositionConfig(
                    monitor_index=settings.overlay_monitor_index,
                    anchor_x_ratio=settings.overlay_anchor_x_ratio,
                    anchor_y_ratio=settings.overlay_anchor_y_ratio,
                    offset_x=settings.overlay_offset_x,
                    offset_y=settings.overlay_offset_y,
                    damage_out_offset_x=settings.overlay_damage_out_offset_x,
                    damage_in_offset_x=settings.overlay_damage_in_offset_x,
                    heal_in_offset_y=settings.overlay_heal_in_offset_y,
                    font_family=settings.overlay_font_family,
                    font_size=settings.overlay_font_size,
                    time_to_fade_s=settings.overlay_time_to_fade_s,
                    event_spacing=settings.overlay_event_spacing,
                )
            )
            return
        floating_text_controller.stop()

    def begin_external_session(self, *, file_path: str) -> int:
        with self._lock:
            if self._running:
                raise RuntimeError("Cannot start external parser session while live capture is running")
            self._known_actors = {"You"}
            self._reset_frame_tracking(skip_next_frame_for_new_lines=False, clear_frame_memory=True)
            return self._open_parser_session(file_path=file_path)

    def end_external_session(self) -> None:
        with self._lock:
            self._close_active_parser_session()
            self._known_actors = {"You"}
            self._reset_frame_tracking(skip_next_frame_for_new_lines=False, clear_frame_memory=True)

    def _close_active_parser_session(self) -> None:
        with SessionLocal() as db:
            session_obj: ParserSession | None = None
            if self._active_parser_session_id is not None:
                session_obj = db.get(ParserSession, self._active_parser_session_id)
            if session_obj is None:
                session_obj = db.scalars(
                    select(ParserSession).where(ParserSession.active.is_(True)).order_by(ParserSession.id.desc())
                ).first()
            if session_obj:
                session_obj.active = False
                session_obj.ended_at = utcnow()
                db.commit()
        self._active_parser_session_id = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "last_successful_ocr_at": self.last_successful_ocr_at.isoformat() if self.last_successful_ocr_at else None,
            "last_error": self.last_error,
            "last_skip_reason": self.last_skip_reason,
            "known_actors": sorted(self._known_actors),
        }

    def _reset_frame_tracking(self, *, skip_next_frame_for_new_lines: bool, clear_frame_memory: bool = True) -> None:
        if clear_frame_memory:
            self._frame_memory_norm_lines = []
        self._ignore_next_frame_for_new_lines = skip_next_frame_for_new_lines

    @staticmethod
    def _combine_overlay_candidates(candidates: list[_OverlayCandidate]) -> list[StructuredCombatEvent]:
        if not candidates:
            return []

        combined: list[_OverlayCandidate] = [candidates[0]]
        for candidate in candidates[1:]:
            previous = combined[-1]
            if (
                previous.event.ability_name is not None
                and candidate.event.ability_name is not None
                and previous.kind == candidate.kind
                and previous.event.source == candidate.event.source
                and previous.event.target == candidate.event.target
                and previous.event.ability_name == candidate.event.ability_name
            ):
                combined[-1] = _OverlayCandidate(
                    kind=previous.kind,
                    event=build_structured_combat_event(
                        event_type=previous.event.event_type,
                        amount=(previous.event.amount or 0) + (candidate.event.amount or 0),
                        source=previous.event.source,
                        target=previous.event.target,
                        ability_name=previous.event.ability_name,
                        timestamp=previous.event.timestamp,
                    ),
                )
                continue
            combined.append(candidate)

        return [candidate.event for candidate in combined]

    def _process_ocr_result(self, *, db, ocr: OCRResult, now: datetime, timings: dict[str, float] | None = None) -> None:
        frame_started = time.perf_counter()
        db_elapsed = 0.0
        settings = self.load_settings()
        encounter_config = EncounterEngineConfig(
            inactivity_timeout_s=settings.encounter_inactivity_timeout_s,
            filter_mode=settings.encounter_filter_mode,
            party_members=settings.encounter_party_members,
        )
        current_frame_lines: list[tuple[str, str, float]] = []
        current_frame_meta: list[_FrameLine] = []
        if ocr.selected_lines:
            for line in ocr.selected_lines:
                if not line.normalized_text:
                    continue
                current_frame_lines.append((line.raw_text, line.normalized_text, line.confidence))
                current_frame_meta.append(
                    _FrameLine(
                        normalized_text=line.normalized_text,
                        confidence=line.confidence,
                        center_y=line.center_y,
                    )
                )
        else:
            for line_text, conf in ocr.lines:
                norm = normalize_text(line_text)
                if not norm:
                    continue
                current_frame_lines.append((line_text, norm, conf))
                current_frame_meta.append(_FrameLine(normalized_text=norm, confidence=conf))

        current_frame_norm_lines = current_frame_meta
        if self._ignore_next_frame_for_new_lines:
            # Skip emission for one frame while still refreshing overlap memory.
            new_line_ranges: list[tuple[int, int]] = []
            self._ignore_next_frame_for_new_lines = False
        else:
            new_line_ranges = self._compute_new_line_ranges(current_frame_norm_lines)

        lines_to_emit: list[tuple[str, str, float]] = []
        for start, end in new_line_ranges:
            lines_to_emit.extend(current_frame_lines[start:end])

        overlay_candidates: list[_OverlayCandidate] = []
        for line_text, norm, conf in lines_to_emit:

            parsed = parse_line(line_text, self._known_actors)
            accepted = parsed is not None and conf >= 0.45 and parsed.parse_confidence >= 0.8
            parse_outcome = "accepted" if accepted else "rejected"
            parse_confidence = parsed.parse_confidence if parsed else None

            if self._active_parser_session_id is not None:
                db_started = time.perf_counter()
                session_line = SessionLogLine(
                    parser_session_id=self._active_parser_session_id,
                    received_at=now,
                    raw_text=line_text,
                    normalized_text=norm,
                    ocr_confidence=conf,
                    parse_confidence=parse_confidence,
                    parse_outcome=parse_outcome,
                    parse_category=parsed.category if parsed else None,
                    source_actor=parsed.source_actor if parsed else None,
                    target_actor=parsed.target_actor if parsed else None,
                    ability_name=parsed.ability_name if parsed else None,
                    amount=parsed.amount if parsed else None,
                    absorbed_amount=parsed.absorbed_amount if parsed else None,
                    blocked_amount=parsed.blocked_amount if parsed else None,
                    result=parsed.result if parsed else None,
                    attack_verb=parsed.attack_verb if parsed else None,
                    effect_name=parsed.effect_name if parsed else None,
                )
                db.add(session_line)
                db.flush()
                encounter_engine.process_committed_line(db, session_line, config=encounter_config)
                db_elapsed += time.perf_counter() - db_started

            if accepted:
                structured_event = build_structured_combat_event(
                    event_type=parsed.category,
                    amount=parsed.amount,
                    source=parsed.source_actor,
                    target=parsed.target_actor,
                    ability_name=parsed.ability_name,
                    timestamp=now,
                )
                mapped = map_event_to_floating_kind(structured_event)
                if mapped is not None:
                    overlay_candidates.append(_OverlayCandidate(event=structured_event, kind=mapped))
                if parsed.source_actor:
                    self._known_actors.add(parsed.source_actor)
                if parsed.target_actor:
                    self._known_actors.add(parsed.target_actor)

        overlay_events = (
            self._combine_overlay_candidates(overlay_candidates)
            if settings.overlay_combine_multi_hit
            else [candidate.event for candidate in overlay_candidates]
        )
        for overlay_event in overlay_events:
            combat_event_bus.publish(overlay_event)

        self._remember_frame_lines(current_frame_norm_lines)
        if timings is not None:
            total_elapsed = time.perf_counter() - frame_started
            timings["db_ms"] = timings.get("db_ms", 0.0) + (db_elapsed * 1000.0)
            timings["postprocess_ms"] = timings.get("postprocess_ms", 0.0) + ((total_elapsed - db_elapsed) * 1000.0)

    @staticmethod
    def _compute_line_position_weights(lines: list["_FrameLine"]) -> list[float]:
        if not lines:
            return []

        if len(lines) == 1:
            return [1.0]

        centers = [line.center_y for line in lines if line.center_y is not None]
        if centers:
            minimum = min(centers)
            maximum = max(centers)
            span = max(maximum - minimum, 1.0)
            return [
                0.55 + (1.0 - abs((((line.center_y if line.center_y is not None else minimum) - minimum) / span) - 0.5) * 2.0) * 0.45
                for line in lines
            ]

        denominator = len(lines) - 1
        return [0.6 + (1.0 - abs((index / denominator) - 0.5) * 2.0) * 0.4 for index in range(len(lines))]

    @staticmethod
    def _is_fuzzy_match(previous_text: str, current_text: str) -> bool:
        if previous_text == current_text:
            return True
        if abs(len(previous_text) - len(current_text)) > 6:
            return False

        shorter = min(len(previous_text), len(current_text))
        if shorter <= 12:
            return False

        token_count = min(len(previous_text.split()), len(current_text.split()))
        if token_count <= 3:
            return SequenceMatcher(a=previous_text, b=current_text).ratio() >= 0.97

        threshold = 0.95 if shorter <= 24 else 0.92
        return SequenceMatcher(a=previous_text, b=current_text).ratio() >= threshold

    @classmethod
    def _best_overlap_bounds(
        cls, previous_lines: list["_FrameLine"], current_lines: list["_FrameLine"]
    ) -> tuple[tuple, int, int]:
        best_key: tuple = (-1, -1, -1, -10_000, -1.0, -1.0)
        best_cur_start = 0
        best_cur_end = 0
        previous_weights = cls._compute_line_position_weights(previous_lines)
        current_weights = cls._compute_line_position_weights(current_lines)
        previous_texts = [line.normalized_text for line in previous_lines]
        current_texts = [line.normalized_text for line in current_lines]

        for prev_start in range(len(previous_lines)):
            for cur_start in range(len(current_lines)):
                max_len = min(len(previous_lines) - prev_start, len(current_lines) - cur_start)
                exact_count = 0
                weighted_sum = 0.0

                for overlap_len in range(1, max_len + 1):
                    previous_index = prev_start + overlap_len - 1
                    current_index = cur_start + overlap_len - 1
                    previous_line = previous_lines[previous_index]
                    current_line = current_lines[current_index]
                    if not cls._is_fuzzy_match(previous_texts[previous_index], current_texts[current_index]):
                        break

                    is_exact = previous_texts[previous_index] == current_texts[current_index]
                    if is_exact:
                        exact_count += 1

                    prev_weight = previous_weights[previous_index]
                    cur_weight = current_weights[current_index]
                    avg_conf = (previous_line.confidence + current_line.confidence) / 2.0
                    weighted_sum += avg_conf * ((prev_weight + cur_weight) / 2.0)

                    all_exact = 1 if exact_count == overlap_len else 0
                    boundary_bonus = 0.0
                    if prev_start + overlap_len == len(previous_lines):
                        boundary_bonus += 0.15
                    if cur_start == 0:
                        boundary_bonus += 0.1
                    score = weighted_sum + boundary_bonus
                    key = (overlap_len, exact_count, all_exact, -cur_start, score, boundary_bonus)
                    if key > best_key:
                        best_key = key
                        best_cur_start = cur_start
                        best_cur_end = cur_start + overlap_len

        return best_key, best_cur_start, best_cur_end

    @staticmethod
    def _coerce_lines(lines: list[str] | list["_FrameLine"]) -> list["_FrameLine"]:
        if not lines:
            return []
        first = lines[0]
        if isinstance(first, _FrameLine):
            return list(lines)
        return [_FrameLine(normalized_text=str(line)) for line in lines]

    def _compute_new_line_ranges(self, current_lines: list[str] | list["_FrameLine"]) -> list[tuple[int, int]]:
        current = self._coerce_lines(current_lines)
        if not current:
            return []

        candidates = self._frame_memory_norm_lines or [[]]
        current_texts = [line.normalized_text for line in current]
        best_cur_start = 0
        best_cur_end = 0
        best_key: tuple = (-1, -1, -1, -10_000, -1.0, -1.0)
        for previous_lines in reversed(candidates):
            if len(previous_lines) >= len(current):
                window_size = len(current)
                previous_texts = [line.normalized_text for line in previous_lines]
                if any(
                    previous_texts[i : i + window_size] == current_texts
                    for i in range(len(previous_lines) - window_size + 1)
                ):
                    return []
            overlap_key, cur_start, cur_end = self._best_overlap_bounds(previous_lines, current)
            if overlap_key > best_key:
                best_key = overlap_key
                best_cur_start = cur_start
                best_cur_end = cur_end

        if best_key[0] <= 0:
            return [(0, len(current))]

        new_ranges: list[tuple[int, int]] = []
        if best_cur_start > 0:
            new_ranges.append((0, best_cur_start))
        if best_cur_end < len(current):
            new_ranges.append((best_cur_end, len(current)))
        return new_ranges

    def _compute_new_line_start(self, current_lines: list[str] | list["_FrameLine"]) -> int:
        ranges = self._compute_new_line_ranges(current_lines)
        if not ranges:
            return len(current_lines)
        return ranges[-1][0]

    def _remember_frame_lines(self, current_lines: list[str] | list["_FrameLine"]) -> None:
        self._frame_memory_norm_lines.append(self._coerce_lines(current_lines))
        if len(self._frame_memory_norm_lines) > 2:
            self._frame_memory_norm_lines = self._frame_memory_norm_lines[-2:]

    def _loop(self) -> None:
        while self._running:
            settings = self.load_settings()
            try:
                if self._should_skip_for_focus(settings):
                    time.sleep(max(settings.capture_interval_ms, 100) / 1000.0)
                    continue
                image = capture_region(CaptureRegion(settings.capture_x, settings.capture_y, settings.capture_width, settings.capture_height))
                self.process_image_frame(image)
            except Exception as exc:
                self.last_error = str(exc)
                self.last_skip_reason = None

            time.sleep(max(settings.capture_interval_ms, 100) / 1000.0)

    def _should_skip_for_focus(self, settings: RuntimeSettings) -> bool:
        if not settings.require_game_focus:
            self.last_skip_reason = None
            return False

        allowed_executables = set(normalize_executable_names(settings.allowed_focus_executables))
        if not allowed_executables:
            self.last_error = None
            self.last_skip_reason = "focus_not_allowed"
            return True

        focused_executable = get_foreground_process_name()
        if focused_executable is None:
            self.last_error = None
            self.last_skip_reason = "focus_lookup_failed"
            return True

        if normalize_executable_name(focused_executable) in allowed_executables:
            self.last_skip_reason = None
            return False

        self.last_error = None
        self.last_skip_reason = "focus_not_allowed"
        return True

    def process_image_frame(
        self,
        image: "Image.Image | np.ndarray",
        *,
        now: datetime | None = None,
        timings: dict[str, float] | None = None,
    ) -> None:
        settings = self.load_settings()
        ocr_timings: dict[str, float] = {}
        try:
            ocr = run_ocr(
                image,
                enabled_passes=set(settings.ocr_passes_enabled),
                increase_accuracy=settings.increase_accuracy,
                timings=ocr_timings if timings is not None else None,
            )
        except TypeError:
            # Backward compatibility for tests/mocks with older run_ocr signatures.
            ocr = run_ocr(
                image,
                enabled_passes=set(settings.ocr_passes_enabled),
                increase_accuracy=settings.increase_accuracy,
            )
        self.last_successful_ocr_at = utcnow()
        self.last_error = None
        self.last_skip_reason = None
        received_at = now or utcnow()

        with SessionLocal() as db:
            processing_timings: dict[str, float] = {}
            self._process_ocr_result(db=db, ocr=ocr, now=received_at, timings=processing_timings if timings is not None else None)
            commit_started = time.perf_counter()
            db.commit()
            commit_elapsed_ms = (time.perf_counter() - commit_started) * 1000.0

        if timings is not None:
            timings["preprocess_ms"] = ocr_timings.get("preprocess_ms", 0.0)
            timings["tesseract_ms"] = ocr_timings.get("tesseract_ms", 0.0)
            timings["postprocess_ms"] = ocr_timings.get("postprocess_ms", 0.0) + processing_timings.get("postprocess_ms", 0.0)
            timings["db_ms"] = processing_timings.get("db_ms", 0.0) + commit_elapsed_ms
            timings["total_ms"] = (
                timings["preprocess_ms"]
                + timings["tesseract_ms"]
                + timings["postprocess_ms"]
                + timings["db_ms"]
            )


service = ParserService()
