from __future__ import annotations

import contextlib
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .combat_events import StructuredCombatEvent, combat_event_bus
from .config import CANONICAL_SELF

FloatingTextKind = Literal["damage_out", "damage_in", "heal_in"]


@dataclass(frozen=True)
class OverlayPositionConfig:
    monitor_index: int = 0
    anchor_x_ratio: float = 0.5
    anchor_y_ratio: float = 0.5
    offset_x: int = 0
    offset_y: int = 0
    damage_out_offset_x: int = 90
    damage_in_offset_x: int = -90
    heal_in_offset_y: int = 70
    font_size: int = 20
    time_to_fade_s: float = 1.0
    event_spacing: int = 18


@dataclass(frozen=True)
class FloatingTextStyle:
    kind: FloatingTextKind
    color_hex: str


@dataclass
class FloatingTextEntry:
    value: int
    kind: FloatingTextKind
    source: str | None
    target: str | None
    spawn_time: float
    start_x: float
    start_y: float
    drift_y: float
    lifetime_s: float
    slot: int
    font_size: int
    event_spacing: int


def _absolute_tk_geometry(*, width: int, height: int, x: int, y: int) -> str:
    """Return Tk geometry with absolute virtual-desktop coordinates."""
    return f"{width}x{height}+{x}+{y}"


class FloatingCombatTextOverlay:
    """Lightweight transparent Tk overlay that draws transient combat numbers."""

    def __init__(self, position: OverlayPositionConfig) -> None:
        self._position = position
        self._events: "queue.Queue[FloatingTextEntry]" = queue.Queue()
        self._ui_commands: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_requested = False
        self._state_lock = threading.Lock()
        self._overlay_width = 420
        self._overlay_height = 240

    def start(self) -> None:
        with self._state_lock:
            if self._running or (self._thread is not None and self._thread.is_alive()):
                if not self._stop_requested:
                    self._ui_commands.put("show")
                return
            self._stop_requested = False
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=False)
            self._thread.start()
            self._ui_commands.put("show")

    def stop(self) -> None:
        with self._state_lock:
            if not self._running:
                return
        self._ui_commands.put("hide")

    def shutdown(self) -> None:
        with self._state_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._running = False
                self._stop_requested = False
                return
            self._stop_requested = True

        self._ui_commands.put("stop")

        if thread is not threading.current_thread():
            thread.join()

    def enqueue(self, entry: FloatingTextEntry) -> None:
        self._events.put(entry)

    def update_position(self, position: OverlayPositionConfig) -> None:
        self._position = position

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            with self._state_lock:
                self._running = False
                self._thread = None
            return

        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.95)
            transparent = "#010101"
            root.configure(bg=transparent)
            root.wm_attributes("-transparentcolor", transparent)
            canvas = tk.Canvas(root, bg=transparent, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
        except Exception:
            with self._state_lock:
                self._running = False
                self._thread = None
            return

        screen_left = 0
        screen_top = 0
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        try:
            import mss

            with mss.mss() as capture:
                physical_monitors = capture.monitors[1:]
                if physical_monitors:
                    monitor_index = min(max(self._position.monitor_index, 0), len(physical_monitors) - 1)
                    monitor = physical_monitors[monitor_index]
                    screen_left = int(monitor["left"])
                    screen_top = int(monitor["top"])
                    screen_w = int(monitor["width"])
                    screen_h = int(monitor["height"])
        except Exception:
            pass
        width, height = self._overlay_width, self._overlay_height
        anchor_x = screen_left + int(screen_w * self._position.anchor_x_ratio) + self._position.offset_x
        anchor_y = screen_top + int(screen_h * self._position.anchor_y_ratio) + self._position.offset_y
        x = anchor_x - width // 2
        y = anchor_y - height // 2
        root.geometry(_absolute_tk_geometry(width=width, height=height, x=x, y=y))
        root.withdraw()

        active: list[FloatingTextEntry] = []
        visible = False

        def _tick() -> None:
            nonlocal active, visible
            with self._state_lock:
                if self._stop_requested:
                    root.quit()
                    return

            while True:
                try:
                    active.append(self._events.get_nowait())
                except queue.Empty:
                    break

            while True:
                try:
                    command = self._ui_commands.get_nowait()
                except queue.Empty:
                    break
                if command == "show":
                    visible = True
                    root.deiconify()
                elif command == "hide":
                    visible = False
                    active = []
                    canvas.delete("all")
                    root.withdraw()
                elif command == "stop":
                    visible = False
                    active = []
                    canvas.delete("all")
                    root.withdraw()
                    root.quit()
                    return

            if visible:
                now = time.time()
                canvas.delete("all")
                next_active: list[FloatingTextEntry] = []
                for entry in active:
                    elapsed = now - entry.spawn_time
                    if elapsed >= entry.lifetime_s:
                        continue
                    progress = elapsed / entry.lifetime_s
                    y_offset = entry.drift_y * progress
                    alpha = max(0.0, 1.0 - progress)
                    text_color = _fade_hex(_style_for_kind(entry.kind).color_hex, alpha)
                    canvas.create_text(
                        entry.start_x,
                        entry.start_y + y_offset + (entry.slot * entry.event_spacing),
                        text=str(entry.value),
                        fill=text_color,
                        font=("Segoe UI", entry.font_size, "bold"),
                    )
                    next_active.append(entry)
                active = next_active
            root.after(16, _tick)

        root.after(0, _tick)
        try:
            root.mainloop()
        finally:
            with contextlib.suppress(Exception):
                root.destroy()
            with self._state_lock:
                self._running = False
                self._stop_requested = False
                self._thread = None


def _style_for_kind(kind: FloatingTextKind) -> FloatingTextStyle:
    if kind == "damage_in":
        return FloatingTextStyle(kind=kind, color_hex="#ff4d4d")
    if kind == "heal_in":
        return FloatingTextStyle(kind=kind, color_hex="#52d66b")
    return FloatingTextStyle(kind=kind, color_hex="#f8f8f8")


def _fade_hex(color_hex: str, alpha: float) -> str:
    alpha = max(0.0, min(1.0, alpha))
    red = int(color_hex[1:3], 16)
    green = int(color_hex[3:5], 16)
    blue = int(color_hex[5:7], 16)
    bg = 1
    red = int(red * alpha + bg * (1 - alpha))
    green = int(green * alpha + bg * (1 - alpha))
    blue = int(blue * alpha + bg * (1 - alpha))
    return f"#{red:02x}{green:02x}{blue:02x}"


class FloatingCombatTextController:
    def __init__(self) -> None:
        self._overlay: FloatingCombatTextOverlay | None = None
        self._unsubscribe = None
        self._recent_slots: list[float] = []
        self._position = OverlayPositionConfig()

    def start(self, position: OverlayPositionConfig) -> None:
        self._position = position
        if self._overlay is None:
            self._overlay = FloatingCombatTextOverlay(position)
        else:
            self._overlay.update_position(position)
        self._overlay.start()

        if self._unsubscribe is None:
            self._unsubscribe = combat_event_bus.subscribe(self._on_event)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._overlay is not None:
            self._overlay.stop()
        self._recent_slots.clear()

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._overlay is not None:
            self._overlay.shutdown()
        self._recent_slots.clear()

    def _on_event(self, event: StructuredCombatEvent) -> None:
        if self._overlay is None or event.amount is None:
            return
        mapped = map_event_to_floating_kind(event)
        if mapped is None:
            return
        now = time.time()
        self._recent_slots = [slot_ts for slot_ts in self._recent_slots if now - slot_ts < 0.35]
        slot = len(self._recent_slots)
        self._recent_slots.append(now)
        spawn_x, spawn_y = self._spawn_point_for_kind(mapped)
        self._overlay.enqueue(
            FloatingTextEntry(
                value=event.amount,
                kind=mapped,
                source=event.source,
                target=event.target,
                spawn_time=now,
                start_x=spawn_x,
                start_y=spawn_y,
                drift_y=26,
                lifetime_s=self._position.time_to_fade_s,
                slot=slot,
                font_size=self._position.font_size,
                event_spacing=self._position.event_spacing,
            )
        )

    def _spawn_point_for_kind(self, kind: FloatingTextKind) -> tuple[float, float]:
        center_x = 210.0
        center_y = 50.0
        if kind == "damage_out":
            return center_x + self._position.damage_out_offset_x, center_y
        if kind == "damage_in":
            return center_x + self._position.damage_in_offset_x, center_y
        if kind == "heal_in":
            return center_x, center_y + self._position.heal_in_offset_y
        return center_x, center_y


def map_event_to_floating_kind(event: StructuredCombatEvent) -> FloatingTextKind | None:
    target = (event.target or "").strip().lower()
    if event.event_type == "damage_out" and event.source == CANONICAL_SELF:
        return "damage_out"
    if event.event_type == "damage_in" and target == CANONICAL_SELF.lower():
        return "damage_in"
    if event.event_type == "heal" and target == CANONICAL_SELF.lower():
        return "heal_in"
    return None


floating_text_controller = FloatingCombatTextController()


def build_structured_combat_event(
    *,
    event_type: str,
    amount: int | None,
    source: str | None,
    target: str | None,
    ability_name: str | None = None,
    timestamp: datetime,
) -> StructuredCombatEvent:
    return StructuredCombatEvent(
        event_type=event_type,
        amount=amount,
        source=source,
        target=target,
        ability_name=ability_name,
        timestamp=timestamp,
    )
