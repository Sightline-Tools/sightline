from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DATA_DIR
from .db import SessionLogLine


@dataclass(frozen=True)
class CompareOptions:
    strict: bool = False
    include_metadata: bool = False


_EXPECTED_TIMESTAMP_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")


def _normalize_expected_line(line: str) -> str:
    return _EXPECTED_TIMESTAMP_PREFIX.sub("", line.strip())


def _serialize_line(line: SessionLogLine, *, options: CompareOptions) -> str:
    base_text = line.raw_text if options.strict else line.normalized_text
    text = base_text.strip()
    if not options.include_metadata:
        return text

    metadata = [
        f"parse_outcome={line.parse_outcome or ''}",
        f"parse_category={line.parse_category or ''}",
        f"source_actor={line.source_actor or ''}",
        f"target_actor={line.target_actor or ''}",
        f"ability_name={line.ability_name or ''}",
        f"amount={line.amount if line.amount is not None else ''}",
        f"result={line.result or ''}",
    ]
    return f"{text}\t" + "\t".join(metadata)


def load_expected_lines(expected_file_path: str | Path) -> list[str]:
    path = Path(expected_file_path).expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        return [_normalize_expected_line(line) for line in handle.readlines()]


def load_session_lines(db: Session, parser_session_id: int, *, options: CompareOptions) -> list[str]:
    lines = db.scalars(
        select(SessionLogLine)
        .where(SessionLogLine.parser_session_id == parser_session_id)
        .order_by(SessionLogLine.id.asc())
    ).all()
    return [_serialize_line(line, options=options) for line in lines]


def _classify_diff_line(line: str) -> dict[str, str]:
    if line.startswith("@@"):
        line_type = "hunk"
    elif line.startswith("---") or line.startswith("+++"):
        line_type = "header"
    elif line.startswith("+"):
        line_type = "added"
    elif line.startswith("-"):
        line_type = "removed"
    else:
        line_type = "context"
    return {"type": line_type, "text": line}


def compare_line_sequences(*, expected_lines: list[str], actual_lines: list[str]) -> dict:
    matcher = SequenceMatcher(a=expected_lines, b=actual_lines)
    matches = 0
    missing = 0
    unexpected = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matches += i2 - i1
        elif tag == "delete":
            missing += i2 - i1
        elif tag == "insert":
            unexpected += j2 - j1
        elif tag == "replace":
            missing += i2 - i1
            unexpected += j2 - j1

    diff_lines = list(
        unified_diff(
            expected_lines,
            actual_lines,
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )

    return {
        "summary": {
            "expected_total": len(expected_lines),
            "actual_total": len(actual_lines),
            "matches": matches,
            "missing": missing,
            "unexpected": unexpected,
        },
        "diff": "\n".join(diff_lines),
        "diff_lines": [_classify_diff_line(line) for line in diff_lines],
    }


def compare_session_to_expected(
    db: Session,
    *,
    parser_session_id: int,
    expected_lines: list[str],
    options: CompareOptions,
) -> dict:
    actual_lines = load_session_lines(db, parser_session_id, options=options)
    return compare_line_sequences(expected_lines=expected_lines, actual_lines=actual_lines)


def maybe_write_compare_artifact(*, parser_session_id: int, payload: dict) -> str:
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    output_path = export_dir / f"parser_session_{parser_session_id}_compare.diff"
    output_path.write_text(payload.get("diff", ""), encoding="utf-8")
    return str(output_path)
