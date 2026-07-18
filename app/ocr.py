from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import statistics
import time

import cv2
import numpy as np
from PIL import Image

from .tesseract_backend import image_to_data


PASS_WEIGHTS = {
    "raw": 1.00,
    "whole_threshold": 1.02,
    "red_mask": 1.06,
    "yellow_mask": 1.00,
    "green_mask": 1.00,
    "white_mask": 0.98,
}

ALL_OCR_PASSES = [
    "whole_threshold",
    "raw",
    "red_mask",
    "yellow_mask",
    "green_mask",
    "white_mask",
]
DEFAULT_OCR_PASSES = {"whole_threshold"}
COLOR_MASK_PASSES = {"red_mask", "yellow_mask", "green_mask", "white_mask"}

TESSERACT_COMBAT_TEXT_CONFIG = "--psm 6"

COLOR_RANGES: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    "red_mask": [
        ((0, 120, 120), (12, 255, 255)),
        ((170, 120, 120), (180, 255, 255)),
    ],
    "yellow_mask": [
        ((18, 120, 120), (38, 255, 255)),
    ],
    "green_mask": [
        ((40, 60, 80), (95, 255, 255)),
    ],
    "white_mask": [
        ((0, 0, 135), (179, 70, 255)),
    ],
}

CLUSTER_TOLERANCE_FACTOR = 0.65
CLUSTER_TOLERANCE_MIN = 6.0
CLUSTER_TOLERANCE_MAX = 24.0
CONSENSUS_BONUS_PER_MATCH = 0.025
CONSENSUS_BONUS_MAX = 0.08
MIN_SELECTED_CONFIDENCE = 0.18
MIN_NORMALIZED_TEXT_LENGTH = 3
MIN_NORMALIZED_ALNUM_RATIO = 0.45
EDGE_MARGIN_RATIO = 0.03
EDGE_MARGIN_MIN_PX = 8.0
EDGE_LOW_TOKEN_MAX = 2
EDGE_LOW_CONFIDENCE_MAX = 0.45
EDGE_SMALL_HEIGHT_RATIO_MAX = 0.035
SHORT_VALID_PATTERNS = (
    re.compile(r"^\d+$"),
    re.compile(r"^[a-z]\d+$"),
    re.compile(r"^\d+[a-z]$"),
)
RED_POST_CLOSE_KERNEL = np.ones((2, 2), np.uint8) if np is not None else None
WHOLE_THRESHOLD_SCALE = 2
WHOLE_THRESHOLD_BLUR_KERNEL = 3
COLOR_MASK_SCALE = 2
COLOR_MASK_BLUR_KERNEL = 3
RED_PASS_SCALE = 3
RED_BLUR_KERNEL = 3
COLOR_RANGES_NP = {
    pass_name: [(np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8)) for lower, upper in ranges]
    for pass_name, ranges in COLOR_RANGES.items()
}


@dataclass
class OCRLineCandidate:
    source: str
    text: str
    normalized_text: str
    confidence: float
    weighted_confidence: float
    center_y: float
    bbox: tuple[int, int, int, int]


@dataclass
class OCRSelectedLine:
    raw_text: str
    normalized_text: str
    source_pass: str
    confidence: float
    center_y: float
    bbox: tuple[int, int, int, int]


@dataclass
class OCRResult:
    lines: list[tuple[str, float]]
    selected_lines: list[OCRSelectedLine] = field(default_factory=list)
    pass_names: list[str] = field(default_factory=list)


def normalize_ocr_text(text: str) -> str:
    lowered = text.lower().replace("|", " ")
    lowered = re.sub(r"\b(\w+)[’']s\b", r"\1", lowered)
    stripped = re.sub(r"^[^\w\d]+|[^\w\d]+$", "", lowered)
    punctuation_trimmed = re.sub(r"[^\w\s]", " ", stripped)
    return re.sub(r"\s+", " ", punctuation_trimmed).strip()


def _pil_to_rgb_array(image: Image.Image) -> np.ndarray:
    return np.array(image.convert("RGB"), dtype=np.uint8)


def _preprocess_whole_threshold(
    image_rgb: np.ndarray,
    *,
    scale: int = WHOLE_THRESHOLD_SCALE,
    increase_accuracy: bool = False,
) -> np.ndarray:
    # For transparent/log-overlay captures, this simpler path tends to give Tesseract cleaner glyph edges.
    brightness_like = np.max(image_rgb, axis=2).astype(np.uint8)
    if increase_accuracy:
        threshold_input = cv2.resize(brightness_like, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        threshold_input = cv2.GaussianBlur(
            threshold_input,
            (WHOLE_THRESHOLD_BLUR_KERNEL, WHOLE_THRESHOLD_BLUR_KERNEL),
            0,
        )
    else:
        threshold_input = brightness_like
    _, thresholded = cv2.threshold(
        threshold_input,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    # Tesseract generally performs better with black text on a white background.
    return cv2.bitwise_not(thresholded)


def _preprocess_red_text(
    image_rgb: np.ndarray,
    *,
    scale: int = RED_PASS_SCALE,
    increase_accuracy: bool = False,
) -> np.ndarray:
    # Red keeps the red-excess signal, but now uses the same blur+Otsu style that improved other passes.
    rgb16 = image_rgb.astype(np.int16)
    red_channel = rgb16[:, :, 0]
    non_red_max = np.maximum(rgb16[:, :, 1], rgb16[:, :, 2])
    red_excess = np.clip(red_channel - non_red_max, 0, 255).astype(np.uint8)

    if increase_accuracy:
        threshold_input = cv2.resize(red_excess, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        threshold_input = cv2.GaussianBlur(threshold_input, (RED_BLUR_KERNEL, RED_BLUR_KERNEL), 0)
    else:
        threshold_input = red_excess
    _, thresholded = cv2.threshold(
        threshold_input,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    closed = cv2.morphologyEx(thresholded, cv2.MORPH_CLOSE, RED_POST_CLOSE_KERNEL)
    return 255 - closed


def _combine_hsv_ranges(hsv: np.ndarray, ranges: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lower_np, upper_np in ranges:
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lower_np, upper_np))
    return mask


def _preprocess_color_mask(
    mask: np.ndarray,
    *,
    scale: int = COLOR_MASK_SCALE,
    increase_accuracy: bool = False,
) -> np.ndarray:
    if increase_accuracy:
        threshold_input = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        threshold_input = cv2.GaussianBlur(threshold_input, (COLOR_MASK_BLUR_KERNEL, COLOR_MASK_BLUR_KERNEL), 0)
    else:
        threshold_input = mask
    _, thresholded = cv2.threshold(
        threshold_input,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    return 255 - thresholded


def _normalize_enabled_passes(enabled_passes: set[str] | None) -> set[str]:
    if not enabled_passes:
        return set(DEFAULT_OCR_PASSES)

    valid_passes = set(ALL_OCR_PASSES)
    selected = {pass_name for pass_name in enabled_passes if pass_name in valid_passes}
    if not selected:
        return set(DEFAULT_OCR_PASSES)
    return selected


def _build_preprocess_passes(
    image: Image.Image | np.ndarray,
    *,
    enabled_passes: set[str] | None = None,
    increase_accuracy: bool = False,
) -> list[tuple[str, Image.Image | np.ndarray, float]]:
    normalized_enabled_passes = _normalize_enabled_passes(enabled_passes)
    passes: list[tuple[str, Image.Image | np.ndarray, float]] = []

    rgb = image if isinstance(image, np.ndarray) else _pil_to_rgb_array(image)

    if "whole_threshold" in normalized_enabled_passes:
        whole_threshold = _preprocess_whole_threshold(
            rgb,
            scale=WHOLE_THRESHOLD_SCALE,
            increase_accuracy=increase_accuracy,
        )
        passes.append(("whole_threshold", whole_threshold, float(WHOLE_THRESHOLD_SCALE if increase_accuracy else 1)))

    if "raw" in normalized_enabled_passes:
        passes.append(("raw", rgb, 1.0))

    selected_color_passes = [
        pass_name
        for pass_name in ALL_OCR_PASSES
        if pass_name in COLOR_MASK_PASSES and pass_name in normalized_enabled_passes
    ]
    if selected_color_passes:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    for pass_name in selected_color_passes:
        if pass_name == "red_mask":
            preprocessed = _preprocess_red_text(rgb, scale=RED_PASS_SCALE, increase_accuracy=increase_accuracy)
            passes.append((pass_name, preprocessed, float(RED_PASS_SCALE if increase_accuracy else 1)))
            continue

        mask = _combine_hsv_ranges(hsv, COLOR_RANGES_NP[pass_name])
        preprocessed = _preprocess_color_mask(mask, scale=COLOR_MASK_SCALE, increase_accuracy=increase_accuracy)
        passes.append((pass_name, preprocessed, float(COLOR_MASK_SCALE if increase_accuracy else 1)))

    return passes


def _extract_line_candidates_from_data(
    data: dict[str, list[str] | list[int] | list[float]],
    image: Image.Image | np.ndarray,
    *,
    source: str,
    scale_back: float,
) -> list[OCRLineCandidate]:
    groups: dict[tuple[int, int, int, int], dict[str, list[float] | list[tuple[float, str]]]] = {}

    for i, raw_text in enumerate(data["text"]):
        text = (raw_text or "").strip()
        if not text:
            continue

        conf_str = str(data["conf"][i]).strip()
        if conf_str in {"", "-1"}:
            continue

        conf = float(conf_str)
        key = (
            int(data["page_num"][i]),
            int(data["block_num"][i]),
            int(data["par_num"][i]),
            int(data["line_num"][i]),
        )

        left = float(data["left"][i]) / scale_back
        top = float(data["top"][i]) / scale_back
        width = float(data["width"][i]) / scale_back
        height = float(data["height"][i]) / scale_back

        group = groups.setdefault(
            key,
            {"word_positions": [], "confs": [], "lefts": [], "rights": [], "tops": [], "bottoms": []},
        )
        group["word_positions"].append((left, text))
        group["confs"].append(conf)
        group["lefts"].append(left)
        group["rights"].append(left + width)
        group["tops"].append(top)
        group["bottoms"].append(top + height)

    candidates: list[OCRLineCandidate] = []
    image_height = float(image.height if isinstance(image, Image.Image) else image.shape[0])
    capture_height = max(image_height / max(scale_back, 1e-6), 1.0)
    edge_margin = max(EDGE_MARGIN_MIN_PX, capture_height * EDGE_MARGIN_RATIO)

    for group in groups.values():
        ordered_words = sorted((tuple(item) for item in group["word_positions"]), key=lambda item: float(item[0]))
        text = " ".join(str(word) for _, word in ordered_words).strip()
        normalized = normalize_ocr_text(text)
        if not text or not normalized:
            continue

        confs = [max(float(conf), 0.0) for conf in group["confs"]]
        avg_conf = sum(confs) / len(confs)

        left = int(min(float(value) for value in group["lefts"]))
        top = int(min(float(value) for value in group["tops"]))
        right = int(max(float(value) for value in group["rights"]))
        bottom = int(max(float(value) for value in group["bottoms"]))
        height = max(0, bottom - top)
        center_y = (top + bottom) / 2

        tokens = [token for token in normalized.split() if token]
        token_count = len(tokens)
        normalized_no_space = normalized.replace(" ", "")
        normalized_len = len(normalized_no_space)
        matches_short_valid_pattern = any(pattern.fullmatch(normalized) for pattern in SHORT_VALID_PATTERNS)
        is_single_char_nondigit_line = (
            token_count == 1 and len(tokens[0]) == 1 and not tokens[0].isdigit()
        )
        alnum_chars = sum(1 for char in normalized if char.isalnum())
        alnum_ratio = alnum_chars / max(len(normalized), 1)

        if is_single_char_nondigit_line and not matches_short_valid_pattern:
            continue
        if normalized_len < MIN_NORMALIZED_TEXT_LENGTH and not matches_short_valid_pattern:
            continue
        if alnum_ratio < MIN_NORMALIZED_ALNUM_RATIO:
            continue

        distance_to_edge = min(float(top), max(capture_height - float(bottom), 0.0), float(center_y))
        near_capture_edge = distance_to_edge <= edge_margin
        small_height_near_edge = (height / capture_height) <= EDGE_SMALL_HEIGHT_RATIO_MAX
        weighted_conf = (avg_conf / 100.0) * PASS_WEIGHTS.get(source, 1.0)
        if near_capture_edge and token_count <= EDGE_LOW_TOKEN_MAX and (
            (avg_conf / 100.0) <= EDGE_LOW_CONFIDENCE_MAX and small_height_near_edge
        ):
            continue

        candidates.append(
            OCRLineCandidate(
                source=source,
                text=text,
                normalized_text=normalized,
                confidence=avg_conf / 100.0,
                weighted_confidence=weighted_conf,
                center_y=center_y,
                bbox=(left, top, max(0, right - left), height),
            )
        )

    return sorted(candidates, key=lambda line: line.center_y)


def _extract_line_candidates(image: Image.Image | np.ndarray, *, source: str, scale_back: float) -> list[OCRLineCandidate]:
    data = image_to_data(image, config=TESSERACT_COMBAT_TEXT_CONFIG)
    return _extract_line_candidates_from_data(data, image, source=source, scale_back=scale_back)


def _compute_dynamic_cluster_tolerance(lines: list[OCRLineCandidate]) -> float:
    if not lines:
        return CLUSTER_TOLERANCE_MIN
    heights = [max(line.bbox[3], 1) for line in lines]
    median_height = statistics.median(heights)
    tolerance = median_height * CLUSTER_TOLERANCE_FACTOR
    return max(CLUSTER_TOLERANCE_MIN, min(CLUSTER_TOLERANCE_MAX, float(tolerance)))


def _cluster_lines(lines: list[OCRLineCandidate]) -> list[list[OCRLineCandidate]]:
    ordered = sorted(lines, key=lambda item: item.center_y)
    if not ordered:
        return []

    tolerance = _compute_dynamic_cluster_tolerance(ordered)
    clusters: list[list[OCRLineCandidate]] = []
    cluster_center = 0.0
    cluster_sum = 0.0
    cluster_count = 0

    for line in ordered:
        if not clusters:
            clusters.append([line])
            cluster_sum = line.center_y
            cluster_count = 1
            cluster_center = line.center_y
            continue

        if abs(line.center_y - cluster_center) <= tolerance:
            clusters[-1].append(line)
            cluster_sum += line.center_y
            cluster_count += 1
            cluster_center = cluster_sum / cluster_count
        else:
            clusters.append([line])
            cluster_sum = line.center_y
            cluster_count = 1
            cluster_center = line.center_y

    return clusters


def _candidate_plausibility_score(candidate: OCRLineCandidate, *, consensus_count: int = 1) -> float:
    tokens = candidate.normalized_text.split()
    if not tokens:
        return -1.0
    numeric_bonus = 0.03 if any(token.isdigit() for token in tokens) else 0.0
    length_bonus = min(len(candidate.normalized_text) / 120.0, 0.08)
    consensus_bonus = min((max(consensus_count - 1, 0) * CONSENSUS_BONUS_PER_MATCH), CONSENSUS_BONUS_MAX)
    return candidate.weighted_confidence + numeric_bonus + length_bonus + consensus_bonus


def _pick_best_candidate(cluster: list[OCRLineCandidate]) -> OCRLineCandidate:
    consensus_counts: dict[str, int] = {}
    for candidate in cluster:
        key = candidate.normalized_text
        consensus_counts[key] = consensus_counts.get(key, 0) + 1

    return max(
        cluster,
        key=lambda item: (
            _candidate_plausibility_score(item, consensus_count=consensus_counts.get(item.normalized_text, 1)),
            item.confidence,
            len(item.normalized_text),
        ),
    )


def _save_debug_passes(passes: list[tuple[str, Image.Image | np.ndarray, float]], debug_dir: Path) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    for pass_name, pass_image, _ in passes:
        output_path = debug_dir / f"{pass_name}.png"
        if isinstance(pass_image, Image.Image):
            pass_image.save(output_path)
            continue
        cv2.imwrite(str(output_path), pass_image)


def run_ocr(
    image: Image.Image | np.ndarray,
    *,
    enabled_passes: set[str] | None = None,
    increase_accuracy: bool = False,
    debug_dir: str | Path | None = None,
    timings: dict[str, float] | None = None,
) -> OCRResult:
    preprocess_started = time.perf_counter()
    passes = _build_preprocess_passes(
        image,
        enabled_passes=enabled_passes,
        increase_accuracy=increase_accuracy,
    )
    if debug_dir is not None:
        _save_debug_passes(passes, Path(debug_dir))
    preprocess_elapsed = time.perf_counter() - preprocess_started

    tesseract_elapsed = 0.0
    postprocess_elapsed = 0.0
    all_candidates: list[OCRLineCandidate] = []
    for pass_name, pass_image, scale_back in passes:
        pass_tesseract_started = time.perf_counter()
        data = image_to_data(pass_image, config=TESSERACT_COMBAT_TEXT_CONFIG)
        tesseract_elapsed += time.perf_counter() - pass_tesseract_started

        pass_postprocess_started = time.perf_counter()
        all_candidates.extend(_extract_line_candidates_from_data(data, pass_image, source=pass_name, scale_back=scale_back))
        postprocess_elapsed += time.perf_counter() - pass_postprocess_started

    select_started = time.perf_counter()
    clusters = _cluster_lines(all_candidates)
    selected = [_pick_best_candidate(cluster) for cluster in clusters]
    selected_lines = [
        OCRSelectedLine(
            raw_text=line.text,
            normalized_text=line.normalized_text,
            source_pass=line.source,
            confidence=line.confidence,
            center_y=line.center_y,
            bbox=line.bbox,
        )
        for line in selected
        if line.normalized_text and line.confidence >= MIN_SELECTED_CONFIDENCE
    ]
    postprocess_elapsed += time.perf_counter() - select_started

    if timings is not None:
        timings["preprocess_ms"] = preprocess_elapsed * 1000.0
        timings["tesseract_ms"] = tesseract_elapsed * 1000.0
        timings["postprocess_ms"] = postprocess_elapsed * 1000.0
        timings["total_ms"] = (preprocess_elapsed + tesseract_elapsed + postprocess_elapsed) * 1000.0

    return OCRResult(
        lines=[(line.raw_text, line.confidence) for line in selected_lines],
        selected_lines=selected_lines,
        pass_names=[pass_name for pass_name, _, _ in passes],
    )
