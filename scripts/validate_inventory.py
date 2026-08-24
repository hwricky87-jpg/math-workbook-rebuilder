#!/usr/bin/env python3
"""Validate a source-first workbook inventory and its downstream item coverage.

The inventory is the workbook-level contract that prevents silently omitting a
concept exploration, example, exercise, figure, or source page.  This script
intentionally fails closed.  PDF sources are inspected with PyMuPDF: their
declared page count must match the current file and each ``problem_box`` is
interpreted in visible-page PDF points (top-left origin, after applying the
page CropBox and rotation) and must fit inside that page.  For a non-PDF
source, page count remains declaration-based and box geometry is syntax-checked
only; the report identifies that compatibility mode explicitly.  Its JSON
schema is small:

.. code-block:: json

  {
    "schema_version": "1.0",
    "source": {"file": "source.pdf", "sha256": "...", "page_count": 4},
    "items": [{
      "id": "c1", "order": 1, "type": "concept_exploration",
      "source_page": 1, "problem_box": [10, 20, 300, 500],
      "has_text": true, "has_figure": true
    }],
    "page_accounts": [{"page": 4, "reason": "blank back page"}]
  }

Every source page must contain at least one item or have a non-empty reason in
``page_accounts``.  Optional content and delivery JSON files must contain the
same item IDs.  Figure-bearing inventory IDs must exactly equal the IDs found
in the supplied geometry manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

import pymupdf


SCHEMA_VERSION = "1.0"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ID_RE = re.compile(r"^[^\s]+$")
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
PDF_HEADER = b"%PDF-"
PDF_BOX_TOLERANCE_PT = 1e-6


def _looks_like_pdf(path: Path) -> bool:
    """Return whether *path* should be treated as a PDF, including renamed PDFs."""
    if path.suffix.casefold() == ".pdf":
        return True
    try:
        with path.open("rb") as stream:
            return stream.read(len(PDF_HEADER)) == PDF_HEADER
    except OSError:
        return False


def _rect_values(rect: pymupdf.Rect) -> list[float]:
    """Return a JSON-friendly rectangle without retaining a document object."""
    return [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]


def _pdf_page_geometries(
    path: Path,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Read current visible-page geometry from a PDF.

    Inventory boxes use the same coordinate space as a rendered PDF page:
    visible CropBox coordinates, normalized to a top-left ``(0, 0)`` origin,
    with the declared page rotation applied.  ``Page.rect`` is PyMuPDF's
    canonical rectangle for precisely that space.  MediaBox and CropBox data
    are retained in the report as audit evidence, but are not mixed with the
    visible-page coordinates used to validate ``problem_box``.
    """
    geometries: list[dict[str, Any]] = []
    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf:
                errors.append(
                    _error(
                        "SOURCE_PDF_INVALID",
                        "$.source.file",
                        "source is declared or detected as PDF but PyMuPDF did not identify it as PDF",
                    )
                )
                return None
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                rect = page.rect
                values = _rect_values(rect)
                if (
                    not all(math.isfinite(value) for value in values)
                    or rect.width <= 0
                    or rect.height <= 0
                ):
                    errors.append(
                        _error(
                            "SOURCE_PDF_PAGE_GEOMETRY_INVALID",
                            f"$.source.pages[{page_index}]",
                            "PDF page has non-finite or non-positive visible geometry",
                            rect=values,
                        )
                    )
                    return None
                rotation = int(page.rotation)
                if rotation not in (0, 90, 180, 270):
                    errors.append(
                        _error(
                            "SOURCE_PDF_PAGE_ROTATION_UNSUPPORTED",
                            f"$.source.pages[{page_index}]",
                            "PDF page rotation must be 0, 90, 180, or 270 degrees",
                            rotation=rotation,
                        )
                    )
                    return None
                geometries.append(
                    {
                        "page": page_index + 1,
                        "visible_rect_pt": [0.0, 0.0, float(rect.width), float(rect.height)],
                        "page_rect_pt": values,
                        "mediabox_pt": _rect_values(page.mediabox),
                        "cropbox_pt": _rect_values(page.cropbox),
                        "rotation": rotation,
                    }
                )
    except (OSError, ValueError, pymupdf.FileDataError) as exc:
        errors.append(
            _error(
                "SOURCE_PDF_OPEN_ERROR",
                "$.source.file",
                f"cannot inspect PDF source with PyMuPDF: {exc}",
            )
        )
        return None
    return geometries


def _sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest for *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(base: Path, value: Any) -> Path | None:
    """Resolve a non-empty string path relative to *base*."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _error(code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    """Create a stable machine-readable validation error."""
    result: dict[str, Any] = {"code": code, "path": path, "message": message}
    if details:
        result["details"] = details
    return result


def _sorted_errors(errors: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return errors in a deterministic order independent of set iteration."""
    return sorted(
        errors,
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("path", "")),
            str(item.get("message", "")),
            json.dumps(item.get("details", {}), sort_keys=True, ensure_ascii=False),
        ),
    )


def _read_json(path: Path, label: str, errors: list[dict[str, Any]]) -> Any:
    """Read JSON without leaking a traceback into a validation workflow."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(_error("FILE_NOT_FOUND", label, f"file does not exist: {path}"))
    except OSError as exc:
        errors.append(_error("FILE_READ_ERROR", label, f"cannot read {path}: {exc}"))
    except json.JSONDecodeError as exc:
        errors.append(
            _error(
                "JSON_INVALID",
                label,
                f"invalid JSON at line {exc.lineno}, column {exc.colno}",
            )
        )
    return None


def _extract_item_ids(
    data: Any,
    label: str,
    errors: list[dict[str, Any]],
    preferred_keys: tuple[str, ...],
) -> list[str]:
    """Extract IDs from a downstream content, delivery, or index document."""
    raw_items: Any = data
    if isinstance(data, dict):
        raw_items = None
        for key in preferred_keys:
            if key in data:
                raw_items = data[key]
                break
        if raw_items is None and isinstance(data.get("id"), str):
            raw_items = [data]

    if not isinstance(raw_items, list):
        errors.append(
            _error(
                "ITEM_LIST_MISSING",
                label,
                f"expected one of {list(preferred_keys)} to be a list",
            )
        )
        return []

    ids: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items):
        value = item.get("id") if isinstance(item, dict) else item
        item_id = value.strip() if isinstance(value, str) else ""
        item_path = f"{label}[{index}].id"
        if not item_id:
            errors.append(_error("ITEM_ID_MISSING", item_path, "item ID is required"))
        elif item_id in seen:
            errors.append(_error("ITEM_ID_DUPLICATE", item_path, f"duplicate item ID: {item_id}"))
        else:
            seen.add(item_id)
            ids.append(item_id)
    return ids


def _compare_sets(
    expected: set[str],
    actual: set[str],
    label: str,
    errors: list[dict[str, Any]],
    code: str,
) -> None:
    """Record a single deterministic exact-set mismatch."""
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        errors.append(
            _error(
                code,
                label,
                "item ID sets are not equal",
                missing=missing,
                extra=extra,
            )
        )


def _manifest_paths(values: list[Path]) -> list[Path]:
    """Expand manifest files and directories into a stable file list."""
    expanded: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved.is_dir():
            expanded.extend(path.resolve() for path in sorted(resolved.glob("*.json")))
        else:
            expanded.append(resolved)
    # Do not silently validate the same file twice through a file and directory.
    return sorted(set(expanded), key=lambda path: str(path).casefold())


def validate_inventory(
    inventory: dict[str, Any],
    base: Path,
    *,
    inventory_path: Path | None = None,
    content_path: Path | None = None,
    delivery_path: Path | None = None,
    manifest_paths: Iterable[Path] = (),
    require_manifest_coverage: bool = True,
) -> dict[str, Any]:
    """Validate inventory schema, source identity, and downstream coverage."""
    errors: list[dict[str, Any]] = []
    if not isinstance(inventory, dict):
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "errors": [_error("INVENTORY_ROOT_INVALID", "$", "inventory root must be an object")],
        }

    if inventory.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            _error(
                "SCHEMA_VERSION_UNSUPPORTED",
                "$.schema_version",
                f"schema_version must be {SCHEMA_VERSION}",
            )
        )

    source = inventory.get("source")
    source_path: Path | None = None
    page_count = 0
    declared_page_count = 0
    page_count_mode = "unavailable"
    page_geometry_mode = "unavailable"
    page_geometries: list[dict[str, Any]] = []
    actual_source_sha: str | None = None
    if not isinstance(source, dict):
        errors.append(_error("SOURCE_INVALID", "$.source", "source must be an object"))
        source = {}

    source_path = _resolve(base, source.get("file"))
    if source_path is None:
        errors.append(_error("SOURCE_FILE_MISSING", "$.source.file", "source.file is required"))
    elif not source_path.is_file():
        errors.append(
            _error("SOURCE_FILE_NOT_FOUND", "$.source.file", f"source file does not exist: {source_path}")
        )
    else:
        actual_source_sha = _sha256(source_path)

    declared_sha = source.get("sha256")
    if not isinstance(declared_sha, str) or not SHA256_RE.fullmatch(declared_sha):
        errors.append(
            _error("SOURCE_SHA256_INVALID", "$.source.sha256", "source.sha256 must be 64 hexadecimal characters")
        )
    elif actual_source_sha is not None and declared_sha.lower() != actual_source_sha:
        errors.append(
            _error(
                "SOURCE_SHA256_STALE",
                "$.source.sha256",
                "declared source hash does not match the current source file",
                declared=declared_sha.lower(),
                actual=actual_source_sha,
            )
        )

    raw_page_count = source.get("page_count")
    if isinstance(raw_page_count, bool) or not isinstance(raw_page_count, int) or raw_page_count < 1:
        errors.append(
            _error("SOURCE_PAGE_COUNT_INVALID", "$.source.page_count", "source.page_count must be a positive integer")
        )
    else:
        declared_page_count = raw_page_count

    if source_path is not None and source_path.is_file() and _looks_like_pdf(source_path):
        inspected_geometries = _pdf_page_geometries(source_path, errors)
        if inspected_geometries is not None:
            page_geometries = inspected_geometries
            page_count = len(page_geometries)
            page_count_mode = "derived_from_pdf"
            page_geometry_mode = "pdf_visible_cropbox_rotated_points"
            if declared_page_count and declared_page_count != page_count:
                errors.append(
                    _error(
                        "SOURCE_PAGE_COUNT_MISMATCH",
                        "$.source.page_count",
                        "declared source page count does not match the current PDF",
                        declared=declared_page_count,
                        actual=page_count,
                    )
                )
        else:
            # Preserve a useful range for secondary diagnostics while the PDF
            # inspection error above keeps the overall result failed closed.
            page_count = declared_page_count
            page_count_mode = "pdf_inspection_failed"
            page_geometry_mode = "pdf_inspection_failed"
    else:
        page_count = declared_page_count
        page_count_mode = "declared_non_pdf"
        page_geometry_mode = "not_available_non_pdf"
    page_range_known = page_count_mode == "derived_from_pdf" or page_count > 0

    raw_items = inventory.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        errors.append(_error("INVENTORY_ITEMS_INVALID", "$.items", "items must be a non-empty list"))
        raw_items = []

    inventory_ids: list[str] = []
    figure_ids: list[str] = []
    seen_ids: set[str] = set()
    item_pages: set[int] = set()
    for index, item in enumerate(raw_items):
        prefix = f"$.items[{index}]"
        if not isinstance(item, dict):
            errors.append(_error("ITEM_INVALID", prefix, "item must be an object"))
            continue

        item_id_value = item.get("id")
        item_id = item_id_value.strip() if isinstance(item_id_value, str) else ""
        if not item_id or not ID_RE.fullmatch(item_id):
            errors.append(_error("ITEM_ID_INVALID", f"{prefix}.id", "id must be a non-empty string without whitespace"))
        elif item_id in seen_ids:
            errors.append(_error("ITEM_ID_DUPLICATE", f"{prefix}.id", f"duplicate item ID: {item_id}"))
        else:
            seen_ids.add(item_id)
            inventory_ids.append(item_id)

        expected_order = index + 1
        order = item.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order != expected_order:
            errors.append(
                _error(
                    "ITEM_ORDER_INVALID",
                    f"{prefix}.order",
                    "item order must be contiguous and equal to its one-based list position",
                    expected=expected_order,
                    actual=order,
                )
            )

        item_type = item.get("type")
        if not isinstance(item_type, str) or not TYPE_RE.fullmatch(item_type):
            errors.append(
                _error(
                    "ITEM_TYPE_INVALID",
                    f"{prefix}.type",
                    "type must be a lowercase slug such as concept_exploration, example, or mission",
                )
            )

        source_page = item.get("source_page")
        if (
            isinstance(source_page, bool)
            or not isinstance(source_page, int)
            or source_page < 1
            or (page_range_known and source_page > page_count)
        ):
            errors.append(
                _error(
                    "ITEM_SOURCE_PAGE_INVALID",
                    f"{prefix}.source_page",
                    "source_page must be within the current source page range",
                    page_count=page_count,
                    page_count_mode=page_count_mode,
                )
            )
        else:
            item_pages.add(source_page)

        box = item.get("problem_box")
        box_ok = isinstance(box, list) and len(box) == 4
        if box_ok:
            box_ok = all(
                not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                for value in box
            )
        if box_ok:
            box_ok = box[2] > box[0] and box[3] > box[1]
        if not box_ok:
            errors.append(
                _error(
                    "ITEM_PROBLEM_BOX_INVALID",
                    f"{prefix}.problem_box",
                    "problem_box must be finite [left, top, right, bottom] coordinates with positive area",
                )
            )
        elif (
            page_geometries
            and isinstance(source_page, int)
            and not isinstance(source_page, bool)
            and 1 <= source_page <= len(page_geometries)
        ):
            page_geometry = page_geometries[source_page - 1]
            page_box = page_geometry["visible_rect_pt"]
            if (
                box[0] < page_box[0] - PDF_BOX_TOLERANCE_PT
                or box[1] < page_box[1] - PDF_BOX_TOLERANCE_PT
                or box[2] > page_box[2] + PDF_BOX_TOLERANCE_PT
                or box[3] > page_box[3] + PDF_BOX_TOLERANCE_PT
            ):
                errors.append(
                    _error(
                        "ITEM_PROBLEM_BOX_OUT_OF_BOUNDS",
                        f"{prefix}.problem_box",
                        "problem_box exceeds the visible PDF page in cropbox- and rotation-aware point coordinates",
                        box=[float(value) for value in box],
                        source_page=source_page,
                        visible_page_rect_pt=page_box,
                        cropbox_pt=page_geometry["cropbox_pt"],
                        rotation=page_geometry["rotation"],
                    )
                )

        has_text = item.get("has_text")
        has_figure = item.get("has_figure")
        if not isinstance(has_text, bool):
            errors.append(_error("ITEM_TEXT_FLAG_INVALID", f"{prefix}.has_text", "has_text must be boolean"))
        if not isinstance(has_figure, bool):
            errors.append(_error("ITEM_FIGURE_FLAG_INVALID", f"{prefix}.has_figure", "has_figure must be boolean"))
        elif has_figure and item_id:
            figure_ids.append(item_id)
        if has_text is False and has_figure is False:
            errors.append(
                _error("ITEM_EMPTY", prefix, "an item must declare text, a figure, or both")
            )

    raw_accounts = inventory.get("page_accounts", [])
    accounted_pages: set[int] = set()
    if not isinstance(raw_accounts, list):
        errors.append(_error("PAGE_ACCOUNTS_INVALID", "$.page_accounts", "page_accounts must be a list"))
        raw_accounts = []
    for index, account in enumerate(raw_accounts):
        prefix = f"$.page_accounts[{index}]"
        if not isinstance(account, dict):
            errors.append(_error("PAGE_ACCOUNT_INVALID", prefix, "page account must be an object"))
            continue
        page = account.get("page")
        reason = account.get("reason")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or (page_range_known and page > page_count)
        ):
            errors.append(_error("PAGE_ACCOUNT_PAGE_INVALID", f"{prefix}.page", "accounted page is outside source range"))
            continue
        if page in accounted_pages:
            errors.append(_error("PAGE_ACCOUNT_DUPLICATE", f"{prefix}.page", f"page {page} is accounted more than once"))
        else:
            accounted_pages.add(page)
        if not isinstance(reason, str) or not reason.strip():
            errors.append(_error("PAGE_ACCOUNT_REASON_MISSING", f"{prefix}.reason", "an explicit non-empty reason is required"))
        if page in item_pages:
            errors.append(
                _error(
                    "PAGE_ACCOUNT_REDUNDANT",
                    f"{prefix}.page",
                    f"page {page} already contains an inventoried item",
                )
            )

    if page_count:
        unaccounted = sorted(set(range(1, page_count + 1)) - item_pages - accounted_pages)
        if unaccounted:
            errors.append(
                _error(
                    "SOURCE_PAGES_UNACCOUNTED",
                    "$.source.page_count",
                    "every source page must contain an item or an explicit page account",
                    pages=unaccounted,
                )
            )

    inventory_id_set = set(inventory_ids)
    if content_path is not None:
        content = _read_json(content_path.resolve(), "content", errors)
        if content is not None:
            content_ids = _extract_item_ids(content, "content.items", errors, ("items", "problems", "item_ids"))
            _compare_sets(inventory_id_set, set(content_ids), "content", errors, "CONTENT_ITEM_SET_MISMATCH")

    if delivery_path is not None:
        delivery = _read_json(delivery_path.resolve(), "delivery", errors)
        if delivery is not None:
            delivery_ids = _extract_item_ids(delivery, "delivery.items", errors, ("problems", "items", "item_ids"))
            _compare_sets(inventory_id_set, set(delivery_ids), "delivery", errors, "DELIVERY_ITEM_SET_MISMATCH")

    manifest_ids: list[str] = []
    manifest_files = _manifest_paths(list(manifest_paths))
    if require_manifest_coverage and figure_ids and not manifest_files:
        errors.append(
            _error(
                "FIGURE_MANIFESTS_MISSING",
                "manifests",
                "figure-bearing inventory items require geometry manifests",
                expected=sorted(figure_ids),
            )
        )
    for manifest_path in manifest_files:
        data = _read_json(manifest_path, f"manifest:{manifest_path.name}", errors)
        if data is None:
            continue
        manifest_ids.extend(
            _extract_item_ids(
                data,
                f"manifest:{manifest_path.name}",
                errors,
                ("items", "manifests", "problems", "item_ids"),
            )
        )
    if manifest_files:
        duplicates = sorted({value for value in manifest_ids if manifest_ids.count(value) > 1})
        if duplicates:
            errors.append(
                _error(
                    "MANIFEST_ID_DUPLICATE",
                    "manifests",
                    "a figure ID occurs in more than one manifest",
                    ids=duplicates,
                )
            )
        _compare_sets(set(figure_ids), set(manifest_ids), "manifests", errors, "FIGURE_MANIFEST_SET_MISMATCH")

    errors = _sorted_errors(errors)
    return {
        "ok": not errors,
        "schema_version": SCHEMA_VERSION,
        "inventory": str(inventory_path.resolve()) if inventory_path else None,
        "inventory_sha256": _sha256(inventory_path) if inventory_path and inventory_path.is_file() else None,
        "source": str(source_path) if source_path else None,
        "source_sha256": actual_source_sha,
        "source_page_count": page_count,
        "declared_source_page_count": declared_page_count or None,
        "source_page_count_mode": page_count_mode,
        "source_page_geometry_mode": page_geometry_mode,
        "source_page_geometries": page_geometries,
        "item_count": len(inventory_ids),
        "figure_item_count": len(figure_ids),
        "accounted_page_count": len(item_pages | accounted_pages),
        "content_checked": content_path is not None,
        "delivery_checked": delivery_path is not None,
        "manifest_coverage_required": require_manifest_coverage,
        "manifest_count": len(manifest_files),
        "errors": errors,
    }


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inventory", type=Path, help="source inventory JSON")
    parser.add_argument("--content", type=Path, help="optional content/transcription JSON to compare")
    parser.add_argument("--delivery", type=Path, help="optional delivery JSON to compare")
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help="geometry manifest JSON or a directory of JSON manifests; repeat as needed",
    )
    parser.add_argument("--report", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args()

    inventory_path = args.inventory.resolve()
    bootstrap_errors: list[dict[str, Any]] = []
    inventory = _read_json(inventory_path, "inventory", bootstrap_errors)
    if inventory is None:
        report: dict[str, Any] = {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "inventory": str(inventory_path),
            "errors": _sorted_errors(bootstrap_errors),
        }
    else:
        report = validate_inventory(
            inventory,
            inventory_path.parent,
            inventory_path=inventory_path,
            content_path=args.content,
            delivery_path=args.delivery,
            manifest_paths=args.manifest,
        )

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
