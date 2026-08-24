#!/usr/bin/env python3
"""Refine a traced figure against native source ink without changing topology.

The tool moves shared, named source landmarks only inside a small native-pixel
radius.  Every incident segment moves with its landmark, so connectivity and
line roles remain unchanged.  It writes a *candidate* observation plus a
before/after overlay; the candidate still needs source-overlay review before it
may replace the locked observation used by a final manifest.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


VERSION = "1"
STRUCTURAL_STYLES = {"solid", "dashed", "emphasis", "guide"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def source_distance(image: Image.Image) -> tuple[np.ndarray, int]:
    gray_image = image.convert("L").filter(ImageFilter.MedianFilter(3))
    gray = np.asarray(gray_image, dtype=np.uint8)
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = gray <= int(threshold)
    distance = cv2.distanceTransform(1 - ink.astype(np.uint8), cv2.DIST_L2, 5)
    return distance, int(threshold)


def _valid_point(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value)
    )


def normalized_segments(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("segments must be a list")
    records: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"segment #{index + 1} must be an object")
        name = str(raw.get("name", "")).strip()
        start = str(raw.get("from", "")).strip()
        end = str(raw.get("to", "")).strip()
        style = str(raw.get("style", "solid")).strip()
        if not name or not start or not end:
            raise ValueError(f"segment #{index + 1} needs name/from/to")
        if name in names:
            raise ValueError(f"duplicate segment name: {name}")
        if start == end:
            raise ValueError(f"segment {name} has the same endpoint twice")
        if style not in STRUCTURAL_STYLES:
            raise ValueError(f"segment {name} has unsupported style: {style}")
        names.add(name)
        records.append({"name": name, "from": start, "to": end, "style": style})
    return records


def curve_links(
    value: object,
    points: dict[str, list[float]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn named curve controls into local optimization links.

    The rendered curve remains smooth; short links merely keep each named
    native control point near the printed curve during coordinate descent.
    Literal curve coordinates are reported for visual review but are not moved
    because they have no shared landmark identity.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        raise ValueError("curves must be a list")
    links: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for curve_index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"curve #{curve_index + 1} must be an object")
        name = str(raw.get("name", f"curve-{curve_index + 1}"))
        controls = raw.get("points")
        if not isinstance(controls, list) or len(controls) < 2:
            raise ValueError(f"curve {name} needs at least two control points")
        if not all(isinstance(control, str) and control in points for control in controls):
            unresolved.append(
                f"curve {name} has literal or unknown controls; name its native controls to refine them"
            )
            continue
        style = str(raw.get("style", "guide"))
        if style not in STRUCTURAL_STYLES:
            style = "guide"
        for index, (start, end) in enumerate(zip(controls, controls[1:])):
            links.append(
                {
                    "name": f"curve:{name}:{index + 1}",
                    "from": start,
                    "to": end,
                    "style": style,
                }
            )
    return links, unresolved


def sample_segment(
    distance: np.ndarray,
    start: list[float],
    end: list[float],
) -> np.ndarray:
    height, width = distance.shape
    length = max(1.0, math.dist(start, end))
    count = max(3, int(round(length)) + 1)
    xs = np.clip(
        np.rint(np.linspace(float(start[0]), float(end[0]), count)).astype(int),
        0,
        width - 1,
    )
    ys = np.clip(
        np.rint(np.linspace(float(start[1]), float(end[1]), count)).astype(int),
        0,
        height - 1,
    )
    # Endpoints often carry labels or several crossing strokes.  Keep them in
    # the score, but reduce their leverage by evaluating the central span when
    # a segment has enough native samples.
    values = distance[ys, xs]
    trim = min(max(0, int(round(count * 0.03))), max(0, count // 4))
    return values[trim:-trim] if trim and count - 2 * trim >= 3 else values


def segment_metric(
    distance: np.ndarray,
    start: list[float],
    end: list[float],
    style: str,
) -> dict[str, float]:
    values = sample_segment(distance, start, end)
    if not values.size:
        return {"score": 999.0, "mean_distance": 999.0, "p95_distance": 999.0, "support_2px": 0.0}
    clipped = np.minimum(values.astype(float), 12.0)
    # Dashed and guide lines contain intended gaps.  A lower quantile rewards
    # alignment with their ink runs without incorrectly demanding continuity.
    score = (
        float(np.quantile(clipped, 0.60))
        if style in {"dashed", "guide"}
        else float(clipped.mean())
    )
    return {
        "score": score,
        "mean_distance": float(values.mean()),
        "p95_distance": float(np.quantile(values, 0.95)),
        "support_2px": float((values <= 2.0).mean()),
    }


def all_metrics(
    distance: np.ndarray,
    points: dict[str, list[float]],
    segments: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    return {
        segment["name"]: segment_metric(
            distance,
            points[segment["from"]],
            points[segment["to"]],
            segment["style"],
        )
        for segment in segments
    }


def incident_objective(
    name: str,
    candidate: list[float],
    points: dict[str, list[float]],
    segments: list[dict[str, Any]],
    incident: dict[str, list[dict[str, Any]]],
    distance: np.ndarray,
    original: list[float],
    displacement_weight: float,
) -> float:
    score = 0.0
    weight = 0.0
    for segment in incident.get(name, []):
        start = candidate if segment["from"] == name else points[segment["from"]]
        end = candidate if segment["to"] == name else points[segment["to"]]
        metric = segment_metric(distance, start, end, segment["style"])
        segment_weight = max(8.0, math.dist(start, end))
        score += metric["score"] * segment_weight
        weight += segment_weight
    height, width = distance.shape
    local_x = int(np.clip(round(candidate[0]), 0, width - 1))
    local_y = int(np.clip(round(candidate[1]), 0, height - 1))
    vertex_distance = float(distance[local_y, local_x])
    base = score / max(weight, 1.0)
    displacement = math.dist(candidate, original)
    return base + 0.18 * min(vertex_distance, 8.0) + displacement_weight * displacement


def refine_points(
    distance: np.ndarray,
    initial: dict[str, list[float]],
    segments: list[dict[str, Any]],
    radius: int,
    passes: int,
    locked: set[str],
    displacement_weight: float,
) -> tuple[dict[str, list[float]], list[dict[str, Any]]]:
    height, width = distance.shape
    points = copy.deepcopy(initial)
    incident: dict[str, list[dict[str, Any]]] = {name: [] for name in points}
    for segment in segments:
        if segment["from"] not in points or segment["to"] not in points:
            raise ValueError(
                f"segment {segment['name']} references an unknown endpoint"
            )
        incident[segment["from"]].append(segment)
        incident[segment["to"]].append(segment)

    history: list[dict[str, Any]] = []
    search_steps = sorted({max(1, radius // 2), max(1, radius // 3), 1}, reverse=True)
    for pass_index in range(max(1, passes)):
        changed = 0
        for name in sorted(points):
            if name in locked or not incident.get(name):
                continue
            origin = initial[name]
            current = points[name]
            best = list(current)
            best_score = incident_objective(
                name,
                best,
                points,
                segments,
                incident,
                distance,
                origin,
                displacement_weight,
            )
            for step in search_steps:
                improved = True
                while improved:
                    improved = False
                    for dy in (-step, 0, step):
                        for dx in (-step, 0, step):
                            if dx == 0 and dy == 0:
                                continue
                            candidate = [best[0] + dx, best[1] + dy]
                            if math.dist(candidate, origin) > radius + 1e-9:
                                continue
                            if not (0 <= candidate[0] < width and 0 <= candidate[1] < height):
                                continue
                            score = incident_objective(
                                name,
                                candidate,
                                points,
                                segments,
                                incident,
                                distance,
                                origin,
                                displacement_weight,
                            )
                            if score < best_score - 1e-6:
                                best, best_score, improved = candidate, score, True
                # The next finer step begins from this best shared-junction
                # position rather than independently moving incident lines.
            if best != current:
                points[name] = [float(best[0]), float(best[1])]
                changed += 1
        history.append({"pass": pass_index + 1, "changed_points": changed})
        if changed == 0:
            break
    return points, history


def draw_overlay(
    source: Image.Image,
    before: dict[str, list[float]],
    after: dict[str, list[float]],
    segments: list[dict[str, Any]],
    curve_segments: list[dict[str, Any]],
    output: Path,
) -> None:
    canvas = source.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    before_color = "#d92d20"
    after_colors = {
        "solid": "#087e8b",
        "emphasis": "#0067c5",
        "dashed": "#7a4bc2",
        "guide": "#4f6f52",
    }
    for segment in segments:
        a0, b0 = before[segment["from"]], before[segment["to"]]
        draw.line((*a0, *b0), fill=before_color, width=2)
    for segment in segments:
        a1, b1 = after[segment["from"]], after[segment["to"]]
        color = after_colors[segment["style"]]
        if segment["style"] == "dashed":
            length = max(1.0, math.dist(a1, b1))
            for start in np.arange(0.0, length, 10.0):
                stop = min(length, start + 6.0)
                t0, t1 = start / length, stop / length
                p0 = [a1[0] + (b1[0] - a1[0]) * t0, a1[1] + (b1[1] - a1[1]) * t0]
                p1 = [a1[0] + (b1[0] - a1[0]) * t1, a1[1] + (b1[1] - a1[1]) * t1]
                draw.line((*p0, *p1), fill=color, width=2)
        else:
            draw.line((*a1, *b1), fill=color, width=2)
    for segment in curve_segments:
        a0, b0 = before[segment["from"]], before[segment["to"]]
        a1, b1 = after[segment["from"]], after[segment["to"]]
        draw.line((*a0, *b0), fill="#d92d20", width=2)
        draw.line((*a1, *b1), fill="#7a4bc2", width=3)
    for name, point in sorted(after.items()):
        before_point = before[name]
        draw.ellipse(
            (point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3),
            fill="#12b76a",
            outline="white",
            width=1,
        )
        if math.dist(point, before_point) > 0.25:
            draw.line((*before_point, *point), fill="#f79009", width=2)
        draw.text((point[0] + 4, point[1] - 10), name, fill="#101828", font=font, stroke_width=2, stroke_fill="white")
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="native geometry crop")
    parser.add_argument("manifest", type=Path, help="trace manifest that fixes topology and line roles")
    parser.add_argument(
        "--observation",
        type=Path,
        required=True,
        help="locked source-first observation JSON (never inferred from the manifest)",
    )
    parser.add_argument("--out-observation", type=Path, required=True, help="candidate observation JSON")
    parser.add_argument("--overlay", type=Path, required=True, help="before/after native overlay PNG")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--radius", type=int, default=6, help="maximum native-pixel movement per point")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--locked", default="", help="comma-separated landmark names that must not move")
    parser.add_argument("--displacement-weight", type=float, default=0.035)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    outputs = (args.out_observation, args.overlay, args.report)
    collisions = [str(path) for path in outputs if path.exists()]
    if collisions and not args.overwrite:
        parser.error("output exists; pass --overwrite to replace: " + ", ".join(collisions))
    if args.radius < 0 or args.radius > 30:
        parser.error("--radius must be between 0 and 30 native pixels")
    if args.passes < 1 or args.passes > 12:
        parser.error("--passes must be between 1 and 12")

    source = Image.open(args.source).convert("RGB")
    manifest = load_json(args.manifest)
    if manifest.get("type") != "trace":
        parser.error("manifest type must be trace")
    observation = load_json(args.observation)
    trace = manifest.get("trace_evidence", {})
    if not isinstance(trace, dict):
        parser.error("manifest trace_evidence must be an object")
    raw_points = observation.get("points")
    if not isinstance(raw_points, dict) or not raw_points:
        parser.error("the locked source-first observation must contain source points")
    if any(not _valid_point(value) for value in raw_points.values()):
        parser.error("every source point must be a finite [x, y] pair")
    before = {str(name): [float(value[0]), float(value[1])] for name, value in raw_points.items()}
    if any(not (0 <= p[0] < source.width and 0 <= p[1] < source.height) for p in before.values()):
        parser.error("source point lies outside the native crop")

    segments = normalized_segments(observation.get("segments"))
    curves = observation.get("curves", [])
    curve_segments, curve_warnings = curve_links(curves, before)
    optimization_segments = [*segments, *curve_segments]
    source_hash = sha256(args.source)
    expected_size = observation.get("source_size")
    if expected_size is not None and expected_size != [source.width, source.height]:
        parser.error(f"source size is {[source.width, source.height]}, evidence says {expected_size}")
    expected_hash = observation.get("source_sha256")
    if not expected_hash:
        parser.error("the locked observation must contain source_sha256")
    if expected_hash != source_hash:
        parser.error("source SHA-256 does not match the observation")

    distance, threshold = source_distance(source)
    before_metrics = all_metrics(distance, before, optimization_segments)
    locked = {name.strip() for name in args.locked.split(",") if name.strip()}
    unknown_locked = sorted(locked - before.keys())
    if unknown_locked:
        parser.error("unknown --locked landmarks: " + ", ".join(unknown_locked))
    after, history = refine_points(
        distance,
        before,
        optimization_segments,
        radius=args.radius,
        passes=args.passes,
        locked=locked,
        displacement_weight=args.displacement_weight,
    )
    after_metrics = all_metrics(distance, after, optimization_segments)

    moves = {
        name: {
            "from": [round(value, 3) for value in before[name]],
            "to": [round(value, 3) for value in after[name]],
            "distance_px": round(math.dist(before[name], after[name]), 3),
        }
        for name in sorted(before)
        if math.dist(before[name], after[name]) > 1e-6
    }
    source_record = copy.deepcopy(observation)
    source_record.update(
        {
            "source_image": str(args.source),
            "source_sha256": source_hash,
            "source_size": [source.width, source.height],
            "points": {name: [round(v, 3) for v in after[name]] for name in sorted(after)},
            "segments": segments,
            "curves": curves if isinstance(curves, list) else [],
            "refinement": {
                "status": "candidate-needs-native-overlay-review",
                "tool": "refine_trace.py",
                "tool_version": VERSION,
                "manifest_sha256": sha256(args.manifest),
                "input_observation_sha256": sha256(args.observation),
                "radius_px": args.radius,
                "passes_requested": args.passes,
                "passes_run": history,
                "locked_points": sorted(locked),
                "moves": moves,
            },
        }
    )
    args.out_observation.parent.mkdir(parents=True, exist_ok=True)
    args.out_observation.write_text(
        json.dumps(source_record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    draw_overlay(source, before, after, segments, curve_segments, args.overlay)

    def aggregate(metrics: dict[str, dict[str, float]]) -> dict[str, float]:
        if not metrics:
            return {"mean_score": 0.0, "mean_distance": 0.0, "mean_support_2px": 0.0}
        return {
            "mean_score": float(np.mean([item["score"] for item in metrics.values()])),
            "mean_distance": float(np.mean([item["mean_distance"] for item in metrics.values()])),
            "mean_support_2px": float(np.mean([item["support_2px"] for item in metrics.values()])),
        }

    report = {
        "ok": True,
        "status": "candidate-needs-native-overlay-review",
        "source": str(args.source),
        "source_sha256": source_hash,
        "source_size": [source.width, source.height],
        "manifest": str(args.manifest),
        "manifest_sha256": sha256(args.manifest),
        "input_observation": str(args.observation),
        "candidate_observation": str(args.out_observation),
        "candidate_observation_sha256": sha256(args.out_observation),
        "overlay": str(args.overlay),
        "overlay_sha256": sha256(args.overlay),
        "source_threshold": threshold,
        "radius_px": args.radius,
        "locked_points": sorted(locked),
        "moved_point_count": len(moves),
        "moves": moves,
        "curve_control_links": len(curve_segments),
        "curve_warnings": curve_warnings,
        "before": {"aggregate": aggregate(before_metrics), "segments": before_metrics},
        "after": {"aggregate": aggregate(after_metrics), "segments": after_metrics},
        "review_required": [
            "Every green point is on the intended native source junction.",
            "Every named segment follows the intended source stroke end to end.",
            "No nearby label, shading boundary, or crossing line attracted a point.",
            "Solid, dashed, guide, and emphasis roles still match the source.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
