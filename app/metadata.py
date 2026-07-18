from __future__ import annotations

import json
import os
import tomllib

from .paths import IS_FROZEN, SOURCE_ROOT, resource_path


PRODUCT_NAME = "Sightline"
PRODUCT_SUBTITLE = "Local-first combat parser"
SOURCE_URL = "https://github.com/Sightline-Tools/sightline"
LICENSE_ID = "AGPL-3.0-only"


def _project_version() -> str:
    pyproject = SOURCE_ROOT / "pyproject.toml"
    if not IS_FROZEN and pyproject.is_file():
        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    return "0.1.1"


def _build_info() -> dict[str, str]:
    path = resource_path("build-info.json")
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {str(key): str(item) for key, item in value.items()}


_BUILD_INFO = _build_info()
APP_VERSION = os.environ.get("SIGHTLINE_VERSION", _BUILD_INFO.get("version", _project_version()))
BUILD_COMMIT = os.environ.get("SIGHTLINE_BUILD_COMMIT", _BUILD_INFO.get("commit", "development"))


def app_info(*, debug_mode: bool = False) -> dict[str, str | bool]:
    build_commit_url = SOURCE_URL if BUILD_COMMIT == "development" else f"{SOURCE_URL}/commit/{BUILD_COMMIT}"
    return {
        "name": PRODUCT_NAME,
        "subtitle": PRODUCT_SUBTITLE,
        "version": APP_VERSION,
        "build_commit": BUILD_COMMIT,
        "build_commit_url": build_commit_url,
        "license": LICENSE_ID,
        "source_url": SOURCE_URL,
        "privacy_url": "/legal/privacy",
        "code_signing_url": "/legal/code-signing",
        "security_url": "/legal/security",
        "debug_mode": debug_mode,
        "packaged": IS_FROZEN,
    }
