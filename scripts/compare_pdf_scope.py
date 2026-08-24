#!/usr/bin/env python3
"""Prove that PDF revisions are confined to allowed pages or problem/figure ROIs.

The two PDFs are rasterized by the same PyMuPDF build and DPI.  Pixel changes
outside the declared scope fail the gate, while per-page overlays make the
changed bounding box and ROI boundary easy to inspect during worksheet repair.

Scope JSON example::

    {
      "allowed_pages": [3, 5],
      "coordinate_space": "points",
      "pages": {
        "3": {"rois": [[72, 120, 510, 420]]},
        "5": {"mask": "page-05-mask.png"}
      }
    }

ROI boxes may use ``points``, ``pixels``, or ``normalized`` coordinates. Mask
images must match the canonical rendered page dimensions; nonzero pixels are
allowed. Pages named in ``pages`` are automatically allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter


REPORT_SCHEMA_VERSION = 1


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


def _parse_pages(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, int) and not isinstance(value, bool):
        return {value}
    if isinstance(value, list):
        pages: set[int] = set()
        for item in value:
            pages.update(_parse_pages(item))
        return pages
    text = str(value).strip()
    if not text:
        return set()
    pages = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start, stop = int(left), int(right)
            if stop < start:
                raise ValueError(f"invalid descending page range: {token}")
            pages.update(range(start, stop + 1))
        else:
            pages.add(int(token))
    return pages


def _bbox(mask: np.ndarray) -> list[int] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def _bbox_points(box: list[int] | None, dpi: int) -> list[float] | None:
    if box is None:
        return None
    scale = 72.0 / dpi
    return [round(value * scale, 3) for value in box]


def _page_geometry(page: pymupdf.Page) -> dict[str, Any]:
    def rect_values(rect: pymupdf.Rect) -> list[float]:
        return [round(float(value), 4) for value in (rect.x0, rect.y0, rect.x1, rect.y1)]

    return {
        "rect_pt": rect_values(page.rect),
        "mediabox_pt": rect_values(page.mediabox),
        "cropbox_pt": rect_values(page.cropbox),
        "rotation": int(page.rotation),
    }


def _rect_close(left: pymupdf.Rect, right: pymupdf.Rect, tolerance: float = 0.01) -> bool:
    return all(
        abs(float(a) - float(b)) <= tolerance
        for a, b in zip(
            (left.x0, left.y0, left.x1, left.y1),
            (right.x0, right.y0, right.x1, right.y1),
        )
    )


def _unsupported_geometry_reasons(page: pymupdf.Page) -> list[str]:
    """Reject PDF page frames whose point-to-render transform is ambiguous here."""
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


def _render(page: pymupdf.Page, dpi: int) -> Image.Image:
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _box_pixels(
    box: Any,
    coordinate_space: str,
    width: int,
    height: int,
    dpi: int,
) -> tuple[int, int, int, int]:
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError(f"ROI must be [x0,y0,x1,y1], got {box!r}")
    values = [float(value) for value in box]
    if coordinate_space == "points":
        scale_x = scale_y = dpi / 72.0
    elif coordinate_space == "pixels":
        scale_x = scale_y = 1.0
    elif coordinate_space == "normalized":
        scale_x, scale_y = width, height
    else:
        raise ValueError(
            f"unsupported coordinate_space={coordinate_space!r}; use points, pixels, or normalized"
        )
    x0 = int(np.floor(values[0] * scale_x))
    y0 = int(np.floor(values[1] * scale_y))
    x1 = int(np.ceil(values[2] * scale_x))
    y1 = int(np.ceil(values[3] * scale_y))
    x0, x1 = sorted((max(0, min(width, x0)), max(0, min(width, x1))))
    y0, y1 = sorted((max(0, min(height, y0)), max(0, min(height, y1))))
    if x0 == x1 or y0 == y1:
        raise ValueError(f"ROI becomes empty after clipping: {box!r}")
    return x0, y0, x1, y1


def _page_scope_entry(scope: dict[str, Any], page_number: int) -> Any:
    pages = scope.get("pages", {})
    if not isinstance(pages, dict):
        raise ValueError("scope JSON pages must be an object keyed by 1-based page number")
    return pages.get(str(page_number), pages.get(page_number))


def _scope_mask(
    scope: dict[str, Any],
    scope_path: Path | None,
    page_number: int,
    width: int,
    height: int,
    dpi: int,
    allowed: bool,
    padding_px: int,
) -> tuple[np.ndarray, list[list[int]], str | None]:
    if not allowed:
        return np.zeros((height, width), dtype=bool), [], None

    entry = _page_scope_entry(scope, page_number)
    if entry is None:
        return np.ones((height, width), dtype=bool), [[0, 0, width, height]], None
    if isinstance(entry, list):
        entry = {"rois": entry}
    if not isinstance(entry, dict):
        raise ValueError(f"scope page {page_number} must be an object or ROI list")

    coordinate_space = str(
        entry.get("coordinate_space", scope.get("coordinate_space", "points"))
    ).lower()
    rois = entry.get("rois", entry.get("boxes", []))
    if rois is None:
        rois = []
    if not isinstance(rois, list):
        raise ValueError(f"scope page {page_number}.rois must be a list")

    mask_image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask_image)
    boxes_px: list[list[int]] = []
    for box in rois:
        x0, y0, x1, y1 = _box_pixels(box, coordinate_space, width, height, dpi)
        boxes_px.append([x0, y0, x1, y1])
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)

    mask_path_text = entry.get("mask")
    resolved_mask: str | None = None
    if mask_path_text:
        mask_path = Path(str(mask_path_text))
        if not mask_path.is_absolute():
            mask_path = (scope_path.parent if scope_path else Path.cwd()) / mask_path
        if not mask_path.is_file():
            raise ValueError(f"scope page {page_number} mask does not exist: {mask_path}")
        with Image.open(mask_path) as opened:
            external = opened.convert("L")
            if external.size != (width, height):
                raise ValueError(
                    f"scope page {page_number} mask size {external.size} does not match "
                    f"render {(width, height)} at {dpi} DPI"
                )
            external = external.point(lambda value: 255 if value else 0, mode="L")
            mask_image = Image.fromarray(
                np.maximum(np.asarray(mask_image), np.asarray(external)).astype(np.uint8),
                mode="L",
            )
        resolved_mask = str(mask_path.resolve())

    if not rois and not mask_path_text:
        raise ValueError(
            f"scope page {page_number} entry declares neither rois/boxes nor a mask"
        )
    if padding_px:
        mask_image = mask_image.filter(ImageFilter.MaxFilter(padding_px * 2 + 1))
    return np.asarray(mask_image) > 0, boxes_px, resolved_mask


def _save_diff_artifacts(
    out: Path,
    page_number: int,
    baseline: Image.Image,
    revised: Image.Image,
    changed: np.ndarray,
    allowed_mask: np.ndarray,
    changed_box: list[int] | None,
    roi_boxes: list[list[int]],
) -> dict[str, str]:
    """Save renders plus a full-page change overlay and a binary diff mask."""
    baseline_path = out / f"page-{page_number:02d}-baseline.png"
    revised_path = out / f"page-{page_number:02d}-revised.png"
    overlay_path = out / f"page-{page_number:02d}-diff-overlay.png"
    mask_path = out / f"page-{page_number:02d}-diff-mask.png"
    baseline.save(baseline_path)
    revised.save(revised_path)

    overlay = revised.convert("RGB").copy()
    overlay_array = np.asarray(overlay).copy()
    inside = changed & allowed_mask
    outside = changed & ~allowed_mask
    # Red = permitted target change; magenta = out-of-scope violation.
    overlay_array[inside] = (
        overlay_array[inside].astype(np.uint16) // 3
        + np.array([170, 25, 25], dtype=np.uint16)
    ).clip(0, 255).astype(np.uint8)
    overlay_array[outside] = np.array([255, 0, 210], dtype=np.uint8)
    overlay = Image.fromarray(overlay_array, mode="RGB")
    draw = ImageDraw.Draw(overlay)
    for x0, y0, x1, y1 in roi_boxes:
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(30, 180, 90), width=3)
    if changed_box:
        x0, y0, x1, y1 = changed_box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(255, 190, 20), width=3)
    overlay.save(overlay_path)
    Image.fromarray((changed * 255).astype(np.uint8), mode="L").save(mask_path)

    return {
        "baseline_render": baseline_path.name,
        "baseline_render_sha256": _sha256(baseline_path),
        "revised_render": revised_path.name,
        "revised_render_sha256": _sha256(revised_path),
        "diff_overlay": overlay_path.name,
        "diff_overlay_sha256": _sha256(overlay_path),
        "diff_mask": mask_path.name,
        "diff_mask_sha256": _sha256(mask_path),
    }


def compare_pdf_scope(
    baseline_pdf: Path,
    revised_pdf: Path,
    out: Path,
    *,
    allowed_pages: set[int],
    scope: dict[str, Any] | None = None,
    scope_path: Path | None = None,
    dpi: int = 150,
    pixel_threshold: int = 8,
    roi_padding_px: int = 2,
    require_each_target_change: bool = False,
    allow_empty_target: bool = False,
) -> dict[str, Any]:
    baseline_pdf = baseline_pdf.resolve()
    revised_pdf = revised_pdf.resolve()
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    scope = scope or {}
    errors: list[str] = []
    pages_report: list[dict[str, Any]] = []

    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if not 0 <= pixel_threshold <= 255:
        raise ValueError("pixel_threshold must be in 0..255")
    if roi_padding_px < 0:
        raise ValueError("roi_padding_px cannot be negative")

    scope_pages = scope.get("pages", {})
    if scope_pages and not isinstance(scope_pages, dict):
        raise ValueError("scope JSON pages must be an object")
    allowed_pages = set(allowed_pages)
    allowed_pages.update(_parse_pages(scope.get("allowed_pages")))
    if isinstance(scope_pages, dict):
        allowed_pages.update(int(page) for page in scope_pages)
    if not allowed_pages:
        raise ValueError("at least one allowed page or ROI must be declared")
    if any(page < 1 for page in allowed_pages):
        raise ValueError("allowed page numbers are 1-based and must be positive")

    renderer = {
        "name": "PyMuPDF",
        "pymupdf_version": str(getattr(pymupdf, "__version__", "unknown")),
        "mupdf_version": str(getattr(pymupdf, "mupdf_version", "unknown")),
        "colorspace": "RGB",
        "alpha": False,
    }
    total_changed = 0
    total_inside = 0
    total_outside = 0
    total_pixels = 0
    baseline_count = 0
    revised_count = 0

    with pymupdf.open(baseline_pdf) as baseline_doc, pymupdf.open(revised_pdf) as revised_doc:
        baseline_count = baseline_doc.page_count
        revised_count = revised_doc.page_count
        if baseline_count != revised_count:
            errors.append(
                f"page count changed from {baseline_count} to {revised_count}; scope comparison "
                "requires one-to-one page geometry"
            )
        common_count = min(baseline_count, revised_count)
        invalid_allowed = sorted(page for page in allowed_pages if page > common_count)
        if invalid_allowed:
            errors.append(f"allowed page(s) outside the comparable range: {invalid_allowed}")

        for page_number in range(1, common_count + 1):
            baseline_page = baseline_doc[page_number - 1]
            revised_page = revised_doc[page_number - 1]
            baseline_geometry = _page_geometry(baseline_page)
            revised_geometry = _page_geometry(revised_page)
            baseline_geometry_errors = _unsupported_geometry_reasons(baseline_page)
            revised_geometry_errors = _unsupported_geometry_reasons(revised_page)
            geometry_supported = not baseline_geometry_errors and not revised_geometry_errors
            if baseline_geometry_errors:
                errors.append(
                    f"page {page_number} baseline uses unsupported PDF page geometry: "
                    + "; ".join(baseline_geometry_errors)
                    + ". Normalize rotation and page boxes before revision-scope comparison."
                )
            if revised_geometry_errors:
                errors.append(
                    f"page {page_number} revised PDF uses unsupported PDF page geometry: "
                    + "; ".join(revised_geometry_errors)
                    + ". Normalize rotation and page boxes before revision-scope comparison."
                )
            geometry_equal = baseline_geometry == revised_geometry
            if not geometry_equal:
                errors.append(f"page {page_number} geometry changed")

            baseline_image = _render(baseline_page, dpi)
            revised_image = _render(revised_page, dpi)
            if baseline_image.size != revised_image.size:
                errors.append(
                    f"page {page_number} render size changed from {baseline_image.size} "
                    f"to {revised_image.size}"
                )
                pages_report.append(
                    {
                        "page": page_number,
                        "allowed": page_number in allowed_pages,
                        "geometry_equal": geometry_equal,
                        "baseline_geometry": baseline_geometry,
                        "revised_geometry": revised_geometry,
                        "error": "render size mismatch",
                    }
                )
                continue

            base_array = np.asarray(baseline_image, dtype=np.int16)
            revised_array = np.asarray(revised_image, dtype=np.int16)
            delta = np.max(np.abs(base_array - revised_array), axis=2)
            changed = delta > pixel_threshold
            changed_count = int(changed.sum())
            page_pixels = int(changed.size)
            total_pixels += page_pixels
            changed_box = _bbox(changed)
            allowed = page_number in allowed_pages

            try:
                if not geometry_supported:
                    raise ValueError(
                        f"page {page_number} ROI/full-page scope was not applied because "
                        "its PDF page geometry is unsupported"
                    )
                allowed_mask, roi_boxes, roi_mask_path = _scope_mask(
                    scope,
                    scope_path,
                    page_number,
                    baseline_image.width,
                    baseline_image.height,
                    dpi,
                    allowed,
                    roi_padding_px,
                )
            except ValueError as exc:
                errors.append(str(exc))
                allowed_mask = np.zeros(changed.shape, dtype=bool)
                roi_boxes = []
                roi_mask_path = None

            inside = changed & allowed_mask
            outside = changed & ~allowed_mask
            inside_count = int(inside.sum())
            outside_count = int(outside.sum())
            total_changed += changed_count
            total_inside += inside_count
            total_outside += outside_count
            if outside_count:
                errors.append(
                    f"page {page_number} has {outside_count} changed pixel(s) outside allowed scope"
                )
            if require_each_target_change and allowed and inside_count == 0:
                errors.append(f"allowed target page {page_number} has no in-scope change")

            artifacts: dict[str, str] = {}
            if allowed or changed_count:
                artifacts = _save_diff_artifacts(
                    out,
                    page_number,
                    baseline_image,
                    revised_image,
                    changed,
                    allowed_mask,
                    changed_box,
                    roi_boxes,
                )
            pages_report.append(
                {
                    "page": page_number,
                    "allowed": allowed,
                    "geometry_supported": geometry_supported,
                    "baseline_geometry_errors": baseline_geometry_errors,
                    "revised_geometry_errors": revised_geometry_errors,
                    "scope": "roi" if allowed and _page_scope_entry(scope, page_number) is not None else (
                        "full-page" if allowed else "none"
                    ),
                    "geometry_equal": geometry_equal,
                    "baseline_geometry": baseline_geometry,
                    "revised_geometry": revised_geometry,
                    "width_px": baseline_image.width,
                    "height_px": baseline_image.height,
                    "baseline_pixel_sha256": _pixel_sha256(baseline_image),
                    "revised_pixel_sha256": _pixel_sha256(revised_image),
                    "changed_pixels": changed_count,
                    "changed_ratio": round(changed_count / page_pixels, 9),
                    "changed_bbox_px": changed_box,
                    "changed_bbox_pt": _bbox_points(changed_box, dpi),
                    "in_scope_changed_pixels": inside_count,
                    "in_scope_changed_ratio": round(inside_count / page_pixels, 9),
                    "in_scope_changed_bbox_px": _bbox(inside),
                    "outside_scope_changed_pixels": outside_count,
                    "outside_scope_changed_ratio": round(outside_count / page_pixels, 9),
                    "outside_scope_changed_bbox_px": _bbox(outside),
                    "roi_boxes_px": roi_boxes,
                    "roi_mask": roi_mask_path,
                    "artifacts": artifacts,
                }
            )

        for page_number in range(common_count + 1, max(baseline_count, revised_count) + 1):
            pages_report.append(
                {
                    "page": page_number,
                    "allowed": page_number in allowed_pages,
                    "geometry_equal": False,
                    "error": "page exists in only one PDF",
                }
            )

    if total_inside == 0 and not allow_empty_target:
        errors.append("allowed target scope contains no changed pixels")

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "baseline_pdf": str(baseline_pdf),
        "baseline_pdf_sha256": _sha256(baseline_pdf),
        "revised_pdf": str(revised_pdf),
        "revised_pdf_sha256": _sha256(revised_pdf),
        "renderer": renderer,
        "coordinate_frame_policy": "unrotated-media-box-equals-crop-box",
        "dpi": dpi,
        "pixel_threshold": pixel_threshold,
        "roi_padding_px": roi_padding_px,
        "allowed_pages": sorted(allowed_pages),
        "baseline_page_count": baseline_count,
        "revised_page_count": revised_count,
        "page_count": len(pages_report),
        "compared_pixels": total_pixels,
        "changed_pixels": total_changed,
        "changed_ratio": round(total_changed / total_pixels, 9) if total_pixels else 0.0,
        "in_scope_changed_pixels": total_inside,
        "in_scope_changed_ratio": round(total_inside / total_pixels, 9)
        if total_pixels
        else 0.0,
        "outside_scope_changed_pixels": total_outside,
        "outside_scope_changed_ratio": round(total_outside / total_pixels, 9)
        if total_pixels
        else 0.0,
        "target_change_nonempty": total_inside > 0,
        "pages": pages_report,
    }
    report_path = out / "scope-comparison.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    report["report_sha256"] = _sha256(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_pdf", type=Path)
    parser.add_argument("revised_pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--allowed-pages",
        help="comma-separated 1-based pages and ranges, for example 3,5-7",
    )
    parser.add_argument(
        "--scope-json",
        type=Path,
        help="optional per-page ROI boxes/masks; paths inside it are relative to this file",
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument(
        "--pixel-threshold",
        type=int,
        default=8,
        help="ignore per-channel render differences at or below this value (default: 8)",
    )
    parser.add_argument(
        "--roi-padding-px",
        type=int,
        default=2,
        help="expand ROI masks for antialiasing spill (default: 2 px)",
    )
    parser.add_argument("--require-each-target-change", action="store_true")
    parser.add_argument(
        "--allow-empty-target",
        action="store_true",
        help="permit identical PDFs/targets (default is fail closed)",
    )
    args = parser.parse_args()

    try:
        scope: dict[str, Any] = {}
        scope_path: Path | None = None
        if args.scope_json:
            scope_path = args.scope_json.resolve()
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
            if not isinstance(scope, dict):
                raise ValueError("scope JSON root must be an object")
        report = compare_pdf_scope(
            args.baseline_pdf,
            args.revised_pdf,
            args.out,
            allowed_pages=_parse_pages(args.allowed_pages),
            scope=scope,
            scope_path=scope_path,
            dpi=args.dpi,
            pixel_threshold=args.pixel_threshold,
            roi_padding_px=args.roi_padding_px,
            require_each_target_change=args.require_each_target_change,
            allow_empty_target=args.allow_empty_target,
        )
    except (OSError, ValueError, json.JSONDecodeError, pymupdf.FileDataError) as exc:
        args.out.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": False,
            "errors": [str(exc)],
            "baseline_pdf": str(args.baseline_pdf),
            "revised_pdf": str(args.revised_pdf),
        }
        (args.out / "scope-comparison.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
