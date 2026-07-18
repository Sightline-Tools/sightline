# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import subprocess
import sys

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks.tcl_tk import tcltk_info


ROOT = Path(SPECPATH).resolve().parent
BUILD = ROOT / "build"

# Always regenerate Windows version resources and bundled build information.
# These files are intentionally ignored, so reusing an older local copy can
# otherwise leak stale product names into an otherwise current executable.
subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "prepare_build_metadata.py")],
    cwd=ROOT,
    check=True,
)

if not tcltk_info.available:
    raise RuntimeError(
        "Sightline requires a complete CPython installation with Tcl/Tk. "
        "Install official CPython 3.12 with Tcl/Tk and rebuild; refusing to create a broken region selector."
    )

def required_file(path: Path, destination: str) -> tuple[str, str]:
    if not path.is_file():
        raise RuntimeError(f"Required release resource is missing: {path}")
    return str(path), destination


STATIC_DIR = ROOT / "app" / "static"
FONT_DIR = ROOT / "app" / "fonts"
MIGRATIONS_DIR = ROOT / "migrations"
STATIC_FILES = (
    "index.html",
    "encounters.html",
    "onboarding.html",
    "overlay.html",
    "replay.html",
    "settings.html",
    "sightline-shell.js",
    "sightline.css",
)
LOGO_FILES = (
    "sightline-logo-dark.svg",
    "sightline-logo-with-tagline-dark.svg",
)
FAVICON_FILES = (
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "apple-touch-icon.png",
    "favicon.ico",
    "favicon.svg",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "favicon-48x48.png",
    "site.webmanifest",
)
FONT_FILES = (
    "OpenDyslexic-Bold.otf",
    "OFL.txt",
    "OFL-FAQ.txt",
)

datas = [
    *(required_file(STATIC_DIR / filename, "app/static") for filename in STATIC_FILES),
    *(required_file(FONT_DIR / filename, "app/fonts") for filename in FONT_FILES),
    required_file(ROOT / "brand" / "web" / "sightline-tokens.css", "brand/web"),
    *(required_file(ROOT / "brand" / "logos" / "svg" / filename, "brand/logos/svg") for filename in LOGO_FILES),
    *(required_file(ROOT / "brand" / "favicons" / filename, "brand/favicons") for filename in FAVICON_FILES),
    required_file(MIGRATIONS_DIR / "env.py", "migrations"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "PRIVACY.md"), "."),
    (str(ROOT / "SECURITY.md"), "."),
    (str(ROOT / "CODE_SIGNING.md"), "."),
    (str(ROOT / "THIRD-PARTY-NOTICES.md"), "."),
    (str(ROOT / "build-info.json"), "."),
    (str(BUILD / "Sightline.ico"), "."),
]
for migration in sorted((MIGRATIONS_DIR / "versions").glob("*.py")):
    datas.append(required_file(migration, "migrations/versions"))

TESSERACT_DIR = ROOT / "tesseract"
if not TESSERACT_DIR.is_dir():
    raise RuntimeError("Required OCR runtime data is missing; run scripts/prepare_ocr_runtime.py before packaging")
datas.extend(
    [
        required_file(TESSERACT_DIR / "tessdata" / "eng.traineddata", "tesseract/tessdata"),
    ]
)
tesseract_license_files = sorted(path for path in (TESSERACT_DIR / "licenses").glob("*") if path.is_file())
if not tesseract_license_files:
    raise RuntimeError("Required Tesseract license texts are missing")
datas.extend(required_file(path, "tesseract/licenses") for path in tesseract_license_files)
if (BUILD / "licenses").is_dir():
    datas.append((str(BUILD / "licenses"), "licenses"))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("tesserocr")
    + ["pystray._win32", "tkinter", "_tkinter"]
)

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy._core._multiarray_tests", "pip", "pytesseract", "setuptools", "tzdata", "wheel"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sightline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="x86_64",
    icon=str(BUILD / "Sightline.ico"),
    version=str(BUILD / "version_info.txt"),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Sightline",
)
