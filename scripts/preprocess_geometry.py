#!/usr/bin/env python3
"""Create crop, grayscale, contrast, threshold, and inspection-sheet variants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    size = (crop.width * args.scale, crop.height * args.scale)
    enlarged = crop.resize(size, Image.Resampling.LANCZOS)
    gray = enlarged.convert("L")
    contrast = ImageOps.autocontrast(gray, cutoff=2).filter(
        ImageFilter.UnsharpMask(radius=4, percent=90, threshold=2)
    )
    threshold = gray.point(lambda value: 255 if value > args.threshold else 0)

    variants = {
        "crop": enlarged,
        "gray": gray.convert("RGB"),
        "contrast": contrast.convert("RGB"),
        "threshold": threshold.convert("RGB"),
    }
    paths: dict[str, str] = {}
    for name, image in variants.items():
        path = args.out / f"{name}.png"
        image.save(path)
        paths[name] = str(path)

    panel_width = max(image.width for image in variants.values())
    panel_height = max(image.height for image in variants.values())
    sheet = Image.new("RGB", (panel_width * 2, (panel_height + 30) * 2), "white")
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
        "outputs": paths,
    }
    (args.out / "inspection.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
