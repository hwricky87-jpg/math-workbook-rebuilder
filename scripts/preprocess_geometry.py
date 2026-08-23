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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--box", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=7)
    parser.add_argument("--threshold", type=int, default=155)
    args = parser.parse_args()

    if not 1 <= args.scale <= 10:
        parser.error("--scale must be between 1 and 10")
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
    size = (crop.width * args.scale, crop.height * args.scale)

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

    variants = {
        "crop": enlarged(crop),
        "gray": enlarged(native_gray).convert("RGB"),
        "contrast": enlarged(contrast_native).convert("RGB"),
        "edge-contrast": enlarged(Image.fromarray(edge_contrast_array)).convert("RGB"),
        "threshold": enlarged(Image.fromarray(manual_threshold_array)).convert("RGB"),
        "adaptive-threshold": enlarged(Image.fromarray(adaptive_array)).convert("RGB"),
    }
    paths: dict[str, str] = {"source_native": str(native_path)}
    for name, image in variants.items():
        path = args.out / f"{name}.png"
        image.save(path)
        paths[name] = str(path)

    panel_width = max(image.width for image in variants.values())
    panel_height = max(image.height for image in variants.values())
    columns = 2
    rows = math.ceil(len(variants) / columns)
    sheet = Image.new("RGB", (panel_width * columns, (panel_height + 30) * rows), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(variants.items()):
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
        "scale": args.scale,
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
