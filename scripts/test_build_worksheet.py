#!/usr/bin/env python3
"""Focused tests for the portable JSON worksheet renderer."""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_worksheet import WorksheetError, _render_svg, build_worksheet


GENERIC_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="420" height="260" viewBox="0 0 420 260">
  <g fill="none" stroke="#26364a" stroke-width="5" stroke-linejoin="round">
    <path d="M60 210 L210 45 L360 210 Z"/>
    <path d="M135 128 L285 128" stroke-dasharray="12 9"/>
  </g>
  <circle cx="210" cy="45" r="7" fill="#26364a"/>
</svg>
"""


class WorksheetRendererTests(unittest.TestCase):
    def _write_fixture(self, root: Path, *, force_second_page: bool = False) -> Path:
        (root / "figure.svg").write_text(GENERIC_SVG, encoding="utf-8")
        problems: list[dict[str, object]] = [
            {
                "number": "1",
                "heading": "도형 복습",
                "text": "그림의 선분을 살펴보고 물음에 답하세요. 점선과 실선을 구분하여 쓰세요.",
                "figure": {
                    "path": "figure.svg",
                    "placement": "below",
                    "max_width_mm": 112,
                    "max_height_mm": 62,
                },
                "answer": {"label": "풀이", "lines": 4, "line_gap_mm": 8},
            }
        ]
        if force_second_page:
            problems.append(
                {
                    "badge": "개념 탐구",
                    "text": "정사각형의 변과 꼭짓점을 세어 보세요.",
                    "start_on_new_page": True,
                    "answer": {"label": "생각", "lines": 3},
                }
            )
        spec = {
            "schema_version": 1,
            "document": {
                "title": "도형 복습 문제지",
                "subtitle": "그림을 보고 차근차근 풀어 보세요.",
                "student_fields": ["이름", "날짜"],
                "footer": "복습용",
            },
            "page": {"dpi": 100, "margin_mm": 14},
            "problems": problems,
        }
        spec_path = root / "worksheet.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        return spec_path

    def test_single_page_png_pdf_and_content_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-render-") as temp:
            root = Path(temp)
            spec_path = self._write_fixture(root)
            output = root / "worksheet.png"
            pdf = root / "worksheet.pdf"
            report_path = root / "report.json"
            report = build_worksheet(
                spec_path,
                output,
                pdf_path=pdf,
                report_path=report_path,
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["page_count"], 1)
            self.assertEqual(report["page_size_px"], [827, 1169])
            self.assertTrue(output.is_file())
            self.assertTrue(pdf.is_file())
            self.assertTrue(report_path.is_file())
            self.assertEqual(len(report["figures"]), 1)
            self.assertTrue(Path(report["font"]["regular"]).is_file())
            self.assertEqual(report["font"]["regular_source"], "bundled")
            self.assertEqual(report["font"]["bold_source"], "bundled")
            self.assertEqual(len(report["font"]["regular_sha256"]), 64)
            self.assertEqual(report["pdf"]["mode"], "reviewed-page-raster-embed")
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["report"], str(report_path.resolve()))

            with Image.open(output) as page:
                self.assertEqual(page.size, (827, 1169))
                ink = ImageOps.grayscale(page).point(
                    lambda value: 255 if value < 245 else 0, mode="L"
                ).getbbox()
                self.assertIsNotNone(ink)
                assert ink is not None
                self.assertGreater(ink[0], 20)
                self.assertLess(ink[2], page.width - 20)
                # The centered SVG contributes dark pixels in the middle of the page.
                middle = ImageOps.grayscale(page).crop((180, 280, 650, 720))
                self.assertLess(middle.getextrema()[0], 100)

            with pymupdf.open(pdf) as document:
                self.assertEqual(document.page_count, 1)
                self.assertAlmostEqual(document[0].rect.width, 595.276, places=2)
                self.assertAlmostEqual(document[0].rect.height, 841.89, places=2)

    def test_svg_dash_pattern_survives_placement_rasterization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-svg-") as temp:
            root = Path(temp)
            source = root / "figure.svg"
            source.write_text(GENERIC_SVG, encoding="utf-8")
            rendered = _render_svg(source, (420, 260)).convert("RGB")
            gray = ImageOps.grayscale(rendered)
            samples = [gray.getpixel((x, 128)) for x in range(140, 281)]
            self.assertTrue(any(value < 100 for value in samples))
            self.assertTrue(any(value > 245 for value in samples))

    def test_forced_multi_page_uses_numbered_png_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-pages-") as temp:
            root = Path(temp)
            spec_path = self._write_fixture(root, force_second_page=True)
            report = build_worksheet(spec_path, root / "review.png")
            self.assertEqual(report["page_count"], 2)
            self.assertEqual(
                [Path(page["path"]).name for page in report["pages"]],
                ["review-01.png", "review-02.png"],
            )
            self.assertFalse((root / "review.png").exists())
            self.assertTrue((root / "review-01.png").is_file())
            self.assertTrue((root / "review-02.png").is_file())

    def test_oversized_problem_is_rejected_instead_of_clipped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-oversize-") as temp:
            root = Path(temp)
            spec_path = self._write_fixture(root)
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["problems"][0]["figure"]["max_height_mm"] = 110
            spec["problems"][0]["answer"] = {
                "label": "풀이",
                "lines": 20,
                "line_gap_mm": 10,
            }
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(WorksheetError, "taller than one printable page"):
                build_worksheet(spec_path, root / "oversized.png")

    def test_fields_only_header_is_counted_before_pagination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-fields-header-") as temp:
            root = Path(temp)
            spec = {
                "schema_version": 1,
                "document": {"student_fields": ["이름", "날짜"]},
                "page": {"dpi": 100, "margin_mm": 14},
                "problems": [
                    {
                        "number": "1",
                        "text": "풀이 과정을 쓰세요.",
                        "answer": {"lines": 20, "line_gap_mm": 11.5},
                    }
                ],
            }
            spec_path = root / "worksheet.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(WorksheetError, "taller than one printable page"):
                build_worksheet(spec_path, root / "worksheet.png")

    def test_output_paths_cannot_overwrite_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worksheet-path-collision-") as temp:
            root = Path(temp)
            figure = root / "figure.png"
            Image.new("RGB", (120, 80), "white").save(figure)
            spec = {
                "schema_version": 1,
                "problems": [
                    {
                        "number": "1",
                        "text": "그림을 살펴보세요.",
                        "figure": {"path": "figure.png"},
                    }
                ],
            }
            spec_path = root / "worksheet.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            original_figure = hashlib.sha256(figure.read_bytes()).hexdigest()
            original_spec = hashlib.sha256(spec_path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(WorksheetError, "would overwrite figure"):
                build_worksheet(spec_path, figure)
            self.assertEqual(hashlib.sha256(figure.read_bytes()).hexdigest(), original_figure)

            with self.assertRaisesRegex(WorksheetError, "would overwrite worksheet spec"):
                build_worksheet(
                    spec_path,
                    root / "worksheet.png",
                    report_path=spec_path,
                )
            self.assertEqual(hashlib.sha256(spec_path.read_bytes()).hexdigest(), original_spec)
            self.assertFalse((root / "worksheet.png").exists())


if __name__ == "__main__":
    unittest.main()
