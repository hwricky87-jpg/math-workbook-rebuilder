#!/usr/bin/env python3
"""Validate a geometry manifest and its rendered SVG; fail closed on hard errors."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from build_observation_overlay import render_overlay as render_observation_overlay
from geometry_svg import (
    _resolve_render_style,
    _resolved_face,
    _resolve_point,
    net_report,
    resolve_marker_points,
)
from imgprobe import stability_report
from validate_visual_review import validate_visual_review


NUMBER = r"-?\d+(?:\.\d+)?"
MAX_TRACE_NORMALIZED_ERROR = 0.04
ALLOWED_VERIFICATION_KINDS = {
    "external-answer",
    "independent-invariant",
    "independent-measurement",
    "user-confirmation",
}
ALLOWED_SEGMENT_STYLES = {"solid", "dashed", "guide", "emphasis"}


def verification_report(manifest: dict[str, Any], fixture: bool = False) -> dict[str, Any]:
    """Require provenance for any claimed semantic or numerical cross-check."""
    evidence = manifest.get("verification_evidence")
    if evidence is None and fixture:
        return {"ok": True, "errors": [], "kind": "synthetic-fixture"}
    if not isinstance(evidence, dict):
        return {
            "ok": False,
            "errors": ["verification_evidence is required for a real source"],
            "kind": None,
        }

    errors: list[str] = []
    kind = str(evidence.get("kind", ""))
    if kind not in ALLOWED_VERIFICATION_KINDS:
        errors.append(
            "verification_evidence.kind must be external-answer, independent-invariant, "
            "independent-measurement, or user-confirmation"
        )
    if not str(evidence.get("description", "")).strip():
        errors.append("verification_evidence.description is required")

    if kind == "external-answer":
        if "value" not in evidence:
            errors.append("external-answer verification requires value")
        if not evidence.get("artifact"):
            errors.append("external-answer verification requires an artifact")
        if not evidence.get("artifact_sha256"):
            errors.append("external-answer verification requires artifact_sha256")
    elif kind == "independent-invariant":
        if not evidence.get("invariant"):
            errors.append("independent-invariant verification requires invariant")
        if "observed" not in evidence:
            errors.append("independent-invariant verification requires observed")
    elif kind == "independent-measurement":
        if not manifest.get("graph_evidence") and not evidence.get("artifact"):
            errors.append(
                "independent-measurement verification requires graph_evidence or an artifact"
            )
        if evidence.get("artifact") and not evidence.get("artifact_sha256"):
            errors.append(
                "independent-measurement verification artifact requires artifact_sha256"
            )
    elif kind == "user-confirmation":
        if not evidence.get("artifact"):
            errors.append("user-confirmation verification requires a comparison artifact")
        if not evidence.get("artifact_sha256"):
            errors.append("user-confirmation verification requires artifact_sha256")

    return {"ok": not errors, "errors": errors, "kind": kind}


def graph_structure(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Validate multigraph multiplicities against independently measured vertex degrees."""
    evidence = manifest.get("graph_evidence")
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        return {"ok": False, "errors": ["graph_evidence must be an object"]}

    errors: list[str] = []
    if not evidence.get("method"):
        errors.append("graph_evidence.method is required")
    if not evidence.get("inspection_artifact"):
        errors.append("graph_evidence.inspection_artifact is required")
    if not evidence.get("source_sha256"):
        errors.append("graph_evidence.source_sha256 is required")
    if not evidence.get("inspection_sha256"):
        errors.append("graph_evidence.inspection_sha256 is required")
    source_size = evidence.get("source_size")
    if (
        not isinstance(source_size, (list, tuple)) or len(source_size) != 2
        or any(not isinstance(value, (int, float)) or value <= 0 for value in source_size)
    ):
        errors.append("graph_evidence.source_size must be two positive numbers")

    vertices = evidence.get("vertices")
    if not isinstance(vertices, dict) or not vertices:
        errors.append("graph_evidence.vertices must be a non-empty object")
        vertices = {}
    observed_degrees: dict[str, int] = {}
    for raw_name, record in vertices.items():
        name = str(raw_name)
        if not isinstance(record, dict):
            errors.append(f"graph vertex evidence must be an object: {name}")
            continue
        degree = record.get("degree")
        if isinstance(degree, bool) or not isinstance(degree, int) or degree < 0:
            errors.append(f"graph vertex degree must be a non-negative integer: {name}")
        else:
            observed_degrees[name] = degree
        if not record.get("probe_report"):
            errors.append(f"graph vertex probe_report is required: {name}")

    drawable_names = {
        str(item.get("name"))
        for item in [*manifest.get("segments", []), *manifest.get("curves", [])]
        if isinstance(item, dict) and item.get("name")
    }
    groups = evidence.get("edge_groups")
    if not isinstance(groups, list) or not groups:
        errors.append("graph_evidence.edge_groups must be a non-empty list")
        groups = []
    computed_degrees = {name: 0 for name in vertices}
    seen_pairs: set[tuple[str, str]] = set()
    seen_drawn: set[str] = set()
    edge_total = 0
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            errors.append(f"graph edge group {index} must be an object")
            continue
        endpoints = group.get("vertices")
        if not isinstance(endpoints, (list, tuple)) or len(endpoints) != 2:
            errors.append(f"graph edge group {index} requires two vertices")
            continue
        left, right = str(endpoints[0]), str(endpoints[1])
        if left == right:
            errors.append(f"graph edge group {index} cannot be a loop")
        for vertex in (left, right):
            if vertex not in vertices:
                errors.append(f"graph edge group {index} uses unknown vertex: {vertex}")
        pair = tuple(sorted((left, right)))
        if pair in seen_pairs:
            errors.append(f"graph edge pair is declared more than once: {left}-{right}")
        seen_pairs.add(pair)

        multiplicity = group.get("multiplicity")
        if isinstance(multiplicity, bool) or not isinstance(multiplicity, int) or multiplicity <= 0:
            errors.append(f"graph edge multiplicity must be a positive integer: {left}-{right}")
            multiplicity_value = 0
        else:
            multiplicity_value = multiplicity
            edge_total += multiplicity
            if left in computed_degrees:
                computed_degrees[left] += multiplicity
            if right in computed_degrees:
                computed_degrees[right] += multiplicity

        drawn = group.get("drawn_elements")
        if not isinstance(drawn, list) or len(drawn) != multiplicity_value:
            errors.append(
                f"graph drawn element count must equal multiplicity for {left}-{right}"
            )
            drawn = [] if not isinstance(drawn, list) else drawn
        for raw_element in drawn:
            element = str(raw_element)
            if element in seen_drawn:
                errors.append(f"graph drawn element is reused: {element}")
            seen_drawn.add(element)
            if element not in drawable_names:
                errors.append(f"graph drawn element does not exist in SVG manifest: {element}")
        if not group.get("probe_report"):
            errors.append(f"graph edge probe_report is required: {left}-{right}")

    for vertex in sorted(vertices):
        if vertex not in observed_degrees:
            continue
        if computed_degrees.get(vertex, 0) != observed_degrees[vertex]:
            errors.append(
                f"graph degree mismatch for {vertex}: edges={computed_degrees.get(vertex, 0)}, "
                f"measured={observed_degrees[vertex]}"
            )
    measured_sum = sum(observed_degrees.values())
    if len(observed_degrees) == len(vertices) and measured_sum != 2 * edge_total:
        errors.append(
            f"graph handshake invariant failed: degree_sum={measured_sum}, edges={edge_total}"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "measured_degrees": observed_degrees,
        "computed_degrees": computed_degrees,
        "edge_total": edge_total,
    }


def trace_alignment(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Compare source-pixel landmarks with the rendered trace coordinate frame."""
    if manifest.get("type", "trace") != "trace":
        return None

    errors: list[str] = []
    evidence = manifest.get("trace_evidence")
    if not isinstance(evidence, dict):
        return {
            "ok": False,
            "errors": ["trace_evidence is required for trace geometry"],
            "point_errors": {},
        }

    if not evidence.get("inspection_artifact"):
        errors.append("trace_evidence.inspection_artifact is required")
    if not evidence.get("method"):
        errors.append("trace_evidence.method is required")

    source_size = evidence.get("source_size")
    if not isinstance(source_size, (list, tuple)) or len(source_size) != 2:
        errors.append("trace_evidence.source_size must be [width, height]")
        source_width = source_height = 0.0
    else:
        source_width, source_height = float(source_size[0]), float(source_size[1])
        if source_width <= 0 or source_height <= 0:
            errors.append("trace_evidence.source_size values must be positive")

    try:
        tolerance = float(evidence["max_normalized_error"])
    except (KeyError, TypeError, ValueError):
        tolerance = 0.0
        errors.append("trace_evidence.max_normalized_error is required")
    if tolerance <= 0:
        errors.append("trace_evidence.max_normalized_error must be positive")
    elif tolerance > MAX_TRACE_NORMALIZED_ERROR:
        errors.append(
            f"trace_evidence.max_normalized_error cannot exceed {MAX_TRACE_NORMALIZED_ERROR}"
        )

    observed = evidence.get("points")
    if not isinstance(observed, dict):
        observed = {}
        errors.append("trace_evidence.points must map every SVG point to source pixels")

    canvas = manifest.get("canvas", {})
    width, height = float(canvas.get("width", 0)), float(canvas.get("height", 0))
    view_box = canvas.get("viewBox", [0, 0, width, height])
    try:
        vx, vy, vw, vh = [float(value) for value in view_box]
    except (TypeError, ValueError):
        vx = vy = vw = vh = 0.0
        errors.append("canvas.viewBox must contain four numeric values")
    if vw <= 0 or vh <= 0:
        errors.append("trace coordinate frame requires a positive canvas viewBox")

    explicit_points = {
        name: _resolve_point(value, {}) for name, value in manifest.get("points", {}).items()
    }
    if not explicit_points:
        errors.append("trace geometry requires explicit points")

    point_errors: dict[str, Any] = {}
    distances: list[float] = []
    for name, actual in explicit_points.items():
        source_point = observed.get(name)
        if not isinstance(source_point, (list, tuple)) or len(source_point) != 2:
            errors.append(f"trace evidence is missing source coordinates for point: {name}")
            continue
        sx, sy = float(source_point[0]), float(source_point[1])
        if source_width > 0 and source_height > 0:
            if sx < 0 or sy < 0 or sx >= source_width or sy >= source_height:
                errors.append(f"source trace point is outside the crop: {name}")
            source_normalized = (sx / source_width, sy / source_height)
        else:
            source_normalized = (0.0, 0.0)
        if vw > 0 and vh > 0:
            canvas_normalized = ((actual[0] - vx) / vw, (actual[1] - vy) / vh)
        else:
            canvas_normalized = (0.0, 0.0)
        dx = canvas_normalized[0] - source_normalized[0]
        dy = canvas_normalized[1] - source_normalized[1]
        distance = math.hypot(dx, dy)
        distances.append(distance)
        point_errors[name] = {
            "source_normalized": list(source_normalized),
            "canvas_normalized": list(canvas_normalized),
            "distance": distance,
        }

    unexpected = sorted(set(observed) - set(explicit_points))
    if unexpected:
        errors.append(f"trace evidence contains unknown points: {', '.join(unexpected)}")

    maximum = max(distances, default=float("inf"))
    rms = math.sqrt(sum(value * value for value in distances) / len(distances)) if distances else float("inf")
    if math.isfinite(maximum) and tolerance > 0 and maximum > tolerance:
        errors.append(
            f"trace landmark error {maximum:.4f} exceeds tolerance {tolerance:.4f}"
        )
    return {
        "ok": not errors,
        "errors": errors,
        "tolerance": tolerance,
        "max_normalized_error": maximum,
        "rms_normalized_error": rms,
        "point_errors": point_errors,
    }


def segment_topology(manifest: dict[str, Any]) -> dict[str, Any]:
    """Reject zero-length and duplicate undirected trace segments."""
    errors: list[str] = []
    points = {
        name: _resolve_point(value, {}) for name, value in manifest.get("points", {}).items()
    }
    seen: dict[tuple[Any, Any], tuple[str, str]] = {}
    seen_names: set[str] = set()
    for index, segment in enumerate(manifest.get("segments", [])):
        if not isinstance(segment, dict):
            errors.append(f"segment {index} must be an object")
            continue
        name = str(segment.get("name") or f"segment-{index}")
        if name in seen_names:
            errors.append(f"duplicate segment name: {name}")
        seen_names.add(name)
        style = str(segment.get("style", "solid"))
        if style not in ALLOWED_SEGMENT_STYLES:
            errors.append(f"segment {name} has unsupported style: {style}")
        raw_left, raw_right = segment.get("from"), segment.get("to")
        if isinstance(raw_left, str) and isinstance(raw_right, str):
            if raw_left == raw_right:
                errors.append(f"segment {name} has zero length")
                continue
            endpoints = tuple(sorted((("name", raw_left), ("name", raw_right))))
        else:
            try:
                left = _resolve_point(raw_left, points)
                right = _resolve_point(raw_right, points)
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"segment {name} has invalid endpoints: {exc}")
                continue
            if math.dist(left, right) <= 1e-9:
                errors.append(f"segment {name} has zero length")
                continue
            endpoints = tuple(sorted((("point", left), ("point", right))))
        if endpoints in seen:
            previous_name, previous_style = seen[endpoints]
            errors.append(
                f"duplicate segment endpoints: {previous_name} ({previous_style}) and "
                f"{name} ({style})"
            )
        else:
            seen[endpoints] = (name, style)
    return {"ok": not errors, "errors": errors, "unique_segments": len(seen)}


def _resolve_artifact(base: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pixel_sha256(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(f"RGB:{rgb.width}x{rgb.height}:".encode("ascii"))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def _read_json(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is unreadable: {exc}")
        return None


def _validate_current_observation_overlay(
    review: dict[str, Any],
    review_base: Path,
    *,
    source_path: Path | None,
    observation_path: Path | None,
    observation: dict[str, Any] | None,
    manifest: dict[str, Any],
    manifest_path: Path,
    errors: list[str],
) -> Path | None:
    """Bind the reviewed overlay to a reproducible source+observation render.

    A visual-review record's own file/hash pair is not provenance: an arbitrary
    image and its matching hash can be substituted together. The observation
    overlay builder emits ``observation-overlay.json`` beside the PNG. Verify
    that report's source/observation lineage and then reproduce the overlay in
    memory, so only the pixels generated from the current evidence are accepted.
    """
    artifacts = review.get("artifacts")
    record = artifacts.get("observation_overlay") if isinstance(artifacts, dict) else None
    if not isinstance(record, dict):
        return None
    overlay_path = _resolve_artifact(review_base, record.get("file"))
    if overlay_path is None or not overlay_path.is_file():
        return None
    if source_path is None or not source_path.is_file():
        return None
    if observation_path is None or not observation_path.is_file() or not isinstance(observation, dict):
        return None

    overlay_report_path = overlay_path.with_suffix(".json")
    if not overlay_report_path.is_file():
        errors.append(
            "visual review observation_overlay lacks its sibling observation overlay report"
        )
        return None
    overlay_report = _read_json(
        overlay_report_path, "observation overlay report", errors
    )
    if not isinstance(overlay_report, dict):
        return None

    if overlay_report.get("ok") is not True:
        errors.append("observation overlay report did not pass")
    for label, reported_value, expected_path in (
        ("source", overlay_report.get("source"), source_path),
        ("observation", overlay_report.get("observation"), observation_path),
    ):
        reported_path = _resolve_artifact(overlay_report_path.parent, reported_value)
        try:
            same_path = (
                reported_path is not None
                and reported_path.resolve() == expected_path.resolve()
            )
        except OSError:
            same_path = False
        if not same_path:
            errors.append(
                f"observation overlay report is not bound to the current {label}"
            )
    if overlay_report.get("source_sha256") != _sha256(source_path):
        errors.append("observation overlay report is stale for the current source crop")
    if overlay_report.get("observation_sha256") != _sha256(observation_path):
        errors.append("observation overlay report is stale for the current observation")

    report_manifest = overlay_report.get("manifest")
    render_manifest: dict[str, Any] | None = None
    if report_manifest:
        reported_manifest_path = _resolve_artifact(
            overlay_report_path.parent, report_manifest
        )
        try:
            same_manifest = (
                reported_manifest_path is not None
                and reported_manifest_path.resolve() == manifest_path.resolve()
            )
        except OSError:
            same_manifest = False
        if not same_manifest:
            errors.append(
                "observation overlay report is not bound to the current manifest"
            )
        if overlay_report.get("manifest_sha256") != _sha256(manifest_path):
            errors.append("observation overlay report is stale for the current manifest")
        render_manifest = manifest

    display_size = overlay_report.get("display_size")
    try:
        reported_display_size = [int(value) for value in display_size]
    except (TypeError, ValueError):
        reported_display_size = []
    if (
        len(reported_display_size) != 2
        or any(value <= 0 for value in reported_display_size)
    ):
        errors.append("observation overlay report has an invalid display_size")
        return overlay_path
    maximum_display_edge = max(reported_display_size)

    try:
        with Image.open(source_path) as source_image:
            expected_overlay, _side, expected_report = render_observation_overlay(
                source_image.convert("RGB"),
                observation,
                manifest=render_manifest,
                maximum_display_edge=maximum_display_edge,
            )
        with Image.open(overlay_path) as actual_overlay:
            actual_pixel_hash = _pixel_sha256(actual_overlay)
        if actual_pixel_hash != _pixel_sha256(expected_overlay):
            errors.append(
                "visual review observation_overlay pixels do not match the current "
                "source and observation"
            )
        report_fields = (
            "ok",
            "source_size",
            "display_size",
            "display_scale",
            "coordinate_frame",
            "grid_step_native_px",
            "source_geometry_roi",
            "source_geometry_roi_kind",
            "source_geometry_roi_geometry",
            "source_geometry_roi_field",
            "counts",
            "unresolved",
            "warnings",
        )
        for field in report_fields:
            if overlay_report.get(field) != expected_report.get(field):
                errors.append(
                    f"observation overlay report field is stale for current evidence: {field}"
                )
    except (OSError, TypeError, ValueError) as exc:
        errors.append(f"observation overlay could not be reproduced: {exc}")
    return overlay_path


def _named_records(
    value: Any,
    kind: str,
    errors: list[str],
    *,
    allow_text_identity: bool = False,
) -> dict[str, dict[str, Any]]:
    """Index audited records while rejecting unnamed and duplicate evidence."""
    if not isinstance(value, list):
        errors.append(f"trace observation artifact requires a {kind} list")
        return {}
    records: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"trace observation {kind} {index} must be an object")
            continue
        raw_name = item.get("name")
        if not raw_name and allow_text_identity:
            raw_name = item.get("text")
        if not raw_name:
            errors.append(f"trace observation {kind} {index} requires a stable name")
            continue
        name = str(raw_name)
        if name in records:
            errors.append(f"duplicate trace observation {kind} name: {name}")
            continue
        records[name] = item
    return records


def _manifest_named_records(
    value: Any,
    kind: str,
    errors: list[str],
    *,
    allow_text_identity: bool = False,
) -> dict[str, dict[str, Any]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        errors.append(f"manifest {kind} must be a list")
        return {}
    records: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"manifest {kind} {index} must be an object")
            continue
        raw_name = item.get("name")
        if not raw_name and allow_text_identity:
            raw_name = item.get("text")
        if not raw_name:
            errors.append(f"manifest {kind} {index} requires a stable name")
            continue
        name = str(raw_name)
        if name in records:
            errors.append(f"duplicate manifest {kind} name: {name}")
            continue
        records[name] = item
    return records


def _required_name_set(
    manifest: dict[str, Any],
    field: str,
    errors: list[str],
    *,
    required_nonempty: bool,
) -> set[str]:
    value = manifest.get(field)
    if not isinstance(value, list) or (required_nonempty and not value):
        if required_nonempty:
            errors.append(f"real-source trace requires a non-empty {field} list")
        elif value is not None and not isinstance(value, list):
            errors.append(f"{field} must be a list")
        return set()
    names = [str(item) for item in value]
    if len(names) != len(set(names)):
        errors.append(f"{field} contains duplicate names")
    return set(names)


def _point_key(
    value: Any,
    points: dict[str, Any],
    errors: list[str],
    label: str,
) -> str | None:
    if isinstance(value, str):
        if value not in points:
            errors.append(f"{label} uses unknown point: {value}")
            return None
        return f"name:{value}"
    try:
        x, y = _resolve_point(value, {})
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"{label} has an invalid point: {exc}")
        return None
    return f"xy:{x:.9g},{y:.9g}"


def topology_signature(data: dict[str, Any]) -> dict[str, Any]:
    """Compute an auditable undirected topology signature from named geometry."""
    errors: list[str] = []
    raw_points = data.get("points", {})
    if not isinstance(raw_points, dict):
        raw_points = {}
        errors.append("topology points must be an object")
    raw_curves = data.get("curves", []) or []
    interior_curve_controls: set[str] = set()
    if isinstance(raw_curves, list):
        for item in raw_curves:
            curve_points = item.get("points") if isinstance(item, dict) else None
            if isinstance(curve_points, list):
                interior_curve_controls.update(
                    str(value) for value in curve_points[1:-1] if isinstance(value, str)
                )
    for collection_name in ("labels", "dimensions"):
        collection = data.get(collection_name, []) or []
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            for field in ("at", "from", "to"):
                value = item.get(field)
                if isinstance(value, str):
                    interior_curve_controls.add(value)
            values = item.get("points")
            if isinstance(values, list):
                interior_curve_controls.update(
                    str(value) for value in values if isinstance(value, str)
                )
    vertices = {
        f"name:{name}" for name in raw_points if str(name) not in interior_curve_controls
    }
    degrees = {vertex: 0 for vertex in vertices}
    adjacency: dict[str, set[str]] = {vertex: set() for vertex in vertices}
    edge_count = 0
    style_counts: dict[str, int] = {}

    def add_edge(left_value: Any, right_value: Any, style: Any, label: str) -> None:
        nonlocal edge_count
        left = _point_key(left_value, raw_points, errors, label)
        right = _point_key(right_value, raw_points, errors, label)
        if left is None or right is None:
            return
        vertices.update((left, right))
        degrees.setdefault(left, 0)
        degrees.setdefault(right, 0)
        adjacency.setdefault(left, set())
        adjacency.setdefault(right, set())
        degrees[left] += 1
        degrees[right] += 1
        adjacency[left].add(right)
        adjacency[right].add(left)
        edge_count += 1
        style_name = str(style or "solid")
        style_counts[style_name] = style_counts.get(style_name, 0) + 1

    raw_segments = data.get("segments", [])
    if not isinstance(raw_segments, list):
        errors.append("topology segments must be a list")
        raw_segments = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            errors.append(f"topology segment {index} must be an object")
            continue
        add_edge(item.get("from"), item.get("to"), item.get("style"), f"segment {index}")

    if not isinstance(raw_curves, list):
        errors.append("topology curves must be a list")
        raw_curves = []
    for index, item in enumerate(raw_curves):
        if not isinstance(item, dict):
            errors.append(f"topology curve {index} must be an object")
            continue
        curve_points = item.get("points")
        if not isinstance(curve_points, list) or len(curve_points) < 2:
            errors.append(f"topology curve {index} requires at least two points")
            continue
        add_edge(
            curve_points[0], curve_points[-1], item.get("style"), f"curve {index}"
        )

    component_count = 0
    visited: set[str] = set()
    for start in sorted(vertices):
        if start in visited:
            continue
        component_count += 1
        stack = [start]
        visited.add(start)
        while stack:
            current = stack.pop()
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
    vertex_count = len(vertices)
    isolated = sum(1 for value in degrees.values() if value == 0)
    signature = {
        "V": vertex_count,
        "E": edge_count,
        "components": component_count,
        "isolated": isolated,
        "degree_multiset": sorted(degrees.values()),
        "cycle_rank": edge_count - vertex_count + component_count,
        "per_style_counts": dict(sorted(style_counts.items())),
    }
    return {"ok": not errors, "errors": errors, "signature": signature}


def _validate_observation_points(
    observation: dict[str, Any], source_size: list[int], errors: list[str]
) -> None:
    points = observation.get("points")
    if not isinstance(points, dict):
        errors.append("trace observation artifact requires a points object")
        return
    width, height = source_size
    seen_coordinates: dict[tuple[float, float], str] = {}
    for raw_name, value in points.items():
        name = str(raw_name)
        try:
            x, y = _resolve_point(value, {})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"trace observation point {name} is invalid: {exc}")
            continue
        if not (0 <= x < width and 0 <= y < height):
            errors.append(f"trace observation point is outside the crop: {name}")
        coordinate = (x, y)
        if coordinate in seen_coordinates:
            errors.append(
                "duplicate trace observation point coordinates: "
                f"{seen_coordinates[coordinate]} and {name}"
            )
        seen_coordinates[coordinate] = name

    def check_coordinate(value: Any, label: str) -> None:
        if isinstance(value, str):
            if value not in points:
                errors.append(f"{label} uses unknown observation point: {value}")
            return
        try:
            x, y = _resolve_point(value, {})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{label} has an invalid observation point: {exc}")
            return
        if not (0 <= x < width and 0 <= y < height):
            errors.append(f"{label} point is outside the crop")

    for index, item in enumerate(observation.get("segments", []) or []):
        if not isinstance(item, dict):
            continue
        check_coordinate(item.get("from"), f"trace observation segment {index}.from")
        check_coordinate(item.get("to"), f"trace observation segment {index}.to")
    for index, item in enumerate(observation.get("curves", []) or []):
        if not isinstance(item, dict) or not isinstance(item.get("points"), list):
            continue
        for point_index, value in enumerate(item["points"]):
            check_coordinate(
                value, f"trace observation curve {index}.points[{point_index}]"
            )
    for collection_name in ("labels", "dimensions"):
        for index, item in enumerate(observation.get(collection_name, []) or []):
            if not isinstance(item, dict):
                continue
            for field in ("at", "from", "to"):
                if field in item:
                    check_coordinate(
                        item[field], f"trace observation {collection_name} {index}.{field}"
                    )
            if isinstance(item.get("points"), list):
                for point_index, value in enumerate(item["points"]):
                    check_coordinate(
                        value,
                        f"trace observation {collection_name} {index}.points[{point_index}]",
                    )


def _inspection_metadata_path(inspection_path: Path | None) -> Path | None:
    if inspection_path is None:
        return None
    if inspection_path.suffix.lower() == ".json":
        return inspection_path
    candidate = inspection_path.parent / "inspection.json"
    return candidate if candidate.is_file() else None


def _parse_geometry_roi(
    value: Any, source_size: list[int]
) -> tuple[str | None, list[Any] | None, str | None]:
    if isinstance(value, dict):
        value = value.get("box") if "box" in value else value.get("polygon")
    width, height = source_size
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    ):
        box = [float(item) for item in value]
        if 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height:
            return "box", box, None
        return None, None, "ROI box is outside the crop"
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
        if all(0 <= x < width and 0 <= y < height for x, y in polygon):
            return "polygon", polygon, None
        return None, None, "ROI polygon is outside the crop"
    return None, None, "ROI must be a four-number box or a three-point polygon"


def _point_in_geometry_roi(
    point: tuple[float, float], kind: str, geometry: list[Any]
) -> bool:
    x, y = point
    if kind == "box":
        x0, y0, x1, y1 = geometry
        return x0 <= x <= x1 and y0 <= y <= y1
    inside = False
    polygon = geometry
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if min(y1, y2) <= y <= max(y1, y2):
            cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
            if abs(cross) <= 1e-9 and min(x1, x2) <= x <= max(x1, x2):
                return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def _validate_inspection_metadata(
    inspection_path: Path | None,
    source_path: Path | None,
    errors: list[str],
) -> dict[str, Any] | None:
    metadata_path = _inspection_metadata_path(inspection_path)
    if metadata_path is None:
        return None
    metadata = _read_json(metadata_path, "trace inspection metadata", errors)
    if not isinstance(metadata, dict):
        return None

    crop_inspection = metadata.get("crop_inspection")
    if isinstance(crop_inspection, dict):
        if crop_inspection.get("crop_complete") is not True:
            errors.append("trace inspection reports crop_complete=false")
        edge_strokes = crop_inspection.get("edge_strokes")
        reviewed = crop_inspection.get("reviewed_exception")
        if (
            isinstance(edge_strokes, dict)
            and edge_strokes.get("detected") is True
            and not (
                isinstance(reviewed, dict)
                and reviewed.get("approved") is True
                and str(reviewed.get("note", "")).strip()
            )
        ):
            errors.append("trace inspection edge strokes require a reviewed exception")

    lineage = metadata.get("lineage")
    if not isinstance(lineage, dict):
        return metadata
    required_fields = (
        "parent_source_path", "parent_source_sha256", "parent_source_size", "crop_box",
        "crop_path", "crop_sha256", "crop_pixel_sha256", "parent_region_pixel_sha256",
        "exact_pixel_lineage",
    )
    missing = [field for field in required_fields if field not in lineage]
    if missing:
        errors.append(
            "trace inspection crop lineage is stale: missing " + ", ".join(missing)
        )
        return metadata
    if lineage.get("exact_pixel_lineage") is not True:
        errors.append("trace inspection crop lineage is stale: exact_pixel_lineage is not true")

    parent_path = _resolve_artifact(metadata_path.parent, lineage.get("parent_source_path"))
    if parent_path is None or not parent_path.is_file():
        errors.append("trace inspection crop lineage is stale: parent source does not exist")
        return metadata
    if lineage.get("parent_source_sha256") != _sha256(parent_path):
        errors.append("trace inspection crop lineage is stale: parent source hash changed")
    try:
        with Image.open(parent_path) as parent_image:
            parent_size = list(parent_image.size)
            recorded_parent_size = [int(value) for value in lineage.get("parent_source_size", [])]
            if recorded_parent_size != parent_size:
                errors.append("trace inspection crop lineage is stale: parent source size changed")
            crop_box = [int(value) for value in lineage.get("crop_box", [])]
            if len(crop_box) != 4:
                errors.append("trace inspection crop lineage is stale: crop_box is invalid")
            else:
                x0, y0, x1, y1 = crop_box
                if x0 < 0 or y0 < 0 or x1 > parent_size[0] or y1 > parent_size[1] or x1 <= x0 or y1 <= y0:
                    errors.append("trace inspection crop lineage is stale: crop_box is out of bounds")
                else:
                    region_hash = _pixel_sha256(parent_image.crop((x0, y0, x1, y1)))
                    if lineage.get("parent_region_pixel_sha256") != region_hash:
                        errors.append(
                            "trace inspection crop lineage is stale: parent crop pixels changed"
                        )
                    if lineage.get("crop_pixel_sha256") != region_hash:
                        errors.append(
                            "trace inspection crop lineage is stale: crop pixels do not match parent"
                        )
    except (OSError, TypeError, ValueError) as exc:
        errors.append(f"trace inspection crop lineage is stale: {exc}")

    crop_path = _resolve_artifact(metadata_path.parent, lineage.get("crop_path"))
    if crop_path is not None:
        if not crop_path.is_file() or lineage.get("crop_sha256") != _sha256(crop_path):
            errors.append("trace inspection crop lineage is stale: native crop hash changed")
    if source_path is not None and source_path.is_file():
        try:
            with Image.open(source_path) as source_image:
                if lineage.get("crop_pixel_sha256") != _pixel_sha256(source_image):
                    errors.append(
                        "trace inspection crop lineage is stale: manifest source crop differs"
                    )
        except OSError as exc:
            errors.append(f"trace inspection crop lineage is stale: {exc}")
    return metadata


def validate_graph_probe_artifacts(
    manifest: dict[str, Any], base: Path, source_path: Path | None
) -> dict[str, Any]:
    """Verify that graph measurements came from the current crop and passed stability gates."""
    evidence = manifest.get("graph_evidence")
    if not isinstance(evidence, dict):
        return {"ok": True, "errors": []}

    errors: list[str] = []
    if source_path is None or not source_path.is_file():
        return {"ok": False, "errors": ["graph evidence requires an existing source crop"]}
    source_hash = _sha256(source_path)
    try:
        with Image.open(source_path) as image:
            source_size = list(image.size)
    except OSError as exc:
        return {"ok": False, "errors": [f"graph source crop is unreadable: {exc}"]}
    if evidence.get("source_sha256") != source_hash:
        errors.append("graph_evidence.source_sha256 is stale for the current source crop")
    try:
        recorded_size = [int(value) for value in evidence.get("source_size", [])]
    except (TypeError, ValueError):
        recorded_size = []
    if recorded_size != source_size:
        errors.append(
            f"graph source_size={recorded_size} does not match actual crop size={source_size}"
        )

    inspection_path = _resolve_artifact(base, evidence.get("inspection_artifact"))
    if inspection_path is None or not inspection_path.is_file():
        errors.append("graph inspection artifact does not exist")
    elif evidence.get("inspection_sha256") != _sha256(inspection_path):
        errors.append("graph inspection artifact hash is stale")

    def check_probe(path_value: Any, expected: int, field: str, label: str) -> None:
        report_path = _resolve_artifact(base, path_value)
        if report_path is None or not report_path.is_file():
            errors.append(f"{label} probe report does not exist")
            return
        report = _read_json(report_path, f"{label} probe report", errors)
        if not isinstance(report, dict):
            return
        if report.get("ok") is not True:
            errors.append(f"{label} probe report did not pass")
        if report.get("source_sha256") != source_hash:
            errors.append(f"{label} probe report is stale for the current source crop")
        if report.get("image_size") != source_size:
            errors.append(f"{label} probe report image_size does not match the source crop")
        measurement = report.get(field)
        if not isinstance(measurement, dict):
            errors.append(f"{label} probe report lacks a {field} measurement")
            return
        counts = measurement.get("counts")
        minimum_samples = measurement.get("minimum_samples")
        if (
            not isinstance(counts, list) or not counts
            or any(isinstance(value, bool) or not isinstance(value, int) for value in counts)
            or isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int)
            or minimum_samples < 2
        ):
            errors.append(f"{label} probe report lacks raw counts and a valid stability threshold")
            return
        recomputed = stability_report(counts, minimum_samples)
        if recomputed["ok"] is not True:
            errors.append(f"{label} probe raw counts have no unique stable {field} value")
        if measurement.get("ok") != recomputed["ok"]:
            errors.append(f"{label} probe reported status does not match recomputed stability")
        if measurement.get("stable_count") != recomputed["stable_count"]:
            errors.append(f"{label} probe stable_count does not match its raw counts")
        if recomputed["stable_count"] != expected:
            errors.append(
                f"{label} probe stable count={recomputed['stable_count']}, expected={expected}"
            )

    for name, record in evidence.get("vertices", {}).items():
        if isinstance(record, dict) and isinstance(record.get("degree"), int):
            check_probe(record.get("probe_report"), record["degree"], "degree", f"vertex {name}")
    for group in evidence.get("edge_groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("multiplicity"), int):
            continue
        endpoints = group.get("vertices", ["?", "?"])
        label = f"edge {'-'.join(map(str, endpoints))}"
        check_probe(group.get("probe_report"), group["multiplicity"], "cut", label)

    return {"ok": not errors, "errors": errors}


def validate_artifact_evidence(
    manifest: dict[str, Any], manifest_path: Path, svg_path: Path | None = None
) -> dict[str, Any]:
    """Reject missing, stale, or mismatched source-comparison artifacts."""
    errors: list[str] = []
    inspection_metadata: dict[str, Any] | None = None
    observation_topology: dict[str, Any] | None = None
    manifest_topology: dict[str, Any] | None = None
    visual_review_report: dict[str, Any] | None = None
    diagnostic_path: Path | None = None
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("fixture"):
        return {"ok": True, "errors": []}

    base = manifest_path.parent
    source_value = next((source.get(key) for key in ("file", "crop", "image") if source.get(key)), None)
    source_path = _resolve_artifact(base, source_value)
    if source_path is None or not source_path.is_file():
        errors.append("source crop file does not exist")

    trace = manifest.get("trace_evidence")
    if manifest.get("type", "trace") == "trace":
        # One ROI path is authoritative.  Historical root/source aliases made
        # it possible for the comparison and validator to inspect different
        # regions, which could hide an omitted disconnected component.
        roi_aliases = (
            ("manifest.source_geometry_roi", manifest, "source_geometry_roi"),
            ("trace_evidence.geometry_roi", trace, "geometry_roi"),
            ("source.source_geometry_roi", source, "source_geometry_roi"),
            ("source.figure_roi", source, "figure_roi"),
        )
        for alias, container, field in roi_aliases:
            if isinstance(container, dict) and field in container:
                errors.append(
                    f"{alias} is not an authoritative ROI; use only "
                    "trace_evidence.source_geometry_roi copied unchanged from the observation"
                )
        inspection_path = (
            _resolve_artifact(base, trace.get("inspection_artifact"))
            if isinstance(trace, dict) else None
        )
        if inspection_path is None or not inspection_path.is_file():
            errors.append("trace inspection artifact does not exist")
        inspection_metadata = _validate_inspection_metadata(
            inspection_path, source_path, errors
        )

        observation_path = (
            _resolve_artifact(base, trace.get("observation_artifact"))
            if isinstance(trace, dict) else None
        )
        if observation_path is None or not observation_path.is_file():
            errors.append("trace observation artifact does not exist")
            observation = None
        else:
            if observation_path.resolve() == manifest_path.resolve():
                errors.append("trace observation artifact must be separate from the manifest")
            if trace.get("observation_sha256") != _sha256(observation_path):
                errors.append("trace observation artifact hash is stale")
            observation = _read_json(observation_path, "trace observation artifact", errors)
        if isinstance(observation, dict):
            if source_path and source_path.is_file() and observation.get("source_sha256") != _sha256(source_path):
                errors.append("trace observation artifact is stale for the current source crop")
            try:
                observation_size = [int(value) for value in observation.get("source_size", [])]
            except (TypeError, ValueError):
                observation_size = []
            if isinstance(trace, dict):
                try:
                    trace_size = [int(value) for value in trace.get("source_size", [])]
                except (TypeError, ValueError):
                    trace_size = []
                if observation_size != trace_size:
                    errors.append("trace observation source_size does not match trace_evidence")
                if observation.get("points") != trace.get("points"):
                    errors.append("trace_evidence.points must come unchanged from the observation artifact")
            if len(observation_size) == 2 and all(value > 0 for value in observation_size):
                _validate_observation_points(observation, observation_size, errors)

            required_segments = _required_name_set(
                manifest, "required_segments", errors, required_nonempty=True
            )
            observed_by_name = _named_records(
                observation.get("segments"), "segment", errors
            )
            manifest_segments = _manifest_named_records(
                manifest.get("segments"), "segment", errors
            )
            if not (
                required_segments == set(observed_by_name) == set(manifest_segments)
            ):
                errors.append(
                    "required_segments, observation segment names, and manifest segment names "
                    "must be identical"
                )
            observed_endpoints: dict[tuple[str, str], str] = {}
            for name, observed_segment in observed_by_name.items():
                endpoint_key = tuple(sorted((
                    json.dumps(observed_segment.get("from"), ensure_ascii=False, sort_keys=True),
                    json.dumps(observed_segment.get("to"), ensure_ascii=False, sort_keys=True),
                )))
                if endpoint_key in observed_endpoints:
                    errors.append(
                        "duplicate trace observation segment endpoints: "
                        f"{observed_endpoints[endpoint_key]} and {name}"
                    )
                observed_endpoints[endpoint_key] = name
                if name not in manifest_segments:
                    continue
                drawn_segment = manifest_segments[name]
                for field in ("from", "to", "style"):
                    if observed_segment.get(field) != drawn_segment.get(field):
                        errors.append(
                            f"trace observation segment {name}.{field} does not match the manifest"
                        )

            observation_curves_present = "curves" in observation
            manifest_curves_present = bool(manifest.get("curves"))
            required_curves_present = "required_curves" in manifest
            if observation_curves_present or manifest_curves_present or required_curves_present:
                observed_curves = _named_records(
                    observation.get("curves"), "curve", errors
                )
                manifest_curves = _manifest_named_records(
                    manifest.get("curves"), "curve", errors
                )
                required_curves = _required_name_set(
                    manifest,
                    "required_curves",
                    errors,
                    required_nonempty=bool(observed_curves or manifest_curves),
                )
                if not (
                    required_curves == set(observed_curves) == set(manifest_curves)
                ):
                    errors.append(
                        "required_curves, observation curve names, and manifest curve names "
                        "must be identical"
                    )
                for name in sorted(set(observed_curves) & set(manifest_curves)):
                    for field in ("points", "style", "smooth"):
                        observed_value = observed_curves[name].get(field)
                        manifest_value = manifest_curves[name].get(field)
                        if field == "smooth":
                            observed_value = True if observed_value is None else observed_value
                            manifest_value = True if manifest_value is None else manifest_value
                        if observed_value != manifest_value:
                            errors.append(
                                f"trace observation curve {name}.{field} does not match the manifest"
                            )

            for plural, singular, fields in (
                ("labels", "label", ("text", "at", "offset", "style")),
                (
                    "dimensions", "dimension",
                    ("text", "at", "from", "to", "points", "offset", "style"),
                ),
            ):
                observed_records = _named_records(
                    observation.get(plural), singular, errors, allow_text_identity=True
                )
                manifest_records = _manifest_named_records(
                    manifest.get(plural), singular, errors, allow_text_identity=True
                )
                required_records = _required_name_set(
                    manifest,
                    f"required_{plural}",
                    errors,
                    required_nonempty=False,
                )
                if not (
                    required_records == set(observed_records) == set(manifest_records)
                ):
                    errors.append(
                        f"required_{plural}, observation {plural}, and manifest {plural} "
                        "must match one-to-one"
                    )
                for name in sorted(set(observed_records) & set(manifest_records)):
                    for field in fields:
                        if observed_records[name].get(field) != manifest_records[name].get(field):
                            errors.append(
                                f"trace observation {singular} {name}.{field} does not match the manifest"
                            )

            observed_markers = _named_records(
                observation.get("markers"), "marker", errors
            )
            manifest_markers = _manifest_named_records(
                manifest.get("markers"), "marker", errors
            )
            required_markers = _required_name_set(
                manifest, "required_markers", errors, required_nonempty=False
            )
            if not (
                required_markers == set(observed_markers) == set(manifest_markers)
            ):
                errors.append(
                    "required_markers, observation markers, and manifest markers "
                    "must match one-to-one"
                )
            resolved_observation_markers: dict[str, list[tuple[float, float]]] = {}
            observation_points = observation.get("points", {})
            manifest_points = manifest.get("points", {})
            for name in sorted(set(observed_markers) & set(manifest_markers)):
                try:
                    observed_marker_points = resolve_marker_points(
                        observed_markers[name], observation_points
                    )
                    drawn_marker_points = resolve_marker_points(
                        manifest_markers[name], manifest_points
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(f"trace marker {name} is invalid: {exc}")
                    continue
                resolved_observation_markers[name] = observed_marker_points
                if observed_marker_points != drawn_marker_points:
                    errors.append(
                        f"trace observation marker {name} geometry does not match the manifest"
                    )
                for field, default in (("kind", None), ("style", "solid")):
                    if observed_markers[name].get(field, default) != manifest_markers[name].get(field, default):
                        errors.append(
                            f"trace observation marker {name}.{field} does not match the manifest"
                        )

            observation_topology = topology_signature(observation)
            manifest_topology = topology_signature(manifest)
            errors.extend(
                f"trace observation topology: {error}"
                for error in observation_topology["errors"]
            )
            errors.extend(
                f"manifest topology: {error}" for error in manifest_topology["errors"]
            )
            if observation_topology["signature"] != manifest_topology["signature"]:
                errors.append(
                    "manifest topology signature does not match the source observation"
                )
            if (
                observation_topology["signature"]["components"] > 1
                and not manifest.get("component_inventory")
            ):
                errors.append(
                    "trace observation has multiple components; component_inventory is required"
                )

            refinement = observation.get("refinement")
            if isinstance(refinement, dict):
                if refinement.get("status") != "approved-after-native-overlay-review":
                    errors.append(
                        "refined observation is still a candidate; inspect the native overlay "
                        "and set refinement.status=approved-after-native-overlay-review"
                    )
                approval = refinement.get("approval")
                if not isinstance(approval, dict):
                    errors.append("refined observation requires a hash-bound approval object")
                else:
                    if not str(approval.get("reviewer") or "").strip():
                        errors.append("refined observation approval.reviewer is required")
                    if approval.get("inspected_native_overlay") is not True:
                        errors.append(
                            "refined observation approval.inspected_native_overlay must be true"
                        )
                    for artifact_field in ("overlay", "report"):
                        approval_path = _resolve_artifact(
                            observation_path.parent, approval.get(artifact_field)
                        )
                        if approval_path is None or not approval_path.is_file():
                            errors.append(
                                f"refined observation approval.{artifact_field} does not exist"
                            )
                        elif approval.get(f"{artifact_field}_sha256") != _sha256(approval_path):
                            errors.append(
                                f"refined observation approval.{artifact_field}_sha256 is stale"
                            )

            roi_key = "source_geometry_roi"
            observed_roi = observation.get(roi_key)
            trace_roi = trace.get(roi_key) if isinstance(trace, dict) else None
            if observed_roi is None:
                errors.append(
                    "real-source trace observation requires source_geometry_roi observed "
                    "before tracing; a trace-point hull is not accepted"
                )
            if trace_roi is None:
                errors.append(
                    "real-source trace requires trace_evidence.source_geometry_roi copied "
                    "unchanged from the observation"
                )
            if observed_roi is not None and trace_roi is not None:
                if observed_roi != trace_roi:
                    errors.append(
                        "trace observation source_geometry_roi must match "
                        "trace_evidence.source_geometry_roi unchanged"
                    )
                roi_method = observation.get(f"{roi_key}_method")
                if not str(roi_method or "").strip():
                    errors.append(f"trace observation {roi_key}_method is required")
                elif any(
                    phrase in str(roi_method).lower().replace("_", " ")
                    for phrase in (
                        "trace-point hull", "trace point hull", "trace points hull",
                        "derived from trace", "generated from trace",
                    )
                ):
                    errors.append(
                        "source geometry ROI must be independently observed, not derived from trace points"
                    )
                if len(observation_size) != 2:
                    errors.append(
                        f"trace observation {roi_key} cannot be checked without source_size"
                    )
                else:
                    roi_kind, roi_geometry, roi_error = _parse_geometry_roi(
                        observed_roi, observation_size
                    )
                    if roi_error:
                        errors.append(f"trace observation {roi_key}: {roi_error}")
                    elif roi_kind and roi_geometry:
                        for raw_name, value in observation.get("points", {}).items():
                            try:
                                point = _resolve_point(value, {})
                            except (KeyError, TypeError, ValueError):
                                continue
                            if not _point_in_geometry_roi(point, roi_kind, roi_geometry):
                                errors.append(
                                    f"trace observation point {raw_name} lies outside {roi_key}"
                                )
                        for marker_name, marker_points in resolved_observation_markers.items():
                            for marker_index, marker_point in enumerate(marker_points):
                                if not _point_in_geometry_roi(
                                    marker_point, roi_kind, roi_geometry
                                ):
                                    errors.append(
                                        f"trace observation marker {marker_name} point "
                                        f"{marker_index} lies outside {roi_key}"
                                    )

    comparison = manifest.get("source_comparison", {})
    artifact_path = _resolve_artifact(base, comparison.get("artifact"))
    report_path = _resolve_artifact(base, comparison.get("report"))
    if artifact_path is None or not artifact_path.is_file():
        errors.append("source comparison artifact does not exist")
    if report_path is None or not report_path.is_file():
        errors.append("source comparison report does not exist")
        report = None
    else:
        report = _read_json(report_path, "source comparison report", errors)

    if source_path and source_path.is_file() and isinstance(trace, dict):
        try:
            with Image.open(source_path) as image:
                actual_size = list(image.size)
            recorded_size = [int(value) for value in trace.get("source_size", [])]
            if recorded_size != actual_size:
                errors.append(
                    f"trace source_size={recorded_size} does not match actual crop size={actual_size}"
                )
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"source crop dimensions could not be verified: {exc}")

    if isinstance(report, dict):
        if report.get("ok") is not True:
            errors.append("source comparison report did not pass")
        trace_report = report.get("trace")
        if manifest.get("type", "trace") == "trace" and (
            not isinstance(trace_report, dict) or trace_report.get("ok") is not True
        ):
            errors.append("source comparison report lacks a passing trace metric")
        ink_report = report.get("ink_alignment")
        if not isinstance(ink_report, dict) or ink_report.get("ok") is not True:
            errors.append("source comparison report lacks a passing source-ink metric")
        residual_report = report.get("source_residual")
        if not isinstance(residual_report, dict) or residual_report.get("ok") is not True:
            errors.append("source comparison report has an unexplained long source-line residual")
        segment_support_report = report.get("segment_source_support")
        if manifest.get("type", "trace") == "trace" and (
            not isinstance(segment_support_report, dict)
            or segment_support_report.get("ok") is not True
            or segment_support_report.get("applied") is not True
        ):
            errors.append("source comparison report has an unsupported declared segment")
        if source_path and source_path.is_file() and report.get("source_sha256") != _sha256(source_path):
            errors.append("source comparison report is stale for the current source crop")
        if svg_path and svg_path.is_file() and report.get("redraw_sha256") != _sha256(svg_path):
            errors.append("source comparison report is stale for the current SVG")
        if artifact_path and artifact_path.is_file() and report.get("artifact_sha256") != _sha256(artifact_path):
            errors.append("source comparison artifact hash does not match its report")
        diagnostic_value = report.get("diagnostic_artifact")
        if manifest.get("type", "trace") == "trace" and not diagnostic_value:
            errors.append("source comparison report lacks diagnostic_artifact")
        if diagnostic_value:
            diagnostic_path = _resolve_artifact(base, diagnostic_value)
            if diagnostic_path is None or not diagnostic_path.is_file():
                errors.append("source comparison diagnostic artifact does not exist")
            elif report.get("diagnostic_artifact_sha256") != _sha256(diagnostic_path):
                errors.append(
                    "source comparison diagnostic artifact hash does not match its report"
                )

    review_path = _resolve_artifact(base, comparison.get("review"))
    if review_path is None or not review_path.is_file():
        errors.append("source comparison review artifact does not exist")
    else:
        if comparison.get("review_sha256") != _sha256(review_path):
            errors.append("source comparison review artifact hash is stale")
        review = _read_json(review_path, "source comparison review artifact", errors)
        if isinstance(review, dict):
            if source_path and source_path.is_file() and review.get("source_sha256") != _sha256(source_path):
                errors.append("source comparison review is stale for the current source crop")
            if svg_path and svg_path.is_file() and review.get("redraw_sha256") != _sha256(svg_path):
                errors.append("source comparison review is stale for the current SVG")
            if artifact_path and artifact_path.is_file() and review.get("artifact_sha256") != _sha256(artifact_path):
                errors.append("source comparison review is stale for the current comparison artifact")
            if report_path and report_path.is_file() and review.get("report_sha256") != _sha256(report_path):
                errors.append("source comparison review is stale for the current comparison report")
            review_checks = review.get("checks")
            if not isinstance(review_checks, dict):
                errors.append("source comparison review requires item-by-item checks")
            else:
                for field in (
                    "text_match", "topology_match", "line_styles_match", "labels_match",
                    "dimensions_match", "projection_match", "inspected_latest_render",
                ):
                    if review_checks.get(field) is not True:
                        errors.append(f"source comparison review check must be true: {field}")
                    if comparison.get(field) != review_checks.get(field):
                        errors.append(f"source_comparison.{field} must match the review artifact")
            if manifest.get("type", "trace") == "trace":
                current_observation_overlay = _validate_current_observation_overlay(
                    review,
                    review_path.parent,
                    source_path=source_path,
                    observation_path=(
                        observation_path if "observation_path" in locals() else None
                    ),
                    observation=observation if "observation" in locals() else None,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    errors=errors,
                )
                expected_visual_artifacts: dict[str, Path] = {}
                for name, path in (
                    ("source", source_path),
                    ("observation", observation_path if 'observation_path' in locals() else None),
                    ("observation_overlay", current_observation_overlay),
                    ("redraw", svg_path),
                    ("comparison", artifact_path),
                    ("diagnostic", diagnostic_path),
                    ("comparison_report", report_path),
                ):
                    if isinstance(path, Path):
                        expected_visual_artifacts[name] = path
                visual_review_report = validate_visual_review(
                    review,
                    review_path.parent,
                    expected=expected_visual_artifacts,
                    require_final_crop=False,
                )
                errors.extend(visual_review_report["errors"])

    verification = manifest.get("verification_evidence")
    if isinstance(verification, dict) and verification.get("artifact"):
        verification_path = _resolve_artifact(base, verification.get("artifact"))
        if verification_path is None or not verification_path.is_file():
            errors.append("verification evidence artifact does not exist")
        elif verification.get("artifact_sha256") != _sha256(verification_path):
            errors.append("verification evidence artifact hash is stale")

    graph_artifacts = validate_graph_probe_artifacts(manifest, base, source_path)
    errors.extend(graph_artifacts["errors"])

    return {
        "ok": not errors,
        "errors": errors,
        "inspection": inspection_metadata,
        "observation_topology": observation_topology,
        "manifest_topology": manifest_topology,
        "visual_review": visual_review_report,
    }


def _path_points(data: str, steps: int = 12) -> list[tuple[float, float]]:
    """Sample M/L/H/V/Q/C SVG paths so off-curve control points do not trigger clipping."""
    tokens = re.findall(rf"[MmLlHhVvQqCcZz]|{NUMBER}", data)
    points: list[tuple[float, float]] = []
    index = 0
    current_x = current_y = start_x = start_y = 0.0
    command = None
    while index < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[index]):
            command = tokens[index]
            index += 1
            if command in "Zz":
                current_x, current_y = start_x, start_y
                points.append((current_x, current_y))
                continue
        if command is None:
            break
        relative = command.islower()

        def value(offset: int) -> float:
            return float(tokens[index + offset])

        if command in "Mm":
            current_x, current_y = (
                (current_x + value(0), current_y + value(1))
                if relative else (value(0), value(1))
            )
            start_x, start_y = current_x, current_y
            points.append((current_x, current_y))
            index += 2
        elif command in "Ll":
            current_x, current_y = (
                (current_x + value(0), current_y + value(1))
                if relative else (value(0), value(1))
            )
            points.append((current_x, current_y))
            index += 2
        elif command in "Hh":
            current_x = current_x + value(0) if relative else value(0)
            points.append((current_x, current_y))
            index += 1
        elif command in "Vv":
            current_y = current_y + value(0) if relative else value(0)
            points.append((current_x, current_y))
            index += 1
        elif command in "Qq":
            control = (
                (current_x + value(0), current_y + value(1))
                if relative else (value(0), value(1))
            )
            end = (
                (current_x + value(2), current_y + value(3))
                if relative else (value(2), value(3))
            )
            for sample in range(steps + 1):
                t = sample / steps
                u = 1 - t
                points.append((
                    u * u * current_x + 2 * u * t * control[0] + t * t * end[0],
                    u * u * current_y + 2 * u * t * control[1] + t * t * end[1],
                ))
            current_x, current_y = end
            index += 4
        elif command in "Cc":
            control1 = (
                (current_x + value(0), current_y + value(1))
                if relative else (value(0), value(1))
            )
            control2 = (
                (current_x + value(2), current_y + value(3))
                if relative else (value(2), value(3))
            )
            end = (
                (current_x + value(4), current_y + value(5))
                if relative else (value(4), value(5))
            )
            for sample in range(steps + 1):
                t = sample / steps
                u = 1 - t
                points.append((
                    u ** 3 * current_x + 3 * u * u * t * control1[0]
                    + 3 * u * t * t * control2[0] + t ** 3 * end[0],
                    u ** 3 * current_y + 3 * u * u * t * control1[1]
                    + 3 * u * t * t * control2[1] + t ** 3 * end[1],
                ))
            current_x, current_y = end
            index += 6
        else:
            # Unsupported arc or shorthand: ignore its coordinates rather than claiming false precision.
            break
    return points


def component_inventory_report(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Validate declared stage/choice components against namespaced geometry."""
    inventory = manifest.get("component_inventory")
    if inventory is None:
        return None
    errors: list[str] = []
    if not isinstance(inventory, list) or not inventory:
        return {"ok": False, "errors": ["component_inventory must be a non-empty list"], "components": []}
    points = manifest.get("points", {})
    segments = manifest.get("segments", [])
    curves = manifest.get("curves", []) or []
    point_names = set(points) if isinstance(points, dict) else set()
    segment_names = {
        str(item.get("name"))
        for item in segments
        if isinstance(item, dict) and item.get("name")
    } if isinstance(segments, list) else set()
    curve_names = {
        str(item.get("name"))
        for item in curves
        if isinstance(item, dict) and item.get("name")
    } if isinstance(curves, list) else set()
    seen: set[str] = set()
    covered_points: set[str] = set()
    covered_segments: set[str] = set()
    covered_curves: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in inventory:
        if not isinstance(item, dict) or not item.get("id"):
            errors.append("every component inventory entry requires an id")
            continue
        component_id = str(item["id"])
        if component_id in seen:
            errors.append(f"duplicate component id: {component_id}")
            continue
        seen.add(component_id)
        prefix = f"{component_id}-"
        component_points = {name for name in point_names if name.startswith(prefix)}
        component_segments = {name for name in segment_names if name.startswith(prefix)}
        component_curves = {name for name in curve_names if name.startswith(prefix)}
        covered_points.update(component_points)
        covered_segments.update(component_segments)
        covered_curves.update(component_curves)
        actual_points = len(component_points)
        actual_segments = len(component_segments)
        actual_curves = len(component_curves)
        expected_points = int(item.get("point_count", -1))
        expected_segments = int(item.get("segment_count", -1))
        expected_curves = int(item.get("curve_count", 0))
        if expected_points < 0 or actual_points != expected_points:
            errors.append(
                f"component {component_id} point_count={actual_points}, expected={expected_points}"
            )
        if expected_segments < 0 or actual_segments != expected_segments:
            errors.append(
                f"component {component_id} segment_count={actual_segments}, expected={expected_segments}"
            )
        if expected_curves < 0 or actual_curves != expected_curves:
            errors.append(
                f"component {component_id} curve_count={actual_curves}, expected={expected_curves}"
            )
        if actual_segments + actual_curves <= 0:
            errors.append(f"component {component_id} has no drawable segment or curve")
        records.append({
            "id": component_id,
            "point_count": actual_points,
            "segment_count": actual_segments,
            "curve_count": actual_curves,
        })
    for kind, all_names, covered in (
        ("points", point_names, covered_points),
        ("segments", segment_names, covered_segments),
        ("curves", curve_names, covered_curves),
    ):
        unassigned = sorted(all_names - covered)
        if unassigned:
            errors.append(
                f"component_inventory leaves {kind} unassigned: {', '.join(unassigned)}"
            )
    verification = manifest.get("verification_evidence", {})
    invariant = verification.get("invariant", {}) if isinstance(verification, dict) else {}
    expected_total = invariant.get("total_components") if isinstance(invariant, dict) else None
    if expected_total is not None and int(expected_total) != len(seen):
        errors.append(
            f"component inventory has {len(seen)} entries, independently expected {int(expected_total)}"
        )
    topology = topology_signature(manifest)
    if topology["ok"] and topology["signature"]["components"] != len(seen):
        errors.append(
            "component inventory count does not match topology components: "
            f"inventory={len(seen)}, topology={topology['signature']['components']}"
        )
    return {"ok": not errors, "errors": errors, "components": records}


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    kind = manifest.get("type", "trace")
    canvas = manifest.get("canvas", {})
    if float(canvas.get("width", 0)) <= 0 or float(canvas.get("height", 0)) <= 0:
        errors.append("canvas width and height must be positive")
    try:
        render_style = _resolve_render_style(manifest)
    except (TypeError, ValueError) as exc:
        render_style = None
        errors.append(f"invalid render_style: {exc}")

    explicit_points = {
        name: _resolve_point(value, {}) for name, value in manifest.get("points", {}).items()
    }
    labels = manifest.get("labels", [])
    segments = manifest.get("segments", [])
    label_names = {str(item.get("text")) for item in labels} | {
        str(item.get("name")) for item in labels if item.get("name")
    }
    segment_names = {str(item.get("name")) for item in segments if item.get("name")}
    segment_report = segment_topology(manifest)
    errors.extend(segment_report["errors"])

    for required in manifest.get("required_labels", []):
        if str(required) not in label_names:
            errors.append(f"required label is missing: {required}")
    for required in manifest.get("required_segments", []):
        if str(required) not in segment_names:
            errors.append(f"required named segment is missing: {required}")

    if kind == "prism":
        prism = manifest.get("prism", {})
        n = int(prism.get("n", 0))
        if n < 3:
            errors.append("a prism requires n >= 3")
        expected = manifest.get("expected", {})
        if expected.get("vertices") not in (None, 2 * n):
            errors.append(f"prism vertex expectation must be {2 * n}")
        if expected.get("edges") not in (None, 3 * n):
            errors.append(f"prism edge expectation must be {3 * n}")
        if expected.get("faces") not in (None, n + 2):
            errors.append(f"prism face expectation must be {n + 2}")

    net = None
    if kind == "net":
        try:
            faces = [_resolved_face(face, explicit_points) for face in manifest.get("faces", [])]
            net = net_report(faces, float(manifest.get("edge_tolerance", 0.25)))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid net faces: {exc}")
            faces = []
        if not faces:
            errors.append("a net requires at least one face")
        if net:
            if net["bad_shared_edges"]:
                errors.append("a net edge is shared by more than two faces")
            if not net["connected"]:
                errors.append("net faces are not connected")
            expected = manifest.get("expected", {})
            for field in ("faces", "folds", "boundary"):
                if field in expected and int(expected[field]) != int(net[field]):
                    errors.append(f"net {field}={net[field]}, expected {expected[field]}")
            prism_n = expected.get("prism_n")
            if prism_n is not None:
                prism_n = int(prism_n)
                invariant = {"faces": prism_n + 2, "folds": prism_n + 1, "boundary": 4 * prism_n - 2}
                for field, value in invariant.items():
                    if int(net[field]) != value:
                        errors.append(f"{prism_n}-gonal prism net requires {field}={value}, got {net[field]}")

    source = manifest.get("source")
    fixture = isinstance(source, dict) and bool(source.get("fixture"))
    if not source:
        errors.append("source crop metadata is required")
    if not manifest.get("semantic_check"):
        errors.append("semantic_check is required; record how the problem text verifies the figure")
    if source and not fixture:
        if not isinstance(source, dict) or not any(source.get(key) for key in ("file", "crop", "image")):
            errors.append("source must identify the original file or crop")
        if kind == "trace":
            required_segment_names = _required_name_set(
                manifest, "required_segments", errors, required_nonempty=True
            )
            if required_segment_names != segment_names:
                errors.append(
                    "required_segments must match manifest segment names one-to-one"
                )
            for required_field in (
                "required_curves", "required_markers", "required_labels",
                "required_dimensions"
            ):
                if required_field not in manifest:
                    errors.append(
                        f"real-source trace requires an explicit {required_field} list "
                        "(use [] only after inspecting the source and finding none)"
                    )
            manifest_curve_records = _manifest_named_records(
                manifest.get("curves"), "curve", errors
            )
            required_curve_names = _required_name_set(
                manifest,
                "required_curves",
                errors,
                required_nonempty=bool(manifest_curve_records),
            )
            if required_curve_names != set(manifest_curve_records):
                errors.append(
                    "required_curves must match manifest curve names one-to-one"
                )
            manifest_marker_records = _manifest_named_records(
                manifest.get("markers"), "marker", errors
            )
            required_marker_names = _required_name_set(
                manifest, "required_markers", errors, required_nonempty=False
            )
            if required_marker_names != set(manifest_marker_records):
                errors.append(
                    "required_markers must match manifest marker names one-to-one"
                )
            for marker_name, marker_record in manifest_marker_records.items():
                try:
                    resolve_marker_points(marker_record, explicit_points)
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(f"invalid marker {marker_name}: {exc}")
            manifest_label_records = _manifest_named_records(
                manifest.get("labels"), "label", errors, allow_text_identity=True
            )
            required_label_names = _required_name_set(
                manifest, "required_labels", errors, required_nonempty=False
            )
            if required_label_names != set(manifest_label_records):
                errors.append(
                    "required_labels must match manifest label names one-to-one"
                )
            manifest_dimension_records = _manifest_named_records(
                manifest.get("dimensions"), "dimension", errors,
                allow_text_identity=True,
            )
            required_dimension_names = _required_name_set(
                manifest, "required_dimensions", errors, required_nonempty=False
            )
            if required_dimension_names != set(manifest_dimension_records):
                errors.append(
                    "required_dimensions must match manifest dimension names one-to-one"
                )
        if (
            kind == "diagram"
            and isinstance(manifest.get("points"), dict)
            and bool(manifest.get("points"))
            and isinstance(manifest.get("segments"), list)
            and bool(manifest.get("segments"))
        ):
            errors.append(
                "real-source coordinate diagram must use type=trace; "
                "type=diagram cannot bypass landmark and per-segment source checks"
            )
        comparison = manifest.get("source_comparison")
        if not isinstance(comparison, dict):
            errors.append("source_comparison is required for a real source image")
        else:
            if not comparison.get("artifact"):
                errors.append("source_comparison.artifact is required")
            if not comparison.get("report"):
                errors.append("source_comparison.report is required")
            if not comparison.get("review"):
                errors.append("source_comparison.review is required")
            if not comparison.get("review_sha256"):
                errors.append("source_comparison.review_sha256 is required")
            required_checks = (
                "text_match",
                "topology_match",
                "line_styles_match",
                "labels_match",
                "dimensions_match",
                "projection_match",
                "inspected_latest_render",
            )
            for field in required_checks:
                if comparison.get(field) is not True:
                    errors.append(f"source_comparison.{field} must be true")

    verification = verification_report(manifest, fixture)
    errors.extend(verification["errors"])

    components = component_inventory_report(manifest)
    if components:
        errors.extend(components["errors"])

    graph = graph_structure(manifest)
    if graph:
        errors.extend(graph["errors"])

    trace = trace_alignment(manifest)
    if trace:
        errors.extend(trace["errors"])

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "render_style": render_style,
        "net": net,
        "graph": graph,
        "trace": trace,
        "segments": segment_report,
        "verification": verification,
        "components": components,
    }


def svg_fit(svg: str, min_margin: float = 1.5) -> dict[str, Any]:
    errors: list[str] = []
    view = re.search(rf'viewBox="({NUMBER}) ({NUMBER}) ({NUMBER}) ({NUMBER})"', svg)
    if not view:
        return {"ok": False, "errors": ["SVG has no numeric viewBox"]}
    vx, vy, vw, vh = map(float, view.groups())
    # Definitions are not painted at their declaration coordinates. Counting pattern,
    # clipPath, or marker geometry as visible ink creates false clipping failures.
    visible_svg = re.sub(r"<defs\b[^>]*>.*?</defs>", "", svg, flags=re.I | re.S)
    xs: list[float] = []
    ys: list[float] = []

    for match in re.finditer(
        rf'<line[^>]*x1="({NUMBER})" y1="({NUMBER})" x2="({NUMBER})" y2="({NUMBER})"', visible_svg
    ):
        x1, y1, x2, y2 = map(float, match.groups())
        xs.extend((x1, x2)); ys.extend((y1, y2))
    for match in re.finditer(r'points="([^"]+)"', visible_svg):
        for x, y in re.findall(rf'({NUMBER}),({NUMBER})', match.group(1)):
            xs.append(float(x)); ys.append(float(y))
    for match in re.finditer(rf'<circle[^>]*cx="({NUMBER})" cy="({NUMBER})" r="({NUMBER})"', visible_svg):
        cx, cy, radius = map(float, match.groups())
        xs.extend((cx - radius, cx + radius)); ys.extend((cy - radius, cy + radius))
    for match in re.finditer(
        rf'<rect[^>]*x="({NUMBER})" y="({NUMBER})" width="({NUMBER})" height="({NUMBER})"',
        visible_svg,
    ):
        x, y, width, height = map(float, match.groups())
        xs.extend((x, x + width)); ys.extend((y, y + height))
    for match in re.finditer(r'<path[^>]*d="([^"]+)"', visible_svg):
        for x, y in _path_points(match.group(1)):
            xs.append(x); ys.append(y)
    for match in re.finditer(r'<text([^>]*)>(.*?)</text>', visible_svg, re.S):
        attributes, raw_content = match.group(1), match.group(2)
        x_match = re.search(rf'\bx="({NUMBER})"', attributes)
        y_match = re.search(rf'\by="({NUMBER})"', attributes)
        if not x_match or not y_match:
            continue
        x, y = float(x_match.group(1)), float(y_match.group(1))
        content = re.sub(r"<[^>]+>", "", raw_content)
        size_match = re.search(rf'font-size(?::|=")\s*({NUMBER})', attributes)
        size = float(size_match.group(1)) if size_match else 15.0
        width = max(
            size * 0.5,
            sum(size if ord(character) > 0x2000 else size * 0.55 for character in content),
        )
        anchor_match = re.search(r'text-anchor="([^"]+)"', attributes)
        anchor = anchor_match.group(1) if anchor_match else "start"
        if anchor == "middle":
            left, right = x - width / 2, x + width / 2
        elif anchor == "end":
            left, right = x - width, x
        else:
            left, right = x, x + width
        xs.extend((left, right)); ys.extend((y - size * 0.82, y + size * 0.26))

    if not xs:
        return {"ok": False, "errors": ["SVG contains no measurable ink"]}
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    margins = [bounds[0] - vx, bounds[1] - vy, vx + vw - bounds[2], vy + vh - bounds[3]]
    if min(margins) < -0.5:
        errors.append(f"SVG ink is clipped; margins={margins}")
    elif min(margins) < min_margin:
        errors.append(f"SVG safety margin is below {min_margin}; margins={margins}")
    return {"ok": not errors, "errors": errors, "bounds": bounds, "margins": margins}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--svg", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--allow-missing-source", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = {"manifest": validate_manifest(manifest)}
    if args.svg:
        result["svg"] = svg_fit(args.svg.read_text(encoding="utf-8"))
    result["artifacts"] = validate_artifact_evidence(manifest, args.manifest, args.svg)
    if args.allow_missing_source:
        result["manifest"]["errors"] = [
            error for error in result["manifest"]["errors"]
            if error != "source crop metadata is required"
        ]
        result["manifest"]["ok"] = not result["manifest"]["errors"]
    result["ok"] = bool(
        result["manifest"]["ok"]
        and result.get("svg", {"ok": True})["ok"]
        and result["artifacts"]["ok"]
    )

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
