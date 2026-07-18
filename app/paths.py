from __future__ import annotations

import os
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT)).resolve()


def resource_path(*parts: str) -> Path:
    """Resolve a read-only resource in source and PyInstaller builds."""
    return BUNDLE_ROOT.joinpath(*parts)


def _app_data_root() -> Path:
    override = os.environ.get("SIGHTLINE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not IS_FROZEN:
        return SOURCE_ROOT / "data"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Sightline"
    return Path.home() / "AppData" / "Local" / "Sightline"


APP_DATA_ROOT = _app_data_root()
# SIGHTLINE_DATA_DIR and the packaged data location both point at the exact
# directory containing the SQLite database and exports.
DATA_DIR = APP_DATA_ROOT
LOG_DIR = APP_DATA_ROOT / "logs"
BACKUP_DIR = APP_DATA_ROOT / "backups"


def ensure_runtime_directories() -> None:
    for directory in (APP_DATA_ROOT, DATA_DIR, LOG_DIR, BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
