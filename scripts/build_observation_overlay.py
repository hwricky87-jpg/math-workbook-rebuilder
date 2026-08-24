#!/usr/bin/env python3
"""Render a source-first geometry observation as an auditable native-coordinate overlay.

The observation JSON is never inferred from the manifest.  It is drawn directly on
the source crop, with a coordinate grid, named landmarks, line roles, curve control
points, labels, and dimensions.  A side-by-side image and a machine-readable list of
unresolved records make coordinate mistakes visible before SVG production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from geometry_svg import MARKER_STYLES, resolve_marker_points


COLORS = {
    "solid": (0, 112, 224, 235),
    "dashed": (238, 112, 0, 245),
    "guide": (142, 68, 173, 235),
    "emphasis": (214, 25, 80, 245),
    "curve": (137, 42, 168, 240),
    "point": (0, 145, 70, 255),
    "label": (190, 30, 45, 255),
    "dimension": (0, 135, 145, 255),
    "marker": (215, 30, 120, 255),
    "unresolved": (230, 0, 0, 255),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                pass
    return ImageFont.load_default()


def point_value(value: Any, points: dict[str, tuple[float, float]]) -> tuple[float, float]:
    if isinstance(value, str):
        if value not in points:
            raise ValueError(f"unknown point reference: {value}")
        return points[value]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric coordinate: {value!r}") from exc
    raise ValueError(f"invalid point value: {value!r}")


def dash_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    fill: tuple[int, int, int, int],
    width: int,
    dash: float,
    gap: float,
) -> None:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    ux, uy = dx / length, dy / length
    position = 0.0
    while position < length:
        stop = min(length, position + dash)
        draw.line(
            (
                start[0] + ux * position,
                start[1] + uy * position,
                start[0] + ux * stop,
                start[1] + uy * stop,
            ),
            fill=fill,
            width=width,
        )
        position += dash + gap


def catmull_rom(points: list[tuple[float, float]], samples: int = 16) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    padded = [points[0], *points, points[-1]]
    result: list[tuple[float, float]] = []
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1:index + 3]
        for step in range(samples):
            t = step / samples
            t2, t3 = t * t, t * t * t
            x = 0.5 * (
                2 * p1[0] + (-p0[0] + p2[0]) * t
                + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                2 * p1[1] + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            result.append((x, y))
    result.append(points[-1])
    return result


def item_name(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("name") or item.get("text") or fallback)


def unique_named_items(
    raw: Any,
    kind: str,
    unresolved: list[str],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        unresolved.append(f"{kind} must be a list")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            unresolved.append(f"{kind}[{index}] must be an object")
            continue
        name = item_name(item, "")
        if not name:
            unresolved.append(f"{kind}[{index}] has no name or text")
            name = f"{kind}-{index}"
        if name in seen:
            unresolved.append(f"duplicate {kind} name: {name}")
        seen.add(name)
        if item.get("resolved") is False:
            unresolved.append(f"{kind} marked unresolved: {name}")
        result.append(item)
    return result


def native_points(
    observation: dict[str, Any],
    size: tuple[int, int],
    unresolved: list[str],
) -> dict[str, tuple[float, float]]:
    raw = observation.get("points")
    if not isinstance(raw, dict):
        unresolved.append("points must be an object")
        return {}
    points: dict[str, tuple[float, float]] = {}
    width, height = size
    coordinate_owners: dict[tuple[float, float], str] = {}
    for raw_name, value in raw.items():
        name = str(raw_name)
        if not name:
            unresolved.append("point name cannot be blank")
            continue
        try:
            point = point_value(value, {})
        except ValueError as exc:
            unresolved.append(f"point {name}: {exc}")
            continue
        if not (0 <= point[0] < width and 0 <= point[1] < height):
            unresolved.append(f"point {name} is outside source bounds: {list(point)}")
        if point in coordinate_owners:
            unresolved.append(
                f"duplicate point coordinates: {coordinate_owners[point]} and {name}"
            )
        coordinate_owners[point] = name
        points[name] = point
    return points


def grid_step(width: int, height: int) -> int:
    target = max(width, height) / 10
    magnitude = 10 ** math.floor(math.log10(max(target, 1)))
    return max(1, int(min((1, 2, 5, 10), key=lambda x: abs(x * magnitude - target)) * magnitude))


def parse_roi(
    value: Any, size: tuple[int, int]
) -> tuple[str | None, list[Any] | None, list[float] | None, str | None]:
    if isinstance(value, dict):
        value = value.get("box") if "box" in value else value.get("polygon")
    width, height = size
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    ):
        box = [float(item) for item in value]
        if 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height:
            return "box", box, box, None
        return None, None, None, "ROI box is outside the source crop"
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 3
        and all(
            isinstance(point, (list, tuple))
            and len(point) == 2
            and all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in point
            )
            for point in value
        )
    ):
        polygon = [[float(item) for item in point] for point in value]
        if not all(0 <= x < width and 0 <= y < height for x, y in polygon):
            return None, None, None, "ROI polygon is outside the source crop"
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        return "polygon", polygon, [min(xs), min(ys), max(xs), max(ys)], None
    return None, None, None, "ROI must be a four-number box or a three-point polygon"


def point_in_roi(point: tuple[float, float], kind: str, geometry: list[Any]) -> bool:
    x, y = point
    if kind == "box":
        x0, y0, x1, y1 = geometry
        return x0 <= x <= x1 and y0 <= y <= y1
    inside = False
    for index, (x1, y1) in enumerate(geometry):
        x2, y2 = geometry[(index + 1) % len(geometry)]
        if min(y1, y2) <= y <= max(y1, y2):
            cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
            if abs(cross) <= 1e-9 and min(x1, x2) <= x <= max(x1, x2):
                return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def render_overlay(
    source: Image.Image,
    observation: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    maximum_display_edge: int = 1800,
) -> tuple[Image.Image, Image.Image, dict[str, Any]]:
    unresolved: list[str] = []
    warnings: list[str] = []
    source_rgb = source.convert("RGB")
    native_size = source_rgb.size
    recorded_size = observation.get("source_size")
    if list(recorded_size or []) != list(native_size):
        unresolved.append(
            f"observation source_size={recorded_size!r} does not match source={list(native_size)}"
        )
    points = native_points(observation, native_size, unresolved)
    segments = unique_named_items(observation.get("segments"), "segment", unresolved)
    curves = unique_named_items(observation.get("curves"), "curve", unresolved)
    markers = unique_named_items(observation.get("markers"), "marker", unresolved)
    labels = unique_named_items(observation.get("labels"), "label", unresolved)
    dimensions = unique_named_items(observation.get("dimensions"), "dimension", unresolved)

    scale = min(6.0, max(1.0, maximum_display_edge / max(native_size)))
    display_size = (
        max(1, int(round(native_size[0] * scale))),
        max(1, int(round(native_size[1] * scale))),
    )
    source_display = source_rgb.resize(display_size, Image.Resampling.LANCZOS)
    overlay = source_display.convert("RGBA")
    tint = Image.new("RGBA", display_size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(tint)
    font = load_font(max(12, int(round(11 * scale))))
    small_font = load_font(max(10, int(round(8 * scale))))
    line_width = max(2, int(round(1.5 * scale)))
    point_radius = max(3, int(round(2.5 * scale)))

    def display(point: tuple[float, float]) -> tuple[float, float]:
        return point[0] * scale, point[1] * scale

    step = grid_step(*native_size)
    for x in range(0, native_size[0], step):
        dx = int(round(x * scale))
        draw.line((dx, 0, dx, display_size[1]), fill=(30, 95, 190, 35), width=1)
        draw.text((dx + 2, 2), str(x), fill=(15, 65, 140, 170), font=small_font)
    for y in range(0, native_size[1], step):
        dy = int(round(y * scale))
        draw.line((0, dy, display_size[0], dy), fill=(30, 95, 190, 35), width=1)
        draw.text((2, dy + 2), str(y), fill=(15, 65, 140, 170), font=small_font)

    roi_key = next(
        (key for key in ("source_geometry_roi", "geometry_roi") if key in observation),
        None,
    )
    roi_box: list[float] | None = None
    roi_kind: str | None = None
    roi_geometry: list[Any] | None = None
    if roi_key:
        raw_roi = observation.get(roi_key)
        roi_kind, roi_geometry, roi_box, roi_error = parse_roi(raw_roi, native_size)
        if roi_error:
            unresolved.append(f"{roi_key}: {roi_error}")
        else:
            assert roi_kind and roi_geometry and roi_box
            for point_name, (px, py) in points.items():
                if not point_in_roi((px, py), roi_kind, roi_geometry):
                    unresolved.append(
                        f"point {point_name} lies outside the source geometry ROI"
                    )
            roi_color = (205, 0, 165, 240)
            if roi_kind == "box":
                x0, y0, x1, y1 = [value * scale for value in roi_geometry]
                draw.rectangle(
                    (x0, y0, x1, y1), outline=roi_color, width=max(2, line_width)
                )
                label_x, label_y = x0, y0
            else:
                display_polygon = [
                    (point[0] * scale, point[1] * scale) for point in roi_geometry
                ]
                draw.line(
                    [*display_polygon, display_polygon[0]],
                    fill=roi_color,
                    width=max(2, line_width),
                    joint="curve",
                )
                label_x = min(point[0] for point in display_polygon)
                label_y = min(point[1] for point in display_polygon)
            draw.text(
                (label_x + 3, max(2, label_y - 16 * scale)),
                f"SOURCE-FIRST ROI [{roi_kind}]",
                fill=roi_color,
                font=small_font,
                stroke_width=1,
                stroke_fill=(255, 255, 255, 235),
            )
        roi_method = observation.get(f"{roi_key}_method")
        if not str(roi_method or "").strip():
            unresolved.append(f"{roi_key}_method is required")
        elif any(
            phrase in str(roi_method).lower().replace("_", " ")
            for phrase in (
                "trace-point hull", "trace point hull", "trace points hull",
                "derived from trace", "generated from trace",
            )
        ):
            unresolved.append("source geometry ROI cannot be derived from the trace-point hull")
        if manifest is not None:
            manifest_roi = None
            trace_evidence = manifest.get("trace_evidence")
            if isinstance(trace_evidence, dict):
                manifest_roi = trace_evidence.get(roi_key)
            if manifest_roi is None:
                manifest_roi = manifest.get(roi_key)
            if raw_roi != manifest_roi:
                unresolved.append(f"manifest {roi_key} does not match the observation")
    else:
        unresolved.append(
            "source_geometry_roi is required; define it by inspecting every source geometry "
            "component before tracing, never from a trace-point hull"
        )

    endpoint_owners: dict[tuple[str, str], str] = {}
    for index, item in enumerate(segments):
        name = item_name(item, f"segment-{index}")
        try:
            start = point_value(item.get("from"), points)
            end = point_value(item.get("to"), points)
        except ValueError as exc:
            unresolved.append(f"segment {name}: {exc}")
            continue
        if start == end:
            unresolved.append(f"segment {name} has zero length")
        endpoint_key = tuple(sorted((str(item.get("from")), str(item.get("to")))))
        if endpoint_key in endpoint_owners:
            unresolved.append(
                f"duplicate segment endpoints: {endpoint_owners[endpoint_key]} and {name}"
            )
        endpoint_owners[endpoint_key] = name
        style = str(item.get("style", "solid"))
        if style not in {"solid", "dashed", "guide", "emphasis"}:
            unresolved.append(f"segment {name} has unsupported style: {style}")
        color = COLORS.get(style, COLORS["unresolved"])
        a, b = display(start), display(end)
        if style in {"dashed", "guide"}:
            dash_line(draw, a, b, color, line_width, 8 * scale, 5 * scale)
        else:
            draw.line((*a, *b), fill=color, width=line_width)
        midpoint = ((a[0] + b[0]) / 2 + 3, (a[1] + b[1]) / 2 + 3)
        draw.text(midpoint, f"{name} [{style}]", fill=color, font=small_font,
                  stroke_width=max(1, int(scale // 2)), stroke_fill=(255, 255, 255, 230))

    for index, item in enumerate(curves):
        name = item_name(item, f"curve-{index}")
        raw_curve_points = item.get("points")
        if not isinstance(raw_curve_points, list) or len(raw_curve_points) < 2:
            unresolved.append(f"curve {name} requires at least two points")
            continue
        if item.get("smooth", True) and len(raw_curve_points) < 4:
            warnings.append(
                f"smooth curve {name} has {len(raw_curve_points)} control points; 4-5 is recommended"
            )
        try:
            controls = [point_value(value, points) for value in raw_curve_points]
        except ValueError as exc:
            unresolved.append(f"curve {name}: {exc}")
            continue
        rendered = catmull_rom(controls) if item.get("smooth", True) else controls
        rendered_display = [display(point) for point in rendered]
        curve_style = str(item.get("style", "solid"))
        color = (
            COLORS["curve"]
            if curve_style in {"solid", "dashed", "guide", "emphasis"}
            else COLORS["unresolved"]
        )
        if curve_style not in {"solid", "dashed", "guide", "emphasis"}:
            unresolved.append(f"curve {name} has unsupported style: {curve_style}")
        if str(item.get("style", "solid")) in {"dashed", "guide"}:
            for left, right in zip(rendered_display, rendered_display[1:]):
                dash_line(draw, left, right, color, line_width, 6 * scale, 4 * scale)
        else:
            draw.line(rendered_display, fill=color, width=line_width, joint="curve")
        for number, control in enumerate(controls):
            cx, cy = display(control)
            draw.ellipse(
                (cx - point_radius, cy - point_radius, cx + point_radius, cy + point_radius),
                outline=color,
                width=max(1, line_width - 1),
            )
            draw.text((cx + 3, cy + 3), str(number), fill=color, font=small_font,
                      stroke_width=1, stroke_fill=(255, 255, 255, 230))
        anchor = display(controls[len(controls) // 2])
        draw.text((anchor[0] + 4, anchor[1] - 4), f"{name} [curve/{curve_style}]", fill=color,
                  font=small_font, stroke_width=1, stroke_fill=(255, 255, 255, 230))

    for index, item in enumerate(markers):
        name = item_name(item, f"marker-{index}")
        try:
            marker_points = resolve_marker_points(item, points)
        except (KeyError, TypeError, ValueError) as exc:
            unresolved.append(f"marker {name}: {exc}")
            continue
        marker_style = str(item.get("style", "solid"))
        if marker_style not in MARKER_STYLES:
            # resolve_marker_points already reports this, but keep the drawing
            # branch explicit if the shared resolver changes in the future.
            unresolved.append(f"marker {name} has unsupported style: {marker_style}")
            continue
        if roi_kind and roi_geometry:
            for number, marker_point in enumerate(marker_points):
                if not point_in_roi(marker_point, roi_kind, roi_geometry):
                    unresolved.append(
                        f"marker {name} point {number} lies outside the source geometry ROI"
                    )
        rendered = [display(point) for point in marker_points]
        color = COLORS["marker"]
        if marker_style in {"dashed", "guide"}:
            for left, right in zip(rendered, rendered[1:]):
                dash_line(draw, left, right, color, line_width, 5 * scale, 3.5 * scale)
        else:
            draw.line(rendered, fill=color, width=line_width, joint="curve")
        for number, marker_point in enumerate(rendered):
            mx, my = marker_point
            draw.ellipse(
                (mx - point_radius, my - point_radius, mx + point_radius, my + point_radius),
                outline=color,
                width=max(1, line_width - 1),
            )
            draw.text((mx + 3, my + 3), str(number), fill=color, font=small_font,
                      stroke_width=1, stroke_fill=(255, 255, 255, 230))
        anchor = rendered[len(rendered) // 2]
        marker_kind = str(item.get("kind", ""))
        draw.text(
            (anchor[0] + 4, anchor[1] - 4),
            f"{name} [marker/{marker_kind}/{marker_style}]",
            fill=color,
            font=small_font,
            stroke_width=1,
            stroke_fill=(255, 255, 255, 230),
        )

    def draw_annotation(item: dict[str, Any], index: int, kind: str) -> None:
        name = item_name(item, f"{kind}-{index}")
        anchor_value = item.get("at")
        annotation_path: list[tuple[float, float]] = []
        if kind == "dimension" and isinstance(item.get("points"), list):
            try:
                annotation_path = [point_value(value, points) for value in item["points"]]
            except ValueError as exc:
                unresolved.append(f"{kind} {name}: {exc}")
                return
        elif kind == "dimension" and item.get("from") is not None and item.get("to") is not None:
            try:
                left = point_value(item.get("from"), points)
                right = point_value(item.get("to"), points)
                annotation_path = [left, right]
            except ValueError as exc:
                unresolved.append(f"{kind} {name}: {exc}")
                return
        if anchor_value is None and annotation_path:
            anchor = annotation_path[len(annotation_path) // 2]
            if len(annotation_path) == 2:
                anchor = (
                    (annotation_path[0][0] + annotation_path[1][0]) / 2,
                    (annotation_path[0][1] + annotation_path[1][1]) / 2,
                )
        else:
            try:
                anchor = point_value(anchor_value, points)
            except ValueError as exc:
                unresolved.append(f"{kind} {name}: {exc}")
                return
        dx, dy = display(anchor)
        color = COLORS[kind]
        radius = point_radius + 1
        if annotation_path:
            display_path = [display(point) for point in annotation_path]
            for left, right in zip(display_path, display_path[1:]):
                dash_line(draw, left, right, color, line_width, 6 * scale, 4 * scale)
            for px, py in display_path:
                draw.ellipse(
                    (px - point_radius, py - point_radius, px + point_radius, py + point_radius),
                    outline=color,
                    width=max(1, line_width - 1),
                )
        draw.line((dx - radius, dy, dx + radius, dy), fill=color, width=line_width)
        draw.line((dx, dy - radius, dx, dy + radius), fill=color, width=line_width)
        shown = str(item.get("text") or name)
        draw.text((dx + radius + 2, dy - radius), f"{kind}: {shown}", fill=color,
                  font=font, stroke_width=1, stroke_fill=(255, 255, 255, 235))

    for index, item in enumerate(labels):
        draw_annotation(item, index, "label")
    for index, item in enumerate(dimensions):
        draw_annotation(item, index, "dimension")

    for name, point in points.items():
        x, y = display(point)
        draw.ellipse(
            (x - point_radius, y - point_radius, x + point_radius, y + point_radius),
            fill=COLORS["point"],
            outline=(255, 255, 255, 255),
            width=max(1, line_width // 2),
        )
        draw.text((x + point_radius + 2, y - point_radius - 2), name,
                  fill=COLORS["point"], font=font, stroke_width=1,
                  stroke_fill=(255, 255, 255, 240))

    if manifest is not None:
        for key, kind in (
            ("required_segments", "segment"),
            ("required_curves", "curve"),
            ("required_markers", "marker"),
            ("required_labels", "label"),
            ("required_dimensions", "dimension"),
        ):
            required = manifest.get(key)
            if required is None:
                continue
            if not isinstance(required, list):
                unresolved.append(f"manifest {key} must be a list")
                continue
            kind_items = {
                "segment": segments,
                "curve": curves,
                "marker": markers,
                "label": labels,
                "dimension": dimensions,
            }[kind]
            observed_items = {item_name(item, "") for item in kind_items}
            if kind in {"label", "dimension"}:
                observed_items.update(
                    str(item.get("text")) for item in kind_items if item.get("text")
                )
            required_names = {str(value) for value in required}
            missing = sorted(required_names - observed_items)
            if missing:
                unresolved.append(f"{key} missing from observation: {', '.join(missing)}")
            extra = sorted(observed_items - required_names)
            if extra and kind in {"segment", "curve", "marker"}:
                unresolved.append(f"observation {kind}s absent from {key}: {', '.join(extra)}")

    overlay = Image.alpha_composite(overlay, tint).convert("RGB")
    gutter = max(12, int(round(12 * scale)))
    side = Image.new("RGB", (display_size[0] * 2 + gutter, display_size[1]), "white")
    side.paste(source_display, (0, 0))
    side.paste(overlay, (display_size[0] + gutter, 0))
    report = {
        "ok": not unresolved,
        "source_size": list(native_size),
        "display_size": list(display_size),
        "display_scale": scale,
        "coordinate_frame": "source-crop native pixels",
        "grid_step_native_px": step,
        "source_geometry_roi": roi_box,
        "source_geometry_roi_kind": roi_kind,
        "source_geometry_roi_geometry": roi_geometry,
        "source_geometry_roi_field": roi_key,
        "counts": {
            "points": len(points),
            "segments": len(segments),
            "curves": len(curves),
            "markers": len(markers),
            "labels": len(labels),
            "dimensions": len(dimensions),
        },
        "unresolved": unresolved,
        "warnings": warnings,
    }
    return overlay, side, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="native source crop")
    parser.add_argument("observation", type=Path, help="source-first observation JSON")
    parser.add_argument("--manifest", type=Path, help="optional manifest for required-item census")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--maximum-display-edge", type=int, default=1800)
    args = parser.parse_args()
    if args.maximum_display_edge < 256:
        parser.error("--maximum-display-edge must be at least 256")

    source = Image.open(args.source).convert("RGB")
    observation = json.loads(args.observation.read_text(encoding="utf-8"))
    manifest = (
        json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    )
    overlay, side, report = render_overlay(
        source,
        observation,
        manifest=manifest,
        maximum_display_edge=args.maximum_display_edge,
    )
    source_hash = sha256_file(args.source)
    report["source"] = str(args.source.resolve())
    report["source_sha256"] = source_hash
    report["observation"] = str(args.observation.resolve())
    report["observation_sha256"] = sha256_file(args.observation)
    if observation.get("source_sha256") != source_hash:
        report["unresolved"].insert(0, "observation source_sha256 is stale for the source crop")
        report["ok"] = False
    if args.manifest:
        report["manifest"] = str(args.manifest.resolve())
        report["manifest_sha256"] = sha256_file(args.manifest)

    args.out.mkdir(parents=True, exist_ok=True)
    source.save(args.out / "source-roi.png")
    overlay.save(args.out / "observation-overlay.png")
    side.save(args.out / "source-vs-observation.png")
    roi_box = report.get("source_geometry_roi")
    if isinstance(roi_box, list) and len(roi_box) == 4:
        native_roi = tuple(int(round(value)) for value in roi_box)
        source_roi = source.crop(native_roi)
        scale = float(report["display_scale"])
        display_roi = tuple(int(round(value * scale)) for value in roi_box)
        overlay_roi = overlay.crop(display_roi)
        source_roi_display = source_roi.resize(overlay_roi.size, Image.Resampling.LANCZOS)
        gutter = max(10, int(round(10 * scale)))
        roi_side = Image.new(
            "RGB",
            (source_roi_display.width * 2 + gutter, source_roi_display.height),
            "white",
        )
        roi_side.paste(source_roi_display, (0, 0))
        roi_side.paste(overlay_roi, (source_roi_display.width + gutter, 0))
        roi_side.save(args.out / "geometry-roi-vs-observation.png")
    report_path = args.out / "observation-overlay.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
