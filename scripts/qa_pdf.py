#!/usr/bin/env python3
"""Render and numerically inspect every PDF page for clipping and pagination."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pymupdf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=130)
    parser.add_argument("--expect-vector", action="store_true")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(args.pdf)
    pages = []
    errors: list[str] = []
    for index, page in enumerate(document):
        text_blocks = page.get_text("blocks")
        drawings = page.get_drawings()
        images = page.get_images(full=True)
        bottoms = [float(block[3]) for block in text_blocks]
        content_drawings = []
        for drawing in drawings:
            rect = drawing["rect"]
            is_page_background = (
                rect.width >= page.rect.width * 0.95 and rect.height >= page.rect.height * 0.95
            )
            if not is_page_background:
                content_drawings.append(drawing)
                bottoms.append(float(rect.y1))
        for image in images:
            xref = image[0]
            bottoms.extend(float(rect.y1) for rect in page.get_image_rects(xref))
        content_bottom = max(bottoms) if bottoms else 0.0
        fill_ratio = content_bottom / float(page.rect.height) if page.rect.height else 0.0
        if not text_blocks and not drawings and not images:
            errors.append(f"page {index + 1} is empty")
        if content_bottom > page.rect.height - 10:
            errors.append(f"page {index + 1} content touches the bottom edge")
        if args.expect_vector and images:
            errors.append(f"page {index + 1} contains {len(images)} raster image object(s)")
        pixmap = page.get_pixmap(dpi=args.dpi, alpha=False)
        render_path = args.out / f"page-{index + 1:02d}.png"
        pixmap.save(render_path)
        pages.append(
            {
                "page": index + 1,
                "width_pt": page.rect.width,
                "height_pt": page.rect.height,
                "content_bottom_pt": content_bottom,
                "fill_ratio": round(fill_ratio, 4),
                "text_blocks": len(text_blocks),
                "drawings": len(drawings),
                "content_drawings": len(content_drawings),
                "images": len(images),
                "render": str(render_path),
            }
        )
    document.close()
    report = {"ok": not errors, "pdf": str(args.pdf), "pages": pages, "errors": errors}
    report_path = args.out / "qa-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
