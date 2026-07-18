from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    inspect,
    select,
    text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import DATA_DIR
from .paths import BACKUP_DIR, ensure_runtime_directories, resource_path
from .time_utils import utcnow


DB_PATH = DATA_DIR / "combat_parser.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class SettingsModel(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    capture_x: Mapped[int] = mapped_column(Integer, default=0)
    capture_y: Mapped[int] = mapped_column(Integer, default=0)
    capture_width: Mapped[int] = mapped_column(Integer, default=800)
    capture_height: Mapped[int] = mapped_column(Integer, default=300)
    combat_region_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    capture_interval_ms: Mapped[int] = mapped_column(Integer, default=500)
    inactivity_timeout_s: Mapped[int] = mapped_column(Integer, default=15)
    encounter_inactivity_timeout_s: Mapped[int] = mapped_column(Integer, default=5)
    debug_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    require_game_focus: Mapped[bool] = mapped_column(Boolean, default=False)
    increase_accuracy: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_focus_executables: Mapped[list] = mapped_column(JSON, default=list)
    # New rows intentionally start with an explicitly configured empty list.
    # Foreground restriction remains off until the user enables it in Settings.
    allowed_focus_executables_configured: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_end_hotkey_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_end_hotkey_binding: Mapped[str] = mapped_column(String(64), default="Shift+F3")
    encounter_filter_mode: Mapped[str] = mapped_column(String(16), default="none")
    encounter_party_members: Mapped[list] = mapped_column(JSON, default=list)
    overlay_monitor_index: Mapped[int] = mapped_column(Integer, default=0)
    overlay_anchor_x_ratio: Mapped[float] = mapped_column(Float, default=0.5)
    overlay_anchor_y_ratio: Mapped[float] = mapped_column(Float, default=0.5)
    overlay_offset_x: Mapped[int] = mapped_column(Integer, default=0)
    overlay_offset_y: Mapped[int] = mapped_column(Integer, default=0)
    overlay_damage_out_offset_x: Mapped[int] = mapped_column(Integer, default=90)
    overlay_damage_in_offset_x: Mapped[int] = mapped_column(Integer, default=-90)
    overlay_heal_in_offset_y: Mapped[int] = mapped_column(Integer, default=70)
    overlay_font_size: Mapped[int] = mapped_column(Integer, default=20)
    overlay_time_to_fade_s: Mapped[float] = mapped_column(Float, default=1.0)
    overlay_event_spacing: Mapped[int] = mapped_column(Integer, default=18)
    overlay_combine_multi_hit: Mapped[bool] = mapped_column(Boolean, default=True)
    ocr_passes_enabled: Mapped[list] = mapped_column(JSON, default=list)
    visible_columns: Mapped[list] = mapped_column(JSON, default=list)


class Encounter(Base):
    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capture_filter_mode: Mapped[str] = mapped_column(String(16), default="none")

    events: Mapped[list[Event]] = relationship("Event", back_populates="encounter", cascade="all, delete-orphan")
    ocr_lines: Mapped[list[OCRLine]] = relationship("OCRLine", back_populates="encounter", cascade="all, delete-orphan")


class OCRLine(Base):
    __tablename__ = "ocr_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int | None] = mapped_column(ForeignKey("encounters.id"), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    debug_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    encounter: Mapped[Encounter | None] = relationship("Encounter", back_populates="ocr_lines")


class ParserSession(Base):
    __tablename__ = "parser_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    log_lines: Mapped[list[SessionLogLine]] = relationship(
        "SessionLogLine",
        back_populates="parser_session",
        cascade="all, delete-orphan",
    )
    charm_windows: Mapped[list[CharmWindow]] = relationship(
        "CharmWindow",
        back_populates="parser_session",
        cascade="all, delete-orphan",
    )


class SessionLogLine(Base):
    __tablename__ = "session_log_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_session_id: Mapped[int] = mapped_column(ForeignKey("parser_sessions.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ability_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    absorbed_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attack_verb: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effect_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    parser_session: Mapped[ParserSession] = relationship("ParserSession", back_populates="log_lines")


class CharmWindow(Base):
    __tablename__ = "charm_windows"
    __table_args__ = (
        Index(
            "ix_charm_windows_session_actor_start",
            "parser_session_id",
            "actor_key",
            "started_at",
            "start_line_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parser_session_id: Mapped[int] = mapped_column(ForeignKey("parser_sessions.id"), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_key: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    start_line_id: Mapped[int] = mapped_column(
        ForeignKey("session_log_lines.id"),
        nullable=False,
        unique=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("session_log_lines.id"),
        nullable=True,
        unique=True,
    )

    parser_session: Mapped[ParserSession] = relationship("ParserSession", back_populates="charm_windows")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"))
    session_log_line_id: Mapped[int | None] = mapped_column(ForeignKey("session_log_lines.id"), nullable=True, index=True, unique=True)
    parser_session_id: Mapped[int | None] = mapped_column(ForeignKey("parser_sessions.id"), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_ocr_line: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    source_actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ability_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    absorbed_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attack_verb: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effect_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    encounter: Mapped[Encounter] = relationship("Encounter", back_populates="events")


class EncounterActorVisibility(Base):
    __tablename__ = "encounter_actor_visibility"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    actor_name: Mapped[str] = mapped_column(String(128), index=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


def init_db() -> None:
    ensure_runtime_directories()
    database_existed = DB_PATH.exists()
    backup_path = _backup_database_if_migration_needed()
    try:
        _upgrade_database()
        _backfill_charm_windows(engine)
    except Exception:
        if backup_path is not None and backup_path.exists():
            engine.dispose()
            shutil.copy2(backup_path, DB_PATH)
        elif not database_existed:
            engine.dispose()
            DB_PATH.unlink(missing_ok=True)
        raise


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(resource_path("migrations")))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{DB_PATH.as_posix()}")
    return config


def _database_revision() -> str | None:
    if not DB_PATH.exists():
        return None
    try:
        with engine.connect() as connection:
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).first()
            if not exists:
                return None
            row = connection.exec_driver_sql("SELECT version_num FROM alembic_version").first()
            return str(row[0]) if row else None
    except Exception:
        return None


def _migration_head() -> str:
    return str(ScriptDirectory.from_config(_alembic_config()).get_current_head())


def _backup_database_if_migration_needed() -> Path | None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None
    if _database_revision() == _migration_head():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_DIR / f"combat_parser-before-migration-{timestamp}.db"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def _upgrade_database() -> None:
    config = _alembic_config()
    if _database_revision() is None:
        # Reconcile and adopt databases from releases that predate Alembic.
        Base.metadata.create_all(bind=engine)
        _migrate_session_log_lines_table(engine)
        _migrate_settings_table(engine)
        _migrate_encounters_table(engine)
        _migrate_events_table(engine)
        command.stamp(config, "head")
        return
    command.upgrade(config, "head")


def _backfill_charm_windows(db_engine) -> None:
    """Idempotently replay accepted Charm markers into parser-session windows."""
    from .charm import observe_charm_event

    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if not {"charm_windows", "session_log_lines"}.issubset(table_names):
        return

    BackfillSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with BackfillSession.begin() as db:
        lines = db.scalars(
            select(SessionLogLine)
            .where(
                SessionLogLine.parse_outcome == "accepted",
                SessionLogLine.parse_category.in_(("charm_start", "charm_end")),
            )
            .order_by(SessionLogLine.received_at.asc(), SessionLogLine.id.asc())
        ).all()
        for line in lines:
            observe_charm_event(db, line)


def _migrate_session_log_lines_table(db_engine) -> None:
    """Backfill new SessionLogLine columns for existing SQLite databases."""
    required_columns = {
        "parse_category": "VARCHAR(64)",
        "source_actor": "VARCHAR(128)",
        "target_actor": "VARCHAR(128)",
        "ability_name": "VARCHAR(128)",
        "amount": "INTEGER",
        "absorbed_amount": "INTEGER",
        "blocked_amount": "INTEGER",
        "result": "VARCHAR(64)",
        "attack_verb": "VARCHAR(64)",
        "effect_name": "VARCHAR(128)",
    }
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if "session_log_lines" not in table_names:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("session_log_lines")}
    with db_engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                text(f"ALTER TABLE session_log_lines ADD COLUMN {column_name} {column_type}")
            )


def _migrate_settings_table(db_engine) -> None:
    required_columns = {
        "combat_region_configured": "BOOLEAN DEFAULT 0",
        # Existing source users have already completed the equivalent setup.
        # Fresh databases still use the model default (False) and see onboarding.
        "onboarding_completed": "BOOLEAN DEFAULT 1",
        "encounter_inactivity_timeout_s": "INTEGER DEFAULT 5",
        "manual_end_hotkey_enabled": "BOOLEAN DEFAULT 0",
        "manual_end_hotkey_binding": "VARCHAR(64) DEFAULT 'Shift+F3'",
        "encounter_filter_mode": "VARCHAR(16) DEFAULT 'none'",
        "encounter_party_members": "JSON",
        "overlay_monitor_index": "INTEGER DEFAULT 0",
        "overlay_anchor_x_ratio": "FLOAT DEFAULT 0.5",
        "overlay_anchor_y_ratio": "FLOAT DEFAULT 0.5",
        "overlay_offset_x": "INTEGER DEFAULT 0",
        "overlay_offset_y": "INTEGER DEFAULT 0",
        "overlay_damage_out_offset_x": "INTEGER DEFAULT 90",
        "overlay_damage_in_offset_x": "INTEGER DEFAULT -90",
        "overlay_heal_in_offset_y": "INTEGER DEFAULT 70",
        "overlay_font_size": "INTEGER DEFAULT 20",
        "overlay_time_to_fade_s": "FLOAT DEFAULT 1.0",
        "overlay_event_spacing": "INTEGER DEFAULT 18",
        "overlay_combine_multi_hit": "BOOLEAN DEFAULT 1",
        "ocr_passes_enabled": "JSON",
        "require_game_focus": "BOOLEAN DEFAULT 0",
        "increase_accuracy": "BOOLEAN DEFAULT 0",
        "allowed_focus_executables": "JSON",
        "allowed_focus_executables_configured": "BOOLEAN DEFAULT 0",
    }
    inspector = inspect(db_engine)
    if "settings" not in set(inspector.get_table_names()):
        return
    existing_columns = {column["name"] for column in inspector.get_columns("settings")}
    with db_engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE settings ADD COLUMN {column_name} {column_type}"))
        # Profiles created before the explicit foreground-allowlist marker
        # defaulted to focus restriction with an empty list. The new
        # cross-game defaults would otherwise leave those users permanently
        # paused without returning them to setup. Preserve explicit settings,
        # but release legacy empty allowlists back to the opt-in default.
        connection.execute(
            text(
                """
                UPDATE settings
                SET require_game_focus = 0
                WHERE COALESCE(require_game_focus, 0) = 1
                  AND COALESCE(allowed_focus_executables_configured, 0) = 0
                  AND (
                    allowed_focus_executables IS NULL
                    OR TRIM(CAST(allowed_focus_executables AS TEXT)) IN ('', '[]', 'null')
                  )
                """
            )
        )


def _migrate_encounters_table(db_engine) -> None:
    required_columns = {
        "last_activity_at": "DATETIME",
        "label": "VARCHAR(255)",
        "ended_reason": "VARCHAR(64)",
        "capture_filter_mode": "VARCHAR(16) DEFAULT 'none'",
    }
    inspector = inspect(db_engine)
    if "encounters" not in set(inspector.get_table_names()):
        return
    existing_columns = {column["name"] for column in inspector.get_columns("encounters")}
    with db_engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE encounters ADD COLUMN {column_name} {column_type}"))


def _migrate_events_table(db_engine) -> None:
    required_columns = {
        "session_log_line_id": "INTEGER",
        "parser_session_id": "INTEGER",
        "attack_verb": "VARCHAR(64)",
        "effect_name": "VARCHAR(128)",
        "absorbed_amount": "INTEGER",
        "blocked_amount": "INTEGER",
    }
    inspector = inspect(db_engine)
    if "events" not in set(inspector.get_table_names()):
        return
    existing_columns = {column["name"] for column in inspector.get_columns("events")}
    existing_indexes = inspector.get_indexes("events")
    existing_unique_constraints = inspector.get_unique_constraints("events")

    unique_session_log_indexes = [
        index
        for index in existing_indexes
        if index.get("unique") and index.get("column_names") == ["session_log_line_id"]
    ]
    unique_session_log_constraints = [
        constraint
        for constraint in existing_unique_constraints
        if constraint.get("column_names") == ["session_log_line_id"]
    ]
    has_unique_session_log_coverage = bool(unique_session_log_indexes or unique_session_log_constraints)
    has_parser_session_index = any(index.get("column_names") == ["parser_session_id"] for index in existing_indexes)

    with db_engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE events ADD COLUMN {column_name} {column_type}"))
        if not has_unique_session_log_coverage:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_events_session_log_line_id_unique ON events (session_log_line_id)"
                )
            )
        if len(unique_session_log_indexes) > 1 and any(
            index.get("name") != "ix_events_session_log_line_id_unique" for index in unique_session_log_indexes
        ):
            connection.execute(text("DROP INDEX IF EXISTS ix_events_session_log_line_id_unique"))
        if not has_parser_session_index:
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_events_parser_session_id ON events (parser_session_id)"))


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
