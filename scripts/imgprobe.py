#!/usr/bin/env python3
"""Turn line-style questions in a workbook crop into numeric evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def load(path: Path, box: tuple[int, int, int, int] | None = None):
    image = Image.open(path).convert("L")
    origin_x, origin_y = 0, 0
    if box:
        origin_x, origin_y = box[0], box[1]
        image = image.crop(box)
    return np.asarray(image, dtype=np.float32), origin_x, origin_y


def otsu(array: np.ndarray) -> int:
    histogram, _ = np.histogram(array.astype(np.uint8), bins=256, range=(0, 256))
    total = array.size
    weighted_sum = np.dot(np.arange(256), histogram)
    background_sum = background_weight = 0.0
    best_variance, threshold = -1.0, 128
    for value in range(256):
        background_weight += histogram[value]
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += value * histogram[value]
        variance = background_weight * foreground_weight * (
            background_sum / background_weight - (weighted_sum - background_sum) / foreground_weight
        ) ** 2
        if variance > best_variance:
            best_variance, threshold = variance, value
    return threshold


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    start = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            output.append((start, index - start))
            start = None
    if start is not None:
        output.append((start, len(mask) - start))
    return output


def value_runs(values: list[int]) -> list[tuple[int, int, int]]:
    """Return start, length, and value for consecutive equal measurements."""
    if not values:
        return []
    output: list[tuple[int, int, int]] = []
    start = 0
    for index in range(1, len(values) + 1):
        if index == len(values) or values[index] != values[start]:
            output.append((start, index - start, values[start]))
            start = index
    return output


def stability_report(values: list[int], minimum_samples: int = 3) -> dict[str, Any]:
    """Accept one unambiguous count sustained for enough consecutive samples."""
    measured_runs = value_runs(values)
    stable_values = sorted({
        value for _, length, value in measured_runs if length >= minimum_samples
    })
    stable_count = stable_values[0] if len(stable_values) == 1 else None
    return {
        "ok": stable_count is not None,
        "stable_count": stable_count,
        "minimum_samples": minimum_samples,
        "runs": [
            {"start": start, "length": length, "value": value}
            for start, length, value in measured_runs
        ],
        "stable_values": stable_values,
    }


def line_peaks(array: np.ndarray, threshold: float, minimum_coverage: float, axis: str, offset: int):
    ink = array < threshold
    coverage = ink.mean(axis=1 if axis == "rows" else 0)
    output = []
    for start, length in runs(coverage >= minimum_coverage):
        segment = coverage[start:start + length]
        centre = start + int(np.argmax(segment))
        output.append((centre + offset, length, float(segment.max())))
    return output


def sample_segment(array: np.ndarray, p0, p1, origin_x: int, origin_y: int, up: int = 4,
                   radius: int | None = None) -> tuple[np.ndarray, int, int]:
    image = Image.fromarray(array.astype(np.uint8)).resize(
        (array.shape[1] * up, array.shape[0] * up), Image.Resampling.LANCZOS
    )
    scaled = np.asarray(image, dtype=np.float32)
    height, width = scaled.shape
    radius = up if radius is None else radius
    x0, y0 = (p0[0] - origin_x) * up, (p0[1] - origin_y) * up
    x1, y1 = (p1[0] - origin_x) * up, (p1[1] - origin_y) * up
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("segment endpoints must differ")
    samples = max(2, int(length))
    normal_x, normal_y = -dy / length, dx / length
    values = []
    # Search only perpendicular to the segment. Searching along it fills dash gaps.
    for index in range(samples):
        t = index / float(samples - 1)
        centre_x, centre_y = x0 + dx * t, y0 + dy * t
        darkest = 255.0
        for shift in range(-radius, radius + 1):
            x = int(round(centre_x + normal_x * shift))
            y = int(round(centre_y + normal_y * shift))
            if 0 <= x < width and 0 <= y < height:
                darkest = min(darkest, float(scaled[y, x]))
        values.append(darkest)
    return np.asarray(values), samples, up


def _bilinear(array: np.ndarray, x: float, y: float) -> float:
    height, width = array.shape
    if x < 0 or y < 0 or x > width - 2 or y > height - 2:
        return 255.0
    x0, y0 = int(x), int(y)
    fx, fy = x - x0, y - y0
    return float(
        array[y0, x0] * (1 - fx) * (1 - fy)
        + array[y0, x0 + 1] * fx * (1 - fy)
        + array[y0 + 1, x0] * (1 - fx) * fy
        + array[y0 + 1, x0 + 1] * fx * fy
    )


def _dark_arcs(mask: list[bool]) -> list[tuple[int, int]]:
    """Find dark arcs on a circular sample and merge the wraparound arc."""
    segments: list[tuple[int, int]] = []
    index = 0
    while index < len(mask):
        if not mask[index]:
            index += 1
            continue
        end = index
        while end < len(mask) and mask[end]:
            end += 1
        segments.append((index, end - 1))
        index = end
    if len(segments) > 1 and segments[0][0] == 0 and segments[-1][1] == len(mask) - 1:
        segments[0] = (segments[-1][0] - len(mask), segments[0][1])
        segments.pop()
    return segments


def ring_strands(
    array: np.ndarray, centre_x: float, centre_y: float, radius: float,
    threshold: float, samples: int = 1440,
) -> list[float]:
    """Measure line directions crossing a ring around one graph vertex."""
    dark = [
        _bilinear(
            array,
            centre_x + radius * math.cos(2 * math.pi * index / samples),
            centre_y + radius * math.sin(2 * math.pi * index / samples),
        ) < threshold
        for index in range(samples)
    ]
    return sorted(
        ((start + end) / 2.0 * 360.0 / samples) % 360
        for start, end in _dark_arcs(dark)
    )


def cut_crossings(
    array: np.ndarray, p0: tuple[float, float], p1: tuple[float, float],
    threshold: float, half_length: float, samples: int = 800,
) -> int:
    """Count strands crossing a perpendicular cut through an edge bundle's midpoint."""
    middle_x, middle_y = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("cut endpoints must differ")
    normal_x, normal_y = -dy / length, dx / length
    dark = np.asarray([
        _bilinear(
            array,
            middle_x + normal_x * (-half_length + 2 * half_length * index / (samples - 1)),
            middle_y + normal_y * (-half_length + 2 * half_length * index / (samples - 1)),
        ) < threshold
        for index in range(samples)
    ], dtype=bool)
    return len(runs(dark))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--box", nargs=4, type=int)
    parser.add_argument("--seg", nargs=4, type=float)
    parser.add_argument("--lines", action="store_true")
    parser.add_argument("--peaks", choices=("rows", "cols"))
    parser.add_argument("--stroke", action="store_true")
    parser.add_argument("--thr", type=float)
    parser.add_argument("--mincov", type=float, default=0.55)
    parser.add_argument("--rad", type=int)
    parser.add_argument("--deg", nargs=2, type=float, metavar=("X", "Y"))
    parser.add_argument("--cut", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--radii", nargs="+", type=float)
    parser.add_argument("--half-lengths", nargs="+", type=float)
    parser.add_argument("--stable-samples", type=int, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.deg and not args.radii:
        parser.error("--deg requires explicit --radii values")
    if args.cut and not args.half_lengths:
        parser.error("--cut requires explicit --half-lengths values")
    if args.radii and not args.deg:
        parser.error("--radii requires --deg")
    if args.half_lengths and not args.cut:
        parser.error("--half-lengths requires --cut")
    if args.stable_samples < 2:
        parser.error("--stable-samples must be at least 2")

    box = tuple(args.box) if args.box else None
    if args.seg and box is None:
        x0, y0, x1, y1 = args.seg
        margin = 12
        box = (
            int(min(x0, x1) - margin), int(min(y0, y1) - margin),
            int(max(x0, x1) + margin), int(max(y0, y1) + margin),
        )
    array, origin_x, origin_y = load(args.image, box)
    threshold = args.thr if args.thr is not None else otsu(array)
    with Image.open(args.image) as source_image:
        source_size = list(source_image.size)
    report: dict[str, Any] = {
        "ok": True,
        "image": str(args.image),
        "source_sha256": _sha256(args.image),
        "image_size": source_size,
        "box": list(box) if box else [0, 0, source_size[0], source_size[1]],
        "threshold": float(threshold),
        "errors": [],
    }
    print(
        f"region=({origin_x},{origin_y})-({origin_x + array.shape[1]},{origin_y + array.shape[0]}) "
        f"{array.shape[1]}x{array.shape[0]} threshold={threshold:.0f}"
    )

    if args.lines or args.peaks:
        report["lines"] = {}
        axes = ("rows", "cols") if args.lines else (args.peaks,)
        for axis in axes:
            results = line_peaks(
                array, threshold, args.mincov, axis, origin_y if axis == "rows" else origin_x
            )
            coordinate = "y" if axis == "rows" else "x"
            details = " ".join(
                f"{coordinate}={centre}(thickness={length},coverage={coverage:.2f})"
                for centre, length, coverage in results
            )
            print(f"{axis}: count={len(results)} {details}")
            report["lines"][axis] = [
                {"coordinate": centre, "thickness": length, "coverage": coverage}
                for centre, length, coverage in results
            ]

    if args.deg:
        radii = [float(value) for value in args.radii]
        if any(value <= 0 for value in radii) or any(
            right <= left for left, right in zip(radii, radii[1:])
        ):
            parser.error("--radii must be positive and strictly increasing")
        centre_x, centre_y = args.deg[0] - origin_x, args.deg[1] - origin_y
        counts: list[int] = []
        angles: list[list[float]] = []
        for radius in radii:
            if (
                centre_x - radius < 1 or centre_y - radius < 1
                or centre_x + radius > array.shape[1] - 2
                or centre_y + radius > array.shape[0] - 2
            ):
                parser.error(f"degree ring radius {radius:g} leaves the selected image region")
            measured_angles = ring_strands(array, centre_x, centre_y, radius, threshold)
            counts.append(len(measured_angles))
            angles.append(measured_angles)
            print(
                f"degree radius={radius:g} count={len(measured_angles)} "
                f"angles={' '.join(f'{angle:.1f}' for angle in measured_angles)}"
            )
        stability = stability_report(counts, args.stable_samples)
        degree_report = {
            "centre": [float(args.deg[0]), float(args.deg[1])],
            "radii": radii,
            "counts": counts,
            "angles": angles,
            **stability,
        }
        report["degree"] = degree_report
        if not stability["ok"]:
            report["errors"].append("degree measurement has no unique stable count")

    if args.cut:
        half_lengths = [float(value) for value in args.half_lengths]
        if any(value <= 0 for value in half_lengths) or any(
            right <= left for left, right in zip(half_lengths, half_lengths[1:])
        ):
            parser.error("--half-lengths must be positive and strictly increasing")
        local_p0 = (args.cut[0] - origin_x, args.cut[1] - origin_y)
        local_p1 = (args.cut[2] - origin_x, args.cut[3] - origin_y)
        dx, dy = local_p1[0] - local_p0[0], local_p1[1] - local_p0[1]
        segment_length = math.hypot(dx, dy)
        if segment_length == 0:
            parser.error("--cut endpoints must differ")
        normal_x, normal_y = -dy / segment_length, dx / segment_length
        middle_x = (local_p0[0] + local_p1[0]) / 2.0
        middle_y = (local_p0[1] + local_p1[1]) / 2.0
        counts = []
        for half_length in half_lengths:
            endpoints = (
                (middle_x - normal_x * half_length, middle_y - normal_y * half_length),
                (middle_x + normal_x * half_length, middle_y + normal_y * half_length),
            )
            if any(
                x < 1 or y < 1 or x > array.shape[1] - 2 or y > array.shape[0] - 2
                for x, y in endpoints
            ):
                parser.error(f"cut half-length {half_length:g} leaves the selected image region")
            count = cut_crossings(array, local_p0, local_p1, threshold, half_length)
            counts.append(count)
            print(f"cut half_length={half_length:g} count={count}")
        stability = stability_report(counts, args.stable_samples)
        cut_report = {
            "endpoints": [list(map(float, args.cut[:2])), list(map(float, args.cut[2:]))],
            "half_lengths": half_lengths,
            "counts": counts,
            **stability,
        }
        report["cut"] = cut_report
        if not stability["ok"]:
            report["errors"].append("cut measurement has no unique stable count")

    if args.seg:
        p0, p1 = (args.seg[0], args.seg[1]), (args.seg[2], args.seg[3])
        values, sample_count, up = sample_segment(array, p0, p1, origin_x, origin_y, radius=args.rad)
        ink = values < threshold
        ink_runs, gap_runs = runs(ink), runs(~ink)
        fraction = float(ink.mean())
        on_length = np.mean([item[1] for item in ink_runs]) / up if ink_runs else 0.0
        off_length = np.mean([item[1] for item in gap_runs]) / up if gap_runs else 0.0
        if fraction < 0.25:
            verdict = "NONE"
        elif fraction > 0.88 or len(ink_runs) <= 1:
            verdict = "SOLID"
        elif on_length < 1.2:
            verdict = "UNCERTAIN"
        else:
            verdict = f"DASHED dash={on_length:.1f}px gap={off_length:.1f}px"
        print(
            f"segment_length={sample_count / up:.1f}px ink_fraction={fraction:.2f} "
            f"runs={len(ink_runs)} verdict={verdict}"
        )
        if args.stroke:
            darkness = values[ink]
            mean = float(darkness.mean()) if darkness.size else 255.0
            deviation = float(darkness.std()) if darkness.size else 0.0
            print(f"stroke_mean={mean:.1f} stroke_sd={deviation:.1f}")
        report["segment"] = {
            "endpoints": [list(map(float, args.seg[:2])), list(map(float, args.seg[2:]))],
            "length": sample_count / up,
            "ink_fraction": fraction,
            "runs": len(ink_runs),
            "verdict": verdict,
        }

    report["ok"] = not report["errors"]
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.deg or args.cut:
        print(
            f"measurement_status={'PASS' if report['ok'] else 'FAIL'} "
            f"source_sha256={report['source_sha256']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
