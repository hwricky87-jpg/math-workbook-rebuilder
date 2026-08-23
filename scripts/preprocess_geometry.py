#!/usr/bin/env python3
"""Create deterministic, source-preserving geometry inspection variants."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


DEFAULT_MAX_LONG_EDGE = 2200
DEFAULT_SHEET_MAX_LONG_EDGE = 1000
MAX_AUTOMATIC_SCALE = 6.0


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
