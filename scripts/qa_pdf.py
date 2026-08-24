#!/usr/bin/env python3
"""Render and numerically inspect every PDF page for clipping and pagination.

The report is content-addressed: it records the PDF hash, renderer build, render
settings, and both encoded-file and decoded-pixel hashes for every page. A
delivery validator can therefore prove that a reviewed page came from the
current PDF instead of trusting a manually checked boolean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageOps


REPORT_SCHEMA_VERSION = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_sha256(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    header = f"{rgb.width}x{rgb.height}:RGB\n".encode("ascii")
    return hashlib.sha256(header + rgb.tobytes()).hexdigest()


def _rounded_box(box: tuple[float, float, float, float] | None) -> list[float] | None:
    if box is None:
        return None
    return [round(float(value), 3) for value in box]


def _rect_values(rect: pymupdf.Rect) -> list[float]:
    return [round(float(value), 4) for value in (rect.x0, rect.y0, rect.x1, rect.y1)]


def _rect_close(left: pymupdf.Rect, right: pymupdf.Rect, tolerance: float = 0.01) -> bool:
    return all(
        abs(float(a) - float(b)) <= tolerance
        for a, b in zip(
            (left.x0, left.y0, left.x1, left.y1),
            (right.x0, right.y0, right.x1, right.y1),
        )
    )


def _page_geometry(page: pymupdf.Page) -> dict[str, Any]:
    """Describe the PDF boxes and the rendered page coordinate frame."""
    return {
        "rect_pt": _rect_values(page.rect),
        "mediabox_pt": _rect_values(page.mediabox),
        "cropbox_pt": _rect_values(page.cropbox),
        "rotation": int(page.rotation),
    }


def _unsupported_geometry_reasons(page: pymupdf.Page) -> list[str]:
    """Return reasons this page cannot safely mix vector and rendered coordinates.

    PyMuPDF reports extracted vector objects in the visible page frame, while PDF
    media/crop boxes and rotation are expressed in related but different frames.
    The current worksheet QA contract deliberately accepts only the canonical
    frame instead of guessing at a transform and risking a false margin pass.
    """
    reasons: list[str] = []
    if int(page.rotation) != 0:
        reasons.append(f"rotation is {int(page.rotation)} degrees (expected 0)")
    if not _rect_close(page.cropbox, page.mediabox):
        reasons.append("crop box differs from media box")
    media = page.mediabox
    if abs(float(media.x0)) > 0.01 or abs(float(media.y0)) > 0.01:
        reasons.append("media box origin is not (0, 0)")
    expected_rect = pymupdf.Rect(0, 0, media.width, media.height)
    if int(page.rotation) == 0 and _rect_close(page.cropbox, page.mediabox):
        if not _rect_close(page.rect, expected_rect):
            reasons.append("rendered page rectangle does not match the canonical media box")
    return reasons


def _ink_bounds(image: Image.Image, threshold: int) -> tuple[int, int, int, int] | None:
    """Return the half-open bounding box of visible non-white pixels."""
    gray = ImageOps.grayscale(image)
    mask = gray.point(lambda value: 255 if value < threshold else 0, mode="L")
    return mask.getbbox()


def _vector_bounds(
    page: pymupdf.Page,
) -> tuple[list[Any], list[Any], list[Any], list[float], list[Any]]:
    text_blocks = page.get_text("blocks")
    drawings = page.get_drawings()
    images = page.get_images(full=True)
    rectangles: list[pymupdf.Rect] = []
    content_drawings: list[Any] = []

    for block in text_blocks:
        rectangles.append(pymupdf.Rect(block[:4]))
    for drawing in drawings:
        rect = pymupdf.Rect(drawing["rect"])
        is_page_background = (
            rect.width >= page.rect.width * 0.95
            and rect.height >= page.rect.height * 0.95
        )
        tolerance = 0.75
        touches_horizontal_edge = rect.x0 <= tolerance or rect.x1 >= page.rect.width - tolerance
        touches_vertical_edge = rect.y0 <= tolerance or rect.y1 >= page.rect.height - tolerance
        is_filled_bleed_band = drawing.get("fill") is not None and (
            (touches_horizontal_edge and rect.width >= page.rect.width * 0.95)
            or (touches_vertical_edge and rect.height >= page.rect.height * 0.95)
        )
        if not is_page_background and not is_filled_bleed_band:
            content_drawings.append(drawing)
            rectangles.append(rect)
    for image in images:
        for rect in page.get_image_rects(image[0]):
            is_page_background = (
                rect.width >= page.rect.width * 0.95
                and rect.height >= page.rect.height * 0.95
            )
            if not is_page_background:
                rectangles.append(rect)

    if rectangles:
        x0 = min(float(rect.x0) for rect in rectangles)
        y0 = min(float(rect.y0) for rect in rectangles)
        x1 = max(float(rect.x1) for rect in rectangles)
        y1 = max(float(rect.y1) for rect in rectangles)
        bounds = [x0, y0, x1, y1]
    else:
        bounds = []
    return text_blocks, drawings, images, bounds, content_drawings


def _margin_overlay(
    image: Image.Image,
    ink_box: tuple[int, int, int, int] | None,
    minimum_margin_px: float,
    edge_checks: dict[str, bool],
) -> Image.Image:
    """Create a visual feedback artifact for page-edge QA."""
    overlay = image.convert("RGB").copy()
    draw = ImageDraw.Draw(overlay)
    inset = max(0, int(round(minimum_margin_px)))
    safe = (
        inset,
        inset,
        max(inset, overlay.width - inset - 1),
        max(inset, overlay.height - inset - 1),
    )
    safe_color = (34, 139, 94) if all(edge_checks.values()) else (215, 64, 64)
    draw.rectangle(safe, outline=safe_color, width=2)
    if ink_box is not None:
        x0, y0, x1, y1 = ink_box
        draw.rectangle(
            (x0, y0, max(x0, x1 - 1), max(y0, y1 - 1)),
            outline=(38, 112, 190),
            width=2,
        )
    return overlay


def inspect_pdf(
    pdf: Path,
    out: Path,
    *,
    dpi: int = 130,
    expect_vector: bool = False,
    min_margin_pt: float = 10.0,
    ink_threshold: int = 250,
) -> dict[str, Any]:
    """Render `pdf`, save page evidence under `out`, and return the QA report."""
    pdf = pdf.resolve()
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    pages: list[dict[str, Any]] = []

    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if min_margin_pt < 0:
        raise ValueError("min_margin_pt cannot be negative")
    if not 1 <= ink_threshold <= 255:
        raise ValueError("ink_threshold must be in 1..255")

    pdf_sha256 = _sha256(pdf)
    scale = dpi / 72.0
    minimum_margin_px = min_margin_pt * scale

    with pymupdf.open(pdf) as document:
        page_count = document.page_count
        if page_count < 1:
            errors.append("PDF contains no pages")

        for index, page in enumerate(document, start=1):
            page_geometry = _page_geometry(page)
            geometry_errors = _unsupported_geometry_reasons(page)
            geometry_supported = not geometry_errors
            if geometry_errors:
                errors.append(
                    f"page {index} uses unsupported PDF page geometry: "
                    + "; ".join(geometry_errors)
                    + ". Normalize rotation and page boxes before margin QA."
                )
            text_blocks, drawings, images, vector_bounds, content_drawings = _vector_bounds(page)
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            render_path = out / f"page-{index:02d}.png"
            image.save(render_path)

            raster_ink_box_px = _ink_bounds(image, ink_threshold)
            # Vector object geometry can distinguish intentional full-bleed header/background
            # fills from clipped text and figures. Image-only/scanned pages instead use the
            # visible raster-ink box so whitespace inside a page-sized image still counts.
            if geometry_supported and (text_blocks or content_drawings) and vector_bounds:
                authoritative_box_pt = tuple(vector_bounds)
                ink_box_px = tuple(
                    int(round(value * scale)) for value in authoritative_box_pt
                )
                ink_box_px = (
                    max(0, min(image.width, ink_box_px[0])),
                    max(0, min(image.height, ink_box_px[1])),
                    max(0, min(image.width, ink_box_px[2])),
                    max(0, min(image.height, ink_box_px[3])),
                )
                bounds_method = "vector-objects-with-bleed-backgrounds-excluded"
            else:
                ink_box_px = raster_ink_box_px
                authoritative_box_pt = None
                bounds_method = "rendered-visible-ink"

            if ink_box_px is None:
                margins_px = {
                    "left": float(image.width),
                    "top": float(image.height),
                    "right": float(image.width),
                    "bottom": float(image.height),
                }
                content_bounds_pt = None
            else:
                x0, y0, x1, y1 = ink_box_px
                margins_px = {
                    "left": float(x0),
                    "top": float(y0),
                    "right": float(image.width - x1),
                    "bottom": float(image.height - y1),
                }
                content_bounds_pt = authoritative_box_pt or (
                    x0 / scale, y0 / scale, x1 / scale, y1 / scale
                )
            margins_pt = {
                edge: round(value / scale, 3) for edge, value in margins_px.items()
            }
            edge_checks = {
                edge: value + 1e-9 >= minimum_margin_px
                for edge, value in margins_px.items()
            }

            if ink_box_px is None:
                errors.append(f"page {index} is empty")
            for edge, ok in edge_checks.items():
                if not ok:
                    errors.append(
                        f"page {index} content enters the {edge} margin "
                        f"({margins_pt[edge]:.3f} pt < {min_margin_pt:.3f} pt)"
                    )
            if expect_vector and images:
                errors.append(f"page {index} contains {len(images)} raster image object(s)")

            overlay_path = out / f"page-{index:02d}-margin-overlay.png"
            _margin_overlay(image, ink_box_px, minimum_margin_px, edge_checks).save(overlay_path)

            content_bottom = content_bounds_pt[3] if content_bounds_pt else 0.0
            fill_ratio = content_bottom / float(page.rect.height) if page.rect.height else 0.0
            pages.append(
                {
                    "page": index,
                    "geometry_supported": geometry_supported,
                    "geometry_errors": geometry_errors,
                    "page_geometry": page_geometry,
                    "width_pt": round(float(page.rect.width), 3),
                    "height_pt": round(float(page.rect.height), 3),
                    "render_width_px": image.width,
                    "render_height_px": image.height,
                    "content_bounds_pt": _rounded_box(content_bounds_pt),
                    "content_bounds_method": bounds_method,
                    "raster_ink_bounds_pt": _rounded_box(
                        tuple(value / scale for value in raster_ink_box_px)
                        if raster_ink_box_px
                        else None
                    ),
                    "vector_object_bounds_pt": [round(value, 3) for value in vector_bounds]
                    if vector_bounds
                    else None,
                    "margins_pt": margins_pt,
                    "edge_checks": edge_checks,
                    # Backward-compatible fields used by earlier worksheet scripts.
                    "content_bottom_pt": round(content_bottom, 3),
                    "fill_ratio": round(fill_ratio, 4),
                    "text_blocks": len(text_blocks),
                    "drawings": len(drawings),
                    "content_drawings": len(content_drawings),
                    "images": len(images),
                    "render": render_path.name,
                    "render_sha256": _sha256(render_path),
                    "pixel_sha256": _pixel_sha256(image),
                    "margin_overlay": overlay_path.name,
                    "margin_overlay_sha256": _sha256(overlay_path),
                }
            )

    renderer = {
        "name": "PyMuPDF",
        "pymupdf_version": str(getattr(pymupdf, "__version__", "unknown")),
        "mupdf_version": str(getattr(pymupdf, "mupdf_version", "unknown")),
        "colorspace": "RGB",
        "alpha": False,
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "pdf": str(pdf),
        "pdf_sha256": pdf_sha256,
        "renderer": renderer,
        "coordinate_frame_policy": "unrotated-media-box-equals-crop-box",
        "dpi": dpi,
        "page_count": page_count,
        "min_margin_pt": min_margin_pt,
        "ink_threshold": ink_threshold,
        "pages": pages,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=130)
    parser.add_argument("--expect-vector", action="store_true")
    parser.add_argument(
        "--min-margin-pt",
        type=float,
        default=10.0,
        help="required whitespace at each of the four page edges (default: 10 pt)",
    )
    parser.add_argument(
        "--ink-threshold",
        type=int,
        default=250,
        help="pixels darker than this value count as page content (default: 250)",
    )
    args = parser.parse_args()

    try:
        report = inspect_pdf(
            args.pdf,
            args.out,
            dpi=args.dpi,
            expect_vector=args.expect_vector,
            min_margin_pt=args.min_margin_pt,
            ink_threshold=args.ink_threshold,
        )
    except (OSError, ValueError, pymupdf.FileDataError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": False,
            "pdf": str(args.pdf),
            "errors": [str(exc)],
        }

    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / "qa-report.json"
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
