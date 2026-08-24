#!/usr/bin/env python3
"""Build high-resolution visual evidence that Codex must actually inspect.

This script does not approve a reconstruction.  It prepares source, redraw,
registered overlay, final-layout, and native landmark zooms at useful sizes and
writes a hash-bound pending review template.  A vision-capable agent then opens
the generated PNGs, inspects them, and records one review per problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from compare_geometry import rasterize_svg


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_image(path: Path) -> Image.Image:
    if path.suffix.lower() == ".svg":
        return rasterize_svg(path, scale=3.0)
    return Image.open(path).convert("RGB")


def fit_without_distortion(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    panel = Image.new("RGB", size, "white")
    fitted = ImageOps.contain(image.convert("RGB"), (size[0] - 28, size[1] - 48))
    panel.paste(fitted, ((size[0] - fitted.width) // 2, 38))
    return panel


def registered_redraw(source: Image.Image, redraw: Image.Image) -> Image.Image:
    # Trace coordinates are compared in normalized x/y source space.  The
    # registered visual uses that same mapping instead of independently fitting
    # two panels, which can make a distorted drawing look deceptively aligned.
    return redraw.convert("RGB").resize(source.size, Image.Resampling.LANCZOS)


def color_difference(source: Image.Image, redraw: Image.Image) -> Image.Image:
    source_gray = np.asarray(ImageOps.grayscale(source), dtype=np.uint8)
    redraw_gray = np.asarray(ImageOps.grayscale(redraw), dtype=np.uint8)
    source_ink = source_gray < 213
    redraw_ink = redraw_gray < 213
    result = np.full((source.height, source.width, 3), 255, dtype=np.uint8)
    overlap = np.logical_and(source_ink, redraw_ink)
    result[overlap] = (35, 110, 65)
    result[np.logical_and(source_ink, np.logical_not(redraw_ink))] = (220, 38, 38)
    result[np.logical_and(redraw_ink, np.logical_not(source_ink))] = (30, 105, 210)
    return Image.fromarray(result, mode="RGB")


def labeled_panel(label: str, image: Image.Image, size: tuple[int, int]) -> Image.Image:
    panel = fit_without_distortion(image, size)
    draw = ImageDraw.Draw(panel)
    font = ImageFont.load_default()
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline="#b8c2d1", width=2)
    draw.text((14, 12), label, fill="#15263a", font=font)
    return panel


def make_main_sheet(
    source: Image.Image,
    redraw: Image.Image | None,
    observation_overlay: Image.Image | None,
    comparison: Image.Image | None,
    final_crop: Image.Image | None,
    output: Path,
    panel_size: tuple[int, int],
) -> list[str]:
    panels: list[tuple[str, Image.Image]] = [("1 SOURCE NATIVE", source)]
    registered: Image.Image | None = None
    if observation_overlay is not None:
        panels.append(("2 SOURCE + NAMED OBSERVATION", observation_overlay))
    if redraw is not None:
        registered = registered_redraw(source, redraw)
        panels.extend(
            [
                ("3 REDRAW REGISTERED TO SOURCE", registered),
                ("4 SOURCE / REDRAW 50% OVERLAY", Image.blend(source, registered, 0.50)),
                ("5 COLOR DIFF: RED=SOURCE ONLY (TEXT MAY BE RED), BLUE=EXTRA, GREEN=MATCH", color_difference(source, registered)),
            ]
        )
    if comparison is not None:
        panels.append(("6 NAMED GEOMETRY DIAGNOSTIC (OPEN SEPARATELY AT ORIGINAL DETAIL)", comparison))
    if final_crop is not None:
        panels.append(("7 FINAL PDF PROBLEM CROP", final_crop))

    columns = 2
    rows = math.ceil(len(panels) / columns)
    sheet = Image.new("RGB", (panel_size[0] * columns, panel_size[1] * rows), "white")
    for index, (label, image) in enumerate(panels):
        x = (index % columns) * panel_size[0]
        y = (index // columns) * panel_size[1]
        sheet.paste(labeled_panel(label, image, panel_size), (x, y))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return [label for label, _ in panels]


def read_points(observation: Path | None) -> dict[str, list[float]]:
    if observation is None:
        return {}
    data = json.loads(observation.read_text(encoding="utf-8"))
    points = data.get("points", {}) if isinstance(data, dict) else {}
    result: dict[str, list[float]] = {}
    if isinstance(points, dict):
        for name, point in points.items():
            if (
                isinstance(point, list)
                and len(point) == 2
                and all(isinstance(value, (int, float)) for value in point)
            ):
                result[str(name)] = [float(point[0]), float(point[1])]
    return result


def make_landmark_sheet(
    source: Image.Image,
    points: dict[str, list[float]],
    output: Path,
    radius: int,
    tile_size: int,
) -> int:
    if not points:
        return 0
    columns = 4
    rows = math.ceil(len(points) / columns)
    sheet = Image.new("RGB", (tile_size * columns, tile_size * rows), "white")
    font = ImageFont.load_default()
    for index, (name, point) in enumerate(sorted(points.items())):
        x, y = point
        box = (
            max(0, int(round(x)) - radius),
            max(0, int(round(y)) - radius),
            min(source.width, int(round(x)) + radius + 1),
            min(source.height, int(round(y)) + radius + 1),
        )
        crop = source.crop(box).resize((tile_size, tile_size), Image.Resampling.NEAREST)
        crop = ImageEnhance.Contrast(crop).enhance(1.25)
        draw = ImageDraw.Draw(crop)
        relative_x = (x - box[0]) / max(1, box[2] - box[0]) * tile_size
        relative_y = (y - box[1]) / max(1, box[3] - box[1]) * tile_size
        draw.line((relative_x - 16, relative_y, relative_x + 16, relative_y), fill="#f04438", width=2)
        draw.line((relative_x, relative_y - 16, relative_x, relative_y + 16), fill="#f04438", width=2)
        draw.rectangle((0, 0, tile_size - 1, tile_size - 1), outline="#344054", width=2)
        draw.text((8, 8), f"{name}  native=({x:.1f},{y:.1f})", fill="#101828", font=font, stroke_width=2, stroke_fill="white")
        sheet.paste(crop, ((index % columns) * tile_size, (index // columns) * tile_size))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return len(points)


def artifact(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return {"file": str(path.resolve()), "sha256": sha256(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="stable problem/figure identifier")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--observation-overlay", type=Path, required=True)
    parser.add_argument("--redraw", type=Path, required=True)
    parser.add_argument(
        "--comparison", type=Path, required=True,
        help="source_comparison.artifact whose hash is bound to the review",
    )
    parser.add_argument(
        "--diagnostic", type=Path, required=True,
        help="high-resolution named diagnostic that the vision reviewer opens",
    )
    parser.add_argument("--comparison-report", type=Path, required=True)
    parser.add_argument("--final-crop", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="main visual review sheet")
    parser.add_argument("--landmarks-out", type=Path, required=True)
    parser.add_argument("--review-template", type=Path, required=True)
    parser.add_argument("--panel-width", type=int, default=900)
    parser.add_argument("--panel-height", type=int, default=680)
    parser.add_argument("--landmark-radius", type=int, default=36)
    parser.add_argument("--landmark-tile", type=int, default=260)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    outputs = (args.out, args.landmarks_out, args.review_template)
    collisions = [str(path) for path in outputs if path.exists()]
    if collisions and not args.overwrite:
        parser.error("output exists; pass --overwrite to replace: " + ", ".join(collisions))
    for path in (
        args.source, args.observation, args.observation_overlay, args.redraw,
        args.comparison, args.diagnostic, args.comparison_report, args.final_crop,
    ):
        if path is not None and not path.is_file():
            parser.error(f"input does not exist: {path}")

    source = load_image(args.source)
    redraw = load_image(args.redraw) if args.redraw else None
    observation_overlay = load_image(args.observation_overlay) if args.observation_overlay else None
    comparison = load_image(args.diagnostic)
    final_crop = load_image(args.final_crop) if args.final_crop else None
    labels = make_main_sheet(
        source,
        redraw,
        observation_overlay,
        comparison,
        final_crop,
        args.out,
        (args.panel_width, args.panel_height),
    )
    points = read_points(args.observation)
    landmark_count = make_landmark_sheet(
        source,
        points,
        args.landmarks_out,
        args.landmark_radius,
        args.landmark_tile,
    )
    if landmark_count == 0:
        # Keep the artifact contract stable even for text-only or not-yet-
        # observed figures; the pending review clearly records the absence.
        blank = Image.new("RGB", (args.landmark_tile * 2, args.landmark_tile), "white")
        ImageDraw.Draw(blank).text((20, 20), "NO LANDMARK OBSERVATION PROVIDED", fill="#b42318")
        args.landmarks_out.parent.mkdir(parents=True, exist_ok=True)
        blank.save(args.landmarks_out)

    template = {
        "id": args.id,
        "status": "pending-vision-review",
        "reviewer": None,
        "review_method": "Open both PNG artifacts with the vision/image viewer at original detail; do not approve from JSON metrics alone.",
        "source_sha256": sha256(args.source),
        "redraw_sha256": sha256(args.redraw),
        "artifact_sha256": sha256(args.comparison),
        "report_sha256": sha256(args.comparison_report),
        "artifacts": {
            "source": artifact(args.source),
            "observation": artifact(args.observation),
            "observation_overlay": artifact(args.observation_overlay),
            "redraw": artifact(args.redraw),
            "comparison": artifact(args.comparison),
            "diagnostic": artifact(args.diagnostic),
            "comparison_report": artifact(args.comparison_report),
            "final_crop": artifact(args.final_crop),
            "visual_review_sheet": artifact(args.out),
            "landmark_zoom_sheet": artifact(args.landmarks_out),
        },
        "panels": labels,
        "landmark_count": landmark_count,
        "checks": {
            "text_match": None,
            "topology_match": None,
            "line_styles_match": None,
            "labels_match": None,
            "dimensions_match": None,
            "projection_match": None,
            "inspected_latest_render": None,
            "source_crop_complete": None,
            "every_component_present": None,
            "every_vertex_on_native_junction": None,
            "segments_one_to_one_with_source": None,
            "solid_dashed_roles_match": None,
            "curves_follow_source": None,
            "final_pdf_scale_and_spacing_are_suitable": None,
            "no_handwriting_or_answer": None,
        },
        "issues": [],
        "required_action": "A vision-capable reviewer must inspect and replace every null check with true/false, describe every false item, and bind the completed review to these hashes.",
    }
    if args.final_crop is None:
        template["checks"].pop("final_pdf_scale_and_spacing_are_suitable")
    args.review_template.parent.mkdir(parents=True, exist_ok=True)
    args.review_template.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(template, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
