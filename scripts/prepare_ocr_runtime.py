from __future__ import annotations

import argparse
from hashlib import sha256
from importlib import import_module
from importlib import metadata
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "Sightline-release-builder/1.0"


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _verified_download(url: str, expected_sha256: str, cache: dict[str, bytes]) -> bytes:
    content = cache.get(url)
    if content is None:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=120) as response:
            content = response.read()
        cache[url] = content
    actual = _digest(content)
    if actual != expected_sha256:
        raise RuntimeError(f"Downloaded artifact hash mismatch for {url}: {actual}")
    return content


def _artifact_content(artifact: dict[str, str], cache: dict[str, bytes]) -> bytes:
    downloaded = _verified_download(artifact["url"], artifact["sha256"], cache)
    member_name = artifact.get("archive_member")
    if member_name is None:
        return downloaded

    with tarfile.open(fileobj=BytesIO(downloaded), mode="r:*") as archive:
        member = archive.getmember(member_name)
        extracted = archive.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"Archive member is not a regular file: {member_name}")
        content = extracted.read()
    actual = _digest(content)
    if actual != artifact["content_sha256"]:
        raise RuntimeError(f"Archive member hash mismatch for {member_name}: {actual}")
    return content


def _destination(root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeError(f"Unsafe OCR runtime destination: {relative}")
    destination = root.joinpath(*relative_path.parts).resolve()
    if root.resolve() not in destination.parents:
        raise RuntimeError(f"OCR runtime destination escaped output directory: {relative}")
    return destination


def _installed_tesserocr_binaries(distribution: metadata.Distribution) -> set[str]:
    binaries: set[str] = set()
    for file in distribution.files or []:
        path = PurePosixPath(str(file).replace("\\", "/"))
        if path.suffix.lower() in {".dll", ".pyd", ".exe"}:
            binaries.add(path.as_posix())
    return binaries


def _validate_manifest(manifest: dict[str, Any], policy: dict[str, Any]) -> metadata.Distribution:
    if manifest.get("schema_version") != 1:
        raise RuntimeError("Unsupported tesserocr runtime manifest schema")

    wheel = manifest["wheel"]
    distribution = metadata.distribution(wheel["name"])
    if distribution.version != wheel["version"]:
        raise RuntimeError(
            f"Installed {wheel['name']} {distribution.version} does not match release-pinned {wheel['version']}"
        )
    linked_version = str(import_module("tesserocr").tesseract_version()).splitlines()[0]
    if not linked_version.startswith(f"tesseract {wheel['tesseract_version']}"):
        raise RuntimeError(
            f"Installed tesserocr links {linked_version!r}, expected Tesseract {wheel['tesseract_version']}"
        )

    expected_binaries = set(wheel["expected_binary_files"])
    installed_binaries = _installed_tesserocr_binaries(distribution)
    if installed_binaries != expected_binaries:
        raise RuntimeError(
            "Installed tesserocr binary payload differs from the reviewed wheel; "
            f"missing={sorted(expected_binaries - installed_binaries)}, "
            f"unexpected={sorted(installed_binaries - expected_binaries)}"
        )
    executables = sorted(path for path in installed_binaries if path.lower().endswith(".exe"))
    if executables:
        raise RuntimeError(f"The in-process OCR wheel unexpectedly contains executables: {executables}")

    components = manifest["native_components"]
    reviewed_native = {component["name"]: component["license"] for component in components}
    if reviewed_native != policy["native"]:
        raise RuntimeError(
            "Native OCR redistribution review differs from the runtime manifest; "
            f"manifest={reviewed_native}, policy={policy['native']}"
        )

    referenced_files = {file for component in components for file in component["files"]}
    data_files = {item["destination"] for item in manifest["data_files"]}
    main_extension = {
        path for path in expected_binaries if path.startswith("tesserocr/tesserocr.") and path.endswith(".pyd")
    }
    if referenced_files != (expected_binaries - main_extension) | data_files:
        raise RuntimeError("Native OCR component inventory does not exactly cover the reviewed binary and model files")
    return distribution


def prepare_runtime(manifest_path: Path, policy_path: Path, output: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest, policy)

    output = output.resolve()
    if output == ROOT.resolve() or ROOT.resolve() not in output.parents:
        raise RuntimeError(f"Refusing to replace OCR runtime outside the repository: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}-staging")
    backup = output.with_name(f".{output.name}-backup")
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        if output.exists():
            shutil.rmtree(backup)
        else:
            backup.replace(output)
    staging.mkdir()

    cache: dict[str, bytes] = {}
    try:
        for artifact in manifest["data_files"]:
            destination = _destination(staging, artifact["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_artifact_content(artifact, cache))

        native_manifest = []
        for component in manifest["native_components"]:
            artifact = component["license_artifact"]
            destination = _destination(staging, artifact["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_artifact_content(artifact, cache))
            native_manifest.append(
                {key: component[key] for key in ("name", "version", "license", "source", "files")}
            )

        (staging / "native-components.json").write_text(
            json.dumps(native_manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        executables = sorted(path for path in staging.rglob("*.exe") if path.is_file())
        if executables:
            raise RuntimeError(f"Prepared in-process OCR runtime unexpectedly contains executables: {executables}")

        if output.exists():
            output.replace(backup)
        try:
            staging.replace(output)
        except Exception:
            if backup.exists() and not output.exists():
                backup.replace(output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "packaging" / "tesserocr-runtime.json")
    parser.add_argument("--policy", type=Path, default=ROOT / "packaging" / "redistribution-licenses.json")
    parser.add_argument("--output", type=Path, default=ROOT / "tesseract")
    args = parser.parse_args()
    prepare_runtime(args.manifest, args.policy, args.output)
    print(f"Prepared verified in-process OCR runtime data in {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
