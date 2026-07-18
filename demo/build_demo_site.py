from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import RuntimeSettings  # noqa: E402
from app.parser import normalize_text, parse_line  # noqa: E402


FIXTURE_PATH = ROOT / "demo" / "fixtures" / "demo_combat_log.json"
DEMO_API_SOURCE = ROOT / "demo" / "assets" / "demo-api.js"
SIGHTLINE_CSS_SOURCE = ROOT / "app" / "static" / "sightline.css"
SIGHTLINE_SHELL_SOURCE = ROOT / "app" / "static" / "sightline-shell.js"
BRAND_SOURCE = ROOT / "brand"
DEFAULT_OUTPUT_DIR = ROOT / "demo-site"
COMBAT_ACTIVITY_CATEGORIES = {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}


DEMO_HEAD_SNIPPET = """  <script>
    window.SIGHTLINE_DEMO = true;
    document.documentElement.classList.add('demo-mode');
  </script>
  <script src="/static/demo-api.js"></script>
"""


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def timestamp(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_settings_payload() -> dict[str, Any]:
    settings = RuntimeSettings(combat_region_configured=True, require_game_focus=False)
    return asdict(settings)


def line_payload(
    *,
    line_id: int,
    parser_session_id: int,
    received_at: datetime,
    raw_text: str,
    ocr_confidence: float,
    known_actors: set[str],
) -> tuple[dict[str, Any], Any | None]:
    normalized = normalize_text(raw_text)
    parsed = parse_line(normalized, known_actors)
    accepted = parsed is not None and ocr_confidence >= 0.45 and parsed.parse_confidence >= 0.8

    if accepted and parsed:
        if parsed.source_actor:
            known_actors.add(parsed.source_actor)
        if parsed.target_actor:
            known_actors.add(parsed.target_actor)

    return (
        {
            "id": line_id,
            "received_at": iso(received_at),
            "timestamp": timestamp(received_at),
            "raw_text": raw_text,
            "normalized_text": normalized,
            "ocr_confidence": ocr_confidence,
            "parse_confidence": parsed.parse_confidence if parsed else None,
            "parse_outcome": "accepted" if accepted else "rejected",
            "parse_category": parsed.category if accepted and parsed else None,
            "source_actor": parsed.source_actor if accepted and parsed else None,
            "target_actor": parsed.target_actor if accepted and parsed else None,
            "ability_name": parsed.ability_name if accepted and parsed else None,
            "amount": parsed.amount if accepted and parsed else None,
            "absorbed_amount": parsed.absorbed_amount if accepted and parsed else None,
            "blocked_amount": parsed.blocked_amount if accepted and parsed else None,
            "result": parsed.result if accepted and parsed else None,
            "attack_verb": parsed.attack_verb if accepted and parsed else None,
            "effect_name": parsed.effect_name if accepted and parsed else None,
        },
        parsed if accepted else None,
    )


def event_from_line(*, event_id: int, encounter_id: int, parser_session_id: int, line: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event_id,
        "encounter_id": encounter_id,
        "parser_session_id": parser_session_id,
        "session_log_line_id": line["id"],
        "timestamp": line["received_at"],
        "raw_ocr_line": line["raw_text"],
        "normalized_text": line["normalized_text"],
        "category": line["parse_category"],
        "source_actor": line["source_actor"],
        "target_actor": line["target_actor"],
        "ability_name": line["ability_name"],
        "amount": line["amount"],
        "absorbed_amount": line["absorbed_amount"],
        "blocked_amount": line["blocked_amount"],
        "result": line["result"],
        "attack_verb": line["attack_verb"],
        "effect_name": line["effect_name"],
        "ocr_confidence": line["ocr_confidence"],
        "parse_confidence": line["parse_confidence"],
    }


def _default_actor_hidden(actor_name: str) -> bool:
    normalized = (actor_name or "").strip().lower()
    if not normalized or normalized == "you":
        return False
    tokens = normalized.split()
    if not tokens:
        return False
    if tokens[0] in {"a", "an", "the"}:
        return True
    return len(tokens) > 1


def _breakdown_bucket(event: dict[str, Any]) -> tuple[str, str]:
    if event["category"] == "heal":
        return "heal", (event.get("ability_name") or "Basic Heal")
    if event.get("ability_name"):
        return "ability", event["ability_name"]
    if event.get("attack_verb"):
        return "melee", event["attack_verb"]
    if event["category"] in {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}:
        return "other", (event.get("result") or event["category"])
    return "other", event["category"]


def _event_dt(event: dict[str, Any]) -> datetime:
    return parse_iso(event["timestamp"])


def _duration_seconds(encounter: dict[str, Any], events: list[dict[str, Any]]) -> float:
    if events:
        times = [_event_dt(event) for event in events]
        return max((max(times) - min(times)).total_seconds(), 1.0)
    return max(((encounter["ended_at"] or datetime.utcnow()) - encounter["started_at"]).total_seconds(), 1.0)


def _classify_multi_attack_groups(events: list[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    ordered_events = sorted(events, key=lambda event: (_event_dt(event), event["id"]))
    combo_by_event_id: dict[int, dict[str, Any]] = {}
    combo_groups: dict[int, dict[str, Any]] = {}
    next_group_id = 1
    run: list[dict[str, Any]] = []

    def is_melee_attempt(event: dict[str, Any]) -> bool:
        return (
            event["category"] in {"damage_out", "damage_in", "damage", "miss_out", "miss_in", "miss"}
            and not event.get("ability_name")
            and bool(event.get("attack_verb"))
            and bool(event.get("source_actor"))
        )

    def flush_run() -> None:
        nonlocal next_group_id
        if len(run) < 2:
            return
        index = 0
        while index < len(run):
            remaining = len(run) - index
            if remaining >= 3:
                size = 3
                kind = "Triple Attack"
            elif remaining == 2:
                size = 2
                kind = "Double Attack"
            else:
                break
            group_events = run[index : index + size]
            group_id = next_group_id
            next_group_id += 1
            combo_groups[group_id] = {
                "group_id": group_id,
                "kind": kind,
                "event_ids": [event["id"] for event in group_events],
                "has_hit": any(event["category"] in {"damage_out", "damage_in", "damage"} for event in group_events),
            }
            for event in group_events:
                combo_by_event_id[event["id"]] = {"group_id": group_id, "kind": kind}
            index += size

    for event in ordered_events:
        if not is_melee_attempt(event):
            flush_run()
            run = []
            continue

        if not run:
            run = [event]
            continue

        previous = run[-1]
        same_sequence = (
            previous["timestamp"] == event["timestamp"]
            and previous.get("source_actor") == event.get("source_actor")
            and previous.get("target_actor") == event.get("target_actor")
            and (previous.get("attack_verb") or "").lower() == (event.get("attack_verb") or "").lower()
        )
        if same_sequence:
            run.append(event)
        else:
            flush_run()
            run = [event]

    flush_run()
    return combo_by_event_id, combo_groups


def _ensure_actor_row(per_actor: dict[str, dict[str, Any]], name: str) -> None:
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


def _breakdown_entry(row: dict[str, Any], source_type: str, bucket_name: str) -> dict[str, Any]:
    return row["breakdown"].setdefault(
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


def _metrics_for_demo_encounter(
    encounter: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    hidden_actor_names: set[str] | None = None,
) -> dict[str, Any]:
    hidden_actor_names = hidden_actor_names or set()
    per_actor: dict[str, dict[str, Any]] = {}
    duration = _duration_seconds(encounter, events)
    combo_by_event_id, combo_groups = _classify_multi_attack_groups(events)
    scored_combo_groups: set[int] = set()

    for event in events:
        category = event["category"]
        if category in {"damage_out", "damage_in", "damage"}:
            source = event.get("source_actor")
            target = event.get("target_actor")
            if not source or not target:
                continue
            _ensure_actor_row(per_actor, source)
            _ensure_actor_row(per_actor, target)
            amount = int(event.get("amount") or 0)
            per_actor[source]["damage_done"] += amount
            per_actor[target]["damage_taken"] += amount
            per_actor[target]["damage_absorbed"] += int(event.get("absorbed_amount") or 0)
            per_actor[target]["damage_blocked"] += int(event.get("blocked_amount") or 0)
            per_actor[source]["hits"] += 1

            combo_meta = combo_by_event_id.get(event["id"])
            source_type, bucket_name = ("ability", combo_meta["kind"]) if combo_meta else _breakdown_bucket(event)
            entry = _breakdown_entry(per_actor[source], source_type, bucket_name)
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
        elif category in {"miss_out", "miss_in", "miss"}:
            source = event.get("source_actor")
            if not source:
                continue
            _ensure_actor_row(per_actor, source)
            per_actor[source]["misses"] += 1
            combo_meta = combo_by_event_id.get(event["id"])
            source_type, bucket_name = ("ability", combo_meta["kind"]) if combo_meta else _breakdown_bucket(event)
            entry = _breakdown_entry(per_actor[source], source_type, bucket_name)
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
        elif category == "heal":
            source = event.get("source_actor")
            target = event.get("target_actor")
            if not source or not target:
                continue
            _ensure_actor_row(per_actor, source)
            _ensure_actor_row(per_actor, target)
            amount = int(event.get("amount") or 0)
            ability_name = event.get("ability_name") or "Basic Heal"
            per_actor[source]["healing_done"] += amount
            per_actor[target]["healing_received"] += amount
            per_actor[source]["healing_by_ability"][ability_name] = (
                per_actor[source]["healing_by_ability"].get(ability_name, 0) + amount
            )
            entry = _breakdown_entry(per_actor[source], "heal", ability_name)
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
    }


def encounter_payload(
    encounter: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    hidden_actor_names: set[str] | None = None,
) -> dict[str, Any]:
    metrics = _metrics_for_demo_encounter(encounter, events, hidden_actor_names=hidden_actor_names or set())
    duration = max(((encounter["ended_at"] or datetime.utcnow()) - encounter["started_at"]).total_seconds(), 0)
    base_label = encounter["label"] or encounter["started_at"].strftime("%H:%M:%S")
    display_label = f"{base_label} (merged)" if encounter["ended_reason"] == "merged" else base_label
    return {
        "encounter": {
            "id": encounter["id"],
            "label": display_label,
            "started_at": iso(encounter["started_at"]),
            "ended_at": iso(encounter["ended_at"]),
            "last_activity_at": iso(encounter["last_activity_at"] or encounter["started_at"]),
            "active": encounter["active"],
            "duration_seconds": duration,
            "ended_reason": encounter["ended_reason"],
        },
        "metrics": metrics,
        "events": [dict(event) for event in sorted(events, key=lambda ev: ev["id"], reverse=True)],
    }


def default_hidden_actors(events: list[dict[str, Any]]) -> set[str]:
    actor_names = {
        actor
        for event in events
        for actor in (event.get("source_actor"), event.get("target_actor"))
        if actor
    }
    return {actor for actor in actor_names if _default_actor_hidden(actor)}


def build_demo_data() -> dict[str, Any]:
    fixture = load_fixture()
    session = fixture["session"]
    parser_session_id = int(session["id"])
    session_started = parse_iso(session["started_at"])
    known_actors: set[str] = {
        "You",
        *(str(actor).strip() for actor in session.get("known_actors", []) if str(actor).strip()),
    }
    log_lines: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    summaries: list[dict[str, Any]] = []
    next_line_id = 1
    next_event_id = 1

    for spec in fixture.get("loose_lines", []):
        received_at = session_started + timedelta(seconds=int(spec.get("offset_s", 0)))
        line, _ = line_payload(
            line_id=next_line_id,
            parser_session_id=parser_session_id,
            received_at=received_at,
            raw_text=spec["raw_text"],
            ocr_confidence=float(spec.get("ocr_confidence", 0.98)),
            known_actors=known_actors,
        )
        next_line_id += 1
        log_lines.append(line)

    for encounter_spec in fixture["encounters"]:
        encounter_id = int(encounter_spec["id"])
        started_at = parse_iso(encounter_spec["started_at"])
        ended_at = parse_iso(encounter_spec["ended_at"]) if encounter_spec.get("ended_at") else None
        encounter_events: list[dict[str, Any]] = []
        last_activity_at: datetime | None = None

        for spec in encounter_spec["lines"]:
            received_at = started_at + timedelta(seconds=int(spec.get("offset_s", 0)))
            line, parsed = line_payload(
                line_id=next_line_id,
                parser_session_id=parser_session_id,
                received_at=received_at,
                raw_text=spec["raw_text"],
                ocr_confidence=float(spec.get("ocr_confidence", 0.98)),
                known_actors=known_actors,
            )
            next_line_id += 1
            log_lines.append(line)
            if parsed is None:
                continue
            event = event_from_line(
                event_id=next_event_id,
                encounter_id=encounter_id,
                parser_session_id=parser_session_id,
                line=line,
            )
            next_event_id += 1
            encounter_events.append(event)
            if parsed.category in COMBAT_ACTIVITY_CATEGORIES:
                last_activity_at = received_at

        encounter = {
            "id": encounter_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "last_activity_at": last_activity_at,
            "active": False,
            "label": encounter_spec["label"],
            "ended_reason": encounter_spec.get("ended_reason", "timeout"),
        }
        hidden = default_hidden_actors(encounter_events)
        detail = encounter_payload(encounter, encounter_events, hidden_actor_names=hidden)
        details[str(encounter_id)] = detail
        summaries.append({**detail["encounter"], **detail["metrics"]["totals"]})

    log_lines.sort(key=lambda line: (line["received_at"], line["id"]))
    summaries.sort(key=lambda row: (row["started_at"], row["id"]), reverse=True)

    session_payload = {
        "session": {
            "session_id": parser_session_id,
            "started_at": session["started_at"],
            "ended_at": session.get("ended_at"),
            "active": False,
            "file_path": session.get("file_path"),
        },
        "log_lines": log_lines,
    }

    return {
        "demo": True,
        "settings": make_settings_payload(),
        "status": {
            "running": False,
            "last_successful_ocr_at": session.get("ended_at"),
            "last_error": None,
            "last_skip_reason": None,
            "known_actors": sorted(known_actors),
            "settings": make_settings_payload(),
            "overlay": {
                "active": False,
                "parser_requested": False,
                "manual_requested": False,
            },
            "demo": True,
        },
        "current_session": session_payload,
        "current_session_summary": {
            "session": session_payload["session"],
            "line_count": len(log_lines),
        },
        "encounters": summaries,
        "encounter_details": details,
    }


def inject_demo_assets(html: str) -> str:
    if "</head>" not in html:
        raise ValueError("Expected </head> in static HTML")
    html = html.replace(
        '    <a href="/replay" class="topbar-link" data-debug-only hidden style="display:none" aria-hidden="true">Replay Harness</a>\n',
        "",
    )
    html = html.replace(
        '    <a href="/replay" data-debug-only hidden style="display:none" aria-hidden="true">Replay Harness</a>\n',
        "",
    )
    return html.replace("</head>", f"{DEMO_HEAD_SNIPPET}</head>", 1)


def redirect_page(target: str) -> str:
    escaped = target.replace('"', "%22")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="0; url={escaped}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#0F1117" />
  <title>Sightline Demo</title>
  <link rel="icon" href="/favicons/favicon.svg" type="image/svg+xml" />
</head>
<body>
  <a href="{escaped}">Continue to Sightline demo</a>
</body>
</html>
"""


def _resolve_output_dir(output_dir: Path) -> Path:
    """Return a safe output directory without permitting arbitrary replacement."""
    default_output_dir = DEFAULT_OUTPUT_DIR.expanduser()
    default_output_is_symlink = default_output_dir.is_symlink()

    if output_dir.is_symlink():
        raise ValueError("Refusing to build into a symlinked output directory")

    resolved_output_dir = output_dir.expanduser().resolve()
    resolved_root = ROOT.resolve()
    resolved_default_output_dir = default_output_dir.resolve()

    if resolved_output_dir == resolved_root or resolved_output_dir in resolved_root.parents:
        raise ValueError("Refusing to use the repository directory or one of its parents as demo output")

    if default_output_is_symlink and resolved_output_dir == resolved_default_output_dir:
        raise ValueError("Refusing to use a symlinked default demo output directory")

    if not default_output_is_symlink and resolved_output_dir == resolved_default_output_dir:
        if resolved_output_dir.exists() and not resolved_output_dir.is_dir():
            raise ValueError("The default demo output path must be a directory")
        return resolved_output_dir

    if resolved_output_dir.exists():
        raise FileExistsError(
            "Refusing to replace an existing custom demo output directory; choose a new output path instead"
        )

    return resolved_output_dir


def write_static_site(output_dir: Path) -> None:
    output_dir = _resolve_output_dir(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "static").mkdir(parents=True)

    (output_dir / "demo-data.json").write_text(
        json.dumps(build_demo_data(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(DEMO_API_SOURCE, output_dir / "static" / "demo-api.js")
    shutil.copyfile(SIGHTLINE_CSS_SOURCE, output_dir / "static" / "sightline.css")
    shutil.copyfile(SIGHTLINE_SHELL_SOURCE, output_dir / "static" / "sightline-shell.js")
    brand_web_dir = output_dir / "brand" / "web"
    brand_logo_dir = output_dir / "brand" / "logos" / "svg"
    brand_web_dir.mkdir(parents=True)
    brand_logo_dir.mkdir(parents=True)
    shutil.copyfile(BRAND_SOURCE / "web" / "sightline-tokens.css", brand_web_dir / "sightline-tokens.css")
    shutil.copyfile(
        BRAND_SOURCE / "logos" / "svg" / "sightline-logo-dark.svg",
        brand_logo_dir / "sightline-logo-dark.svg",
    )
    shutil.copytree(BRAND_SOURCE / "favicons", output_dir / "favicons")
    shutil.copyfile(BRAND_SOURCE / "favicons" / "favicon.ico", output_dir / "favicon.ico")
    shutil.copyfile(BRAND_SOURCE / "favicons" / "site.webmanifest", output_dir / "site.webmanifest")

    index_html = inject_demo_assets((ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8"))
    encounters_html = inject_demo_assets((ROOT / "app" / "static" / "encounters.html").read_text(encoding="utf-8"))

    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    (output_dir / "encounters.html").write_text(encounters_html, encoding="utf-8")
    (output_dir / "encounters").mkdir()
    (output_dir / "encounters" / "index.html").write_text(encounters_html, encoding="utf-8")

    for route in ("settings",):
        (output_dir / route).mkdir()
        (output_dir / route / "index.html").write_text(redirect_page("/"), encoding="utf-8")
        (output_dir / f"{route}.html").write_text(redirect_page("/"), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static Sightline demo site.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for the static demo site (must be new unless it is demo-site).",
    )
    args = parser.parse_args(argv)
    output_dir = _resolve_output_dir(args.output)
    write_static_site(output_dir)
    print(f"Built static demo site at {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
