from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .service import ParserService
from .tesseract_backend import get_backend_stats, set_forced_backend


_PATTERN_RE = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)(?P<suffix>[^/\\]*)$")


@dataclass(frozen=True)
class ReplayConfig:
    start_image_path: str
    end_index: int | None = None
    frame_delay_ms: int | None = None
    repeat_count: int = 1
    force_pytesseract: bool = False


@dataclass(frozen=True)
class SequencePattern:
    directory: Path
    prefix: str
    suffix: str
    padding: int
    start_index: int

    @classmethod
    def from_start_path(cls, start_path: str | Path) -> "SequencePattern":
        path = Path(start_path).expanduser().resolve()
        match = _PATTERN_RE.match(path.name)
        if match is None:
            raise ValueError("start_image_path must include a numeric index, e.g. sample1.png")

        number_text = match.group("number")
        return cls(
            directory=path.parent,
            prefix=match.group("prefix"),
            suffix=match.group("suffix"),
            padding=len(number_text),
            start_index=int(number_text),
        )

    def path_for(self, index: int) -> Path:
        number_text = str(index).zfill(self.padding)
        return self.directory / f"{self.prefix}{number_text}{self.suffix}"


class ReplayRunner:
    _PROFILE_STAGE_KEYS = ("decode_ms", "preprocess_ms", "tesseract_ms", "postprocess_ms", "db_ms", "total_ms")

    def __init__(self, parser_service: ParserService) -> None:
        self._service = parser_service
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._processed_frames = 0
        self._total_processing_time_s = 0.0
        self._last_error: str | None = None
        self._current_config: ReplayConfig | None = None
        self._stage_totals_ms: dict[str, float] = {key: 0.0 for key in self._PROFILE_STAGE_KEYS}

    def start(self, config: ReplayConfig) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Replay is already running")

            self._running = True
            self._stop_event.clear()
            self._processed_frames = 0
            self._total_processing_time_s = 0.0
            self._last_error = None
            self._current_config = config
            self._stage_totals_ms = {key: 0.0 for key in self._PROFILE_STAGE_KEYS}

            self._thread = threading.Thread(target=self._run, args=(config,), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()

        if thread is not None and thread.is_alive():
            thread.join()

    def status(self) -> dict:
        with self._lock:
            config = self._current_config
            average_processing_time_ms = (
                (self._total_processing_time_s / self._processed_frames) * 1000.0
                if self._processed_frames > 0
                else None
            )
            return {
                "running": self._running,
                "processed_frames": self._processed_frames,
                "average_processing_time_ms": average_processing_time_ms,
                "average_stage_times_ms": {
                    key: (self._stage_totals_ms[key] / self._processed_frames if self._processed_frames > 0 else None)
                    for key in self._PROFILE_STAGE_KEYS
                },
                "ocr_backend": get_backend_stats(),
                "last_error": self._last_error,
                "config": config.__dict__ if config else None,
            }

    def _run(self, config: ReplayConfig) -> None:
        metadata_path = f"replay:{Path(config.start_image_path).expanduser().resolve()}"
        session_started = False
        previous_forced_backend: str | None = None
        try:
            pattern = SequencePattern.from_start_path(config.start_image_path)
            previous_forced_backend = get_backend_stats().get("forced_backend")
            set_forced_backend("pytesseract" if config.force_pytesseract else None)
            self._service.begin_external_session(file_path=metadata_path)
            session_started = True

            frame_delay_s = max(config.frame_delay_ms or 0, 0) / 1000.0
            repeat_count = max(config.repeat_count, 1)
            for _loop_index in range(repeat_count):
                if self._stop_event.is_set():
                    break

                frames_processed_this_loop = 0
                index = pattern.start_index
                while not self._stop_event.is_set():
                    if config.end_index is not None and index > config.end_index:
                        break

                    image_path = pattern.path_for(index)
                    if not image_path.exists():
                        break

                    decode_started = time.perf_counter()
                    image = self._load_frame(image_path)
                    decode_elapsed_ms = (time.perf_counter() - decode_started) * 1000.0

                    frame_started = time.perf_counter()
                    frame_timings: dict[str, float] = {}
                    try:
                        self._service.process_image_frame(image, timings=frame_timings)
                    except TypeError:
                        # Backward-compatibility for test doubles or integrations without the optional timings kwarg.
                        self._service.process_image_frame(image)
                    frame_elapsed_s = time.perf_counter() - frame_started

                    with self._lock:
                        self._processed_frames += 1
                        self._total_processing_time_s += frame_elapsed_s
                        self._stage_totals_ms["decode_ms"] += decode_elapsed_ms
                        self._stage_totals_ms["preprocess_ms"] += frame_timings.get("preprocess_ms", 0.0)
                        self._stage_totals_ms["tesseract_ms"] += frame_timings.get("tesseract_ms", 0.0)
                        self._stage_totals_ms["postprocess_ms"] += frame_timings.get("postprocess_ms", 0.0)
                        self._stage_totals_ms["db_ms"] += frame_timings.get("db_ms", 0.0)
                        self._stage_totals_ms["total_ms"] += (
                            decode_elapsed_ms
                            + frame_timings.get("preprocess_ms", 0.0)
                            + frame_timings.get("tesseract_ms", 0.0)
                            + frame_timings.get("postprocess_ms", 0.0)
                            + frame_timings.get("db_ms", 0.0)
                        )

                    frames_processed_this_loop += 1
                    index += 1
                    if frame_delay_s > 0:
                        time.sleep(frame_delay_s)

                if frames_processed_this_loop == 0:
                    break
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            with self._lock:
                self._last_error = str(exc)
        finally:
            set_forced_backend(previous_forced_backend)
            if session_started:
                self._service.end_external_session()
            with self._lock:
                self._running = False
                self._thread = None

    @staticmethod
    def _load_frame(image_path: Path) -> np.ndarray:
        encoded = np.fromfile(image_path, dtype=np.uint8)
        loaded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if loaded is None:
            raise RuntimeError(f"Failed to decode replay frame: {image_path}")
        return cv2.cvtColor(loaded, cv2.COLOR_BGR2RGB)


__all__ = ["ReplayConfig", "ReplayRunner", "SequencePattern"]
