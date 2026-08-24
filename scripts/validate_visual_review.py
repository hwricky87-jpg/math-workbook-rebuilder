#!/usr/bin/env python3
"""Validate that a vision reviewer opened current, hash-bound reconstruction images."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


APPROVED_STATUSES = {"approved", "complete", "completed", "verified"}
GEOMETRY_CHECKS = (
    "text_match",
    "topology_match",
    "line_styles_match",
    "labels_match",
    "dimensions_match",
    "projection_match",
    "inspected_latest_render",
    "source_crop_complete",
    "every_component_present",
    "every_vertex_on_native_junction",
    "segments_one_to_one_with_source",
    "solid_dashed_roles_match",
    "curves_follow_source",
    "no_handwriting_or_answer",
)
GEOMETRY_ARTIFACTS = (
    "source",
    "observation",
    "observation_overlay",
    "redraw",
    "comparison",
    "diagnostic",
    "comparison_report",
    "visual_review_sheet",
    "landmark_zoom_sheet",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def validate_visual_review(
    review: dict[str, Any],
    base: Path,
    *,
    expected: dict[str, Path] | None = None,
    require_final_crop: bool = False,
) -> dict[str, Any]:
    """Require genuine image-viewer review and recalculate every evidence hash."""
    errors: list[str] = []
    status = str(review.get("status") or "").strip().lower()
    if status not in APPROVED_STATUSES:
        errors.append(
            "visual review status must be approved/complete/verified after opening both review PNGs"
        )
    if not str(review.get("reviewer") or "").strip():
        errors.append("visual review reviewer is required")
    method = str(review.get("review_method") or "").lower()
    if not any(token in method for token in ("image viewer", "vision", "view_image", "original detail")):
        errors.append(
            "visual review method must record that the images were opened in a vision/image viewer"
        )

    checks = review.get("checks")
    if not isinstance(checks, dict):
        errors.append("visual review checks must be an object")
        checks = {}
    required_checks = list(GEOMETRY_CHECKS)
    if require_final_crop:
        required_checks.append("final_pdf_scale_and_spacing_are_suitable")
    for name in required_checks:
        if checks.get(name) is not True:
            errors.append(f"visual review check must be true: {name}")

    issues = review.get("issues")
    if not isinstance(issues, list):
        errors.append("visual review issues must be a list")
    elif issues:
        errors.append("approved visual review must have no unresolved issues")

    artifacts = review.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("visual review artifacts must be an object")
        artifacts = {}
    required_artifacts = list(GEOMETRY_ARTIFACTS)
    if require_final_crop:
        required_artifacts.append("final_crop")
    records: dict[str, dict[str, Any]] = {}
    for name in required_artifacts:
        record = artifacts.get(name)
        if not isinstance(record, dict):
            errors.append(f"visual review artifact is required: {name}")
            continue
        path = resolve(base, record.get("file"))
        if path is None or not path.is_file():
            errors.append(f"visual review artifact does not exist: {name}")
            continue
        actual = sha256(path)
        if str(record.get("sha256") or "").lower() != actual:
            errors.append(f"visual review artifact hash is stale: {name}")
        expected_path = (expected or {}).get(name)
        if expected_path is not None:
            try:
                same = path.resolve() == expected_path.resolve()
            except OSError:
                same = False
            if not same or not expected_path.is_file() or sha256(expected_path) != actual:
                errors.append(f"visual review artifact is not the current {name}")
        records[name] = {"path": str(path), "sha256": actual}

    return {
        "ok": not errors,
        "errors": errors,
        "status": status or None,
        "reviewer": review.get("reviewer"),
        "artifacts": records,
        "required_checks": required_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path)
    parser.add_argument("--require-final-crop", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    review_path = args.review.resolve()
    value = json.loads(review_path.read_text(encoding="utf-8"))
    report = validate_visual_review(
        value,
        review_path.parent,
        require_final_crop=args.require_final_crop,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
