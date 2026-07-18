from __future__ import annotations

from dataclasses import dataclass, field
from .paths import DATA_DIR, ensure_runtime_directories


ensure_runtime_directories()
DEBUG_ARTIFACT_DIR = DATA_DIR / "debug_artifacts"
DEBUG_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# Foreground-application restriction is an optional privacy control configured
# from Settings. New profiles rely on the explicitly selected capture rectangle
# until the user opts into executable filtering.
DEFAULT_ALLOWED_FOCUS_EXECUTABLES: tuple[str, ...] = ()

def normalize_executable_name(value: object) -> str | None:
    """Return a lowercase executable basename with an .exe suffix."""
    executable = str(value or "").strip().strip('"').strip("'")
    executable = executable.replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()
    if not executable:
        return None
    if not executable.endswith(".exe"):
        executable = f"{executable}.exe"
    return None if executable == ".exe" else executable


def normalize_executable_names(values: list[str] | str | None) -> list[str]:
    """Normalize and de-duplicate executable names while preserving order."""
    if values is None:
        raw_values: list[str] = []
    elif isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, list) and all(isinstance(value, str) for value in values):
        raw_values = values
    else:
        raise ValueError("allowed_focus_executables must be a string or a list of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        executable = normalize_executable_name(value)
        if executable and executable not in seen:
            normalized.append(executable)
            seen.add(executable)
    return normalized


@dataclass
class RuntimeSettings:
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
    allowed_focus_executables: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_FOCUS_EXECUTABLES))
    manual_end_hotkey_enabled: bool = False
    manual_end_hotkey_binding: str = "Shift+F3"
    encounter_filter_mode: str = "none"
    encounter_party_members: list[dict[str, str | None] | str] = field(default_factory=list)
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
    ocr_passes_enabled: list[str] = field(default_factory=lambda: ["whole_threshold"])
    visible_columns: list[str] = field(
        default_factory=lambda: [
            "actor",
            "damage_done",
            "dps",
            "damage_taken",
            "hit_count",
            "miss_count",
            "hit_rate",
            "heal_amount",
            "pet_damage",
        ]
    )


SELF_ALIASES = {"you", "your", "YOU", "Your", "You"}
CANONICAL_SELF = "You"
