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

from geometry_svg import _resolved_face, _resolve_point, net_report
from imgprobe import stability_report


NUMBER = r"-?\d+(?:\.\d+)?"
MAX_TRACE_NORMALIZED_ERROR = 0.04
ALLOWED_VERIFICATION_KINDS = {
    "external-answer",
    "independent-invariant",
    "independent-measurement",
    "user-confirmation",
}


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
            if sx < 0 or sy < 0 or sx > source_width or sy > source_height:
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


def _read_json(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is unreadable: {exc}")
        return None


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
        inspection_path = (
            _resolve_artifact(base, trace.get("inspection_artifact"))
            if isinstance(trace, dict) else None
        )
        if inspection_path is None or not inspection_path.is_file():
            errors.append("trace inspection artifact does not exist")

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
        if source_path and source_path.is_file() and report.get("source_sha256") != _sha256(source_path):
            errors.append("source comparison report is stale for the current source crop")
        if svg_path and svg_path.is_file() and report.get("redraw_sha256") != _sha256(svg_path):
            errors.append("source comparison report is stale for the current SVG")
        if artifact_path and artifact_path.is_file() and report.get("artifact_sha256") != _sha256(artifact_path):
            errors.append("source comparison artifact hash does not match its report")

    verification = manifest.get("verification_evidence")
    if isinstance(verification, dict) and verification.get("kind") in {
        "external-answer", "user-confirmation"
    }:
        verification_path = _resolve_artifact(base, verification.get("artifact"))
        if verification_path is None or not verification_path.is_file():
            errors.append("verification evidence artifact does not exist")
        elif verification.get("artifact_sha256") != _sha256(verification_path):
            errors.append("verification evidence artifact hash is stale")

    graph_artifacts = validate_graph_probe_artifacts(manifest, base, source_path)
    errors.extend(graph_artifacts["errors"])

    return {"ok": not errors, "errors": errors}


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


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    kind = manifest.get("type", "trace")
    canvas = manifest.get("canvas", {})
    if float(canvas.get("width", 0)) <= 0 or float(canvas.get("height", 0)) <= 0:
        errors.append("canvas width and height must be positive")

    explicit_points = {
        name: _resolve_point(value, {}) for name, value in manifest.get("points", {}).items()
    }
    labels = manifest.get("labels", [])
    segments = manifest.get("segments", [])
    label_names = {str(item.get("text")) for item in labels} | {
        str(item.get("name")) for item in labels if item.get("name")
    }
    segment_names = {str(item.get("name")) for item in segments if item.get("name")}

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
        comparison = manifest.get("source_comparison")
        if not isinstance(comparison, dict):
            errors.append("source_comparison is required for a real source image")
        else:
            if not comparison.get("artifact"):
                errors.append("source_comparison.artifact is required")
            if not comparison.get("report"):
                errors.append("source_comparison.report is required")
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
        "net": net,
        "graph": graph,
        "trace": trace,
        "verification": verification,
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
