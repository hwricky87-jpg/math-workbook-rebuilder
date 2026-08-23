#!/usr/bin/env python3
"""Turn line-style questions in a workbook crop into numeric evidence."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

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


def main() -> int:
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
    args = parser.parse_args()

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
    print(
        f"region=({origin_x},{origin_y})-({origin_x + array.shape[1]},{origin_y + array.shape[0]}) "
        f"{array.shape[1]}x{array.shape[0]} threshold={threshold:.0f}"
    )

    if args.lines or args.peaks:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
