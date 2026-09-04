"""Validate the reviewed, versioned release notes used for public releases."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
PLACEHOLDER_PATTERNS = (
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"Publish reviewed Sightline source snapshot", re.IGNORECASE),
    re.compile(r"Synchronize public CI and release workflows", re.IGNORECASE),
    re.compile(r"sightline-gatekeeper\[bot\]", re.IGNORECASE),
)


def release_notes_path(root: Path, version: str) -> Path:
    if SEMANTIC_VERSION.fullmatch(version) is None:
        raise ValueError(f"Release version is not semantic: {version}")
    return root / "release-notes" / f"v{version}.md"


def validate_release_notes(path: Path, version: str) -> list[str]:
    errors: list[str] = []
    expected_name = f"v{version}.md"

    if path.name != expected_name:
        errors.append(f"release notes filename must be {expected_name}")
    if path.is_symlink():
        errors.append("release notes must not be a symbolic link")
        return errors
    if not path.is_file():
        errors.append(f"release notes are missing: release-notes/{expected_name}")
        return errors

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append("release notes must be UTF-8 text")
        return errors

    if not content.startswith("## Highlights\n"):
        errors.append("release notes must begin with a '## Highlights' section")
    if not any(line.startswith("- ") and line[2:].strip() for line in content.splitlines()):
        errors.append("release notes must contain at least one non-empty bullet")
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(content):
            errors.append(f"release notes contain forbidden placeholder or transport text: {pattern.pattern}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()

    root = args.root.resolve()
    try:
        path = release_notes_path(root, args.version)
    except ValueError as exc:
        parser.error(str(exc))

    errors = validate_release_notes(path, args.version)
    if errors:
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Validated release notes: release-notes/{path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
