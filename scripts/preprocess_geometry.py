#!/usr/bin/env python3
"""Create deterministic, source-preserving geometry inspection variants."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


DEFAULT_MAX_LONG_EDGE = 2200
DEFAULT_SHEET_MAX_LONG_EDGE = 1000
MAX_AUTOMATIC_SCALE = 6.0


def sha256_file(path: Path) -> str:
    """Return a content hash without depending on image encoder metadata."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pixel_sha256(image: Image.Image) -> str:
    """Hash decoded RGB pixels and dimensions for exact crop-lineage checks."""
    rgb = image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(f"RGB:{rgb.width}x{rgb.height}:".encode("ascii"))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def edge_stroke_report(
    image: Image.Image,
    dark_threshold: int = 190,
    minimum_depth: int = 6,
    minimum_pixels: int = 5,
) -> dict[str, object]:
    """Find connected dark strokes that enter the crop through an image edge.

    A component is reported only when it touches an exact boundary, contains enough
    dark pixels, and extends at least ``minimum_depth`` pixels into the crop.  This
    avoids treating isolated JPEG speckles as clipped diagram strokes.
    """
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    mask = (gray <= dark_threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    height, width = gray.shape
    hits: list[dict[str, object]] = []
    edge_counts = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    for component in range(1, count):
        x, y, component_width, component_height, area = [
            int(value) for value in stats[component]
        ]
        if area < minimum_pixels:
            continue
        edges: list[str] = []
        if x == 0 and component_width >= minimum_depth:
            edges.append("left")
        if y == 0 and component_height >= minimum_depth:
            edges.append("top")
        if x + component_width == width and component_width >= minimum_depth:
            edges.append("right")
        if y + component_height == height and component_height >= minimum_depth:
            edges.append("bottom")
        if not edges:
            continue
        boundary_pixels = 0
        component_mask = labels == component
        if "left" in edges:
            boundary_pixels += int(component_mask[:, 0].sum())
        if "right" in edges:
            boundary_pixels += int(component_mask[:, -1].sum())
        if "top" in edges:
            boundary_pixels += int(component_mask[0, :].sum())
        if "bottom" in edges:
            boundary_pixels += int(component_mask[-1, :].sum())
        if boundary_pixels < 1:
            continue
        for edge in edges:
            edge_counts[edge] += 1
        hits.append({
            "component": component,
            "edges": edges,
            "bbox": [x, y, x + component_width, y + component_height],
            "dark_pixels": area,
            "boundary_pixels": boundary_pixels,
        })
    return {
        "detected": bool(hits),
        "dark_threshold": dark_threshold,
        "minimum_depth": minimum_depth,
        "minimum_pixels": minimum_pixels,
        "edge_component_counts": edge_counts,
        "components": hits,
    }


def crop_context_image(source: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """Return a parent-source overview with the exact native crop rectangle."""
    context = source.convert("RGB").copy()
    draw = ImageDraw.Draw(context)
    x0, y0, x1, y1 = box
    line_width = min(
        max(1, min(x1 - x0, y1 - y0) // 2),
        max(2, int(round(max(context.size) / 500))),
    )
    for offset in range(line_width):
        draw.rectangle(
            (x0 + offset, y0 + offset, x1 - 1 - offset, y1 - 1 - offset),
            outline=(220, 25, 45),
        )
    label = f"crop [{x0}, {y0}, {x1}, {y1}]"
    label_y = max(0, y0 - 18)
    draw.rectangle((x0, label_y, x0 + max(150, len(label) * 7), label_y + 18), fill="white")
    draw.text((x0 + 3, label_y + 3), label, fill=(180, 0, 25), font=ImageFont.load_default())
    preview_size = sheet_preview_size(context.width, context.height, 1400)
    if context.size != preview_size:
        context = context.resize(preview_size, Image.Resampling.LANCZOS)
    return context


def inspection_size(
    width: int,
    height: int,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    explicit_scale: float | None = None,
) -> tuple[tuple[int, int], float, str]:
    """Choose a readable inspection size without multiplying every crop by a fixed factor."""
    if width <= 0 or height <= 0:
        raise ValueError("inspection source dimensions must be positive")
    if explicit_scale is not None:
        scale = float(explicit_scale)
        mode = "explicit"
    else:
        scale = min(MAX_AUTOMATIC_SCALE, max_long_edge / max(width, height))
        mode = "adaptive-max-long-edge"
    return (
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        scale,
        mode,
    )


def coordinate_grid_step(display_scale: float, target_display_spacing: float = 96.0) -> int:
    """Choose a readable 1/2/5 native-pixel grid interval for an enlarged crop."""
    if display_scale <= 0:
        raise ValueError("display_scale must be positive")
    desired_native = target_display_spacing / display_scale
    magnitude = 10 ** math.floor(math.log10(max(desired_native, 1e-9)))
    candidates = [factor * magnitude for factor in (1, 2, 5, 10)]
    return max(1, int(round(min(candidates, key=lambda value: abs(value - desired_native)))))


def coordinate_grid(
    image: Image.Image,
    native_size: tuple[int, int],
    display_scale: float,
) -> tuple[Image.Image, int]:
    """Overlay crop-local native coordinates without changing the evidence frame."""
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    step = coordinate_grid_step(display_scale)
    minor_step = step // 2 if step >= 10 else 0
    if minor_step:
        for x in range(minor_step, native_size[0], minor_step):
            if x % step:
                display_x = int(round(x * display_scale))
                draw.line((display_x, 0, display_x, result.height), fill=(26, 115, 232, 42), width=1)
        for y in range(minor_step, native_size[1], minor_step):
            if y % step:
                display_y = int(round(y * display_scale))
                draw.line((0, display_y, result.width, display_y), fill=(26, 115, 232, 42), width=1)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(13, int(round(3.2 * display_scale))))
    except OSError:
        font = ImageFont.load_default()
    for x in range(0, native_size[0] + 1, step):
        display_x = min(result.width - 1, int(round(x * display_scale)))
        draw.line((display_x, 0, display_x, result.height), fill=(0, 83, 190, 105), width=1)
        draw.text((display_x + 3, 3), str(x), font=font, fill=(0, 50, 130, 230), stroke_width=2, stroke_fill=(255, 255, 255, 220))
    for y in range(0, native_size[1] + 1, step):
        display_y = min(result.height - 1, int(round(y * display_scale)))
        draw.line((0, display_y, result.width, display_y), fill=(0, 83, 190, 105), width=1)
        draw.text((3, display_y + 3), str(y), font=font, fill=(0, 50, 130, 230), stroke_width=2, stroke_fill=(255, 255, 255, 220))
    return Image.alpha_composite(result, overlay).convert("RGB"), step


def sheet_preview_size(
    width: int,
    height: int,
    max_long_edge: int = DEFAULT_SHEET_MAX_LONG_EDGE,
) -> tuple[int, int]:
    """Cap only the overview sheet; keep every individual inspection at full size."""
    scale = min(1.0, max_long_edge / max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--box", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--out", type=Path, required=True)
    size_group = parser.add_mutually_exclusive_group()
    size_group.add_argument(
        "--max-long-edge",
        type=int,
        default=DEFAULT_MAX_LONG_EDGE,
        help=f"adaptive inspection image cap in pixels (default: {DEFAULT_MAX_LONG_EDGE})",
    )
    size_group.add_argument(
        "--scale",
        type=float,
        help="explicit legacy enlargement multiplier; overrides adaptive sizing",
    )
    parser.add_argument("--threshold", type=int, default=155)
    parser.add_argument(
        "--edge-dark-threshold",
        type=int,
        default=190,
        help="maximum grayscale value counted as a crop-edge stroke (default: 190)",
    )
    parser.add_argument(
        "--edge-minimum-depth",
        type=int,
        default=6,
        help="minimum inward reach in native pixels for an edge continuation",
    )
    parser.add_argument(
        "--accept-edge-strokes",
        action="store_true",
        help="approve detected edge strokes only when --edge-review-note explains why",
    )
    parser.add_argument(
        "--edge-review-note",
        help="human review note for an intentional, complete edge-touching crop",
    )
    parser.add_argument(
        "--sheet-max-long-edge",
        type=int,
        default=DEFAULT_SHEET_MAX_LONG_EDGE,
        help=(
            "overview-sheet panel cap; individual inspection files retain full size "
            f"(default: {DEFAULT_SHEET_MAX_LONG_EDGE})"
        ),
    )
    args = parser.parse_args()

    if args.scale is not None and not 1 <= args.scale <= 10:
        parser.error("--scale must be between 1 and 10")
    if args.max_long_edge is not None and not 256 <= args.max_long_edge <= 10000:
        parser.error("--max-long-edge must be between 256 and 10000")
    if not 256 <= args.sheet_max_long_edge <= 4000:
        parser.error("--sheet-max-long-edge must be between 256 and 4000")
    if not 0 <= args.edge_dark_threshold <= 255:
        parser.error("--edge-dark-threshold must be between 0 and 255")
    if not 2 <= args.edge_minimum_depth <= 100:
        parser.error("--edge-minimum-depth must be between 2 and 100")
    if args.accept_edge_strokes and not str(args.edge_review_note or "").strip():
        parser.error("--accept-edge-strokes requires --edge-review-note")
    if args.edge_review_note and not args.accept_edge_strokes:
        parser.error("--edge-review-note requires --accept-edge-strokes")
    source = Image.open(args.image)
    box = tuple(args.box) if args.box else (0, 0, source.width, source.height)
    if box[0] < 0 or box[1] < 0 or box[2] > source.width or box[3] > source.height:
        parser.error("crop box extends outside the source image")
    if box[2] <= box[0] or box[3] <= box[1]:
        parser.error("crop box must have positive area")

    args.out.mkdir(parents=True, exist_ok=True)
    crop = source.crop(box).convert("RGB")
    native_path = args.out / "source-native.png"
    crop.save(native_path)
    parent_region = source.convert("RGB").crop(box)
    lineage_exact = pixel_sha256(parent_region) == pixel_sha256(crop)
    edge_report = edge_stroke_report(
        crop,
        dark_threshold=args.edge_dark_threshold,
        minimum_depth=args.edge_minimum_depth,
    )
    reviewed_exception = None
    if edge_report["detected"] and args.accept_edge_strokes:
        reviewed_exception = {
            "approved": True,
            "note": str(args.edge_review_note).strip(),
        }
    crop_complete = bool(not edge_report["detected"] or reviewed_exception)
    size, display_scale, scale_mode = inspection_size(
        crop.width,
        crop.height,
        max_long_edge=args.max_long_edge or DEFAULT_MAX_LONG_EDGE,
        explicit_scale=args.scale,
    )

    native_gray = crop.convert("L")
    gray_array = np.asarray(native_gray, dtype=np.uint8)
    contrast_native = ImageOps.autocontrast(native_gray, cutoff=2).filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=2)
    )
    clahe_array = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray_array)
    denoised_array = cv2.bilateralFilter(clahe_array, 5, 30, 30)
    blur_array = cv2.GaussianBlur(denoised_array, (0, 0), 1.0)
    edge_contrast_array = cv2.addWeighted(denoised_array, 1.55, blur_array, -0.55, 0)
    manual_threshold_array = np.where(gray_array > args.threshold, 255, 0).astype(np.uint8)
    adaptive_array = cv2.adaptiveThreshold(
        clahe_array,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )

    def enlarged(image: Image.Image) -> Image.Image:
        return image.resize(size, Image.Resampling.LANCZOS)

    enlarged_edge_contrast = enlarged(Image.fromarray(edge_contrast_array)).convert("RGB")
    grid_image, grid_step = coordinate_grid(
        enlarged_edge_contrast,
        (crop.width, crop.height),
        display_scale,
    )
    variants = {
        "crop": enlarged(crop),
        "gray": enlarged(native_gray).convert("RGB"),
        "contrast": enlarged(contrast_native).convert("RGB"),
        "edge-contrast": enlarged_edge_contrast,
        "threshold": enlarged(Image.fromarray(manual_threshold_array)).convert("RGB"),
        "adaptive-threshold": enlarged(Image.fromarray(adaptive_array)).convert("RGB"),
        "coordinate-grid": grid_image,
    }
    paths: dict[str, str] = {"source_native": str(native_path)}
    for name, image in variants.items():
        path = args.out / f"{name}.png"
        image.save(path)
        paths[name] = str(path)

    preview_size = sheet_preview_size(size[0], size[1], args.sheet_max_long_edge)
    previews = {
        name: image.resize(preview_size, Image.Resampling.LANCZOS)
        if image.size != preview_size else image
        for name, image in variants.items()
    }
    panel_width = max(image.width for image in previews.values())
    panel_height = max(image.height for image in previews.values())
    columns = 2
    rows = math.ceil(len(previews) / columns)
    sheet = Image.new("RGB", (panel_width * columns, (panel_height + 30) * rows), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(previews.items()):
        column, row = index % 2, index // 2
        x, y = column * panel_width, row * (panel_height + 30)
        sheet.paste(image, (x, y + 30))
        draw.text((x + 8, y + 7), name.upper(), fill="#28334a", font=ImageFont.load_default())
    sheet_path = args.out / "inspection-sheet.png"
    sheet.save(sheet_path)
    paths["sheet"] = str(sheet_path)

    context_path = args.out / "crop-context.png"
    crop_context_image(source, box).save(context_path)
    paths["crop_context"] = str(context_path)

    parent_path = args.image.resolve()
    crop_hash = sha256_file(native_path)
    crop_pixel_hash = pixel_sha256(crop)

    metadata = {
        "source": str(args.image.resolve()),
        "box": list(box),
        "source_size": [source.width, source.height],
        "crop_size": [crop.width, crop.height],
        "scale": display_scale,
        "scale_mode": scale_mode,
        "max_long_edge": args.max_long_edge if scale_mode == "adaptive-max-long-edge" else None,
        "display_size": list(size),
        "coordinate_frame": "crop-local native pixels",
        "coordinate_grid_step_native_px": grid_step,
        "sheet_panel_size": list(preview_size),
        "sheet_max_long_edge": args.sheet_max_long_edge,
        "threshold": args.threshold,
        "lineage": {
            "parent_source_path": str(parent_path),
            "parent_source_sha256": sha256_file(parent_path),
            "parent_source_size": [source.width, source.height],
            "crop_box": list(box),
            "crop_path": str(native_path.resolve()),
            "crop_sha256": crop_hash,
            "crop_pixel_sha256": crop_pixel_hash,
            "parent_region_pixel_sha256": pixel_sha256(parent_region),
            "exact_pixel_lineage": lineage_exact,
        },
        "crop_inspection": {
            "crop_complete": crop_complete,
            "edge_strokes": edge_report,
            "reviewed_exception": reviewed_exception,
        },
        "coordinate_transform": {
            "frame": "crop-local native pixels",
            "crop_origin_in_parent": [box[0], box[1]],
            "crop_to_parent": "parent_x = crop_x + crop_box[0]; parent_y = crop_y + crop_box[1]",
            "parent_to_crop": "crop_x = parent_x - crop_box[0]; crop_y = parent_y - crop_box[1]",
            "display_scale": display_scale,
        },
        "evidence_policy": (
            "All enhanced images are deterministic reading aids only; accept geometry "
            "only when supported in source-native.png and keep coordinates in its pixel frame."
        ),
        "outputs": paths,
    }
    (args.out / "inspection.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
