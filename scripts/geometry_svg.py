#!/usr/bin/env python3
"""Deterministic SVG renderer for photographed workbook geometry.

The JSON manifest keeps observed 2-D coordinates separate from styling. It can
render explicit traces, regular projected prisms, and polygon-face nets whose
fold lines are derived from shared edges.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RENDER_STYLE = {
    "solid_width": 2.6,
    "dashed_width": 2.3,
    "guide_width": 1.6,
    "emphasis_width": 3.4,
    "label_size": 18.0,
    "measure_size": 16.0,
    "label_weight": 700.0,
    "measure_weight": 600.0,
    "label_outline": 0.65,
    "measure_outline": 0.45,
    "label_halo": 2.4,
    "measure_halo": 2.0,
}

STYLE_LIMITS = {
    "solid_width": (0.75, 6.0),
    "dashed_width": (0.75, 6.0),
    "guide_width": (0.5, 4.0),
    "emphasis_width": (1.0, 8.0),
    "label_size": (9.0, 36.0),
    "measure_size": (9.0, 36.0),
    "label_weight": (400.0, 800.0),
    "measure_weight": (400.0, 800.0),
    "label_outline": (0.0, 2.0),
    "measure_outline": (0.0, 2.0),
    "label_halo": (0.0, 6.0),
    "measure_halo": (0.0, 6.0),
}

FONT_FAMILY_CSS = "'Noto Sans KR','Malgun Gothic','Apple SD Gothic Neo',Arial,sans-serif"
FONT_FAMILY_ATTR = "Noto Sans KR, Malgun Gothic, Apple SD Gothic Neo, Arial, sans-serif"


def _resolve_render_style(manifest: dict[str, Any]) -> dict[str, float]:
    requested = manifest.get("render_style", {})
    if not isinstance(requested, dict):
        raise TypeError("render_style must be an object")
    unknown = sorted(set(requested) - set(DEFAULT_RENDER_STYLE))
    if unknown:
        raise ValueError(f"unknown render_style keys: {', '.join(unknown)}")
    resolved = dict(DEFAULT_RENDER_STYLE)
    for key, value in requested.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"render_style.{key} must be numeric")
        number = float(value)
        low, high = STYLE_LIMITS[key]
        if not low <= number <= high:
            raise ValueError(f"render_style.{key} must be between {low:g} and {high:g}")
        resolved[key] = number
    return resolved


def _style_maps(profile: dict[str, float]) -> tuple[str, dict[str, str], dict[str, str]]:
    solid_width = _num(profile["solid_width"])
    dashed_width = _num(profile["dashed_width"])
    guide_width = _num(profile["guide_width"])
    emphasis_width = _num(profile["emphasis_width"])
    label_size = _num(profile["label_size"])
    measure_size = _num(profile["measure_size"])
    label_weight = _num(profile["label_weight"])
    measure_weight = _num(profile["measure_weight"])
    label_outline = _num(profile["label_outline"])
    measure_outline = _num(profile["measure_outline"])
    label_halo = _num(profile["label_halo"])
    measure_halo = _num(profile["measure_halo"])
    style = f"""
.solid{{fill:none;stroke:#28334a;stroke-width:{solid_width};stroke-linejoin:round;stroke-linecap:round}}
.dashed{{fill:none;stroke:#28334a;stroke-width:{dashed_width};stroke-dasharray:8 5.5;stroke-linejoin:round;stroke-linecap:round}}
.guide{{fill:none;stroke:#687386;stroke-width:{guide_width};stroke-dasharray:3.5 4.5;stroke-linejoin:round;stroke-linecap:round}}
.emphasis{{fill:none;stroke:#28334a;stroke-width:{emphasis_width};stroke-linejoin:round;stroke-linecap:round}}
.face-top{{fill:#f4f5f8;stroke:none}}.face-side{{fill:#dce1e8;stroke:none}}.face-dark{{fill:#c8cfd9;stroke:none}}
.label-halo{{font-family:{FONT_FAMILY_CSS};font-size:{label_size}px;font-weight:{label_weight};fill:#fff;stroke:#fff;stroke-width:{label_halo}px;stroke-linejoin:round}}
.measure-halo{{font-family:{FONT_FAMILY_CSS};font-size:{measure_size}px;font-weight:{measure_weight};fill:#fff;stroke:#fff;stroke-width:{measure_halo}px;stroke-linejoin:round}}
.label{{font-family:{FONT_FAMILY_CSS};font-size:{label_size}px;font-weight:{label_weight};fill:#202a3d;paint-order:stroke fill;stroke:#202a3d;stroke-width:{label_outline}px;stroke-linejoin:round}}
.measure{{font-family:{FONT_FAMILY_CSS};font-size:{measure_size}px;font-weight:{measure_weight};fill:#202a3d;paint-order:stroke fill;stroke:#202a3d;stroke-width:{measure_outline}px;stroke-linejoin:round}}
.point{{fill:#28334a;stroke:none}}
"""
    inline = {
        "solid": f"fill:none;stroke:#28334a;stroke-width:{solid_width};stroke-linejoin:round;stroke-linecap:round",
        "dashed": f"fill:none;stroke:#28334a;stroke-width:{dashed_width};stroke-dasharray:8 5.5;stroke-linejoin:round;stroke-linecap:round",
        "guide": f"fill:none;stroke:#687386;stroke-width:{guide_width};stroke-dasharray:3.5 4.5;stroke-linejoin:round;stroke-linecap:round",
        "emphasis": f"fill:none;stroke:#28334a;stroke-width:{emphasis_width};stroke-linejoin:round;stroke-linecap:round",
        "face-top": "fill:#f4f5f8;stroke:none",
        "face-side": "fill:#dce1e8;stroke:none",
        "face-dark": "fill:#c8cfd9;stroke:none",
        "label-halo": f"font-family:{FONT_FAMILY_CSS};font-size:{label_size}px;font-weight:{label_weight};fill:#fff;stroke:#fff;stroke-width:{label_halo}px;stroke-linejoin:round",
        "measure-halo": f"font-family:{FONT_FAMILY_CSS};font-size:{measure_size}px;font-weight:{measure_weight};fill:#fff;stroke:#fff;stroke-width:{measure_halo}px;stroke-linejoin:round",
        "label": f"font-family:{FONT_FAMILY_CSS};font-size:{label_size}px;font-weight:{label_weight};fill:#202a3d;paint-order:stroke fill;stroke:#202a3d;stroke-width:{label_outline}px;stroke-linejoin:round",
        "measure": f"font-family:{FONT_FAMILY_CSS};font-size:{measure_size}px;font-weight:{measure_weight};fill:#202a3d;paint-order:stroke fill;stroke:#202a3d;stroke-width:{measure_outline}px;stroke-linejoin:round",
        "point": "fill:#28334a;stroke:none",
    }
    presentation = {
        "solid": f'fill="none" stroke="#28334a" stroke-width="{solid_width}" stroke-linejoin="round" stroke-linecap="round"',
        "dashed": f'fill="none" stroke="#28334a" stroke-width="{dashed_width}" stroke-dasharray="8 5.5" stroke-linejoin="round" stroke-linecap="round"',
        "guide": f'fill="none" stroke="#687386" stroke-width="{guide_width}" stroke-dasharray="3.5 4.5" stroke-linejoin="round" stroke-linecap="round"',
        "emphasis": f'fill="none" stroke="#28334a" stroke-width="{emphasis_width}" stroke-linejoin="round" stroke-linecap="round"',
        "face-top": 'fill="#f4f5f8" stroke="none"',
        "face-side": 'fill="#dce1e8" stroke="none"',
        "face-dark": 'fill="#c8cfd9" stroke="none"',
        "label-halo": f'fill="#fff" stroke="#fff" stroke-width="{label_halo}" stroke-linejoin="round" font-family="{FONT_FAMILY_ATTR}" font-size="{label_size}" font-weight="{label_weight}"',
        "measure-halo": f'fill="#fff" stroke="#fff" stroke-width="{measure_halo}" stroke-linejoin="round" font-family="{FONT_FAMILY_ATTR}" font-size="{measure_size}" font-weight="{measure_weight}"',
        "label": f'fill="#202a3d" stroke="#202a3d" stroke-width="{label_outline}" paint-order="stroke fill" stroke-linejoin="round" font-family="{FONT_FAMILY_ATTR}" font-size="{label_size}" font-weight="{label_weight}"',
        "measure": f'fill="#202a3d" stroke="#202a3d" stroke-width="{measure_outline}" paint-order="stroke fill" stroke-linejoin="round" font-family="{FONT_FAMILY_ATTR}" font-size="{measure_size}" font-weight="{measure_weight}"',
        "point": 'fill="#28334a" stroke="none"',
    }
    return style, inline, presentation

Point = tuple[float, float]


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _points_text(points: Iterable[Point]) -> str:
    return " ".join(f"{_num(x)},{_num(y)}" for x, y in points)


def _edge_key(a: Point, b: Point, tolerance: float = 0.25) -> tuple[tuple[int, int], tuple[int, int]]:
    def q(p: Point) -> tuple[int, int]:
        return (round(p[0] / tolerance), round(p[1] / tolerance))
    return tuple(sorted((q(a), q(b))))  # type: ignore[return-value]


def regular_polygon(n: int, cx: float, cy: float, radius: float, rotation: float = 0.0,
                    y_scale: float = 1.0) -> list[Point]:
    return [
        (
            cx + radius * math.cos(math.radians(rotation + 360 * i / n)),
            cy - radius * y_scale * math.sin(math.radians(rotation + 360 * i / n)),
        )
        for i in range(n)
    ]


def polygon_on_edge(n: int, p: Point, q: Point, away: Point) -> list[Point]:
    """Return a regular n-gon attached to p-q on the side opposite away."""
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("polygon_on_edge requires a non-zero edge")
    nx, ny = -dy / length, dx / length
    if (mx + nx - away[0]) ** 2 + (my + ny - away[1]) ** 2 < (
        (mx - away[0]) ** 2 + (my - away[1]) ** 2
    ):
        nx, ny = -nx, -ny
    apothem = length / (2 * math.tan(math.pi / n))
    cx, cy = mx + nx * apothem, my + ny * apothem
    radius = length / (2 * math.sin(math.pi / n))
    angle0 = math.degrees(math.atan2(p[1] - cy, p[0] - cx))
    points = [
        (
            cx + radius * math.cos(math.radians(angle0 + 360 * i / n)),
            cy + radius * math.sin(math.radians(angle0 + 360 * i / n)),
        )
        for i in range(n)
    ]
    # Reverse if necessary so the second point is q, preserving the shared edge.
    if math.dist(points[1], q) > math.dist(points[-1], q):
        points = [points[0], *reversed(points[1:])]
    return points


def net_report(faces: list[list[Point]], tolerance: float = 0.25) -> dict[str, Any]:
    counts: dict[Any, int] = defaultdict(int)
    owners: dict[Any, list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for i, a in enumerate(face):
            key = _edge_key(a, face[(i + 1) % len(face)], tolerance)
            counts[key] += 1
            owners[key].append(face_index)

    folds = [key for key, count in counts.items() if count == 2]
    boundary = [key for key, count in counts.items() if count == 1]
    bad = [key for key, count in counts.items() if count > 2]

    parent = list(range(len(faces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for key in folds:
        left, right = owners[key][:2]
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    connected = bool(faces) and len({find(i) for i in range(len(faces))}) == 1
    return {
        "faces": len(faces),
        "folds": len(folds),
        "boundary": len(boundary),
        "bad_shared_edges": len(bad),
        "connected": connected,
    }


class SVGDocument:
    def __init__(self, width: float, height: float, view_box: list[float] | None = None,
                 render_style: dict[str, float] | None = None):
        self.width = width
        self.height = height
        self.view_box = view_box or [0.0, 0.0, width, height]
        self.items: list[str] = []
        self.render_style = render_style or dict(DEFAULT_RENDER_STYLE)
        self.style, self.inline_style, self.presentation = _style_maps(self.render_style)

    def polygon(self, points: list[Point], css_class: str = "solid", fill: str | None = None,
                stroke: bool = True) -> None:
        style = self.inline_style.get(css_class, self.inline_style["solid"])
        presentation = self.presentation.get(css_class, self.presentation["solid"])
        if fill is not None:
            style += f";fill:{html.escape(fill)}"
            presentation = re.sub(r'fill="[^"]*"', f'fill="{html.escape(fill)}"', presentation)
        if not stroke:
            style += ";stroke:none"
            presentation = re.sub(r'stroke="[^"]*"', 'stroke="none"', presentation)
        self.items.append(
            f'<polygon class="{html.escape(css_class)}" points="{_points_text(points)}"'
            f' {presentation} style="{style}"/>'
        )

    def line(self, a: Point, b: Point, css_class: str = "solid", name: str | None = None) -> None:
        name_attr = f' data-name="{html.escape(name)}"' if name else ""
        style = self.inline_style.get(css_class, self.inline_style["solid"])
        presentation = self.presentation.get(css_class, self.presentation["solid"])
        self.items.append(
            f'<line class="{html.escape(css_class)}" x1="{_num(a[0])}" y1="{_num(a[1])}" '
            f'x2="{_num(b[0])}" y2="{_num(b[1])}" {presentation} style="{style}"{name_attr}/>'
        )

    def path(self, points: list[Point], css_class: str = "solid", smooth: bool = False,
             name: str | None = None) -> None:
        if len(points) < 2:
            raise ValueError("a path needs at least two points")
        if not smooth or len(points) < 3:
            d = "M " + " L ".join(f"{_num(x)} {_num(y)}" for x, y in points)
        else:
            # Catmull-Rom to cubic Bezier conversion through every observed point.
            d = f"M {_num(points[0][0])} {_num(points[0][1])}"
            padded = [points[0], *points, points[-1]]
            for i in range(1, len(padded) - 2):
                p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
                c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
                c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
                d += (
                    f" C {_num(c1[0])} {_num(c1[1])} {_num(c2[0])} {_num(c2[1])}"
                    f" {_num(p2[0])} {_num(p2[1])}"
                )
        name_attr = f' data-name="{html.escape(name)}"' if name else ""
        style = self.inline_style.get(css_class, self.inline_style["solid"])
        presentation = self.presentation.get(css_class, self.presentation["solid"])
        self.items.append(
            f'<path class="{html.escape(css_class)}" d="{d}" {presentation} style="{style}"{name_attr}/>'
        )

    def text(self, point: Point, content: str, css_class: str = "label", anchor: str = "middle",
             name: str | None = None) -> None:
        name_attr = f' data-name="{html.escape(name)}"' if name else ""
        style = self.inline_style.get(css_class, self.inline_style["label"])
        presentation = self.presentation.get(css_class, self.presentation["label"])
        escaped_content = html.escape(str(content))
        if css_class in {"label", "measure"} and self.render_style[f"{css_class}_halo"] > 0:
            halo_class = f"{css_class}-halo"
            self.items.append(
                f'<text class="{halo_class}" x="{_num(point[0])}" y="{_num(point[1])}" '
                f'text-anchor="{html.escape(anchor)}" {self.presentation[halo_class]} '
                f'style="{self.inline_style[halo_class]}" aria-hidden="true">'
                f'{escaped_content}</text>'
            )
        self.items.append(
            f'<text class="{html.escape(css_class)}" x="{_num(point[0])}" y="{_num(point[1])}" '
            f'text-anchor="{html.escape(anchor)}" {presentation} style="{style}"{name_attr}>'
            f'{escaped_content}</text>'
        )

    def dot(self, point: Point, radius: float = 2.7) -> None:
        self.items.append(
            f'<circle class="point" cx="{_num(point[0])}" cy="{_num(point[1])}" r="{_num(radius)}" '
            f'{self.presentation["point"]} style="{self.inline_style["point"]}"/>'
        )

    def serialize(self) -> str:
        x, y, w, h = self.view_box
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(self.width)}" height="{_num(self.height)}" '
            f'viewBox="{_num(x)} {_num(y)} {_num(w)} {_num(h)}">'
            f'<style>{self.style}</style>{"".join(self.items)}</svg>\n'
        )


def _resolve_point(value: Any, points: dict[str, Point]) -> Point:
    if isinstance(value, str):
        if value not in points:
            raise KeyError(f"unknown point: {value}")
        return points[value]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    raise TypeError(f"invalid point: {value!r}")


def _resolved_face(face: Any, points: dict[str, Point]) -> list[Point]:
    values = face.get("points", face) if isinstance(face, dict) else face
    return [_resolve_point(value, points) for value in values]


def _prism(doc: SVGDocument, spec: dict[str, Any]) -> dict[str, Point]:
    n = int(spec["n"])
    cx = float(spec.get("cx", doc.width / 2))
    cy = float(spec.get("cy", 45))
    radius = float(spec.get("radius", 60))
    y_scale = float(spec.get("y_scale", 0.42))
    height = float(spec.get("height", 100))
    rotation = float(spec.get("rotation", 90))
    hidden = bool(spec.get("hidden", True))

    top = regular_polygon(n, cx, cy, radius, rotation, y_scale)
    bottom = [(x, y + height) for x, y in top]
    face_visible = [
        math.sin(math.radians(rotation + 360 * (i + 0.5) / n)) < -1e-9 for i in range(n)
    ]
    vertex_visible = [face_visible[i] or face_visible[(i - 1) % n] for i in range(n)]

    visible_index = 0
    for i in range(n):
        if face_visible[i]:
            j = (i + 1) % n
            fill_class = "face-side" if visible_index % 2 == 0 else "face-dark"
            doc.polygon([top[i], top[j], bottom[j], bottom[i]], fill_class, stroke=False)
            visible_index += 1
    doc.polygon(top, "face-top", stroke=False)

    if hidden:
        for i in range(n):
            j = (i + 1) % n
            if not face_visible[i]:
                doc.line(bottom[i], bottom[j], "dashed", f"bottom-{i}-{j}")
            if not vertex_visible[i]:
                doc.line(top[i], bottom[i], "dashed", f"vertical-{i}")

    for i in range(n):
        j = (i + 1) % n
        doc.line(top[i], top[j], "solid", f"top-{i}-{j}")
        if face_visible[i]:
            doc.line(bottom[i], bottom[j], "solid", f"bottom-{i}-{j}")
        if vertex_visible[i]:
            doc.line(top[i], bottom[i], "solid", f"vertical-{i}")

    return {**{f"T{i}": point for i, point in enumerate(top)}, **{f"B{i}": point for i, point in enumerate(bottom)}}


def render_manifest(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    canvas = manifest.get("canvas", {})
    width = float(canvas.get("width", 320))
    height = float(canvas.get("height", 240))
    view_box = [float(v) for v in canvas.get("viewBox", [0, 0, width, height])]
    render_style = _resolve_render_style(manifest)
    doc = SVGDocument(width, height, view_box, render_style)
    points: dict[str, Point] = {
        name: _resolve_point(value, {}) for name, value in manifest.get("points", {}).items()
    }
    report: dict[str, Any] = {
        "type": manifest.get("type", "trace"),
        "render_style": render_style,
    }

    if manifest.get("type") == "prism":
        generated = _prism(doc, manifest["prism"])
        generated.update(points)
        points = generated

    faces = [_resolved_face(face, points) for face in manifest.get("faces", [])]
    if manifest.get("type") == "net":
        report["net"] = net_report(faces, float(manifest.get("edge_tolerance", 0.25)))
        for index, face in enumerate(faces):
            fill = manifest.get("faces", [])[index].get("fill", "#eef0f4") if isinstance(
                manifest.get("faces", [])[index], dict
            ) else "#eef0f4"
            doc.polygon(face, "face-side", fill=fill, stroke=False)
        counts: dict[Any, int] = defaultdict(int)
        edge_value: dict[Any, tuple[Point, Point]] = {}
        tolerance = float(manifest.get("edge_tolerance", 0.25))
        for face in faces:
            for i, a in enumerate(face):
                b = face[(i + 1) % len(face)]
                key = _edge_key(a, b, tolerance)
                counts[key] += 1
                edge_value[key] = (a, b)
        for key, count in counts.items():
            a, b = edge_value[key]
            doc.line(a, b, "dashed" if count == 2 else "solid")
    else:
        for index, face in enumerate(faces):
            source = manifest.get("faces", [])[index]
            fill = source.get("fill", "#eef0f4") if isinstance(source, dict) else "#eef0f4"
            stroke = bool(source.get("stroke", False)) if isinstance(source, dict) else False
            doc.polygon(face, "solid", fill=fill, stroke=stroke)

    for segment in manifest.get("segments", []):
        doc.line(
            _resolve_point(segment["from"], points),
            _resolve_point(segment["to"], points),
            segment.get("style", "solid"),
            segment.get("name"),
        )

    for curve in manifest.get("curves", []):
        doc.path(
            [_resolve_point(point, points) for point in curve["points"]],
            curve.get("style", "guide"),
            bool(curve.get("smooth", True)),
            curve.get("name"),
        )

    for point_name in manifest.get("dots", []):
        doc.dot(_resolve_point(point_name, points))

    for label in manifest.get("labels", []):
        base = _resolve_point(label.get("at", label.get("point")), points)
        offset = label.get("offset", [0, 0])
        position = (base[0] + float(offset[0]), base[1] + float(offset[1]))
        doc.text(
            position,
            str(label["text"]),
            label.get("style", "label"),
            label.get("anchor", "middle"),
            label.get("name"),
        )

    report["points"] = len(points)
    report["segments"] = len(manifest.get("segments", []))
    report["labels"] = len(manifest.get("labels", []))
    return doc.serialize(), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    svg, report = render_manifest(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"svg": str(args.output), **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
