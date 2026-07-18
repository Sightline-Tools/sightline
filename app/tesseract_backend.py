from __future__ import annotations

import atexit
import csv
from io import StringIO
import os
from pathlib import Path
import shlex
import threading
from typing import Any

import numpy as np
from PIL import Image

from .paths import IS_FROZEN, resource_path

try:
    import tesserocr
except (ModuleNotFoundError, ImportError):  # pragma: no cover - fallback for environments without tesserocr
    tesserocr = None

if IS_FROZEN:
    # The packaged application has no CLI backend or tesseract executable.
    pytesseract = None
else:
    import pytesseract


def configure_tesseract_runtime() -> Path | None:
    """Select a repository-local CLI runtime for development when one exists."""
    if pytesseract is None:
        return None
    executable_name = "tesseract.exe" if os.name == "nt" else "tesseract"
    executable = resource_path("tesseract", executable_name)
    tessdata = resource_path("tesseract", "tessdata")
    if executable.is_file():
        pytesseract.pytesseract.tesseract_cmd = str(executable)
        if tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        return executable
    return None


DEVELOPMENT_TESSERACT = configure_tesseract_runtime()

_backend_lock = threading.Lock()
_backend_stats: dict[str, Any] = {
    "last_backend": None,
    "tesserocr_calls": 0,
    "tesserocr_initializations": 0,
    "pytesseract_calls": 0,
    "tesserocr_fallback_runtime_errors": 0,
    "last_tesserocr_error": None,
    "forced_backend": None,
}
_forced_backend: str | None = None
_api_lock = threading.Lock()
_tesserocr_api: Any | None = None


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
    packaged_path = resource_path("tesseract", "tessdata")
    if IS_FROZEN:
        # Production builds must use the model shipped with the application.
        return [str(packaged_path)] if packaged_path.is_dir() else []

    paths: list[str | None] = [None]
    env_path = os.environ.get("TESSDATA_PREFIX")
    source_model_path = resource_path("ocr-data")
    if source_model_path.is_dir():
        paths.insert(0, str(source_model_path))
    if packaged_path.is_dir():
        paths.insert(0, str(packaged_path))
    if env_path and env_path.strip() not in {"", ".", "./"} and Path(env_path).is_dir():
        paths.insert(0, env_path)

    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None) if pytesseract is not None else None
    if cmd:
        candidate = Path(cmd).expanduser().resolve().parent / "tessdata"
        if candidate.is_dir():
            paths.insert(0, str(candidate))

    deduped: list[str | None] = []
    for item in paths:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _create_tesserocr_api(psm: int) -> Any:
    global _tesserocr_api

    candidates = _candidate_tessdata_paths()
    if not candidates:
        raise RuntimeError("packaged tessdata is missing")

    last_error: Exception | None = None
    original_tessdata_prefix = os.environ.get("TESSDATA_PREFIX")
    if original_tessdata_prefix in {"", ".", "./"}:
        os.environ.pop("TESSDATA_PREFIX", None)
    try:
        for tessdata_path in candidates:
            try:
                if tessdata_path is None:
                    api = tesserocr.PyTessBaseAPI(psm=psm)
                else:
                    api = tesserocr.PyTessBaseAPI(path=tessdata_path, lang="eng", psm=psm)
                _tesserocr_api = api
                with _backend_lock:
                    _backend_stats["tesserocr_initializations"] += 1
                return api
            except Exception as exc:
                last_error = exc
    finally:
        if original_tessdata_prefix is not None:
            os.environ["TESSDATA_PREFIX"] = original_tessdata_prefix
        else:
            os.environ.pop("TESSDATA_PREFIX", None)

    raise RuntimeError(f"could not initialize tesserocr: {last_error}") from last_error


def _close_tesserocr_api_unlocked() -> None:
    global _tesserocr_api

    api = _tesserocr_api
    _tesserocr_api = None
    if api is not None:
        try:
            api.End()
        except Exception:
            pass


def close_tesserocr_api() -> None:
    """Release the persistent native OCR instance during orderly shutdown or tests."""
    with _api_lock:
        _close_tesserocr_api_unlocked()


atexit.register(close_tesserocr_api)


def _tesserocr_image_to_tsv(image: Image.Image, *, config: str) -> str:
    psm = _parse_psm(config)
    with _api_lock:
        api = _tesserocr_api or _create_tesserocr_api(psm)
        try:
            api.SetPageSegMode(psm)
            api.SetImage(image)
            return api.GetTSVText(0)
        except Exception:
            # A failed native instance may carry invalid recognition state. End
            # it once and allow the next frame to initialize a clean instance.
            _close_tesserocr_api_unlocked()
            raise
        finally:
            if _tesserocr_api is api:
                try:
                    api.Clear()
                except Exception:
                    _close_tesserocr_api_unlocked()


def _pytesseract_image_to_data(image: Image.Image, *, config: str) -> dict[str, list[Any]]:
    if pytesseract is None:
        raise RuntimeError("The CLI OCR backend is not included in packaged builds")
    with _backend_lock:
        _backend_stats["last_backend"] = "pytesseract"
        _backend_stats["pytesseract_calls"] += 1
    return pytesseract.image_to_data(
        image,
        output_type=pytesseract.Output.DICT,
        config=config,
    )


def _tsv_to_data(tsv_text: str) -> dict[str, list[Any]]:
    fieldnames = [
        "level", "page_num", "block_num", "par_num", "line_num", "word_num",
        "left", "top", "width", "height", "conf", "text",
    ]

    rows = csv.DictReader(StringIO(tsv_text), delimiter="\t", fieldnames=fieldnames)
    data: dict[str, list[Any]] = {field: [] for field in fieldnames}

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


def image_to_data(image: Image.Image | np.ndarray, *, config: str) -> dict[str, list[Any]]:
    pil_image = _to_pil(image)
    with _backend_lock:
        forced_backend = _forced_backend

    if forced_backend == "pytesseract":
        return _pytesseract_image_to_data(pil_image, config=config)

    if tesserocr is None:
        if IS_FROZEN:
            raise RuntimeError("Packaged OCR requires the in-process tesserocr backend")
        return _pytesseract_image_to_data(pil_image, config=config)

    try:
        tsv_text = _tesserocr_image_to_tsv(pil_image, config=config)
    except Exception as exc:
        runtime_error_message = str(exc)
        with _backend_lock:
            _backend_stats["last_backend"] = "tesserocr"
            _backend_stats["tesserocr_fallback_runtime_errors"] += 1
            _backend_stats["last_tesserocr_error"] = runtime_error_message

        if IS_FROZEN:
            raise RuntimeError(f"Packaged in-process OCR failed: {runtime_error_message}") from exc
        return _pytesseract_image_to_data(pil_image, config=config)

    with _backend_lock:
        _backend_stats["last_backend"] = "tesserocr"
        _backend_stats["tesserocr_calls"] += 1
        _backend_stats["last_tesserocr_error"] = None

    return _tsv_to_data(tsv_text)


def get_backend_stats() -> dict[str, Any]:
    with _backend_lock:
        stats = dict(_backend_stats)
        stats["forced_backend"] = _forced_backend
        return stats


def set_forced_backend(backend: str | None) -> None:
    if backend not in {None, "pytesseract"}:
        raise ValueError("backend must be None or 'pytesseract'")
    if backend == "pytesseract" and IS_FROZEN:
        raise ValueError("the CLI OCR backend is available only in development/source runs")
    global _forced_backend
    with _backend_lock:
        _forced_backend = backend
