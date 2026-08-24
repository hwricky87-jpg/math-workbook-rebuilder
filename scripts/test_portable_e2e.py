#!/usr/bin/env python3
"""Portable synthetic end-to-end test for the visual worksheet workflow.

The test deliberately stops at a hash-bound ``pending-vision-review`` record.
It prepares every image a vision-capable reviewer must open, but never pretends
that an automated process actually looked at those images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont


SCRIPTS_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_ROOT.parent
FONT_ROOT = SKILL_ROOT / "assets" / "fonts"
REGULAR_FONT = FONT_ROOT / "NanumGothic-Regular.ttf"
BOLD_FONT = FONT_ROOT / "NanumGothic-Bold.ttf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pixel_sha256(path: Path) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    header = f"{image.width}x{image.height}:RGB\n".encode("ascii")
    return hashlib.sha256(header + image.tobytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object: {path}")
    return value


def _run(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("NODE_PATH", None)
    environment.pop("MWR_KOREAN_FONT", None)
    environment.pop("MWR_FONT_FILES", None)
    environment.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        printable = subprocess.list2cmdline(list(command))
        raise AssertionError(
            f"command failed ({completed.returncode}): {printable}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _script(name: str, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    target = SCRIPTS_ROOT / name
    if not target.is_file():
        raise AssertionError(f"bundled script is missing: {target}")
    return _run((sys.executable, str(target), *arguments), cwd=cwd)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _trace_drawing() -> dict[str, Any]:
    points = {
        "A": [88, 100],
        "B": [248, 54],
        "C": [520, 88],
        "D": [570, 286],
        "E": [390, 398],
        "F": [115, 336],
        "G": [268, 165],
        "H": [454, 176],
        "I": [355, 302],
    }
    segments = [
        {"name": "outer-A-B", "from": "A", "to": "B", "style": "solid"},
        {"name": "outer-B-C", "from": "B", "to": "C", "style": "solid"},
        {"name": "outer-C-D", "from": "C", "to": "D", "style": "solid"},
        {"name": "outer-D-E", "from": "D", "to": "E", "style": "solid"},
        {"name": "outer-E-F", "from": "E", "to": "F", "style": "solid"},
        {"name": "outer-F-A", "from": "F", "to": "A", "style": "solid"},
        {"name": "B-G", "from": "B", "to": "G", "style": "solid"},
        {"name": "A-G", "from": "A", "to": "G", "style": "solid"},
        {"name": "G-H-hidden", "from": "G", "to": "H", "style": "dashed"},
        {"name": "H-C", "from": "H", "to": "C", "style": "solid"},
        {"name": "H-D", "from": "H", "to": "D", "style": "solid"},
        {"name": "G-I-hidden", "from": "G", "to": "I", "style": "dashed"},
        {"name": "I-H-hidden", "from": "I", "to": "H", "style": "dashed"},
        {"name": "I-E", "from": "I", "to": "E", "style": "solid"},
        {"name": "F-I", "from": "F", "to": "I", "style": "solid"},
        {"name": "D-I", "from": "D", "to": "I", "style": "solid"},
    ]
    curves = [
        {
            "name": "height-guide",
            "points": [[576, 91], [610, 143], [623, 213], [610, 283], [574, 331]],
            "style": "guide",
            "smooth": True,
        }
    ]
    labels = [
        {"name": "label-A", "text": "ㄱ", "at": "A", "offset": [-20, -10]},
        {"name": "label-C", "text": "ㄴ", "at": "C", "offset": [18, -8]},
        {"name": "measure-top", "text": "6 cm", "at": [360, 48], "style": "measure"},
        {"name": "measure-height", "text": "12 cm", "at": [617, 226], "style": "measure"},
    ]
    return {
        "id": "portable-e2e",
        "type": "trace",
        "canvas": {"width": 660, "height": 460, "viewBox": [0, 0, 660, 460]},
        "render_style": {
            "solid_width": 3.0,
            "dashed_width": 2.5,
            "guide_width": 1.8,
            "label_size": 21,
            "measure_size": 19,
            "label_weight": 700,
            "measure_weight": 700,
        },
        "points": points,
        "segments": segments,
        "curves": curves,
        "markers": [],
        "labels": labels,
        "dimensions": [],
        "required_segments": [item["name"] for item in segments],
        "required_curves": [item["name"] for item in curves],
        "required_markers": [],
        "required_labels": [item["name"] for item in labels],
        "required_dimensions": [],
    }


def _make_source_page(work: Path, drawing: dict[str, Any]) -> tuple[Path, list[int]]:
    seed_manifest = work / "fixture-source.json"
    seed_svg = work / "fixture-source.svg"
    seed_png = work / "fixture-source.png"
    seed = dict(drawing)
    seed["source"] = {"fixture": "portable-asymmetric-korean-trace"}
    _write_json(seed_manifest, seed)
    _script("geometry_svg.py", str(seed_manifest), str(seed_svg), cwd=work)

    node = shutil.which("node")
    local_resvg = SKILL_ROOT / "node_modules" / "@resvg" / "resvg-js"
    if not node:
        raise AssertionError("Node.js is missing; run scripts/bootstrap.py --verify")
    if not local_resvg.exists():
        raise AssertionError(
            "skill-local @resvg/resvg-js is missing; run scripts/bootstrap.py --verify"
        )
    _run((node, str(SCRIPTS_ROOT / "render_svg.mjs"), str(seed_svg), str(seed_png), "1"), cwd=work)

    with Image.open(seed_png) as opened:
        figure = opened.convert("RGB")
    if figure.size != (660, 460):
        figure = figure.resize((660, 460), Image.Resampling.LANCZOS)

    page = Image.new("RGB", (1400, 1000), "#fbfaf7")
    draw = ImageDraw.Draw(page)
    title_font = ImageFont.truetype(str(BOLD_FONT), 34)
    body_font = ImageFont.truetype(str(REGULAR_FONT), 28)
    draw.text((72, 55), "합성 원본 · 공간도형 관찰", font=title_font, fill="#26364a")
    draw.text(
        (72, 112),
        "도형의 실선, 점선, 곡선과 표시된 길이를 원본과 대조하여 다시 그리세요.",
        font=body_font,
        fill="#3c4655",
    )
    crop_box = [520, 260, 1180, 720]
    page.paste(figure, (crop_box[0], crop_box[1]))

    # A synthetic student mark lives outside the retained geometry crop.  Its
    # absence from the reconstructed worksheet can be checked by the viewer.
    draw.arc((75, 730, 350, 900), start=15, end=330, fill="#c44141", width=8)
    draw.line((145, 820, 250, 755, 205, 875), fill="#c44141", width=8, joint="curve")

    source_page = work / "source-page.png"
    page.save(source_page, format="PNG", optimize=True)
    return source_page, crop_box


def _assert_pending_review(path: Path) -> dict[str, Any]:
    review = _read_json(path)
    if review.get("status") != "pending-vision-review":
        raise AssertionError("automated integration test must leave visual review pending")
    if review.get("reviewer") is not None:
        raise AssertionError("automated integration test must not name a vision reviewer")
    checks = review.get("checks")
    if not isinstance(checks, dict) or not checks or any(value is not None for value in checks.values()):
        raise AssertionError("pending visual review checks must remain null")
    return review


def run_portable_e2e(output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if _is_within(output, SKILL_ROOT):
        raise AssertionError("portable E2E output must live outside the skill installation")
    output.mkdir(parents=True, exist_ok=False)
    if not REGULAR_FONT.is_file() or not BOLD_FONT.is_file():
        raise AssertionError("bundled NanumGothic regular/bold fonts are missing")

    drawing = _trace_drawing()
    source_page, crop_box = _make_source_page(output, drawing)
    inspection = output / "inspection"
    _script(
        "preprocess_geometry.py",
        str(source_page),
        "--box",
        *(str(value) for value in crop_box),
        "--out",
        str(inspection),
        cwd=output,
    )
    source_native = inspection / "source-native.png"
    inspection_report = inspection / "inspection.json"
    with Image.open(source_native) as opened:
        source_size = [opened.width, opened.height]
    if source_size != [660, 460]:
        raise AssertionError(f"unexpected native source size: {source_size}")

    observation = {
        "source_image": "inspection/source-native.png",
        "source_sha256": _sha256(source_native),
        "source_size": source_size,
        "source_geometry_roi": [42, 24, 650, 430],
        "source_geometry_roi_method": "synthetic source ink census before redraw construction",
        "points": drawing["points"],
        "segments": drawing["segments"],
        "curves": drawing["curves"],
        "markers": drawing["markers"],
        "labels": drawing["labels"],
        "dimensions": drawing["dimensions"],
        "component_inventory": [
            {
                "id": "asymmetric-projection",
                "point_count": len(drawing["points"]),
                "segment_count": len(drawing["segments"]),
            }
        ],
    }
    observation_path = output / "source-observation.json"
    _write_json(observation_path, observation)

    manifest = dict(drawing)
    manifest.update(
        {
            "source": {
                "file": "inspection/source-native.png",
                "problem_box": [0, 0, source_size[0], source_size[1]],
            },
            "semantic_check": (
                "Synthetic asymmetric projection with Korean labels, solid and dashed edges, "
                "and one five-landmark smooth guide curve."
            ),
            "trace_evidence": {
                "method": "synthetic native-pixel shared-junction trace",
                "inspection_artifact": "inspection/inspection.json",
                "inspection_sha256": _sha256(inspection_report),
                "observation_artifact": "source-observation.json",
                "observation_sha256": _sha256(observation_path),
                "source_sha256": _sha256(source_native),
                "source_size": source_size,
                "source_geometry_roi": observation["source_geometry_roi"],
                "max_normalized_error": 0.015,
                "points": drawing["points"],
            },
            "verification_evidence": {
                "kind": "independent-invariant",
                "description": "The synthetic fixture generator independently declares nine junctions.",
                "invariant": {"junction_count": 9},
                "observed": {"junction_count": len(drawing["points"])},
            },
        }
    )
    manifest_path = output / "trace.json"
    _write_json(manifest_path, manifest)

    observation_review = output / "observation-review"
    _script(
        "build_observation_overlay.py",
        str(source_native),
        str(observation_path),
        "--manifest",
        str(manifest_path),
        "--out",
        str(observation_review),
        cwd=output,
    )
    overlay_report = _read_json(observation_review / "observation-overlay.json")
    if not overlay_report.get("ok"):
        raise AssertionError(f"observation overlay failed: {overlay_report.get('unresolved')}")

    figure_svg = output / "figure.svg"
    figure_warm_svg = output / "figure-warm.svg"
    _script(
        "geometry_svg.py",
        str(manifest_path),
        str(figure_svg),
        "--report",
        str(output / "figure-render.json"),
        cwd=output,
    )
    _script("geometry_svg.py", str(manifest_path), str(figure_warm_svg), cwd=output)
    if _sha256(figure_svg) != _sha256(figure_warm_svg):
        raise AssertionError("unchanged geometry SVG rerender is not deterministic")

    comparison = output / "comparison.png"
    diagnostic = output / "diagnostic.png"
    comparison_report_path = output / "comparison.json"
    _script(
        "compare_geometry.py",
        str(source_native),
        str(figure_svg),
        "--manifest",
        str(manifest_path),
        "--out",
        str(comparison),
        "--diagnostic-out",
        str(diagnostic),
        "--report",
        str(comparison_report_path),
        cwd=output,
    )
    comparison_report = _read_json(comparison_report_path)
    if not comparison_report.get("ok"):
        raise AssertionError(f"synthetic source/redraw comparison failed: {comparison_report}")

    warm_comparison = output / "comparison-warm.png"
    warm_diagnostic = output / "diagnostic-warm.png"
    warm_report_path = output / "comparison-warm.json"
    _script(
        "compare_geometry.py",
        str(source_native),
        str(figure_svg),
        "--manifest",
        str(manifest_path),
        "--out",
        str(warm_comparison),
        "--diagnostic-out",
        str(warm_diagnostic),
        "--report",
        str(warm_report_path),
        cwd=output,
    )
    warm_report = _read_json(warm_report_path)
    if not warm_report.get("ok") or not warm_report.get("svg_raster_cache", {}).get("hit"):
        raise AssertionError("warm source comparison did not reuse the local SVG raster cache")
    if _pixel_sha256(comparison) != _pixel_sha256(warm_comparison):
        raise AssertionError("warm comparison pixels differ from the cold comparison")
    if _pixel_sha256(diagnostic) != _pixel_sha256(warm_diagnostic):
        raise AssertionError("warm diagnostic pixels differ from the cold diagnostic")

    initial_review = output / "visual-review-initial.png"
    initial_landmarks = output / "landmark-zooms-initial.png"
    initial_review_json = output / "visual-review-initial.json"
    _script(
        "build_visual_review.py",
        "--id",
        "portable-e2e",
        "--source",
        str(source_native),
        "--observation",
        str(observation_path),
        "--observation-overlay",
        str(observation_review / "observation-overlay.png"),
        "--redraw",
        str(figure_svg),
        "--comparison",
        str(comparison),
        "--diagnostic",
        str(diagnostic),
        "--comparison-report",
        str(comparison_report_path),
        "--out",
        str(initial_review),
        "--landmarks-out",
        str(initial_landmarks),
        "--review-template",
        str(initial_review_json),
        cwd=output,
    )
    _assert_pending_review(initial_review_json)

    worksheet_spec = {
        "schema_version": 1,
        "document": {
            "title": "복습 문제지",
            "subtitle": "원본 좌표 추적 · 휴대성 합성 검증",
            "student_fields": ["이름", "날짜"],
            "footer": "자동 생성 검수본 · 시각 확인 전",
        },
        "page": {"dpi": 144, "margin_mm": 14},
        "problems": [
            {
                "number": "1",
                "heading": "공간도형 관찰",
                "text": (
                    "다음 도형의 실선, 점선, 곡선과 표시된 길이를 살펴보고 "
                    "도형의 특징을 설명하세요."
                ),
                "figure": {
                    "path": "figure.svg",
                    "placement": "below",
                    "max_width_mm": 130,
                    "max_height_mm": 82,
                },
                "answer": {"label": "풀이", "lines": 5},
            }
        ],
    }
    worksheet_spec_path = output / "worksheet.json"
    _write_json(worksheet_spec_path, worksheet_spec)
    worksheet_a = output / "worksheet-a.png"
    worksheet_b = output / "worksheet-b.png"
    pdf_a = output / "worksheet-a.pdf"
    pdf_b = output / "worksheet-b.pdf"
    worksheet_report_a = output / "worksheet-a-report.json"
    worksheet_report_b = output / "worksheet-b-report.json"
    for png, pdf, report in (
        (worksheet_a, pdf_a, worksheet_report_a),
        (worksheet_b, pdf_b, worksheet_report_b),
    ):
        _script(
            "build_worksheet.py",
            str(worksheet_spec_path),
            "--png",
            str(png),
            "--pdf",
            str(pdf),
            "--report",
            str(report),
            cwd=output,
        )

    worksheet_a_report = _read_json(worksheet_report_a)
    worksheet_b_report = _read_json(worksheet_report_b)
    for report in (worksheet_a_report, worksheet_b_report):
        if not report.get("ok") or report.get("page_count") != 1:
            raise AssertionError("worksheet renderer did not produce exactly one valid page")
        font = report.get("font", {})
        if font.get("regular_source") != "bundled" or font.get("bold_source") != "bundled":
            raise AssertionError("worksheet renderer did not use bundled deterministic fonts")
        if Path(font.get("regular", "")).resolve() != REGULAR_FONT.resolve():
            raise AssertionError("worksheet regular font is not the bundled NanumGothic file")
        if Path(font.get("bold", "")).resolve() != BOLD_FONT.resolve():
            raise AssertionError("worksheet bold font is not the bundled NanumGothic file")
    page_hash_a = worksheet_a_report["pages"][0]["pixel_sha256"]
    page_hash_b = worksheet_b_report["pages"][0]["pixel_sha256"]
    if page_hash_a != page_hash_b or _pixel_sha256(worksheet_a) != _pixel_sha256(worksheet_b):
        raise AssertionError("unchanged worksheet rerender changed decoded page pixels")

    qa_a = output / "pdf-qa-a"
    qa_b = output / "pdf-qa-b"
    _script("qa_pdf.py", str(pdf_a), "--out", str(qa_a), cwd=output)
    _script("qa_pdf.py", str(pdf_b), "--out", str(qa_b), cwd=output)
    qa_report_a = _read_json(qa_a / "qa-report.json")
    qa_report_b = _read_json(qa_b / "qa-report.json")
    if not qa_report_a.get("ok") or not qa_report_b.get("ok"):
        raise AssertionError("worksheet PDF QA failed")
    pdf_pixel_a = qa_report_a["pages"][0]["pixel_sha256"]
    pdf_pixel_b = qa_report_b["pages"][0]["pixel_sha256"]
    if pdf_pixel_a != pdf_pixel_b:
        raise AssertionError("unchanged worksheet PDF changed decoded page pixels")
    if _sha256(pdf_a) != _sha256(pdf_b):
        raise AssertionError("unchanged worksheet rerender changed PDF bytes")

    final_review = output / "visual-review-final.png"
    final_landmarks = output / "landmark-zooms-final.png"
    final_review_json = output / "visual-review-final.json"
    _script(
        "build_visual_review.py",
        "--id",
        "portable-e2e",
        "--source",
        str(source_native),
        "--observation",
        str(observation_path),
        "--observation-overlay",
        str(observation_review / "observation-overlay.png"),
        "--redraw",
        str(figure_svg),
        "--comparison",
        str(comparison),
        "--diagnostic",
        str(diagnostic),
        "--comparison-report",
        str(comparison_report_path),
        "--final-crop",
        str(worksheet_a),
        "--out",
        str(final_review),
        "--landmarks-out",
        str(final_landmarks),
        "--review-template",
        str(final_review_json),
        cwd=output,
    )
    _assert_pending_review(final_review_json)

    viewer_artifacts = [
        ("source page with synthetic handwriting outside the retained crop", source_page),
        ("native geometry crop", source_native),
        ("native coordinate grid", inspection / "coordinate-grid.png"),
        ("named source observation overlay", observation_review / "observation-overlay.png"),
        ("source-first geometry ROI comparison", observation_review / "geometry-roi-vs-observation.png"),
        ("registered source/redraw comparison", comparison),
        ("named registered diagnostic", diagnostic),
        ("final visual review sheet", final_review),
        ("native landmark zoom sheet", final_landmarks),
        ("worksheet PNG", worksheet_a),
        ("current PDF page render", qa_a / "page-01.png"),
    ]
    for _role, path in viewer_artifacts:
        if not path.is_file() or not _is_within(path, output):
            raise AssertionError(f"viewer artifact escaped the isolated output root: {path}")

    for declarative in (observation_path, manifest_path, worksheet_spec_path):
        text = declarative.read_text(encoding="utf-8")
        if str(SKILL_ROOT) in text or "work/p38" in text.replace("\\", "/").lower():
            raise AssertionError(f"declarative fixture contains a machine-local skill/work path: {declarative}")

    report = {
        "schema_version": 1,
        "ok": True,
        "automated_pipeline_ok": True,
        "vision_status": "pending-vision-review",
        "vision_approval_fabricated": False,
        "skill_root": str(SKILL_ROOT),
        "output_root": str(output),
        "local_path_isolation": {
            "output_outside_skill_root": not _is_within(output, SKILL_ROOT),
            "all_viewer_artifacts_under_output_root": True,
            "declarative_inputs_are_skill_path_free": True,
            "local_resvg": str(SKILL_ROOT / "node_modules" / "@resvg" / "resvg-js"),
        },
        "determinism": {
            "svg_sha256": _sha256(figure_svg),
            "comparison_pixel_sha256": _pixel_sha256(comparison),
            "diagnostic_pixel_sha256": _pixel_sha256(diagnostic),
            "worksheet_pixel_sha256": page_hash_a,
            "pdf_page_pixel_sha256": pdf_pixel_a,
            "pdf_sha256": _sha256(pdf_a),
            "warm_svg_raster_cache_hit": True,
        },
        "viewer_required": [
            {"role": role, "path": str(path), "sha256": _sha256(path)}
            for role, path in viewer_artifacts
        ],
        "pending_reviews": [str(initial_review_json), str(final_review_json)],
        "required_action": (
            "A vision-capable reviewer must open every viewer_required image at original detail "
            "and complete one current hash-bound review; this automated test does not approve it."
        ),
    }
    _write_json(output / "portable-e2e-report.json", report)
    return report


class PortableEndToEndTests(unittest.TestCase):
    def test_portable_visual_pipeline_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mwr-portable-e2e-") as temporary:
            report = run_portable_e2e(Path(temporary) / "artifacts")
            self.assertTrue(report["ok"])
            self.assertEqual(report["vision_status"], "pending-vision-review")
            self.assertFalse(report["vision_approval_fabricated"])
            self.assertGreaterEqual(len(report["viewer_required"]), 10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new external artifact directory")
    args = parser.parse_args()
    try:
        report = run_portable_e2e(args.out)
    except (AssertionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
