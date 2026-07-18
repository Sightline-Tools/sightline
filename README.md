# Sightline

**Local-first combat parser**

Sightline is a local-only Windows combat parser. It reads a user-selected game-text rectangle with OCR, parses combat lines conservatively, shows live and historical encounters in a localhost browser UI, and provides optional floating combat text.

## Public beta installation

The supported public-beta platform is Windows 11 x64. Download `Sightline-Setup-x64-vX.Y.Z.exe` and `SHA256SUMS.txt` from this repository's GitHub Releases page, verify the SHA-256 checksum, and run the installer. Installation is per-user and does not request administrator access. Python, Git, Tesseract, and a terminal are not required.

The installer creates a Start-menu shortcut, can optionally create a desktop shortcut, and launches Sightline after installation. The application starts its loopback server, opens the default browser after a successful health check, and stays available from the tray icon. Tray actions open Sightline, open its data folder, show version information, and exit cleanly.

Current public-beta releases are intentionally unsigned while the project establishes a release history and user reputation. Windows will identify the publisher as unknown and Microsoft SmartScreen may warn before installation; this is expected, but users should verify the published checksum before continuing. SignPath signing is planned for a later release stage. Sightline does not implement automatic updates; installing a newer version upgrades in place and preserves data.

## First run and privacy boundary

The first-run wizard:

1. explains local storage and passive capture;
2. selects the combat region across DPI-scaled, multi-monitor Windows desktops;
3. runs one in-memory OCR test without persisting the screenshot;
4. offers to start parsing.

Sightline captures only the chosen rectangle. Optional foreground-application restriction can be enabled in Settings and limited to executable names the user explicitly adds. Sightline performs no memory reading, packet inspection, code injection, automation, telemetry, cloud processing, or account login.

Packaged data is stored under `%LOCALAPPDATA%\Sightline`; logs are in its `logs` directory. Application binaries are installed under `%LOCALAPPDATA%\Programs\Sightline`. Normal uninstall preserves user data. The final all-data removal choice defaults off.

See [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md), and [CODE_SIGNING.md](CODE_SIGNING.md).

## Developer setup

Sightline requires a complete official CPython 3.11 or newer installation with Tcl/Tk; release builds use Python 3.12. Minimal or embedded Python runtimes that omit Tcl/Tk cannot produce a working Sightline build. The lockfile contains the exact runtime dependency graph.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --requirement requirements.lock
python -m pip install --editable . --no-deps
python -m pip install pytest==8.4.2
python -m pytest -q
```

Source checkouts may use a system Tesseract installation for development. Signed builds instead bundle the verified Tesseract 5.5.2 x64 runtime and a pinned `tessdata_fast` English model, and never consult system `PATH`.

Run the production-style launcher:

```powershell
python -m app.launcher
```

For browser-focused development without the launcher or tray, use the source runner. It enables loopback-only source access, checks that runtime dependencies such as Alembic are installed, and reloads on code changes by default:

```powershell
python -m app.dev
```

The traditional Uvicorn command is also supported directly from a source checkout:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

If either command reports a missing module, activate the intended virtual environment and run `python -m pip install --editable .`. Use `python -m app.dev --no-reload` for a stable long-running source process. Source mode accepts unauthenticated API requests only from the local computer; packaged and production-style launcher runs retain the cookie handshake.

`SIGHTLINE_DATA_DIR` overrides the repository `data` directory for tests or developer profiles. Packaged resources are resolved independently of the process working directory.

## Desktop and API security

The server binds only to `127.0.0.1`, preferring port 8765 and falling back to an available loopback port. A random secret is created on every launch and established as an HttpOnly, SameSite cookie through the launch handshake. Every `/api/*` route except `/api/health` requires that cookie, and cross-origin browser requests are rejected.

Public application endpoints added for distribution are:

- `GET /api/health` — minimal launcher health response.
- `GET /api/app-info` — version, commit, license, source, and Debug Mode state.
- `POST /api/setup/test-ocr` — transient one-frame OCR verification.
- `POST /api/diagnostics/export` — privacy-safe support ZIP.

Replay navigation and APIs are available only while Debug Mode is enabled. Turning Debug Mode off stops an active replay, and replay endpoints then return `403`.

The launcher also supports internal `--smoke-test`, `--no-browser`, and `--shutdown` switches for release acceptance and installer upgrades.

## Building a release

The tag-driven Windows workflow in `.github/workflows/release.yml` is the authoritative unsigned public-beta build. A tag must exactly match the semantic version in `pyproject.toml` (for example, `v0.1.0`). The workflow:

- installs `requirements.lock` and runs the complete tests;
- generates a nine-layer Windows icon from `brand/favicons/android-chrome-512x512.png`, a raster rendering of the canonical `brand/favicons/favicon.svg` artwork;
- builds Tesseract 5.5.2 from its pinned official source commit and checks out a pinned official English model;
- produces a PyInstaller one-folder application and per-user Inno Setup installer;
- explicitly verifies that the application, uninstaller, and installer are unsigned;
- verifies install, OCR, authentication, second launch, port fallback, shutdown, upgrade, and both uninstall paths;
- scans with Microsoft Defender and emits SHA-256 checksums, a CycloneDX SBOM, reviewed third-party notices, and GitHub build provenance;
- publishes the installer with an explicit unsigned-release notice as a GitHub prerelease after protected-environment approval.

The transition plan for a later signed release is documented in [packaging/SIGNPATH.md](packaging/SIGNPATH.md). Windows 11 clean-machine acceptance, Windows Defender review, redistribution review, checksums, SBOM generation, and build provenance remain release gates. Portable ZIP releases are not published.

## Static demo

Regenerate the branded no-backend demo after interface changes:

```powershell
python demo/build_demo_site.py
python -m http.server 4173 --directory demo-site
```

## License

Sightline is licensed under [AGPL-3.0-only](LICENSE). Reviewed dependency licenses are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
