#!/usr/bin/env python3
"""Create SOURCE / REDRAW / OVERLAY evidence for a geometry reconstruction."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from validate_geometry import trace_alignment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def trace_routing_report(manifest: dict[str, object] | None) -> dict[str, object]:
    """Reject real-source coordinate drawings routed around trace verification."""
    errors: list[str] = []
    if isinstance(manifest, dict):
        source = manifest.get("source")
        fixture = isinstance(source, dict) and bool(source.get("fixture"))
        coordinate_diagram = (
            manifest.get("type") == "diagram"
            and isinstance(manifest.get("points"), dict)
            and bool(manifest.get("points"))
            and isinstance(manifest.get("segments"), list)
            and bool(manifest.get("segments"))
        )
        if source and not fixture and coordinate_diagram:
            errors.append(
                "real-source coordinate diagram must use type=trace; "
                "generic diagram routing cannot bypass trace verification"
            )
    return {
        "ok": not errors,
        "errors": errors,
        "method": "real-source coordinate routing gate",
    }


def rasterize_svg(path: Path, scale: float = 2.0) -> Image.Image:
    renderer = Path(__file__).with_name("render_svg.mjs")
    with tempfile.TemporaryDirectory(prefix="geometry-render-") as directory:
        output = Path(directory) / "render.png"
        subprocess.run(
            ["node", str(renderer), str(path), str(output), str(scale)],
            check=True,
            capture_output=True,
            text=True,
        )
        return Image.open(output).convert("RGB").copy()


def fit_panel(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    result = Image.new("RGB", size, "white")
    fitted = ImageOps.contain(image.convert("RGB"), (size[0] - 24, size[1] - 24))
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    result.paste(fitted, (x, y))
    return result


def _otsu_threshold(image: Image.Image) -> int:
    """Return a deterministic dark-ink threshold for a photographed crop."""
    histogram = np.bincount(np.asarray(image.convert("L"), dtype=np.uint8).ravel(), minlength=256)
    total = int(histogram.sum())
    weighted_total = float(np.dot(np.arange(256), histogram))
    background_weight = 0
    background_sum = 0.0
    best_variance = -1.0
    best_threshold = 127
    for threshold in range(1, 255):
        count = int(histogram[threshold - 1])
        background_weight += count
        background_sum += (threshold - 1) * count
        foreground_weight = total - background_weight
        if background_weight == 0 or foreground_weight == 0:
            continue
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


@dataclass
class SourceEvidence:
    """Source-derived masks shared by all comparison gates in one run."""

    size: tuple[int, int]
    threshold: int
    mask: np.ndarray
    mask_image: Image.Image
    _distance_to_ink: np.ndarray | None = field(default=None, repr=False)
    _stroke_width_px: float | None = field(default=None, repr=False)

    @classmethod
    def prepare(cls, source: Image.Image) -> "SourceEvidence":
        source_gray = source.convert("L").filter(ImageFilter.MedianFilter(3))
        threshold = _otsu_threshold(source_gray)
        source_array = np.asarray(source_gray, dtype=np.uint8)
        mask = source_array <= threshold
        return cls(
            size=source.size,
            threshold=threshold,
            mask=mask,
            mask_image=Image.fromarray(mask.astype(np.uint8) * 255, mode="L"),
        )

    def distance_to_ink(self) -> np.ndarray:
        if self._distance_to_ink is None:
            self._distance_to_ink = cv2.distanceTransform(
                1 - self.mask.astype(np.uint8), cv2.DIST_L2, 5
            )
        return self._distance_to_ink

    def estimated_stroke_width(self) -> float:
        if self._stroke_width_px is None:
            inside_distance = cv2.distanceTransform(
                self.mask.astype(np.uint8), cv2.DIST_L2, 5
            )
            positive = inside_distance[self.mask]
            estimate = float(2 * np.median(positive)) if positive.size else 1.0
            self._stroke_width_px = max(1.0, estimate)
        return self._stroke_width_px


@dataclass(frozen=True)
class RedrawEvidence:
    """A redraw normalized once to the source pixel frame."""

    source_size: tuple[int, int]
    mask: np.ndarray
    threshold: int = 225

    @classmethod
    def prepare(cls, redraw: Image.Image, source_size: tuple[int, int]) -> "RedrawEvidence":
        normalized = redraw.convert("L").resize(source_size, Image.Resampling.LANCZOS)
        return cls(
            source_size=source_size,
            mask=np.asarray(normalized, dtype=np.uint8) <= 225,
        )


def ink_alignment(
    source: Image.Image,
    redraw: Image.Image,
    tolerance_ratio: float = 0.012,
    minimum_coverage: float = 0.60,
    source_evidence: SourceEvidence | None = None,
    redraw_evidence: RedrawEvidence | None = None,
) -> dict[str, object]:
    """Measure how much redraw ink lies near independently observed source ink.

    The redraw is normalized to the source crop frame.  This is deliberately a
    one-way precision measure: source labels and page noise may be absent from a
    clean redraw, but invented or displaced redraw strokes are penalized.
    """
    prepared_source = source_evidence or SourceEvidence.prepare(source)
    prepared_redraw = redraw_evidence or RedrawEvidence.prepare(redraw, source.size)
    if prepared_source.size != source.size or prepared_redraw.source_size != source.size:
        raise ValueError("comparison evidence does not match the source size")
    redraw_mask = prepared_redraw.mask
    redraw_count = int(redraw_mask.sum())

    diagonal = math.hypot(*source.size)
    maximum_radius = max(2, int(round(diagonal * tolerance_ratio)))
    radii = sorted({max(1, maximum_radius // 3), max(1, (2 * maximum_radius) // 3), maximum_radius})
    coverage: dict[str, float] = {}
    for radius in radii:
        filter_size = 2 * radius + 1
        dilated = prepared_source.mask_image.filter(ImageFilter.MaxFilter(filter_size))
        nearby_source = np.asarray(dilated, dtype=np.uint8) > 0
        matched = int(np.logical_and(redraw_mask, nearby_source).sum())
        coverage[str(radius)] = matched / redraw_count if redraw_count else 0.0

    final_coverage = coverage[str(maximum_radius)]
    errors: list[str] = []
    if redraw_count == 0:
        errors.append("redraw contains no measurable ink")
    if final_coverage < minimum_coverage:
        errors.append(
            f"redraw ink coverage {final_coverage:.4f} is below {minimum_coverage:.4f} "
            f"within {maximum_radius}px of source ink"
        )
    return {
        "ok": not errors,
        "errors": errors,
        "method": "one-way redraw-to-source ink proximity",
        "source_threshold": prepared_source.threshold,
        "redraw_threshold": prepared_redraw.threshold,
        "tolerance_ratio": tolerance_ratio,
        "maximum_radius_px": maximum_radius,
        "minimum_coverage": minimum_coverage,
        "coverage_by_radius": coverage,
        "redraw_ink_pixels": redraw_count,
    }


def segment_source_support(
    source: Image.Image,
    manifest: dict[str, object] | None,
    radius_ratio: float = 0.004,
    minimum_support: float = 0.80,
    maximum_unsupported_gap_ratio: float = 0.18,
    source_evidence: SourceEvidence | None = None,
) -> dict[str, object]:
    """Check every declared solid segment against source ink at narrow tolerance.

    Whole-image coverage can hide one invented chord when it runs near several
    real edges. This gate samples each declared straight segment independently
    in the original source frame. It rejects a segment whose centerline lacks
    sustained source support, even if the drawing as a whole still overlaps.
    Dashed segments are left to the line-style and residual checks because their
    intended gaps make continuous centerline support inapplicable.
    """
    if not isinstance(manifest, dict) or manifest.get("type", "trace") != "trace":
        return {
            "ok": True,
            "errors": [],
            "applied": False,
            "method": "per-solid-segment narrow source support",
            "segments": [],
        }

    evidence = manifest.get("trace_evidence", {})
    points = evidence.get("points", {}) if isinstance(evidence, dict) else {}
    evidence_size = evidence.get("source_size") if isinstance(evidence, dict) else None
    errors: list[str] = []
    if not isinstance(points, dict) or not points:
        errors.append("trace evidence has no source points for per-segment support")
    if evidence_size != list(source.size):
        errors.append(
            f"trace source_size={evidence_size} does not match comparison source={list(source.size)}"
        )
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "applied": True,
            "method": "per-solid-segment narrow source support",
            "segments": [],
        }

    prepared_source = source_evidence or SourceEvidence.prepare(source)
    if prepared_source.size != source.size:
        raise ValueError("source evidence does not match the source size")
    diagonal = math.hypot(*source.size)
    nominal_radius = max(2, int(round(diagonal * radius_ratio)))
    source_stroke_width = prepared_source.estimated_stroke_width()
    stroke_radius = max(2, int(round(source_stroke_width * 2 + 1)))
    radius = min(nominal_radius, stroke_radius)

    # A whole-page Otsu threshold can suppress a faint printed edge that is
    # stable in its own crop. Add only segment-local native-pixel evidence,
    # capped close to the global threshold, before computing the shared
    # distance field. This is deterministic contrast assistance, not generated
    # super-resolution: no coordinates or pixels are invented.
    source_gray = np.asarray(
        source.convert("L").filter(ImageFilter.MedianFilter(3)), dtype=np.uint8
    )
    support_mask = prepared_source.mask.copy()
    local_thresholds: dict[str, int] = {}
    local_margin = max(6, radius * 3)
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict) or segment.get("style", "solid") not in {
            "solid", "emphasis"
        }:
            continue
        start = points.get(segment.get("from"))
        end = points.get(segment.get("to"))
        if (
            not isinstance(start, list) or len(start) != 2
            or not isinstance(end, list) or len(end) != 2
        ):
            continue
        x0 = max(0, int(math.floor(min(float(start[0]), float(end[0])))) - local_margin)
        y0 = max(0, int(math.floor(min(float(start[1]), float(end[1])))) - local_margin)
        x1 = min(source.width, int(math.ceil(max(float(start[0]), float(end[0])))) + local_margin + 1)
        y1 = min(source.height, int(math.ceil(max(float(start[1]), float(end[1])))) + local_margin + 1)
        patch = source_gray[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        local_otsu = _otsu_threshold(Image.fromarray(patch, mode="L"))
        local_threshold = max(
            prepared_source.threshold,
            min(local_otsu, prepared_source.threshold + 12),
        )
        name = str(segment.get("name", f"{segment.get('from')}-{segment.get('to')}"))
        local_thresholds[name] = local_threshold
        support_mask[y0:y1, x0:x1] |= patch <= local_threshold
    distance_to_ink = cv2.distanceTransform(
        1 - support_mask.astype(np.uint8), cv2.DIST_L2, 5
    )

    records: list[dict[str, object]] = []
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict) or segment.get("style", "solid") not in {
            "solid", "emphasis"
        }:
            continue
        start_name, end_name = segment.get("from"), segment.get("to")
        start, end = points.get(start_name), points.get(end_name)
        name = str(segment.get("name", f"{start_name}-{end_name}"))
        if (
            not isinstance(start, list)
            or not isinstance(end, list)
            or len(start) != 2
            or len(end) != 2
        ):
            errors.append(f"segment {name} lacks named source endpoints")
            continue
        x1, y1 = (float(start[0]), float(start[1]))
        x2, y2 = (float(end[0]), float(end[1]))
        length = math.hypot(x2 - x1, y2 - y1)
        sample_count = max(2, int(round(length)) + 1)
        xs = np.clip(
            np.rint(np.linspace(x1, x2, sample_count)).astype(int), 0, source.width - 1
        )
        ys = np.clip(
            np.rint(np.linspace(y1, y2, sample_count)).astype(int), 0, source.height - 1
        )
        supported = distance_to_ink[ys, xs] <= radius
        support = float(supported.mean())
        longest_gap = 0
        current_gap = 0
        for value in supported:
            current_gap = 0 if bool(value) else current_gap + 1
            longest_gap = max(longest_gap, current_gap)
        gap_ratio = longest_gap / max(1, sample_count - 1)
        record_ok = (
            support >= minimum_support
            and gap_ratio <= maximum_unsupported_gap_ratio
        )
        record = {
            "name": name,
            "from": start_name,
            "to": end_name,
            "style": segment.get("style", "solid"),
            "length_px": length,
            "support": support,
            "longest_unsupported_gap_px": longest_gap,
            "longest_unsupported_gap_ratio": gap_ratio,
            "ok": record_ok,
        }
        records.append(record)
        if not record_ok:
            errors.append(
                f"segment {name} is not continuously supported by source ink: "
                f"support={support:.4f}, unsupported_gap_ratio={gap_ratio:.4f}"
            )

    return {
        "ok": not errors,
        "errors": errors,
        "applied": True,
        "method": "per-solid-segment narrow source support",
        "source_threshold": prepared_source.threshold,
        "threshold_method": "global Otsu plus segment-local native-pixel Otsu capped at global+12",
        "segment_local_thresholds": local_thresholds,
        "estimated_source_stroke_width_px": source_stroke_width,
        "radius_ratio": radius_ratio,
        "nominal_radius_px": nominal_radius,
        "stroke_limited_radius_px": stroke_radius,
        "radius_px": radius,
        "minimum_support": minimum_support,
        "maximum_unsupported_gap_ratio": maximum_unsupported_gap_ratio,
        "segments": records,
    }


def residual_line_report(
    source: Image.Image,
    redraw: Image.Image,
    manifest: dict[str, object] | None = None,
    tolerance_ratio: float = 0.012,
    minimum_line_ratio: float = 0.085,
    source_evidence: SourceEvidence | None = None,
    redraw_evidence: RedrawEvidence | None = None,
) -> dict[str, object]:
    """Detect long straight source strokes left unexplained by the redraw.

    Text and dimension annotations leave many small residual components, so the
    gate only considers long, nearly continuous Hough segments. This reverse
    direction complements ``ink_alignment`` and catches omitted geometry
    without requiring the missing segment to have been named first.
    """
    prepared_source = source_evidence or SourceEvidence.prepare(source)
    prepared_redraw = redraw_evidence or RedrawEvidence.prepare(redraw, source.size)
    if prepared_source.size != source.size or prepared_redraw.source_size != source.size:
        raise ValueError("comparison evidence does not match the source size")
    source_mask = prepared_source.mask
    redraw_mask = prepared_redraw.mask

    diagonal = math.hypot(*source.size)
    tolerance_radius = max(2, int(round(diagonal * tolerance_ratio)))
    kernel_size = 2 * tolerance_radius + 1
    redraw_nearby = cv2.dilate(
        redraw_mask.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
    ).astype(bool)
    residual = np.logical_and(source_mask, np.logical_not(redraw_nearby)).astype(np.uint8) * 255
    residual = cv2.morphologyEx(
        residual,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )

    # Dimension arcs and text outside the observed geometry are legitimate
    # source-only ink. When trace landmarks exist, limit the omission census
    # to their convex hull (plus a small margin) so an unlisted internal edge
    # cannot hide among annotation strokes.
    scope = "full source crop"
    evidence = manifest.get("trace_evidence", {}) if isinstance(manifest, dict) else {}
    evidence_points = evidence.get("points", {}) if isinstance(evidence, dict) else {}
    evidence_size = evidence.get("source_size") if isinstance(evidence, dict) else None
    if (
        isinstance(evidence_points, dict)
        and len(evidence_points) >= 3
        and evidence_size == list(source.size)
    ):
        hull_points: list[list[int]] = []
        for value in evidence_points.values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and all(isinstance(coordinate, (int, float)) for coordinate in value)
            ):
                hull_points.append([int(round(value[0])), int(round(value[1]))])
        if len(hull_points) >= 3:
            hull = cv2.convexHull(np.asarray(hull_points, dtype=np.int32))
            scope_mask = np.zeros_like(residual)
            cv2.fillConvexPoly(scope_mask, hull, 255)
            scope_margin = max(3, tolerance_radius // 2)
            scope_mask = cv2.dilate(
                scope_mask,
                cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE,
                    (2 * scope_margin + 1, 2 * scope_margin + 1),
                ),
            )
            residual = cv2.bitwise_and(residual, scope_mask)
            scope = "trace-landmark convex hull"

    minimum_length = max(35, int(round(diagonal * minimum_line_ratio)))
    maximum_gap = max(6, int(round(diagonal * 0.014)))
    vote_threshold = max(14, int(round(minimum_length * 0.15)))
    raw_lines = cv2.HoughLinesP(
        residual,
        1,
        math.pi / 360,
        threshold=vote_threshold,
        minLineLength=minimum_length,
        maxLineGap=maximum_gap,
    )

    candidates: list[dict[str, object]] = []
    if raw_lines is not None:
        for [[x1, y1, x2, y2]] in raw_lines:
            length = math.hypot(x2 - x1, y2 - y1)
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
            candidate = {
                "from": [int(x1), int(y1)],
                "to": [int(x2), int(y2)],
                "length_px": length,
                "length_ratio": length / diagonal,
                "angle_degrees": angle,
            }
            if any(
                abs(float(candidate["angle_degrees"]) - float(prior["angle_degrees"])) <= 2.0
                and math.dist(candidate["from"], prior["from"]) <= tolerance_radius * 2
                and math.dist(candidate["to"], prior["to"]) <= tolerance_radius * 2
                for prior in candidates
            ):
                continue
            candidates.append(candidate)
    candidates.sort(key=lambda item: float(item["length_px"]), reverse=True)
    errors: list[str] = []
    if candidates:
        longest = candidates[0]
        errors.append(
            "source contains a long straight residual not explained by the redraw: "
            f"length={longest['length_px']:.1f}px, angle={longest['angle_degrees']:.1f}deg"
        )
    return {
        "ok": not errors,
        "errors": errors,
        "method": "reverse source-to-redraw long-line residual census",
        "scope": scope,
        "source_threshold": prepared_source.threshold,
        "redraw_threshold": prepared_redraw.threshold,
        "tolerance_ratio": tolerance_ratio,
        "tolerance_radius_px": tolerance_radius,
        "minimum_line_ratio": minimum_line_ratio,
        "minimum_line_length_px": minimum_length,
        "maximum_gap_px": maximum_gap,
        "candidates": candidates[:20],
        "residual_ink_pixels": int((residual > 0).sum()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("redraw", type=Path)
    parser.add_argument("--box", nargs=4, type=int)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, default=560)
    parser.add_argument("--height", type=int, default=420)
    parser.add_argument("--ink-tolerance", type=float, default=0.012)
    parser.add_argument("--min-redraw-ink-coverage", type=float, default=0.60)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGB")
    if args.box:
        source = source.crop(tuple(args.box))
    redraw = rasterize_svg(args.redraw) if args.redraw.suffix.lower() == ".svg" else Image.open(args.redraw).convert("RGB")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    routing = trace_routing_report(manifest)
    prepared_source = SourceEvidence.prepare(source)
    prepared_redraw = RedrawEvidence.prepare(redraw, source.size)
    trace = trace_alignment(manifest) if manifest else None
    ink = ink_alignment(
        source,
        redraw,
        tolerance_ratio=args.ink_tolerance,
        minimum_coverage=args.min_redraw_ink_coverage,
        source_evidence=prepared_source,
        redraw_evidence=prepared_redraw,
    )
    residual = residual_line_report(
        source,
        redraw,
        manifest=manifest,
        tolerance_ratio=args.ink_tolerance,
        source_evidence=prepared_source,
        redraw_evidence=prepared_redraw,
    )
    segment_support = segment_source_support(
        source,
        manifest,
        source_evidence=prepared_source,
    )
    size = (args.width, args.height)
    source_panel, redraw_panel = fit_panel(source, size), fit_panel(redraw, size)
    overlay = Image.blend(source_panel, redraw_panel, 0.5)

    label_height = 102 if trace or not routing["ok"] else 70
    sheet = Image.new("RGB", (args.width * 3, args.height + label_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (label, panel) in enumerate(
        (("SOURCE", source_panel), ("REDRAW", redraw_panel), ("OVERLAY", overlay))
    ):
        x = index * args.width
        sheet.paste(panel, (x, label_height))
        draw.text((x + 10, 10), label, fill="#28334a", font=font)
        draw.rectangle((x, label_height, x + args.width - 1, args.height + label_height - 1), outline="#d6dce7")

    if trace:
        maximum = trace.get("max_normalized_error", float("inf"))
        tolerance = trace.get("tolerance", 0.0)
        status = "PASS" if trace.get("ok") else "FAIL"
        draw.text(
            (args.width * 2 + 10, 30),
            f"TRACE MAX={maximum:.4f} LIMIT={tolerance:.4f} {status}",
            fill="#176b3a" if trace.get("ok") else "#b42318",
            font=font,
        )
    if not routing["ok"]:
        draw.text(
            (args.width * 2 + 10, 30),
            "ROUTING=FAIL USE type=trace",
            fill="#b42318",
            font=font,
        )
    metric_y = 46 if trace or not routing["ok"] else 30
    ink_coverage = ink["coverage_by_radius"][str(ink["maximum_radius_px"])]
    ink_status = "PASS" if ink["ok"] else "FAIL"
    draw.text(
        (args.width * 2 + 10, metric_y),
        f"INK={ink_coverage:.4f} LIMIT={ink['minimum_coverage']:.4f} {ink_status}",
        fill="#176b3a" if ink["ok"] else "#b42318",
        font=font,
    )
    residual_status = "PASS" if residual["ok"] else "FAIL"
    residual_count = len(residual["candidates"])
    draw.text(
        (args.width * 2 + 10, metric_y + 16),
        f"MISSING-LINE={residual_count} {residual_status}",
        fill="#176b3a" if residual["ok"] else "#b42318",
        font=font,
    )
    segment_status = "PASS" if segment_support["ok"] else "FAIL"
    unsupported_count = sum(
        1 for item in segment_support["segments"] if not item.get("ok")
    )
    draw.text(
        (args.width * 2 + 10, metric_y + 32),
        f"UNSUPPORTED-SEGMENT={unsupported_count} {segment_status}",
        fill="#176b3a" if segment_support["ok"] else "#b42318",
        font=font,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    result = {
        "ok": bool(
            routing.get("ok")
            and (trace is None or trace.get("ok"))
            and ink.get("ok")
            and residual.get("ok")
            and segment_support.get("ok")
        ),
        "source": str(args.source),
        "redraw": str(args.redraw),
        "artifact": str(args.out),
        "source_sha256": sha256(args.source),
        "redraw_sha256": sha256(args.redraw),
        "artifact_sha256": sha256(args.out),
        "source_size": list(source.size),
        "redraw_size": list(redraw.size),
        "panel_size": list(size),
        "evidence_preparation": {
            "source_mask_passes": 1,
            "redraw_normalizations": 1,
            "estimated_source_stroke_width_px": prepared_source.estimated_stroke_width(),
        },
        "trace": trace,
        "trace_routing": routing,
        "ink_alignment": ink,
        "source_residual": residual,
        "segment_source_support": segment_support,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
