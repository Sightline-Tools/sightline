from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def commit_id() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def numeric_version(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?", version)
    if not match:
        raise ValueError(f"Version is not semantic: {version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), 0


def version_resource(version: str, commit: str) -> str:
    numbers = numeric_version(version)
    dotted = ".".join(str(part) for part in numbers)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Sightline contributors'),
    StringStruct('FileDescription', 'Sightline — Local-first analysis'),
    StringStruct('FileVersion', '{dotted}'),
    StringStruct('InternalName', 'Sightline'),
    StringStruct('LegalCopyright', 'Copyright Sightline contributors; AGPL-3.0-only'),
    StringStruct('OriginalFilename', 'Sightline.exe'),
    StringStruct('ProductName', 'Sightline'),
    StringStruct('ProductVersion', '{version} ({commit})')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--commit", default=commit_id())
    args = parser.parse_args()
    version = project_version()
    if args.tag and args.tag != f"v{version}":
        raise SystemExit(f"Release tag {args.tag!r} does not match project version v{version}")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.commit):
        raise SystemExit("Build commit must be a Git hexadecimal object ID")
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build-info.json").write_text(
        json.dumps({"version": version, "commit": args.commit}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "build" / "version_info.txt").write_text(
        version_resource(version, args.commit), encoding="utf-8"
    )
    print(f"Prepared Sightline {version} metadata for {args.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
