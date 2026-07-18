from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sbom", type=Path, default=Path("build/Sightline.cdx.json"))
    parser.add_argument("--native-manifest", type=Path, default=Path("tesseract/native-components.json"))
    parser.add_argument("--bundled-manifest", type=Path, default=Path("build/bundled-components.json"))
    args = parser.parse_args()
    sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
    additional = []
    for manifest_path in (args.bundled_manifest, args.native_manifest):
        additional.extend(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
    components = sbom.setdefault("components", [])
    existing_refs = {component.get("bom-ref") for component in components}
    added = 0
    for item in additional:
        name = str(item["name"])
        version = str(item["version"])
        purl = f"pkg:generic/{quote(name, safe='')}@{quote(version, safe='')}"
        if purl in existing_refs:
            continue
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "licenses": [{"expression": str(item["license"])}],
                "purl": purl,
                "externalReferences": [{"type": "vcs", "url": str(item["source"])}],
            }
        )
        existing_refs.add(purl)
        added += 1
    components.sort(key=lambda component: (component.get("name", "").lower(), component.get("version", "")))
    args.sbom.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Added {added} bundled runtime and native OCR components to {args.sbom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
