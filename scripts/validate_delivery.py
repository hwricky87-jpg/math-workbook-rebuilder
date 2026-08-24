#!/usr/bin/env python3
"""Fail closed unless final-PDF review evidence belongs to the current build."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image
from validate_inventory import validate_inventory
from validate_visual_review import validate_visual_review


DELIVERY_SCHEMA_VERSION = 2
COMPLETE_STATUSES = {"approved", "complete", "completed", "verified"}
INCOMPLETE_STATUSES = {
    "draft",
    "manual",
    "needs-review",
    "needs_review",
    "pending",
    "stale",
    "unreviewed",
}
REQUIRED_PROBLEM_CHECKS = (
    "text_match",
    "figure_match",
    "line_styles_match",
    "labels_dimensions_match",
    "no_handwriting",
    "no_clipping",
    "inspected_latest_pdf",
)
LEGACY_EVIDENCE_FIELDS = ("source_crop", "comparison_artifact", "final_page_render")
EVIDENCE_FIELDS = LEGACY_EVIDENCE_FIELDS + ("final_problem_crop",)
VISUAL_REVIEW_FIELD = "visual_review"


def _resolve(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else base / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_sha256(path: Path) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        header = f"{image.width}x{image.height}:RGB\n".encode("ascii")
        return hashlib.sha256(header + image.tobytes()).hexdigest()


def _parse_final_problem_box(
    value: Any, field: str
) -> tuple[tuple[int, int, int, int] | None, list[str]]:
    """Parse the required RGB-pixel half-open problem box for schema v2."""
    if not isinstance(value, list) or len(value) != 4:
        return None, [f"{field} is required as [x0, y0, x1, y1]"]
    if any(type(coordinate) is not int for coordinate in value):
        return None, [f"{field} coordinates must be integers (booleans are not coordinates)"]
    x0, y0, x1, y1 = value
    if x1 <= x0 or y1 <= y0:
        return None, [f"{field} must be a non-empty half-open box with x0 < x1 and y0 < y1"]
    return (x0, y0, x1, y1), []


def _validate_final_problem_crop_at_box(
    crop_path: Path,
    page_path: Path,
    box: tuple[int, int, int, int],
) -> list[str]:
    """Compare a crop only at its declared box in the current RGB QA render.

    Requiring an explicit coordinate prevents a tiny or unrelated patch from
    passing merely because identical pixels occur somewhere else on a mostly
    white worksheet page.
    """
    try:
        with Image.open(page_path) as opened:
            page = opened.convert("RGB")
        with Image.open(crop_path) as opened:
            crop = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        return [f"cannot decode crop/page render: {exc}"]

    x0, y0, x1, y1 = box
    page_width, page_height = page.size
    if x0 < 0 or y0 < 0 or x1 > page_width or y1 > page_height:
        return [
            "final_problem_box is outside the current QA render bounds "
            f"{page_width}x{page_height}"
        ]

    crop_width = x1 - x0
    crop_height = y1 - y0
    crop_area = crop_width * crop_height
    page_area = page_width * page_height
    # A real problem region is not a one-pixel proof token.  The relative
    # floors scale with the render while the capped absolute floors keep small
    # synthetic pages testable when their whole page is the reviewed region.
    minimum_width = min(page_width, max(16, (page_width + 99) // 100))
    minimum_height = min(page_height, max(16, (page_height + 99) // 100))
    minimum_area = min(page_area, max(1024, (page_area + 999) // 1000))
    if (
        crop_width < minimum_width
        or crop_height < minimum_height
        or crop_area < minimum_area
    ):
        return [
            "final_problem_box is too small to be a substantive problem crop: "
            f"got {crop_width}x{crop_height} ({crop_area} pixels), require at least "
            f"{minimum_width}x{minimum_height} and {minimum_area} pixels"
        ]

    expected = page.crop(box)
    if crop.size != expected.size:
        return [
            "final_problem_crop dimensions do not match final_problem_box: "
            f"got {crop.width}x{crop.height}, expected {expected.width}x{expected.height}"
        ]
    if crop.tobytes() != expected.tobytes():
        return [
            "final_problem_crop is not pixel-exact RGB content from final_problem_box "
            "in the current QA render"
        ]
    return []


def _schema_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_error(value: Any, field: str) -> str | None:
    status = str(value or "").strip().lower()
    if not status:
        return f"{field} is required"
    if status in INCOMPLETE_STATUSES:
        return f"{field}={status!r} is not deliverable"
    if status not in COMPLETE_STATUSES:
        return f"{field} must be one of {sorted(COMPLETE_STATUSES)}"
    return None


def _incomplete_evidence_states(value: Any, prefix: str = "") -> list[str]:
    """Find stale/pending markers that contradict an otherwise approved record."""
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            lowered_key = key_text.lower()
            if lowered_key != "status" and (
                lowered_key.endswith("_status") or lowered_key.endswith("_state")
            ):
                state = str(child or "").strip().lower()
                if state in INCOMPLETE_STATUSES:
                    errors.append(f"{path}={state!r} is incomplete/stale evidence")
            elif lowered_key in {"pending", "stale"} and child is True:
                errors.append(f"{path}=true marks incomplete/stale evidence")
            errors.extend(_incomplete_evidence_states(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            errors.extend(_incomplete_evidence_states(child, path))
    return errors


def _declared_hash_error(path: Path, declared: Any, field: str) -> tuple[str | None, str]:
    actual = _sha256(path)
    if not isinstance(declared, str) or not declared.strip():
        return f"{field} is required; path existence alone is stale-prone evidence", actual
    if declared.lower() != actual:
        return f"{field} does not match the current file ({actual})", actual
    return None, actual


def _inventory_specs(manifest: dict[str, Any]) -> list[tuple[Any, Any]]:
    """Normalize the supported single- and multi-inventory declarations."""
    raw = manifest.get("source_inventories")
    if raw is None:
        raw = manifest.get("source_inventory")
    if raw is None:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    top_hash = manifest.get("source_inventory_sha256")
    hashes = top_hash if isinstance(top_hash, list) else [top_hash] * len(entries)
    specs: list[tuple[Any, Any]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            specs.append((entry.get("path") or entry.get("file"), entry.get("sha256")))
        else:
            specs.append((entry, hashes[index] if index < len(hashes) else None))
    return specs


def _inventory_ids(data: Any, label: str, errors: list[str]) -> set[str]:
    if not isinstance(data, dict):
        errors.append(f"{label} must be a JSON object")
        return set()

    collection: Any = None
    for key in ("item_ids", "problem_ids", "items", "problems"):
        if key in data:
            collection = data[key]
            break
    if not isinstance(collection, list):
        errors.append(
            f"{label} must contain one list named item_ids, problem_ids, items, or problems"
        )
        return set()

    ids: list[str] = []
    for index, item in enumerate(collection, start=1):
        value = item.get("id") if isinstance(item, dict) else item
        item_id = str(value or "").strip()
        if not item_id:
            errors.append(f"{label} entry {index} has no id")
        else:
            ids.append(item_id)
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        errors.append(f"{label} contains duplicate id(s): {duplicates}")

    for count_key in ("item_count", "problem_count"):
        if count_key in data and data[count_key] != len(collection):
            errors.append(
                f"{label}.{count_key}={data[count_key]!r} does not match "
                f"the {len(collection)} inventory entries"
            )
    return set(ids)


def _load_source_inventory(
    manifest: dict[str, Any], base: Path, errors: list[str]
) -> tuple[set[str] | None, list[dict[str, Any]]]:
    specs = _inventory_specs(manifest)
    if not specs:
        errors.append(
            "delivery schema v2 requires at least one current source_inventory with sha256"
        )
        return None, []

    combined: set[str] = set()
    records: list[dict[str, Any]] = []
    for index, (raw_path, declared_hash) in enumerate(specs, start=1):
        label = f"source_inventory[{index}]"
        path = _resolve(base, raw_path)
        if path is None:
            errors.append(f"{label}.path is required")
            continue
        if not path.is_file():
            errors.append(f"{label} does not exist: {path}")
            continue
        actual_hash = _sha256(path)
        if not isinstance(declared_hash, str) or not declared_hash.strip():
            errors.append(f"{label}.sha256 is required")
        elif declared_hash.lower() != actual_hash:
            errors.append(f"{label}.sha256 does not match the current inventory")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label} is not valid JSON: {exc}")
            continue
        inventory_gate = validate_inventory(
            data,
            path.parent,
            inventory_path=path,
            require_manifest_coverage=False,
        )
        if inventory_gate.get("ok") is not True:
            for item in inventory_gate.get("errors", []):
                if isinstance(item, dict):
                    errors.append(
                        f"{label} failed inventory validation: "
                        f"{item.get('code')} {item.get('path')}: {item.get('message')}"
                    )
                else:
                    errors.append(f"{label} failed inventory validation: {item}")
        ids = _inventory_ids(data, label, errors)
        overlap = sorted(combined & ids)
        if overlap:
            errors.append(f"source inventories repeat id(s): {overlap}")
        combined.update(ids)
        records.append({"path": str(path), "sha256": actual_hash, "item_count": len(ids)})
    return combined, records


def _load_and_validate_qa(
    qa_path: Path,
    pdf_sha256: str,
    page_count: int,
    errors: list[str],
) -> tuple[dict[str, Any] | None, dict[int, dict[str, Any]]]:
    try:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"pdf_qa_report is not valid JSON: {exc}")
        return None, {}
    if not isinstance(qa, dict):
        errors.append("pdf_qa_report must contain a JSON object")
        return None, {}

    if _schema_number(qa.get("schema_version")) != 2:
        errors.append("pdf_qa_report.schema_version must be 2; rerun qa_pdf.py")
    if qa.get("ok") is not True or qa.get("errors") != []:
        errors.append("pdf_qa_report must be successful and contain no errors")
    if str(qa.get("pdf_sha256", "")).lower() != pdf_sha256:
        errors.append("pdf_qa_report.pdf_sha256 is stale for the current PDF")
    if qa.get("page_count") != page_count:
        errors.append(
            f"pdf_qa_report.page_count={qa.get('page_count')!r} does not match {page_count}"
        )
    qa_pdf_path = _resolve(qa_path.parent, qa.get("pdf"))
    if qa_pdf_path is None:
        errors.append("pdf_qa_report.pdf is required")
    elif qa_pdf_path.is_file() and _sha256(qa_pdf_path) != pdf_sha256:
        errors.append("pdf_qa_report.pdf path now contains bytes different from the reviewed PDF")

    renderer = qa.get("renderer")
    if not isinstance(renderer, dict) or not renderer.get("name") or not (
        renderer.get("pymupdf_version") or renderer.get("version")
    ):
        errors.append("pdf_qa_report.renderer identifier/version is missing")
    if not isinstance(qa.get("dpi"), int) or qa["dpi"] <= 0:
        errors.append("pdf_qa_report.dpi must be a positive integer")

    qa_pages = qa.get("pages")
    if not isinstance(qa_pages, list) or len(qa_pages) != page_count:
        errors.append("pdf_qa_report.pages must contain exactly one entry per PDF page")
        return qa, {}

    by_page: dict[int, dict[str, Any]] = {}
    for index, page in enumerate(qa_pages, start=1):
        prefix = f"pdf_qa_report.pages[{index}]"
        if not isinstance(page, dict):
            errors.append(f"{prefix} must be an object")
            continue
        page_number = page.get("page")
        if not isinstance(page_number, int) or page_number in by_page:
            errors.append(f"{prefix}.page must be a unique integer")
            continue
        by_page[page_number] = page

        render = _resolve(qa_path.parent, page.get("render"))
        if render is None or not render.is_file():
            errors.append(f"{prefix}.render does not exist: {render}")
        else:
            actual = _sha256(render)
            if str(page.get("render_sha256", "")).lower() != actual:
                errors.append(f"{prefix}.render_sha256 is stale")
            declared_pixel = page.get("pixel_sha256")
            if not isinstance(declared_pixel, str) or not declared_pixel:
                errors.append(f"{prefix}.pixel_sha256 is required")
            else:
                try:
                    if declared_pixel.lower() != _pixel_sha256(render):
                        errors.append(f"{prefix}.pixel_sha256 is stale")
                except OSError as exc:
                    errors.append(f"{prefix}.render cannot be decoded: {exc}")

        edge_checks = page.get("edge_checks")
        required_edges = {"left", "top", "right", "bottom"}
        if not isinstance(edge_checks, dict) or set(edge_checks) != required_edges:
            errors.append(f"{prefix}.edge_checks must cover all four page edges")
        elif any(edge_checks[edge] is not True for edge in required_edges):
            errors.append(f"{prefix}.edge_checks contains a failed page margin")

    expected_pages = set(range(1, page_count + 1))
    if set(by_page) != expected_pages:
        errors.append(
            f"pdf_qa_report page set is {sorted(by_page)}, expected {sorted(expected_pages)}"
        )
    return qa, by_page


def _validate_legacy(manifest: dict[str, Any], base: Path) -> dict[str, Any]:
    """Compatibility path for pre-v2 manifests, enabled only by --allow-legacy."""
    errors: list[str] = []
    pdf_path = _resolve(base, manifest.get("pdf"))
    qa_path = _resolve(base, manifest.get("pdf_qa_report"))
    page_count = 0
    pdf_sha256 = None

    if pdf_path is None or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        errors.append("legacy pdf must identify an existing .pdf file")
    else:
        try:
            pdf_sha256 = _sha256(pdf_path)
            with pymupdf.open(pdf_path) as document:
                page_count = document.page_count
        except Exception as exc:
            errors.append(f"pdf cannot be opened: {exc}")

    if qa_path is None or not qa_path.is_file():
        errors.append("legacy pdf_qa_report must identify an existing file")
    else:
        try:
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
            if qa.get("ok") is not True:
                errors.append("legacy pdf_qa_report.ok must be true")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"legacy pdf_qa_report is invalid: {exc}")

    if manifest.get("all_pages_inspected") is not True:
        errors.append("all_pages_inspected must be true")
    problems = manifest.get("problems")
    if not isinstance(problems, list) or not problems:
        errors.append("problems must contain at least one comparison")
        problems = []
    seen_ids: set[str] = set()
    seen_pages: set[int] = set()
    for index, problem in enumerate(problems, start=1):
        prefix = f"problems[{index}]"
        if not isinstance(problem, dict):
            errors.append(f"{prefix} must be an object")
            continue
        problem_id = str(problem.get("id", "")).strip()
        if not problem_id or problem_id in seen_ids:
            errors.append(f"{prefix}.id is missing or duplicated")
        seen_ids.add(problem_id)
        for field in LEGACY_EVIDENCE_FIELDS:
            path = _resolve(base, problem.get(field))
            if path is None or not path.is_file():
                errors.append(f"{prefix}.{field} must identify an existing file")
        page = problem.get("final_page")
        if not isinstance(page, int) or page < 1 or (page_count and page > page_count):
            errors.append(f"{prefix}.final_page is outside the PDF page range")
        else:
            seen_pages.add(page)
        for field in REQUIRED_PROBLEM_CHECKS:
            if problem.get(field) is not True:
                errors.append(f"{prefix}.{field} must be true")
    if page_count and set(range(1, page_count + 1)) - seen_pages:
        errors.append("not every PDF page is referenced by a problem comparison")
    return {
        "ok": not errors,
        "legacy": True,
        "warnings": [
            "legacy mode cannot prove that review artifacts belong to the current PDF"
        ],
        "errors": errors,
        "pdf": str(pdf_path) if pdf_path else None,
        "pdf_sha256": pdf_sha256,
        "page_count": page_count,
        "problem_count": len(problems),
    }


def validate_delivery_manifest(
    manifest: dict[str, Any], base: Path, *, allow_legacy: bool = False
) -> dict[str, Any]:
    """Validate delivery evidence and return a JSON-serializable report."""
    if not isinstance(manifest, dict):
        return {
            "ok": False,
            "legacy": False,
            "errors": ["delivery manifest root must be a JSON object"],
            "pdf": None,
            "page_count": 0,
            "problem_count": 0,
        }
    schema = _schema_number(manifest.get("schema_version"))
    if schema != DELIVERY_SCHEMA_VERSION:
        if allow_legacy and schema in (None, 1):
            return _validate_legacy(manifest, base)
        return {
            "ok": False,
            "legacy": False,
            "errors": [
                "delivery schema_version must be 2; old boolean-only evidence is rejected "
                "unless --allow-legacy is explicitly supplied"
            ],
            "pdf": str(_resolve(base, manifest.get("pdf")))
            if _resolve(base, manifest.get("pdf"))
            else None,
            "page_count": 0,
            "problem_count": len(manifest.get("problems", []))
            if isinstance(manifest.get("problems"), list)
            else 0,
        }

    errors: list[str] = []
    errors.extend(_incomplete_evidence_states(manifest))
    status_error = _status_error(manifest.get("status"), "status")
    if status_error:
        errors.append(status_error)

    pdf_path = _resolve(base, manifest.get("pdf"))
    qa_path = _resolve(base, manifest.get("pdf_qa_report"))
    page_count = 0
    pdf_sha256 = ""
    if pdf_path is None:
        errors.append("pdf is required")
    elif pdf_path.suffix.lower() != ".pdf":
        errors.append("pdf must identify a .pdf file")
    elif not pdf_path.is_file():
        errors.append(f"pdf does not exist: {pdf_path}")
    else:
        try:
            pdf_sha256 = _sha256(pdf_path)
            with pymupdf.open(pdf_path) as document:
                page_count = document.page_count
            if page_count < 1:
                errors.append("pdf contains no pages")
        except Exception as exc:
            errors.append(f"pdf cannot be opened: {exc}")
    if pdf_sha256 and str(manifest.get("pdf_sha256", "")).lower() != pdf_sha256:
        errors.append("pdf_sha256 is required and must match the current PDF")

    qa_pages: dict[int, dict[str, Any]] = {}
    if qa_path is None:
        errors.append("pdf_qa_report is required")
    elif not qa_path.is_file():
        errors.append(f"pdf_qa_report does not exist: {qa_path}")
    elif pdf_path and pdf_sha256 and page_count:
        _, qa_pages = _load_and_validate_qa(
            qa_path, pdf_sha256, page_count, errors
        )

    if manifest.get("all_pages_inspected") is not True:
        errors.append("all_pages_inspected must be true")

    inventory_ids, inventory_records = _load_source_inventory(manifest, base, errors)
    problems = manifest.get("problems")
    if not isinstance(problems, list) or not problems:
        errors.append("problems must contain at least one source-to-PDF comparison")
        problems = []

    seen_ids: set[str] = set()
    seen_pages: set[int] = set()
    visual_review_records: list[dict[str, Any]] = []
    for index, problem in enumerate(problems, start=1):
        prefix = f"problems[{index}]"
        if not isinstance(problem, dict):
            errors.append(f"{prefix} must be an object")
            continue
        problem_id = str(problem.get("id", "")).strip()
        if not problem_id:
            errors.append(f"{prefix}.id is required")
        elif problem_id in seen_ids:
            errors.append(f"{prefix}.id is duplicated: {problem_id}")
        else:
            seen_ids.add(problem_id)

        problem_status = _status_error(problem.get("status"), f"{prefix}.status")
        if problem_status:
            errors.append(problem_status)
        if pdf_sha256 and str(problem.get("pdf_sha256", "")).lower() != pdf_sha256:
            errors.append(
                f"{prefix}.pdf_sha256 is required and must identify the reviewed current PDF"
            )

        evidence_hashes: dict[str, str] = {}
        evidence_paths: dict[str, Path] = {}
        for field in EVIDENCE_FIELDS + (VISUAL_REVIEW_FIELD,):
            evidence_path = _resolve(base, problem.get(field))
            if evidence_path is None:
                errors.append(f"{prefix}.{field} is required")
            elif not evidence_path.is_file():
                errors.append(f"{prefix}.{field} does not exist: {evidence_path}")
            else:
                hash_error, actual_hash = _declared_hash_error(
                    evidence_path,
                    problem.get(f"{field}_sha256"),
                    f"{prefix}.{field}_sha256",
                )
                evidence_hashes[field] = actual_hash
                evidence_paths[field] = evidence_path
                if hash_error:
                    errors.append(hash_error)

        visual_review_report: dict[str, Any] | None = None
        visual_review_path = evidence_paths.get(VISUAL_REVIEW_FIELD)
        if visual_review_path is not None:
            try:
                visual_review = json.loads(visual_review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{prefix}.visual_review is not valid JSON: {exc}")
            else:
                if not isinstance(visual_review, dict):
                    errors.append(f"{prefix}.visual_review must contain a JSON object")
                else:
                    if str(visual_review.get("id") or "").strip() != problem_id:
                        errors.append(
                            f"{prefix}.visual_review.id must match the delivery problem id"
                        )
                    expected_review_artifacts = {
                        artifact: evidence_paths[field]
                        for artifact, field in (
                            ("source", "source_crop"),
                            ("comparison", "comparison_artifact"),
                            ("final_crop", "final_problem_crop"),
                        )
                        if field in evidence_paths
                    }
                    visual_review_report = validate_visual_review(
                        visual_review,
                        visual_review_path.parent,
                        expected=expected_review_artifacts,
                        require_final_crop=True,
                    )
                    for message in visual_review_report.get("errors", []):
                        errors.append(f"{prefix}.visual_review failed: {message}")
        visual_review_records.append(
            {
                "id": problem_id or None,
                "path": str(visual_review_path) if visual_review_path else None,
                "sha256": evidence_hashes.get(VISUAL_REVIEW_FIELD),
                "validation": visual_review_report,
            }
        )

        final_problem_box, box_errors = _parse_final_problem_box(
            problem.get("final_problem_box"), f"{prefix}.final_problem_box"
        )
        errors.extend(box_errors)
        page = problem.get("final_page")
        if not isinstance(page, int):
            errors.append(f"{prefix}.final_page must be an integer")
        elif page < 1 or (page_count and page > page_count):
            errors.append(f"{prefix}.final_page={page} is outside the PDF page range")
        else:
            seen_pages.add(page)
            qa_page = qa_pages.get(page)
            if qa_page is None:
                errors.append(f"{prefix} has no QA page evidence for page {page}")
            elif "final_page_render" in evidence_hashes:
                if evidence_hashes["final_page_render"] != str(
                    qa_page.get("render_sha256", "")
                ).lower():
                    errors.append(
                        f"{prefix}.final_page_render is not the current QA render for page {page}"
                    )
                final_crop_path = evidence_paths.get("final_problem_crop")
                final_page_render_path = evidence_paths.get("final_page_render")
                if (
                    final_problem_box is not None
                    and final_crop_path is not None
                    and final_page_render_path is not None
                ):
                    for crop_error in _validate_final_problem_crop_at_box(
                        final_crop_path, final_page_render_path, final_problem_box
                    ):
                        errors.append(f"{prefix}.{crop_error}")

        for field in REQUIRED_PROBLEM_CHECKS:
            if problem.get(field) is not True:
                errors.append(f"{prefix}.{field} must be true")

    if inventory_ids is not None:
        missing = sorted(inventory_ids - seen_ids)
        unexpected = sorted(seen_ids - inventory_ids)
        if missing:
            errors.append(f"delivery is missing source inventory item(s): {missing}")
        if unexpected:
            errors.append(f"delivery contains item(s) absent from source inventory: {unexpected}")

    if page_count and manifest.get("all_pages_inspected") is True:
        missing_pages = sorted(set(range(1, page_count + 1)) - seen_pages)
        if missing_pages:
            errors.append(f"no problem comparison references final PDF page(s): {missing_pages}")

    return {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "ok": not errors,
        "legacy": False,
        "errors": errors,
        "pdf": str(pdf_path) if pdf_path else None,
        "pdf_sha256": pdf_sha256 or None,
        "qa_report": str(qa_path) if qa_path else None,
        "qa_report_sha256": _sha256(qa_path) if qa_path and qa_path.is_file() else None,
        "page_count": page_count,
        "problem_count": len(problems),
        "visual_reviews": visual_review_records,
        "source_inventories": inventory_records,
        "source_inventory_item_count": len(inventory_ids) if inventory_ids is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="explicitly accept pre-v2 boolean-only manifests (not safe for final delivery)",
    )
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = validate_delivery_manifest(
        manifest, manifest_path.parent, allow_legacy=args.allow_legacy
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
