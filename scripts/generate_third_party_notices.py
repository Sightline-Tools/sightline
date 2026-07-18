from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import shutil
import sys

from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")
LICENSE_FILENAME_PATTERN = re.compile(
    r"^(?:licenses?|licences?|copying|notice)(?:$|[._-])",
    re.IGNORECASE,
)


def locked_packages(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LOCK_PATTERN.match(line)
        if match:
            packages[canonicalize_name(match.group(1))] = match.group(2)
    return packages


def distribution_url(distribution: metadata.Distribution) -> str:
    for entry in distribution.metadata.get_all("Project-URL") or []:
        label, separator, url = entry.partition(",")
        if separator and label.strip().lower() in {"homepage", "source", "repository"}:
            return url.strip()
    return distribution.metadata.get("Home-page") or ""


def is_license_filename(filename: str) -> bool:
    return bool(LICENSE_FILENAME_PATTERN.match(Path(filename).name))


def copy_license_files(distribution: metadata.Distribution, destination: Path) -> int:
    count = 0
    package_destination = destination / canonicalize_name(distribution.metadata["Name"])
    for file in distribution.files or []:
        filename = Path(str(file)).name
        if not is_license_filename(filename):
            continue
        source = Path(distribution.locate_file(file))
        if not source.is_file():
            continue
        package_destination.mkdir(parents=True, exist_ok=True)
        target = package_destination / Path(str(file)).name
        if not target.exists():
            shutil.copy2(source, target)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements.lock")
    parser.add_argument("--policy", type=Path, default=ROOT / "packaging" / "redistribution-licenses.json")
    parser.add_argument("--output", type=Path, default=ROOT / "THIRD-PARTY-NOTICES.md")
    parser.add_argument("--license-dir", type=Path, default=ROOT / "build" / "licenses")
    parser.add_argument("--native-manifest", type=Path, default=ROOT / "tesseract" / "native-components.json")
    parser.add_argument("--bundled-manifest", type=Path, default=ROOT / "build" / "bundled-components.json")
    args = parser.parse_args()

    locked = locked_packages(args.lock)
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    allowed = {canonicalize_name(name): license_id for name, license_id in policy["runtime"].items()}
    unreviewed = sorted(set(locked) - set(allowed))
    stale = sorted(set(allowed) - set(locked))
    if unreviewed or stale:
        raise SystemExit(f"Redistribution review mismatch; unreviewed={unreviewed}, stale={stale}")
    if any("proprietary" in license_id.lower() for license_id in allowed.values()):
        raise SystemExit("Proprietary dependency blocked by redistribution policy")

    if args.license_dir.exists():
        shutil.rmtree(args.license_dir)
    args.license_dir.mkdir(parents=True)
    rows: list[tuple[str, str, str, str]] = []
    missing_license_files: list[str] = []
    for name in sorted(locked):
        distribution = metadata.distribution(name)
        installed_version = distribution.version
        if installed_version != locked[name]:
            raise SystemExit(f"Installed {name} {installed_version} does not match lock {locked[name]}")
        if copy_license_files(distribution, args.license_dir) == 0:
            missing_license_files.append(name)
        rows.append((distribution.metadata["Name"], installed_version, allowed[name], distribution_url(distribution)))
    if missing_license_files:
        raise SystemExit(f"License text missing from installed distributions: {missing_license_files}")

    bundled_policy = policy.get("bundled", {})
    expected_bundled = {"CPython Windows runtime", "PyInstaller bootloader", "Tcl/Tk runtime"}
    if set(bundled_policy) != expected_bundled:
        raise SystemExit(
            f"Bundled-runtime redistribution review mismatch; expected={sorted(expected_bundled)}, "
            f"reviewed={sorted(bundled_policy)}"
        )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit(f"CPython license text is missing: {python_license}")
    python_license_destination = args.license_dir / "cpython" / "LICENSE.txt"
    python_license_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(python_license, python_license_destination)

    tk_license_candidates = sorted((Path(sys.base_prefix) / "tcl").glob("tk*/license.terms"))
    if not tk_license_candidates:
        raise SystemExit("Tcl/Tk runtime license text is missing from the CPython installation")
    tk_license_destination = args.license_dir / "tcl-tk" / "license.terms"
    tk_license_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tk_license_candidates[-1], tk_license_destination)
    tk_version = tk_license_candidates[-1].parent.name.removeprefix("tk")

    pyinstaller = metadata.distribution("pyinstaller")
    if copy_license_files(pyinstaller, args.license_dir) == 0:
        raise SystemExit("PyInstaller bootloader license text is missing")
    bundled_components = [
        {
            "name": "CPython Windows runtime",
            "version": platform.python_version(),
            "license": bundled_policy["CPython Windows runtime"],
            "source": "https://github.com/python/cpython",
        },
        {
            "name": "PyInstaller bootloader",
            "version": pyinstaller.version,
            "license": bundled_policy["PyInstaller bootloader"],
            "source": "https://github.com/pyinstaller/pyinstaller",
        },
        {
            "name": "Tcl/Tk runtime",
            "version": tk_version,
            "license": bundled_policy["Tcl/Tk runtime"],
            "source": "https://core.tcl-lang.org/tk/",
        },
    ]
    args.bundled_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.bundled_manifest.write_text(json.dumps(bundled_components, indent=2) + "\n", encoding="utf-8")

    output = [
        "# Sightline third-party notices",
        "",
        "This file is generated from `requirements.lock` and the reviewed redistribution policy. The corresponding license texts are bundled in Sightline's `licenses` directory.",
        "",
        "| Component | Version | License | Source |",
        "|---|---:|---|---|",
    ]
    for name, version, license_id, url in rows:
        source = f"[project]({url})" if url else "package metadata"
        output.append(f"| {name} | {version} | {license_id} | {source} |")
    output.extend(["", "## Bundled application runtime", "", "| Component | Version | License | Source |", "|---|---:|---|---|"])
    for component in bundled_components:
        output.append(
            f"| {component['name']} | {component['version']} | {component['license']} | "
            f"[source]({component['source']}) |"
        )
    output.extend(["", "## Native OCR components", "", "| Component | Version | License | Source |", "|---|---:|---|---|"])
    if args.native_manifest.is_file():
        native_components = json.loads(args.native_manifest.read_text(encoding="utf-8-sig"))
        for component in native_components:
            output.append(
                f"| {component['name']} | {component['version']} | {component['license']} | "
                f"[source]({component['source']}) |"
            )
    else:
        for name, license_id in policy["native"].items():
            output.append(f"| {name} | release-pinned | {license_id} | release build manifest |")
    output.extend(
        [
            "",
            "Sightline itself is licensed under AGPL-3.0-only. Inclusion in this notice is not an endorsement by any third-party author.",
            "",
        ]
    )
    args.output.write_text("\n".join(output), encoding="utf-8")
    print(f"Generated {args.output} and reviewed {len(rows)} locked Python distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
