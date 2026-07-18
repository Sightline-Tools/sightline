from __future__ import annotations

import csv
from io import StringIO
import os
from pathlib import Path
import shlex
import threading
from typing import Any

import numpy as np
from PIL import Image

try:
    import tesserocr
except (ModuleNotFoundError, ImportError):  # pragma: no cover - fallback for environments without tesserocr
    tesserocr = None

import pytesseract

from .paths import IS_FROZEN, resource_path


def configure_tesseract_runtime() -> Path | None:
    """Point OCR at the packaged runtime without consulting the system PATH."""
    executable_name = "tesseract.exe" if os.name == "nt" else "tesseract"
    executable = resource_path("tesseract", executable_name)
    tessdata = resource_path("tesseract", "tessdata")
    if executable.is_file():
        pytesseract.pytesseract.tesseract_cmd = str(executable)
        if tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        return executable
    if IS_FROZEN:
        # A packaged build must never silently use an unrelated machine install.
        pytesseract.pytesseract.tesseract_cmd = str(executable)
    return None


BUNDLED_TESSERACT = configure_tesseract_runtime()

_backend_lock = threading.Lock()
_backend_stats: dict[str, Any] = {
    "last_backend": None,
    "tesserocr_calls": 0,
    "pytesseract_calls": 0,
    "tesserocr_fallback_runtime_errors": 0,
    "last_tesserocr_error": None,
    "forced_backend": None,
}
_forced_backend: str | None = None


def _to_pil(image: Image.Image | np.ndarray) -> Image.Image:
    if isinstance(image, Image.Image):
        return image
    return Image.fromarray(image)


def _parse_psm(config: str) -> int:
    if tesserocr is None:
        raise RuntimeError("tesserocr is not available")
    tokens = shlex.split(config, posix=True)
    for index, token in enumerate(tokens):
        if token == "--psm" and (index + 1) < len(tokens):
            try:
                mode = int(tokens[index + 1])
                # tesserocr expects the numeric PSM value, not enum instantiation.
                return mode
            except ValueError:
                break
    return int(tesserocr.PSM.SINGLE_BLOCK)


def _candidate_tessdata_paths() -> list[str | None]:
    paths: list[str | None] = [None]
    env_path = os.environ.get("TESSDATA_PREFIX")
    packaged_path = resource_path("tesseract", "tessdata")
    source_model_path = resource_path("ocr-data")
    if source_model_path.is_dir():
        paths.insert(0, str(source_model_path))
    if packaged_path.is_dir():
        paths.insert(0, str(packaged_path))
    if env_path and env_path.strip() not in {"", ".", "./"} and Path(env_path).is_dir():
        paths.insert(0, env_path)

    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
    if cmd:
        candidate = Path(cmd).expanduser().resolve().parent / "tessdata"
        if candidate.is_dir():
            paths.insert(0, str(candidate))

    deduped: list[str | None] = []
    for item in paths:
        if item not in deduped:
            deduped.append(item)
    return deduped


def image_to_data(image: Image.Image | np.ndarray, *, config: str) -> dict[str, list[Any]]:
    with _backend_lock:
        forced_backend = _forced_backend

    if forced_backend == "pytesseract":
        with _backend_lock:
            _backend_stats["last_backend"] = "pytesseract"
            _backend_stats["pytesseract_calls"] += 1
        return pytesseract.image_to_data(
            _to_pil(image),
            output_type=pytesseract.Output.DICT,
            config=config,
        )

    if tesserocr is None:
        with _backend_lock:
            _backend_stats["last_backend"] = "pytesseract"
            _backend_stats["pytesseract_calls"] += 1
        return pytesseract.image_to_data(
            _to_pil(image),
            output_type=pytesseract.Output.DICT,
            config=config,
        )

    pil_image = _to_pil(image)
    tsv_text: str | None = None
    runtime_error_message: str | None = None
    original_tessdata_prefix = os.environ.get("TESSDATA_PREFIX")
    if original_tessdata_prefix in {"", ".", "./"}:
        os.environ.pop("TESSDATA_PREFIX", None)
    try:
        for tessdata_path in _candidate_tessdata_paths():
            try:
                if tessdata_path is None:
                    api_context = tesserocr.PyTessBaseAPI(psm=_parse_psm(config))
                else:
                    api_context = tesserocr.PyTessBaseAPI(path=tessdata_path, psm=_parse_psm(config))
                with api_context as api:
                    api.SetImage(pil_image)
                    tsv_text = api.GetTSVText(0)
                    break
            except Exception as exc:
                runtime_error_message = str(exc)
    finally:
        if original_tessdata_prefix is not None:
            os.environ["TESSDATA_PREFIX"] = original_tessdata_prefix

    if tsv_text is None:
        with _backend_lock:
            _backend_stats["last_backend"] = "pytesseract"
            _backend_stats["pytesseract_calls"] += 1
            _backend_stats["tesserocr_fallback_runtime_errors"] += 1
            _backend_stats["last_tesserocr_error"] = runtime_error_message
        return pytesseract.image_to_data(
            pil_image,
            output_type=pytesseract.Output.DICT,
            config=config,
        )

    with _backend_lock:
        _backend_stats["last_backend"] = "tesserocr"
        _backend_stats["tesserocr_calls"] += 1
        _backend_stats["last_tesserocr_error"] = None

    fieldnames = [
    "level", "page_num", "block_num", "par_num", "line_num", "word_num",
    "left", "top", "width", "height", "conf", "text"
    ]

    rows = csv.DictReader(StringIO(tsv_text), delimiter="\t", fieldnames=fieldnames)
    data: dict[str, list[Any]] = {
        "level": [],
        "page_num": [],
        "block_num": [],
        "par_num": [],
        "line_num": [],
        "word_num": [],
        "left": [],
        "top": [],
        "width": [],
        "height": [],
        "conf": [],
        "text": [],
    }

    for row in rows:
        data["level"].append(int(row.get("level", 0) or 0))
        data["page_num"].append(int(row.get("page_num", 0) or 0))
        data["block_num"].append(int(row.get("block_num", 0) or 0))
        data["par_num"].append(int(row.get("par_num", 0) or 0))
        data["line_num"].append(int(row.get("line_num", 0) or 0))
        data["word_num"].append(int(row.get("word_num", 0) or 0))
        data["left"].append(int(row.get("left", 0) or 0))
        data["top"].append(int(row.get("top", 0) or 0))
        data["width"].append(int(row.get("width", 0) or 0))
        data["height"].append(int(row.get("height", 0) or 0))
        data["conf"].append(float(row.get("conf", -1) or -1))
        data["text"].append(row.get("text", ""))

    return data


def get_backend_stats() -> dict[str, Any]:
    with _backend_lock:
        stats = dict(_backend_stats)
        stats["forced_backend"] = _forced_backend
        return stats


def set_forced_backend(backend: str | None) -> None:
    if backend not in {None, "pytesseract"}:
        raise ValueError("backend must be None or 'pytesseract'")
    global _forced_backend
    with _backend_lock:
        _forced_backend = backend
