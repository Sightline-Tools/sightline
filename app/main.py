from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .capture import CaptureRegion, capture_region
from .config import DEFAULT_ALLOWED_FOCUS_EXECUTABLES, DATA_DIR, RuntimeSettings, normalize_executable_names
from .charm import (
    CHARM_SIGNAL_ABILITIES,
    canonical_actor_key,
    is_probable_npc_actor,
    is_tagged_charmed_pet,
    window_is_active_for_event,
)
from .db import (
    CharmWindow,
    Encounter,
    EncounterActorVisibility,
    Event,
    OCRLine,
    ParserSession,
    SessionLogLine,
    get_session,
    init_db,
)
from .encounter_engine import COMBAT_ACTIVITY_CATEGORIES, EncounterEngineConfig, encounter_engine
from .floating_combat_text import floating_text_controller
from .log_compare import (
    CompareOptions,
    compare_session_to_expected,
    load_expected_lines,
    maybe_write_compare_artifact,
)
from .local_auth import COOKIE_NAME, LocalAPISecurityMiddleware, create_session_secret, valid_secret
from .metadata import APP_VERSION, PRODUCT_NAME, PRODUCT_SUBTITLE, app_info
from .ocr import run_ocr
from .paths import APP_DATA_ROOT, IS_FROZEN, LOG_DIR, resource_path
from .replay import ReplayConfig, ReplayRunner
from .region_selector import select_region
from .service import service
from .tesseract_backend import get_backend_stats
from .time_utils import utcnow


logger = logging.getLogger(__name__)


class SettingsDTO(BaseModel):
    capture_x: int = 0
    capture_y: int = 0
    capture_width: int = 800
    capture_height: int = 300
    combat_region_configured: bool = False
    onboarding_completed: bool = False
    capture_interval_ms: int = 500
    inactivity_timeout_s: int = 15
    encounter_inactivity_timeout_s: int = 5
    debug_mode: bool = False
    require_game_focus: bool = False
    increase_accuracy: bool = False
    allowed_focus_executables: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_FOCUS_EXECUTABLES))
    manual_end_hotkey_enabled: bool = False
    manual_end_hotkey_binding: str = "Shift+F3"
    encounter_filter_mode: str = "none"
    encounter_party_members: list[dict[str, str | None] | str] = Field(default_factory=list)
    overlay_monitor_index: int = 0
    overlay_anchor_x_ratio: float = 0.5
    overlay_anchor_y_ratio: float = 0.5
    overlay_offset_x: int = 0
    overlay_offset_y: int = 0
    overlay_damage_out_offset_x: int = 90
    overlay_damage_in_offset_x: int = -90
    overlay_heal_in_offset_y: int = 70
    overlay_font_size: int = 20
    overlay_time_to_fade_s: float = 1.0
    overlay_event_spacing: int = 18
    overlay_combine_multi_hit: bool = True
    ocr_passes_enabled: list[str] = Field(default_factory=lambda: ["whole_threshold"])
    visible_columns: list[str] = Field(default_factory=list)

    @field_validator("allowed_focus_executables", mode="before")
    @classmethod
    def normalize_allowed_focus_executables(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return normalize_executable_names(value)
        if not isinstance(value, list) or not all(isinstance(executable, str) for executable in value):
            raise ValueError("allowed_focus_executables must be a string or a list of strings")
        return normalize_executable_names(value)

class SplitRequest(BaseModel):
    event_id: int


class MergeRequest(BaseModel):
    encounter_ids: list[int]


class ActorVisibilityRequest(BaseModel):
    actor_name: str
    hidden: bool


class ReplayStartRequest(BaseModel):
    start_image_path: str
    end_index: int | None = None
    frame_delay_ms: int | None = None
    repeat_count: int = Field(default=1, ge=1, le=1000)
    force_pytesseract: bool = False



replay_runner = ReplayRunner(service)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    service.load_settings()
    try:
        yield
    finally:
        replay_runner.stop()
        service.stop()
        floating_text_controller.shutdown()


STATIC_DIR = resource_path("app", "static")
BRAND_DIR = resource_path("brand")
FAVICON_DIR = BRAND_DIR / "favicons"
SESSION_SECRET = create_session_secret()


def _is_direct_source_server() -> bool:
    """Identify intentional source-mode servers without weakening packaged runs."""
    if IS_FROZEN:
        return False
    if os.environ.get("SIGHTLINE_SOURCE_SERVER") == "1":
        return True
    return "app.main:app" in sys.argv


DIRECT_SOURCE_SERVER = _is_direct_source_server()


def _ui_file(filename: str, *, media_type: str | None = None) -> FileResponse:
    response = FileResponse(STATIC_DIR / filename, media_type=media_type)
    response.headers["Cache-Control"] = "no-store"
    return response


app = FastAPI(
    title=PRODUCT_NAME,
    description=PRODUCT_SUBTITLE,
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.session_secret = SESSION_SECRET
app.add_middleware(
    LocalAPISecurityMiddleware,
    session_secret=SESSION_SECRET,
    allow_unauthenticated_loopback=DIRECT_SOURCE_SERVER,
)
app.mount("/brand/web", StaticFiles(directory=str(BRAND_DIR / "web")), name="brand-web")
app.mount("/brand/logos", StaticFiles(directory=str(BRAND_DIR / "logos")), name="brand-logos")
app.mount("/brand/favicons", StaticFiles(directory=str(FAVICON_DIR)), name="brand-favicons")
app.mount("/favicons", StaticFiles(directory=str(FAVICON_DIR)), name="favicons")


@app.get("/static/sightline.css", include_in_schema=False)
def sightline_stylesheet() -> FileResponse:
    return _ui_file("sightline.css", media_type="text/css")


@app.get("/static/sightline-shell.js", include_in_schema=False)
def sightline_shell_script() -> FileResponse:
    return _ui_file("sightline-shell.js", media_type="text/javascript")


def _format_log_timestamp(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def _resolve_allowed_expected_path(expected_file_path: str) -> Path:
    expected_path = Path(expected_file_path).expanduser().resolve()
    allowed_dir = (DATA_DIR / "exports").resolve()
    if not expected_path.is_relative_to(allowed_dir):
        raise HTTPException(
            status_code=400,
            detail="expected_file_path must be under data/exports",
        )
    return expected_path


@app.get("/")
def root() -> Response:
    if not service.load_settings().onboarding_completed:
        return RedirectResponse(url="/onboarding", status_code=303)
    return _ui_file("index.html")


@app.get("/launch", include_in_schema=False)
def launch(token: str = Query(...)) -> RedirectResponse:
    if not valid_secret(token, SESSION_SECRET):
        raise HTTPException(status_code=403, detail="Invalid or expired Sightline launch token")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(COOKIE_NAME, SESSION_SECRET, httponly=True, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(FAVICON_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/site.webmanifest", include_in_schema=False)
def site_manifest() -> FileResponse:
    return FileResponse(FAVICON_DIR / "site.webmanifest", media_type="application/manifest+json")


@app.get("/legal/license", include_in_schema=False)
def license_document() -> FileResponse:
    return FileResponse(resource_path("LICENSE"), media_type="text/plain")


@app.get("/legal/privacy", include_in_schema=False)
def privacy_document() -> FileResponse:
    return FileResponse(resource_path("PRIVACY.md"), media_type="text/markdown")


@app.get("/legal/security", include_in_schema=False)
def security_document() -> FileResponse:
    return FileResponse(resource_path("SECURITY.md"), media_type="text/markdown")


@app.get("/legal/code-signing", include_in_schema=False)
def code_signing_document() -> FileResponse:
    return FileResponse(resource_path("CODE_SIGNING.md"), media_type="text/markdown")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}




@app.get("/replay")
def replay_page() -> FileResponse:
    if not service.load_settings().debug_mode:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "no-store"})
    return _ui_file("replay.html")


@app.get("/encounters")
def encounters_page() -> FileResponse:
    return _ui_file("encounters.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return _ui_file("settings.html")


@app.get("/onboarding")
def onboarding_page() -> FileResponse:
    return _ui_file("onboarding.html")


@app.get("/overlay")
def overlay_page() -> FileResponse:
    return _ui_file("overlay.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    settings = service.load_settings()
    return {**service.status(), "settings": settings.__dict__, "overlay": service.overlay_status()}


@app.get("/api/app-info")
def get_app_info(response: Response) -> dict[str, str | bool]:
    response.headers["Cache-Control"] = "no-store"
    return app_info(debug_mode=service.load_settings().debug_mode)


@app.post("/api/setup/test-ocr")
def test_setup_ocr() -> dict[str, Any]:
    settings = service.load_settings()
    if not settings.combat_region_configured:
        raise HTTPException(status_code=409, detail="Select a combat region before testing OCR")
    image = capture_region(
        CaptureRegion(
            x=settings.capture_x,
            y=settings.capture_y,
            width=settings.capture_width,
            height=settings.capture_height,
        )
    )
    timings: dict[str, float] = {}
    result = run_ocr(
        image,
        enabled_passes=set(settings.ocr_passes_enabled or ["whole_threshold"]),
        increase_accuracy=settings.increase_accuracy,
        timings=timings,
    )
    return {
        "ok": True,
        "recognized_lines": [line for line, _confidence in result.lines],
        "pass_names": result.pass_names,
        "timings_ms": timings,
        "backend": get_backend_stats(),
    }


def _diagnostic_settings(settings: RuntimeSettings) -> dict[str, Any]:
    return {
        "combat_region_configured": settings.combat_region_configured,
        "onboarding_completed": settings.onboarding_completed,
        "capture_interval_ms": settings.capture_interval_ms,
        "inactivity_timeout_s": settings.inactivity_timeout_s,
        "encounter_inactivity_timeout_s": settings.encounter_inactivity_timeout_s,
        "debug_mode": settings.debug_mode,
        "require_game_focus": settings.require_game_focus,
        "increase_accuracy": settings.increase_accuracy,
        "allowed_focus_executable_count": len(settings.allowed_focus_executables),
        "ocr_passes_enabled": settings.ocr_passes_enabled,
        "overlay_monitor_index": settings.overlay_monitor_index,
    }


def _sanitized_service_status() -> dict[str, Any]:
    value = service.status()
    return {
        "running": bool(value.get("running")),
        "last_successful_ocr_at": value.get("last_successful_ocr_at"),
        "last_skip_reason": value.get("last_skip_reason"),
        "last_error_present": bool(value.get("last_error")),
        "known_actor_count": len(value.get("known_actors") or []),
    }


def _sanitized_backend_status() -> dict[str, Any]:
    value = get_backend_stats()
    return {
        "last_backend": value.get("last_backend"),
        "tesserocr_calls": value.get("tesserocr_calls", 0),
        "pytesseract_calls": value.get("pytesseract_calls", 0),
        "tesserocr_fallback_runtime_errors": value.get("tesserocr_fallback_runtime_errors", 0),
        "last_tesserocr_error_present": bool(value.get("last_tesserocr_error")),
        "forced_backend": value.get("forced_backend"),
    }


def _sanitize_log_text(value: str) -> str:
    replacements = {
        str(Path.home()): "<user-profile>",
        str(APP_DATA_ROOT): "<sightline-data>",
    }
    result = value
    for sensitive, replacement in replacements.items():
        if sensitive:
            result = result.replace(sensitive, replacement).replace(sensitive.replace("\\", "/"), replacement)
    result = re.sub(r"(?i)\b[A-Z]:[\\/][^\r\n\t\"]+", "<path>", result)
    return result


@app.post("/api/diagnostics/export")
def export_diagnostics() -> Response:
    output = BytesIO()
    settings = service.load_settings()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary = {
            "application": app_info(debug_mode=settings.debug_mode),
            "settings": _diagnostic_settings(settings),
            "service": _sanitized_service_status(),
            "ocr_backend": _sanitized_backend_status(),
        }
        archive.writestr("diagnostics.json", json.dumps(summary, indent=2, sort_keys=True, default=str))
        if LOG_DIR.is_dir():
            for log_path in sorted(LOG_DIR.glob("*.log*")):
                if not log_path.is_file():
                    continue
                log_text = log_path.read_text(encoding="utf-8", errors="replace")[-1_000_000:]
                archive.writestr(f"logs/{log_path.name}", _sanitize_log_text(log_text))
        archive.writestr(
            "PRIVACY.txt",
            "This bundle excludes the Sightline database, OCR screenshots, encounter text, exports, and capture coordinates.\n",
        )
    filename = f"Sightline-Diagnostics-{APP_VERSION}.zip"
    return Response(
        content=output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _require_debug_mode() -> None:
    if not service.load_settings().debug_mode:
        raise HTTPException(status_code=403, detail="Enable Debug Mode in Sightline Settings to use the Replay Harness")


@app.post("/api/parser/start")
def start_parser() -> dict[str, str]:
    if replay_runner.status().get("running"):
        raise HTTPException(status_code=409, detail="Replay is running; stop replay before starting live parser capture")
    if not service.load_settings().combat_region_configured:
        raise HTTPException(status_code=409, detail="Select a combat region before starting the parser")
    service.start()
    return {"status": "started"}


@app.post("/api/parser/stop")
def stop_parser() -> dict[str, str]:
    service.stop()
    return {"status": "stopped"}


@app.post("/api/overlay/start")
def start_overlay() -> dict[str, Any]:
    service.start_overlay()
    return {"status": "started", "overlay": service.overlay_status()}


@app.post("/api/overlay/stop")
def stop_overlay() -> dict[str, Any]:
    service.stop_overlay()
    return {"status": "stopped", "overlay": service.overlay_status()}


@app.get("/api/overlay/status")
def overlay_status() -> dict[str, bool]:
    return service.overlay_status()


@app.post("/api/replay/start")
def start_replay(payload: ReplayStartRequest) -> dict[str, str]:
    _require_debug_mode()
    if service.status()["running"]:
        raise HTTPException(status_code=409, detail="Live parser capture is running; stop it before replay")
    replay_runner.start(ReplayConfig(**payload.model_dump()))
    return {"status": "started"}


@app.post("/api/replay/stop")
def stop_replay() -> dict[str, str]:
    _require_debug_mode()
    replay_runner.stop()
    return {"status": "stopped"}


@app.get("/api/replay/status")
def replay_status() -> dict[str, Any]:
    _require_debug_mode()
    return replay_runner.status()


@app.post("/api/replay/compare")
async def compare_replay_output(
    session_id: int = Form(...),
    expected_file_path: str | None = Form(default=None),
    strict: bool = Form(default=False),
    include_metadata: bool = Form(default=False),
    write_artifact: bool = Form(default=False),
    expected_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _require_debug_mode()
    parser_session = db.get(ParserSession, session_id)
    if parser_session is None:
        raise HTTPException(status_code=404, detail="Parser session not found")

    expected_lines: list[str]
    if expected_file is not None:
        expected_text = (await expected_file.read()).decode("utf-8")
        expected_lines = [line.strip() for line in expected_text.splitlines()]
    elif expected_file_path:
        safe_expected_path = _resolve_allowed_expected_path(expected_file_path)
        expected_lines = load_expected_lines(safe_expected_path)
    else:
        raise HTTPException(status_code=400, detail="Provide expected_file_path or expected_file upload")

    options = CompareOptions(strict=strict, include_metadata=include_metadata)
    result = compare_session_to_expected(
        db,
        parser_session_id=session_id,
        expected_lines=expected_lines,
        options=options,
    )

    artifact_path = None
    if write_artifact:
        artifact_path = maybe_write_compare_artifact(parser_session_id=session_id, payload=result)

    return {
        "session_id": session_id,
        "options": {
            "strict": strict,
            "include_metadata": include_metadata,
        },
        **result,
        "artifact_path": artifact_path,
    }


def _export_session_log_file(db: Session, parser_session: ParserSession) -> Path:
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    output_path = export_dir / f"parser_session_{parser_session.id}.log"
    lines = db.scalars(
        select(SessionLogLine)
        .where(SessionLogLine.parser_session_id == parser_session.id)
        .order_by(SessionLogLine.id.asc())
    ).all()
    with output_path.open("w", encoding="utf-8") as handle:
        for line in lines:
            timestamp = _format_log_timestamp(line.received_at)
            handle.write(f"[{timestamp}] {line.raw_text}\n")
    return output_path


@app.get("/api/parser/sessions")
def list_parser_sessions(db: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = db.execute(
        select(ParserSession, func.count(SessionLogLine.id).label("line_count"))
        .outerjoin(SessionLogLine, SessionLogLine.parser_session_id == ParserSession.id)
        .group_by(ParserSession.id)
        .order_by(ParserSession.started_at.desc(), ParserSession.id.desc())
    ).all()
    return [
        {
            "session_id": parser_session.id,
            "started_at": parser_session.started_at.isoformat(),
            "ended_at": parser_session.ended_at.isoformat() if parser_session.ended_at else None,
            "active": parser_session.active,
            "file_path": parser_session.file_path,
            "line_count": line_count,
            "duration_seconds": max(
                ((parser_session.ended_at or utcnow()) - parser_session.started_at).total_seconds(),
                0,
            ),
        }
        for parser_session, line_count in rows
    ]


@app.get("/api/parser/sessions/current")
def get_current_parser_session(db: Session = Depends(get_session)) -> dict[str, Any]:
    current = db.scalars(
        select(ParserSession).order_by(ParserSession.id.desc())
    ).first()
    if current is None:
        current = db.scalars(select(ParserSession).order_by(ParserSession.id.desc())).first()
    if current is None:
        return {"session": None, "log_lines": []}

    payload = get_parser_session(current.id, db)
    return {
        "session": {
            "session_id": payload["id"],
            "started_at": payload["started_at"],
            "ended_at": payload["ended_at"],
            "active": payload["active"],
            "file_path": payload["file_path"],
        },
        "log_lines": payload["log_lines"],
    }


@app.get("/api/parser/sessions/current/summary")
def get_current_parser_session_summary(db: Session = Depends(get_session)) -> dict[str, Any]:
    current = db.scalars(
        select(ParserSession).order_by(ParserSession.id.desc())
    ).first()
    if current is None:
        current = db.scalars(select(ParserSession).order_by(ParserSession.id.desc())).first()
    if current is None:
        return {"session": None, "line_count": 0}

    line_count = db.scalar(
        select(func.count(SessionLogLine.id)).where(SessionLogLine.parser_session_id == current.id)
    ) or 0

    return {
        "session": {
            "session_id": current.id,
            "started_at": current.started_at.isoformat(),
            "ended_at": current.ended_at.isoformat() if current.ended_at else None,
            "active": current.active,
            "file_path": current.file_path,
        },
        "line_count": line_count,
    }


@app.get("/api/parser/sessions/{session_id}")
def get_parser_session(session_id: int, db: Session = Depends(get_session)) -> dict[str, Any]:
    parser_session = db.get(ParserSession, session_id)
    if parser_session is None:
        raise HTTPException(status_code=404, detail="Parser session not found")
    lines = db.scalars(
        select(SessionLogLine)
        .where(SessionLogLine.parser_session_id == parser_session.id)
        .order_by(SessionLogLine.id.asc())
    ).all()
    return {
        "id": parser_session.id,
        "started_at": parser_session.started_at.isoformat(),
        "ended_at": parser_session.ended_at.isoformat() if parser_session.ended_at else None,
        "active": parser_session.active,
        "file_path": parser_session.file_path,
        "log_lines": [
            {
                "id": line.id,
                "received_at": line.received_at.isoformat(),
                "timestamp": _format_log_timestamp(line.received_at),
                "raw_text": line.raw_text,
                "normalized_text": line.normalized_text,
                "ocr_confidence": line.ocr_confidence,
                "parse_confidence": line.parse_confidence,
                "parse_outcome": line.parse_outcome,
                "parse_category": line.parse_category,
                "source_actor": line.source_actor,
                "target_actor": line.target_actor,
                "ability_name": line.ability_name,
                "amount": line.amount,
                "result": line.result,
                "attack_verb": line.attack_verb,
                "effect_name": line.effect_name,
            }
            for line in lines
        ],
    }


@app.get("/api/parser/sessions/current/export")
def export_current_parser_session(db: Session = Depends(get_session)) -> FileResponse:
    current = db.scalars(
        select(ParserSession).order_by(ParserSession.id.desc())
    ).first()
    if current is None:
        current = db.scalars(select(ParserSession).order_by(ParserSession.id.desc())).first()
    if current is None:
        raise HTTPException(status_code=404, detail="No parser sessions found")
    path = _export_session_log_file(db, current)
    return FileResponse(str(path), media_type="text/plain", filename=path.name)


@app.post("/api/parser/sessions/{session_id}/export")
def export_selected_parser_session(session_id: int, db: Session = Depends(get_session)) -> FileResponse:
    parser_session = db.get(ParserSession, session_id)
    if parser_session is None:
        raise HTTPException(status_code=404, detail="Parser session not found")
    path = _export_session_log_file(db, parser_session)
    return FileResponse(str(path), media_type="text/plain", filename=path.name)


@app.get("/api/settings", response_model=SettingsDTO)
def get_settings() -> SettingsDTO:
    return SettingsDTO(**service.load_settings().__dict__)


@app.put("/api/settings", response_model=SettingsDTO)
def put_settings(payload: SettingsDTO) -> SettingsDTO:
    previous_debug_mode = service.load_settings().debug_mode
    settings = RuntimeSettings(**payload.model_dump())
    saved = service.save_settings(settings)
    if previous_debug_mode and not saved.debug_mode:
        replay_runner.stop()
    return SettingsDTO(**saved.__dict__)


@app.post("/api/settings/select-region", response_model=SettingsDTO)
def choose_region() -> SettingsDTO:
    try:
        region = select_region()
    except Exception as exc:
        logger.exception("Region selector failed")
        raise HTTPException(
            status_code=503,
            detail=(
                "Sightline could not open the region selector. Restart Sightline and try again. "
                "If the problem continues, export a support bundle from Settings."
            ),
        ) from exc
    if region is None:
        raise HTTPException(status_code=400, detail="Region selection cancelled")
    settings = service.load_settings()
    settings.capture_x = region.x
    settings.capture_y = region.y
    settings.capture_width = region.width
    settings.capture_height = region.height
    settings.combat_region_configured = True
    saved = service.save_settings(settings)
    return SettingsDTO(**saved.__dict__)


def _encounter_summary(encounter: Encounter) -> dict[str, Any]:
    duration = max(((encounter.ended_at or utcnow()) - encounter.started_at).total_seconds(), 0)
    base_label = encounter.label or encounter.started_at.strftime('%H:%M:%S')
    display_label = f"{base_label} (merged)" if encounter.ended_reason == "merged" else base_label
    return {
        "id": encounter.id,
        "label": display_label,
        "started_at": encounter.started_at.isoformat(),
        "ended_at": encounter.ended_at.isoformat() if encounter.ended_at else None,
        "last_activity_at": (encounter.last_activity_at or encounter.started_at).isoformat(),
        "active": encounter.active,
        "duration_seconds": duration,
        "ended_reason": encounter.ended_reason,
        "capture_filter_mode": getattr(encounter, "capture_filter_mode", None) or "none",
    }


def _duration_seconds(encounter: Encounter, events: list[Event]) -> float:
    duration_events = [ev for ev in events if ev.category not in {"charm_start", "charm_end"}]
    if duration_events:
        return max(
            (max(ev.received_at for ev in duration_events) - min(ev.received_at for ev in duration_events)).total_seconds(),
            1.0,
        )
    return max(((encounter.ended_at or utcnow()) - encounter.started_at).total_seconds(), 1.0)


def _is_npc_vs_npc_damage_event(ev: Event) -> bool:
    return (
        ev.category in {"damage_out", "damage_in", "damage"}
        and is_probable_npc_actor(ev.source_actor)
        and is_probable_npc_actor(ev.target_actor)
    )


def _event_follows_signal(
    ev: Event,
    signals: list[Any],
    *,
    lookback_seconds: int = 15,
) -> bool:
    earliest = ev.received_at - timedelta(seconds=lookback_seconds)
    for signal in signals:
        if signal.parser_session_id != ev.parser_session_id:
            continue
        if signal.received_at < earliest or signal.received_at > ev.received_at:
            continue
        if signal.received_at == ev.received_at:
            signal_line_id = getattr(signal, "session_log_line_id", None) or getattr(signal, "id", None)
            if (
                ev.session_log_line_id is not None
                and signal_line_id is not None
                and signal_line_id >= ev.session_log_line_id
            ):
                continue
        if (
            (getattr(signal, "parse_category", None) or getattr(signal, "category", None)) == "cast_start"
            and (signal.ability_name or "").strip().lower() in CHARM_SIGNAL_ABILITIES
        ):
            return True
    return False


def _charm_attribution_for_encounter(
    events: list[Event],
    duration: float,
    *,
    charm_windows: list[CharmWindow] | None = None,
    charm_signals: list[SessionLogLine] | None = None,
) -> dict[str, Any]:
    """Classify Charm damage without treating weak NPC combat as a point estimate."""

    charm_windows = charm_windows or []
    charm_signals = list(charm_signals or [])
    charm_signals.extend(
        event
        for event in events
        if event.category == "cast_start"
        and (event.ability_name or "").strip().lower() in CHARM_SIGNAL_ABILITIES
    )
    window_session_ids = {window.parser_session_id for window in charm_windows}
    active_local_windows: dict[tuple[int | None, str], int] = {}
    per_charmed_actor: dict[str, dict[str, Any]] = {}
    potential_by_actor: dict[str, dict[str, Any]] = {}

    def actor_scope(ev: Event, actor_name: str) -> tuple[int | None, str]:
        return ev.parser_session_id, canonical_actor_key(actor_name)

    def ensure_charmed_actor(name: str) -> dict[str, Any]:
        actor_key = canonical_actor_key(name)
        row = per_charmed_actor.setdefault(
            actor_key,
            {
                "actor": name,
                "confirmed": 0,
                "ambiguous": 0,
                "tagged": 0,
                "active_window": 0,
                "same_name_collision": 0,
            },
        )
        if is_tagged_charmed_pet(name):
            row["actor"] = name
        return row

    def ensure_potential_actor(name: str) -> dict[str, Any]:
        actor_key = canonical_actor_key(name)
        return potential_by_actor.setdefault(
            actor_key,
            {
                "actor": name,
                "candidate": 0,
                "recent_signal": 0,
                "weak_candidate": 0,
            },
        )

    def has_active_window(ev: Event, source_actor: str) -> bool:
        if ev.parser_session_id in window_session_ids:
            return any(
                window.parser_session_id == ev.parser_session_id
                and window.actor_key == canonical_actor_key(source_actor)
                and window_is_active_for_event(window, ev)
                for window in charm_windows
            )
        return active_local_windows.get(actor_scope(ev, source_actor), 0) > 0

    ordered_events = sorted(
        enumerate(events),
        key=lambda item: (
            item[1].received_at,
            item[1].id if item[1].id is not None else item[0],
        ),
    )
    for _, ev in ordered_events:
        if ev.category == "charm_start":
            if ev.target_actor:
                scope = actor_scope(ev, ev.target_actor)
                active_local_windows[scope] = active_local_windows.get(scope, 0) + 1
            continue

        if ev.category == "charm_end":
            if ev.target_actor:
                scope = actor_scope(ev, ev.target_actor)
                active_local_windows[scope] = max(active_local_windows.get(scope, 0) - 1, 0)
            continue

        if ev.category not in {"damage_out", "damage_in", "damage"}:
            continue
        if not ev.source_actor or not ev.target_actor or ev.amount is None:
            continue

        source_is_tagged = is_tagged_charmed_pet(ev.source_actor)
        if not source_is_tagged and is_tagged_charmed_pet(ev.target_actor):
            # The raw log explicitly identifies the target as the owned Charm pet,
            # so the untagged same-name source is the hostile actor.
            continue
        source_has_window = has_active_window(ev, ev.source_actor)
        if source_has_window and not source_is_tagged and not is_probable_npc_actor(ev.target_actor):
            # A plain same-name NPC attacking a player is hostile activity, not
            # evidence that the charmed instance dealt the damage.
            continue
        if not source_is_tagged and not source_has_window:
            if not is_tagged_charmed_pet(ev.target_actor) and _is_npc_vs_npc_damage_event(ev):
                potential = ensure_potential_actor(ev.source_actor)
                potential["candidate"] += ev.amount
                if _event_follows_signal(ev, charm_signals):
                    potential["recent_signal"] += ev.amount
                else:
                    potential["weak_candidate"] += ev.amount
            continue

        charm_row = ensure_charmed_actor(ev.source_actor)
        if source_is_tagged:
            charm_row["confirmed"] += ev.amount
            charm_row["tagged"] += ev.amount
        elif canonical_actor_key(ev.target_actor) == canonical_actor_key(ev.source_actor):
            charm_row["ambiguous"] += ev.amount
            charm_row["same_name_collision"] += ev.amount
        else:
            charm_row["confirmed"] += ev.amount
            charm_row["active_window"] += ev.amount

    rows: list[dict[str, Any]] = []
    for damage in per_charmed_actor.values():
        confirmed = damage["confirmed"]
        ambiguous = damage["ambiguous"]
        if confirmed <= 0 and ambiguous <= 0:
            continue
        estimated = confirmed + (ambiguous / 2)
        rows.append(
            {
                "actor": damage["actor"],
                "confirmed_damage": confirmed,
                "ambiguous_damage": ambiguous,
                "minimum_damage": confirmed,
                "maximum_damage": confirmed + ambiguous,
                "estimated_damage": round(estimated, 2),
                "dps": round(estimated / duration, 2),
                "evidence": {
                    "tagged_damage": damage["tagged"],
                    "active_window_damage": damage["active_window"],
                    "same_name_collision_damage": damage["same_name_collision"],
                },
            }
        )

    rows.sort(key=lambda row: (-row["estimated_damage"], -row["confirmed_damage"], row["actor"].lower()))
    potential_rows = [
        {
            "actor": damage["actor"],
            "candidate_damage": damage["candidate"],
            "minimum_damage": 0,
            "maximum_damage": damage["candidate"],
            "reason": "recent_charm_cast" if damage["recent_signal"] else "npc_vs_npc_without_charm_evidence",
            "evidence": {
                "recent_signal_damage": damage["recent_signal"],
                "weak_candidate_damage": damage["weak_candidate"],
            },
        }
        for damage in potential_by_actor.values()
        if damage["candidate"] > 0
    ]
    potential_rows.sort(key=lambda row: (-row["candidate_damage"], row["actor"].lower()))
    return {
        "charm_damage": rows,
        "potential_charm_damage": potential_rows,
        "charm_estimator": {
            "version": 2,
            "ambiguous_weight": 0.5,
            "duration_basis": "encounter",
            "potential_candidates_included_in_estimate": False,
        },
    }


def _default_actor_hidden(actor_name: str) -> bool:
    normalized = (actor_name or "").strip().lower()
    if not normalized:
        return False
    if normalized == "you":
        return False
    tokens = normalized.split()
    if not tokens:
        return False
    if tokens[0] in {"a", "an", "the"}:
        return True
    return len(tokens) > 1


def _breakdown_bucket(ev: Event) -> tuple[str, str]:
    if ev.category == "heal":
        return "heal", (ev.ability_name or "Basic Heal")
    if ev.ability_name:
        return "ability", ev.ability_name
    if ev.attack_verb:
        return "melee", ev.attack_verb
    if ev.category in {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}:
        return "other", (ev.result or ev.category)
    return "other", ev.category


def _classify_multi_attack_groups(events: list[Event]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Identify same-frame consecutive melee sequences that represent double/triple attacks."""
    ordered_events = sorted(events, key=lambda ev: (ev.received_at, ev.id if ev.id is not None else -1, id(ev)))
    combo_by_event_key: dict[int, dict[str, Any]] = {}
    combo_groups: dict[int, dict[str, Any]] = {}
    next_group_id = 1

    def event_key(ev: Event) -> int:
        return id(ev)

    def is_melee_attempt(ev: Event) -> bool:
        return (
            ev.category in {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}
            and not ev.ability_name
            and bool(ev.attack_verb)
            and bool(ev.source_actor)
        )

    run: list[Event] = []

    def flush_run() -> None:
        nonlocal next_group_id
        if len(run) < 2:
            return
        idx = 0
        while idx < len(run):
            remaining = len(run) - idx
            if remaining >= 3:
                size = 3
                kind = "Triple Attack"
            elif remaining == 2:
                size = 2
                kind = "Double Attack"
            else:
                break
            group_events = run[idx : idx + size]
            group_id = next_group_id
            next_group_id += 1
            combo_groups[group_id] = {
                "group_id": group_id,
                "kind": kind,
                "event_ids": [ev.id for ev in group_events],
                "has_hit": any(ev.category in {"damage_out", "damage_in", "damage"} for ev in group_events),
            }
            for ev in group_events:
                combo_by_event_key[event_key(ev)] = {"group_id": group_id, "kind": kind}
            idx += size

    for ev in ordered_events:
        if not is_melee_attempt(ev):
            flush_run()
            run = []
            continue

        if not run:
            run = [ev]
            continue

        prev = run[-1]
        same_frame = prev.received_at == ev.received_at
        same_sequence = (
            prev.source_actor == ev.source_actor
            and prev.target_actor == ev.target_actor
            and (prev.attack_verb or "").lower() == (ev.attack_verb or "").lower()
            and same_frame
        )
        if same_sequence:
            run.append(ev)
        else:
            flush_run()
            run = [ev]

    flush_run()
    return combo_by_event_key, combo_groups


def _metrics_for_encounter(
    encounter: Encounter,
    events: list[Event],
    hidden_actor_names: set[str] | None = None,
    *,
    charm_windows: list[CharmWindow] | None = None,
    charm_signals: list[SessionLogLine] | None = None,
    include_charm_metrics: bool = True,
) -> dict[str, Any]:
    hidden_actor_names = hidden_actor_names or set()
    per_actor: dict[str, dict[str, Any]] = {}
    duration = _duration_seconds(encounter, events)
    combo_by_event_key, combo_groups = _classify_multi_attack_groups(events)
    scored_combo_groups: set[int] = set()

    def ensure_actor_row(name: str) -> None:
        per_actor.setdefault(
            name,
            {
                "actor": name,
                "damage_done": 0,
                "damage_taken": 0,
                "damage_absorbed": 0,
                "damage_blocked": 0,
                "hits": 0,
                "misses": 0,
                "healing_done": 0,
                "healing_received": 0,
                "healing_by_ability": {},
                "breakdown": {},
            },
        )

    for ev in events:
        if ev.category in {"damage_out", "damage_in", "damage"}:
            if not ev.source_actor or not ev.target_actor:
                continue
            actor = ev.source_actor
            target = ev.target_actor
            ensure_actor_row(actor)
            ensure_actor_row(target)
            per_actor[actor]["damage_done"] += ev.amount or 0
            per_actor[target]["damage_taken"] += ev.amount or 0
            per_actor[target]["damage_absorbed"] += ev.absorbed_amount or 0
            per_actor[target]["damage_blocked"] += ev.blocked_amount or 0
            per_actor[actor]["hits"] += 1
            combo_meta = combo_by_event_key.get(id(ev))
            if combo_meta:
                source_type, bucket_name = "ability", combo_meta["kind"]
            else:
                source_type, bucket_name = _breakdown_bucket(ev)
            entry = per_actor[actor]["breakdown"].setdefault(
                (source_type, bucket_name),
                {
                    "source_type": source_type,
                    "name": bucket_name,
                    "damage": 0,
                    "healing": 0,
                    "hits": 0,
                    "misses": 0,
                    "attempts": 0,
                    "average_amount": 0,
                    "hit_pct": 0,
                    "dps": 0,
                    "hps": 0,
                },
            )
            amount = ev.amount or 0
            entry["damage"] += amount
            if combo_meta:
                group_id = combo_meta["group_id"]
                if group_id not in scored_combo_groups:
                    scored_combo_groups.add(group_id)
                    entry["attempts"] += 1
                    if combo_groups[group_id]["has_hit"]:
                        entry["hits"] += 1
                    else:
                        entry["misses"] += 1
            else:
                entry["hits"] += 1
                entry["attempts"] += 1
        elif ev.category in {"miss_out", "miss_in", "miss"}:
            if not ev.source_actor:
                continue
            actor = ev.source_actor
            ensure_actor_row(actor)
            per_actor[actor]["misses"] += 1
            combo_meta = combo_by_event_key.get(id(ev))
            if combo_meta:
                source_type, bucket_name = "ability", combo_meta["kind"]
            else:
                source_type, bucket_name = _breakdown_bucket(ev)
            entry = per_actor[actor]["breakdown"].setdefault(
                (source_type, bucket_name),
                {
                    "source_type": source_type,
                    "name": bucket_name,
                    "damage": 0,
                    "healing": 0,
                    "hits": 0,
                    "misses": 0,
                    "attempts": 0,
                    "average_amount": 0,
                    "hit_pct": 0,
                    "dps": 0,
                    "hps": 0,
                },
            )
            if combo_meta:
                group_id = combo_meta["group_id"]
                if group_id not in scored_combo_groups:
                    scored_combo_groups.add(group_id)
                    entry["attempts"] += 1
                    if combo_groups[group_id]["has_hit"]:
                        entry["hits"] += 1
                    else:
                        entry["misses"] += 1
            else:
                entry["misses"] += 1
                entry["attempts"] += 1
        elif ev.category == "heal":
            if not ev.source_actor or not ev.target_actor:
                continue
            actor = ev.source_actor
            target = ev.target_actor
            ensure_actor_row(actor)
            ensure_actor_row(target)
            amount = ev.amount or 0
            ability_name = ev.ability_name or "Basic Heal"
            per_actor[actor]["healing_done"] += amount
            per_actor[target]["healing_received"] += amount
            per_actor[actor]["healing_by_ability"][ability_name] = per_actor[actor]["healing_by_ability"].get(ability_name, 0) + amount
            entry = per_actor[actor]["breakdown"].setdefault(
                ("heal", ability_name),
                {
                    "source_type": "heal",
                    "name": ability_name,
                    "damage": 0,
                    "healing": 0,
                    "hits": 0,
                    "misses": 0,
                    "attempts": 0,
                    "average_amount": 0,
                    "hit_pct": 0,
                    "dps": 0,
                    "hps": 0,
                },
            )
            entry["healing"] += amount
            entry["hits"] += 1
            entry["attempts"] += 1

    rows = []
    for row in per_actor.values():
        attempts = row["hits"] + row["misses"]
        row["hit_attempts"] = attempts
        row["hit_pct"] = round((row["hits"] / attempts * 100), 2) if attempts else 0
        row["dps"] = round(row["damage_done"] / duration, 2)
        row["hidden"] = row["actor"] in hidden_actor_names
        row["shown"] = not row["hidden"]
        breakdown_rows = []
        for breakdown in row["breakdown"].values():
            attempts = breakdown["attempts"]
            total_amount = breakdown["damage"] + breakdown["healing"]
            breakdown["average_amount"] = round(total_amount / attempts, 2) if attempts else 0
            breakdown["hit_pct"] = round((breakdown["hits"] / attempts * 100), 2) if attempts else 0
            breakdown["dps"] = round(breakdown["damage"] / duration, 2)
            breakdown["hps"] = round(breakdown["healing"] / duration, 2)
            breakdown_rows.append(breakdown)
        row["breakdown"] = breakdown_rows
        rows.append(row)

    visible_rows = [row for row in rows if not row["hidden"]]
    total_hits = sum(row["hits"] for row in visible_rows)
    total_misses = sum(row["misses"] for row in visible_rows)
    total_damage_done = sum(row["damage_done"] for row in visible_rows)

    visible_healing_by_ability: dict[str, int] = {}
    for row in visible_rows:
        for ability_name, amount in row.get("healing_by_ability", {}).items():
            visible_healing_by_ability[ability_name] = visible_healing_by_ability.get(ability_name, 0) + amount

    charm_metrics = (
        _charm_attribution_for_encounter(
            events,
            duration,
            charm_windows=charm_windows,
            charm_signals=charm_signals,
        )
        if include_charm_metrics
        else {
            "charm_damage": [],
            "potential_charm_damage": [],
            "charm_estimator": None,
        }
    )
    return {
        "duration_seconds": duration,
        "totals": {
            "damage_done": total_damage_done,
            "dps": round(total_damage_done / duration, 2),
            "damage_taken": sum(row["damage_taken"] for row in visible_rows),
            "damage_absorbed": sum(row["damage_absorbed"] for row in visible_rows),
            "damage_blocked": sum(row["damage_blocked"] for row in visible_rows),
            "healing_done": sum(row["healing_done"] for row in visible_rows),
            "healing_received": sum(row["healing_received"] for row in visible_rows),
            "hit_attempts": total_hits + total_misses,
            "hits": total_hits,
            "misses": total_misses,
            "hit_pct": round((total_hits / max(total_hits + total_misses, 1)) * 100, 2),
            "healing_by_ability": visible_healing_by_ability,
        },
        "actors": rows,
        **charm_metrics,
    }


def _ensure_timeout_consistency(db: Session) -> None:
    settings = service.load_settings()
    encounter_engine.reconcile_timeouts(
        db,
        now=utcnow(),
        timeout_s=settings.encounter_inactivity_timeout_s,
    )
    db.commit()


def _charm_context_for_events(
    db: Session,
    events: list[Event],
) -> tuple[list[CharmWindow], list[SessionLogLine]]:
    parser_session_ids = {event.parser_session_id for event in events if event.parser_session_id is not None}
    if not parser_session_ids or not events:
        return [], []

    first_event_at = min(event.received_at for event in events)
    last_event_at = max(event.received_at for event in events)
    windows = list(
        db.scalars(
            select(CharmWindow).where(
                CharmWindow.parser_session_id.in_(parser_session_ids),
                CharmWindow.started_at <= last_event_at,
                or_(CharmWindow.ended_at.is_(None), CharmWindow.ended_at >= first_event_at),
            )
        ).all()
    )
    signals = list(
        db.scalars(
            select(SessionLogLine).where(
                SessionLogLine.parser_session_id.in_(parser_session_ids),
                SessionLogLine.parse_outcome == "accepted",
                SessionLogLine.parse_category == "cast_start",
                func.lower(func.trim(SessionLogLine.ability_name)).in_(CHARM_SIGNAL_ABILITIES),
                SessionLogLine.received_at >= first_event_at - timedelta(seconds=15),
                SessionLogLine.received_at <= last_event_at,
            )
        ).all()
    )
    return windows, signals


@app.get("/api/encounters")
def list_encounters(db: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ensure_timeout_consistency(db)
    rows = db.scalars(select(Encounter).order_by(Encounter.id.desc())).all()
    payload = []
    for encounter in rows:
        events = db.scalars(select(Event).where(Event.encounter_id == encounter.id).order_by(Event.id.asc())).all()
        metrics = _metrics_for_encounter(encounter, events, include_charm_metrics=False)
        payload.append({**_encounter_summary(encounter), **metrics["totals"]})
    return payload


@app.get("/api/encounters/{encounter_id}")
def get_encounter(encounter_id: int, db: Session = Depends(get_session)) -> dict[str, Any]:
    _ensure_timeout_consistency(db)
    encounter = db.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    events = db.scalars(select(Event).where(Event.encounter_id == encounter_id).order_by(Event.id.desc())).all()
    visibility_rows = db.scalars(
        select(EncounterActorVisibility).where(EncounterActorVisibility.encounter_id == encounter_id)
    ).all()
    explicit_hidden = {row.actor_name for row in visibility_rows if row.hidden}
    explicit_shown = {row.actor_name for row in visibility_rows if not row.hidden}

    actor_names = {name for event in events for name in (event.source_actor, event.target_actor) if name}
    hidden = set(explicit_hidden)
    for actor_name in actor_names:
        if actor_name in explicit_shown or actor_name in explicit_hidden:
            continue
        if _default_actor_hidden(actor_name):
            hidden.add(actor_name)

    charm_windows, charm_signals = _charm_context_for_events(db, events)
    metrics = _metrics_for_encounter(
        encounter,
        events,
        hidden_actor_names=hidden,
        charm_windows=charm_windows,
        charm_signals=charm_signals,
    )
    return {
        "encounter": _encounter_summary(encounter),
        "metrics": metrics,
        "events": [
            {
                "id": e.id,
                "session_log_line_id": e.session_log_line_id,
                "timestamp": e.received_at.isoformat(),
                "raw_ocr_line": e.raw_ocr_line,
                "category": e.category,
                "source_actor": e.source_actor,
                "target_actor": e.target_actor,
                "ability_name": e.ability_name,
                "amount": e.amount,
                "absorbed_amount": e.absorbed_amount,
                "blocked_amount": e.blocked_amount,
                "result": e.result,
                "attack_verb": e.attack_verb,
                "effect_name": e.effect_name,
                "ocr_confidence": e.ocr_confidence,
                "parse_confidence": e.parse_confidence,
            }
            for e in events
        ],
    }


@app.post("/api/encounters/end-active")
def end_active_encounter(db: Session = Depends(get_session)) -> dict[str, str]:
    if not encounter_engine.end_active_encounter(db, reason="manual"):
        raise HTTPException(status_code=409, detail="No active encounter")
    db.commit()
    return {"status": "ended"}


@app.post("/api/encounters/{encounter_id}/actor-visibility")
def set_actor_visibility(encounter_id: int, payload: ActorVisibilityRequest, db: Session = Depends(get_session)) -> dict[str, str]:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    row = db.scalars(
        select(EncounterActorVisibility).where(
            EncounterActorVisibility.encounter_id == encounter_id,
            EncounterActorVisibility.actor_name == payload.actor_name,
        )
    ).first()
    if row is None:
        row = EncounterActorVisibility(encounter_id=encounter_id, actor_name=payload.actor_name, hidden=payload.hidden)
        db.add(row)
    else:
        row.hidden = payload.hidden
    db.commit()
    return {"status": "ok"}


@app.post("/api/encounters/{encounter_id}/delete")
def delete_encounter(encounter_id: int, db: Session = Depends(get_session)) -> dict[str, str]:
    encounter = db.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")
    db.query(EncounterActorVisibility).filter(EncounterActorVisibility.encounter_id == encounter_id).delete()
    db.delete(encounter)
    db.commit()
    return {"status": "deleted"}


@app.post("/api/encounters/clear")
def clear_encounters(db: Session = Depends(get_session)) -> dict[str, str]:
    db.query(Event).delete()
    db.query(OCRLine).delete()
    db.query(EncounterActorVisibility).delete()
    db.query(Encounter).delete()
    db.commit()
    return {"status": "cleared"}


@app.post("/api/encounters/merge")
def merge_encounters(payload: MergeRequest, db: Session = Depends(get_session)) -> dict[str, str]:
    encounter_ids = sorted(set(payload.encounter_ids))
    if len(encounter_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least two encounter ids")

    encounters = [db.get(Encounter, encounter_id) for encounter_id in encounter_ids]
    if any(encounter is None for encounter in encounters):
        raise HTTPException(status_code=404, detail="Encounter missing")

    primary = encounters[0]
    has_active_encounter = any(encounter.active for encounter in encounters)
    capture_modes = {
        (getattr(encounter, "capture_filter_mode", None) or "none").lower()
        for encounter in encounters
    }
    for secondary in encounters[1:]:
        db.query(Event).filter(Event.encounter_id == secondary.id).update({Event.encounter_id: primary.id})
        secondary_visibility_rows = db.scalars(
            select(EncounterActorVisibility).where(EncounterActorVisibility.encounter_id == secondary.id)
        ).all()
        for secondary_visibility in secondary_visibility_rows:
            primary_visibility = db.scalars(
                select(EncounterActorVisibility).where(
                    EncounterActorVisibility.encounter_id == primary.id,
                    EncounterActorVisibility.actor_name == secondary_visibility.actor_name,
                )
            ).first()
            if primary_visibility is None:
                secondary_visibility.encounter_id = primary.id
                continue
            primary_visibility.hidden = primary_visibility.hidden or secondary_visibility.hidden
            db.delete(secondary_visibility)
        primary.started_at = min(primary.started_at, secondary.started_at)
        if secondary.last_activity_at and (primary.last_activity_at is None or secondary.last_activity_at > primary.last_activity_at):
            primary.last_activity_at = secondary.last_activity_at
        if (
            not has_active_encounter
            and secondary.ended_at
            and (primary.ended_at is None or secondary.ended_at > primary.ended_at)
        ):
            primary.ended_at = secondary.ended_at
        db.delete(secondary)

    if has_active_encounter:
        primary.active = True
        primary.ended_at = None
        primary.ended_reason = None
    else:
        primary.active = False
        primary.ended_reason = "merged"
    primary.capture_filter_mode = next(iter(capture_modes)) if len(capture_modes) == 1 else "mixed"
    db.commit()
    return {"status": "merged"}


@app.post("/api/encounters/{encounter_id}/split")
def split_encounter(encounter_id: int, payload: SplitRequest, db: Session = Depends(get_session)) -> dict[str, str]:
    encounter = db.get(Encounter, encounter_id)
    split_event = db.get(Event, payload.event_id)
    if encounter is None or split_event is None or split_event.encounter_id != encounter_id:
        raise HTTPException(status_code=404, detail="Encounter/event not found")

    new_encounter = Encounter(
        started_at=split_event.received_at,
        last_activity_at=split_event.received_at if split_event.category in COMBAT_ACTIVITY_CATEGORIES else None,
        active=False,
        label=split_event.received_at.strftime('%H:%M:%S'),
        ended_reason="split",
        capture_filter_mode=getattr(encounter, "capture_filter_mode", None) or "none",
    )
    db.add(new_encounter)
    db.flush()

    db.query(Event).filter(
        Event.encounter_id == encounter_id,
        Event.received_at >= split_event.received_at,
    ).update({Event.encounter_id: new_encounter.id})
    db.query(OCRLine).filter(
        OCRLine.encounter_id == encounter_id,
        OCRLine.received_at >= split_event.received_at,
    ).update({OCRLine.encounter_id: new_encounter.id})

    first_new = db.scalars(select(Event).where(Event.encounter_id == new_encounter.id).order_by(Event.id.asc())).first()
    last_old = db.scalars(select(Event).where(Event.encounter_id == encounter_id).order_by(Event.id.desc())).first()
    if first_new:
        new_encounter.started_at = first_new.received_at
    if last_old:
        encounter.ended_at = last_old.received_at
        encounter.active = False
        encounter.ended_reason = "split"
    db.commit()
    return {"status": "split"}
