#!/usr/bin/env python3
"""Regression smoke test for prism, internal line, curve, and prism-net rules."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from geometry_svg import polygon_on_edge, render_manifest
from validate_delivery import validate_delivery_manifest
from validate_geometry import svg_fit, validate_manifest


def save_case(directory: Path, name: str, manifest: dict) -> tuple[Path, Path, dict]:
    manifest_path = directory / f"{name}.json"
    svg_path = directory / f"{name}.svg"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    svg, render_report = render_manifest(manifest)
    svg_path.write_text(svg, encoding="utf-8")
    report = {
        "manifest": validate_manifest(manifest),
        "svg": svg_fit(svg),
        "render": render_report,
    }
    report["ok"] = report["manifest"]["ok"] and report["svg"]["ok"]
    return manifest_path, svg_path, report


def rasterize(path: Path) -> Image.Image:
    renderer = Path(__file__).with_name("render_svg.mjs")
    with tempfile.TemporaryDirectory(prefix="geometry-selftest-") as directory:
        output = Path(directory) / "render.png"
        subprocess.run(
            ["node", str(renderer), str(path), str(output), "2"],
            check=True,
            capture_output=True,
            text=True,
        )
        return Image.open(output).convert("RGB").copy()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    prism = {
        "type": "prism",
        "source": {"fixture": "synthetic-pentagonal-prism"},
        "semantic_check": "Five-gonal prism with one required internal segment and a curved dimension guide.",
        "canvas": {"width": 330, "height": 255},
        "prism": {"n": 5, "cx": 135, "cy": 62, "radius": 68, "y_scale": 0.43, "height": 120, "rotation": 90, "hidden": True},
        "segments": [
            {"name": "inside", "from": "T1", "to": "B3", "style": "emphasis"}
        ],
        "curves": [
            {"name": "height-guide", "points": [[225, 42], [244, 72], [251, 113], [248, 157], [229, 191]], "style": "guide", "smooth": True}
        ],
        "labels": [
            {"text": "ㄱ", "at": "T1", "offset": [-10, -8]},
            {"text": "20 cm", "at": [272, 116], "style": "measure"}
        ],
        "required_labels": ["ㄱ", "20 cm"],
        "required_segments": ["inside"],
        "expected": {"vertices": 10, "edges": 15, "faces": 7}
    }

    left, top, side, height = 55.0, 103.0, 40.0, 60.0
    rectangles = [
        [(left + i * side, top), (left + (i + 1) * side, top),
         (left + (i + 1) * side, top + height), (left + i * side, top + height)]
        for i in range(6)
    ]
    upper = polygon_on_edge(6, rectangles[1][0], rectangles[1][1], rectangles[1][2])
    lower = polygon_on_edge(6, rectangles[4][3], rectangles[4][2], rectangles[4][1])
    net = {
        "type": "net",
        "source": {"fixture": "synthetic-hexagonal-prism-net"},
        "semantic_check": "Six rectangles and two congruent hexagons; all shared edges are folds.",
        "canvas": {"width": 360, "height": 285},
        "faces": [{"name": f"side-{i}", "points": face} for i, face in enumerate(rectangles)] + [
            {"name": "top", "points": upper, "fill": "#f4f5f8"},
            {"name": "bottom", "points": lower, "fill": "#f4f5f8"}
        ],
        "expected": {"prism_n": 6, "faces": 8, "folds": 7, "boundary": 22}
    }

    trace = {
        "type": "trace",
        "source": {"fixture": "synthetic-asymmetric-pyramid-projection"},
        "semantic_check": "An asymmetric photographed projection must retain its observed landmark positions.",
        "canvas": {"width": 300, "height": 240},
        "points": {
            "A": [118, 20],
            "B0": [34, 162],
            "B1": [92, 145],
            "B2": [166, 148],
            "B3": [245, 174]
        },
        "segments": [
            {"name": "A-B0", "from": "A", "to": "B0", "style": "solid"},
            {"name": "A-B1", "from": "A", "to": "B1", "style": "solid"},
            {"name": "A-B2", "from": "A", "to": "B2", "style": "solid"},
            {"name": "A-B3", "from": "A", "to": "B3", "style": "solid"},
            {"name": "base-01", "from": "B0", "to": "B1", "style": "solid"},
            {"name": "base-12", "from": "B1", "to": "B2", "style": "solid"},
            {"name": "base-23", "from": "B2", "to": "B3", "style": "solid"}
        ],
        "required_segments": ["A-B0", "A-B1", "A-B2", "A-B3"],
        "trace_evidence": {
            "method": "synthetic pixel-landmark trace",
            "inspection_artifact": "synthetic-inspection.png",
            "source_size": [600, 480],
            "max_normalized_error": 0.015,
            "points": {
                "A": [236, 40],
                "B0": [68, 324],
                "B1": [184, 290],
                "B2": [332, 296],
                "B3": [490, 348]
            }
        }
    }

    reports = {}
    rendered = []
    for name, manifest in (
        ("pentagonal-prism", prism),
        ("hexagonal-net", net),
        ("asymmetric-trace", trace),
    ):
        _, svg_path, report = save_case(args.out, name, manifest)
        reports[name] = report
        rendered.append((name, rasterize(svg_path)))

    panel_width = max(image.width for _, image in rendered)
    panel_height = max(image.height for _, image in rendered)
    sheet = Image.new("RGB", (panel_width * len(rendered), panel_height + 36), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (name, image) in enumerate(rendered):
        x = index * panel_width + (panel_width - image.width) // 2
        sheet.paste(image, (x, 36))
        draw.text((index * panel_width + 10, 10), name, fill="#28334a", font=font)
    sheet_path = args.out / "selftest-contact-sheet.png"
    sheet.save(sheet_path)

    pdf_path = args.out / "selftest-delivery.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(pdf_path)
    qa_path = args.out / "selftest-pdf-qa.json"
    qa_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    delivery = {
        "pdf": str(pdf_path),
        "pdf_qa_report": str(qa_path),
        "all_pages_inspected": True,
        "problems": [{
            "id": "fixture-01",
            "source_crop": str(sheet_path),
            "comparison_artifact": str(sheet_path),
            "final_page_render": str(sheet_path),
            "final_page": 1,
            "text_match": True,
            "figure_match": True,
            "line_styles_match": True,
            "labels_dimensions_match": True,
            "no_handwriting": True,
            "no_clipping": True,
            "inspected_latest_pdf": True,
        }],
    }
    delivery_report = validate_delivery_manifest(delivery, args.out)
    bad_delivery = json.loads(json.dumps(delivery))
    bad_delivery["problems"][0]["figure_match"] = False
    bad_delivery_report = validate_delivery_manifest(bad_delivery, args.out)

    missing_segment = json.loads(json.dumps(prism))
    missing_segment["required_segments"] = ["inside", "must-not-exist"]
    missing_segment_report = validate_manifest(missing_segment)
    missing_comparison = json.loads(json.dumps(prism))
    missing_comparison["source"] = {"file": "source-page.jpg", "box": [0, 0, 100, 100]}
    missing_comparison_report = validate_manifest(missing_comparison)
    missing_trace = json.loads(json.dumps(trace))
    missing_trace.pop("trace_evidence")
    missing_trace_report = validate_manifest(missing_trace)
    idealized_trace = json.loads(json.dumps(trace))
    idealized_trace["points"]["B1"] = [125, 145]
    idealized_trace_report = validate_manifest(idealized_trace)
    inflated_tolerance = json.loads(json.dumps(trace))
    inflated_tolerance["trace_evidence"]["max_normalized_error"] = 0.25
    inflated_tolerance_report = validate_manifest(inflated_tolerance)
    clipped_svg_report = svg_fit(
        '<svg viewBox="0 0 100 100"><line x1="-8" y1="10" x2="50" y2="50"/></svg>'
    )
    negative_gates = {
        "missing_required_segment_rejected": not missing_segment_report["ok"],
        "missing_source_comparison_rejected": not missing_comparison_report["ok"],
        "missing_trace_evidence_rejected": not missing_trace_report["ok"],
        "idealized_projection_rejected": not idealized_trace_report["ok"],
        "inflated_trace_tolerance_rejected": not inflated_tolerance_report["ok"],
        "clipped_svg_rejected": not clipped_svg_report["ok"],
        "unmatched_final_figure_rejected": not bad_delivery_report["ok"],
    }

    all_ok = (
        all(report["ok"] for report in reports.values())
        and delivery_report["ok"]
        and all(negative_gates.values())
    )
    output = {
        "ok": all_ok,
        "cases": reports,
        "delivery_gate": delivery_report,
        "negative_gates": negative_gates,
        "contact_sheet": str(sheet_path),
    }
    (args.out / "selftest-report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
