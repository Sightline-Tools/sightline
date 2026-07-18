from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def generate_icon(source: Path, destination: Path) -> set[tuple[int, int]]:
    image = Image.open(source).convert("RGBA")
    if image.width != image.height:
        raise ValueError("Sightline icon source must be square")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    with Image.open(destination) as generated:
        sizes = set(generated.ico.sizes())
    expected = {(size, size) for size in ICON_SIZES}
    if not expected.issubset(sizes):
        raise RuntimeError(f"Generated icon is missing layers: {sorted(expected - sizes)}")
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("brand/favicons/android-chrome-512x512.png"),
        help="Raster rendering of the canonical brand/favicons/favicon.svg artwork",
    )
    parser.add_argument("--output", type=Path, default=Path("build/Sightline.ico"))
    args = parser.parse_args()
    sizes = generate_icon(args.source, args.output)
    print(f"Generated {args.output} with {len(sizes)} layers from {args.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
