#!/usr/bin/env python3
"""Fail closed unless the final PDF was compared problem-by-problem with its sources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pymupdf


REQUIRED_PROBLEM_CHECKS = (
    "text_match",
    "figure_match",
    "line_styles_match",
    "labels_dimensions_match",
    "no_handwriting",
    "no_clipping",
    "inspected_latest_pdf",
)


def _resolve(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else base / path


def validate_delivery_manifest(manifest: dict[str, Any], base: Path) -> dict[str, Any]:
    """Validate delivery evidence and return a JSON-serializable report."""
    errors: list[str] = []
    pdf_path = _resolve(base, manifest.get("pdf"))
    qa_path = _resolve(base, manifest.get("pdf_qa_report"))
    page_count = 0

    if pdf_path is None:
        errors.append("pdf is required")
    elif pdf_path.suffix.lower() != ".pdf":
        errors.append("pdf must identify a .pdf file")
    elif not pdf_path.is_file():
        errors.append(f"pdf does not exist: {pdf_path}")
    else:
        try:
            with pymupdf.open(pdf_path) as document:
                page_count = document.page_count
            if page_count < 1:
                errors.append("pdf contains no pages")
        except Exception as exc:  # PyMuPDF exposes several format-specific exception types.
            errors.append(f"pdf cannot be opened: {exc}")

    if qa_path is None:
        errors.append("pdf_qa_report is required")
    elif not qa_path.is_file():
        errors.append(f"pdf_qa_report does not exist: {qa_path}")
    else:
        try:
            qa_report = json.loads(qa_path.read_text(encoding="utf-8"))
            if qa_report.get("ok") is not True:
                errors.append("pdf_qa_report.ok must be true")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"pdf_qa_report is not valid JSON: {exc}")

    if manifest.get("all_pages_inspected") is not True:
        errors.append("all_pages_inspected must be true")

    problems = manifest.get("problems")
    if not isinstance(problems, list) or not problems:
        errors.append("problems must contain at least one source-to-PDF comparison")
        problems = []

    seen_ids: set[str] = set()
    seen_pages: set[int] = set()
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

        for field in ("source_crop", "comparison_artifact", "final_page_render"):
            evidence_path = _resolve(base, problem.get(field))
            if evidence_path is None:
                errors.append(f"{prefix}.{field} is required")
            elif not evidence_path.is_file():
                errors.append(f"{prefix}.{field} does not exist: {evidence_path}")

        page = problem.get("final_page")
        if not isinstance(page, int):
            errors.append(f"{prefix}.final_page must be an integer")
        elif page < 1 or (page_count and page > page_count):
            errors.append(f"{prefix}.final_page={page} is outside the PDF page range")
        else:
            seen_pages.add(page)

        for field in REQUIRED_PROBLEM_CHECKS:
            if problem.get(field) is not True:
                errors.append(f"{prefix}.{field} must be true")

    if page_count and manifest.get("all_pages_inspected") is True:
        missing_pages = sorted(set(range(1, page_count + 1)) - seen_pages)
        if missing_pages:
            errors.append(f"no problem comparison references final PDF page(s): {missing_pages}")

    return {
        "ok": not errors,
        "errors": errors,
        "pdf": str(pdf_path) if pdf_path else None,
        "page_count": page_count,
        "problem_count": len(problems),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = validate_delivery_manifest(manifest, manifest_path.parent)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
