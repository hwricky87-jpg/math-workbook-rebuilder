#!/usr/bin/env python3
"""Regression test for geometry, projection, multigraph, artifact, and PDF gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pymupdf
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from compare_geometry import (
    RedrawEvidence,
    SourceEvidence,
    ink_alignment,
    rasterize_svg,
    residual_line_report,
    segment_source_support,
    trace_routing_report,
)
from geometry_svg import polygon_on_edge, render_manifest
from imgprobe import cut_crossings, ring_strands, stability_report
from preprocess_geometry import (
    coordinate_grid,
    coordinate_grid_step,
    inspection_size,
    sheet_preview_size,
)
from process_utils import run_utf8
from validate_delivery import validate_delivery_manifest
from validate_geometry import (
    component_inventory_report,
    graph_structure,
    svg_fit,
    validate_artifact_evidence,
    validate_graph_probe_artifacts,
    validate_manifest,
    verification_report,
)


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
        run_utf8(
            ["node", str(renderer), str(path), str(output), "2"],
            check=True,
            capture_output=True,
        )
        return Image.open(output).convert("RGB").copy()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
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

    graph_curves = [
        {"name": "KN-1", "points": ["K", [88, 64], "N"], "style": "solid"},
        {"name": "KN-2", "points": ["K", [101, 82], "N"], "style": "solid"},
        {"name": "KN-3", "points": ["K", [114, 99], "N"], "style": "solid"},
        {"name": "KR-1", "points": ["K", [95, 145], "R"], "style": "solid"},
        {"name": "KR-2", "points": ["K", [111, 166], "R"], "style": "solid"},
        {"name": "NR-1", "points": ["N", [132, 117], "R"], "style": "solid"},
        {"name": "NR-2", "points": ["N", [158, 120], "R"], "style": "solid"},
        {"name": "ND-1", "points": ["N", [193, 61], "D"], "style": "solid"},
        {"name": "DR-1", "points": ["D", [219, 120], "R"], "style": "solid"},
        {"name": "DR-2", "points": ["D", [205, 139], "R"], "style": "solid"},
        {"name": "DR-3", "points": ["D", [191, 157], "R"], "style": "solid"},
        {"name": "DR-4", "points": ["D", [178, 174], "R"], "style": "solid"},
    ]
    graph = {
        "type": "graph",
        "source": {"fixture": "synthetic-multigraph"},
        "semantic_check": "A four-vertex multigraph whose measured degrees uniquely reject an undercount.",
        "canvas": {"width": 300, "height": 240},
        "points": {"K": [45, 120], "N": [140, 35], "D": [255, 95], "R": [150, 205]},
        "curves": graph_curves,
        "labels": [
            {"text": "K", "at": "K", "offset": [-14, 0]},
            {"text": "N", "at": "N", "offset": [0, -14]},
            {"text": "D", "at": "D", "offset": [14, 0]},
            {"text": "R", "at": "R", "offset": [0, 16]},
        ],
        "graph_evidence": {
            "method": "independent ring-degree and midpoint-cut measurements",
            "inspection_artifact": "synthetic-graph-inspection.png",
            "source_size": [600, 480],
            "source_sha256": "0" * 64,
            "inspection_sha256": "1" * 64,
            "vertices": {
                "K": {"degree": 5, "probe_report": "K-degree.json"},
                "N": {"degree": 6, "probe_report": "N-degree.json"},
                "D": {"degree": 5, "probe_report": "D-degree.json"},
                "R": {"degree": 8, "probe_report": "R-degree.json"},
            },
            "edge_groups": [
                {"vertices": ["K", "N"], "multiplicity": 3,
                 "drawn_elements": ["KN-1", "KN-2", "KN-3"], "probe_report": "KN-cut.json"},
                {"vertices": ["K", "R"], "multiplicity": 2,
                 "drawn_elements": ["KR-1", "KR-2"], "probe_report": "KR-cut.json"},
                {"vertices": ["N", "R"], "multiplicity": 2,
                 "drawn_elements": ["NR-1", "NR-2"], "probe_report": "NR-cut.json"},
                {"vertices": ["N", "D"], "multiplicity": 1,
                 "drawn_elements": ["ND-1"], "probe_report": "ND-cut.json"},
                {"vertices": ["D", "R"], "multiplicity": 4,
                 "drawn_elements": ["DR-1", "DR-2", "DR-3", "DR-4"],
                 "probe_report": "DR-cut.json"},
            ],
        },
    }

    readability = {
        "type": "trace",
        "source": {"fixture": "synthetic-print-readability"},
        "semantic_check": "A labeled segment remains legible after the standard 2x raster render.",
        "canvas": {"width": 240, "height": 120},
        "points": {"L": [30, 85], "R": [210, 85]},
        "segments": [
            {"name": "readable-line", "from": "L", "to": "R", "style": "solid"}
        ],
        "labels": [
            {"text": "A", "at": [60, 42]},
            {"text": "ㄱ", "at": [180, 42]},
        ],
        "trace_evidence": {
            "method": "synthetic pixel-landmark trace",
            "inspection_artifact": "synthetic-readability-inspection.png",
            "source_size": [240, 120],
            "max_normalized_error": 0.015,
            "points": {"L": [30, 85], "R": [210, 85]},
        },
    }

    reports = {}
    rendered = []
    svg_paths: dict[str, Path] = {}
    for name, manifest in (
        ("pentagonal-prism", prism),
        ("hexagonal-net", net),
        ("asymmetric-trace", trace),
        ("measured-multigraph", graph),
        ("print-readability", readability),
    ):
        _, svg_path, report = save_case(args.out, name, manifest)
        svg_paths[name] = svg_path
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
    duplicate_style_segment = json.loads(json.dumps(trace))
    duplicate_style_segment["segments"].append({
        "name": "A-B0-hidden-duplicate", "from": "A", "to": "B0", "style": "dashed"
    })
    duplicate_style_segment_report = validate_manifest(duplicate_style_segment)
    undercounted_graph = json.loads(json.dumps(graph))
    undercounted_graph["graph_evidence"]["edge_groups"][0]["multiplicity"] = 2
    undercounted_graph["graph_evidence"]["edge_groups"][0]["drawn_elements"] = ["KN-1", "KN-2"]
    undercounted_graph_report = graph_structure(undercounted_graph)
    incomplete_degrees = json.loads(json.dumps(graph))
    incomplete_degrees["graph_evidence"]["vertices"]["K"].pop("degree")
    incomplete_degrees_report = graph_structure(incomplete_degrees)
    reused_drawn = json.loads(json.dumps(graph))
    reused_drawn["graph_evidence"]["edge_groups"][1]["drawn_elements"][0] = "KN-1"
    reused_drawn_report = graph_structure(reused_drawn)
    self_referential = verification_report({
        "verification_evidence": {
            "kind": "reconstructed-answer",
            "description": "Expected value was calculated from the same reconstructed edges.",
        }
    })
    clipped_svg_report = svg_fit(
        '<svg viewBox="0 0 100 100"><line x1="-8" y1="10" x2="50" y2="50"/></svg>'
    )
    defs_pattern_report = svg_fit(
        '<svg viewBox="0 0 100 100"><defs><pattern id="stip" width="5" height="5" '
        'patternUnits="userSpaceOnUse"><rect x="0" y="0" width="5" height="5"/>'
        '<circle cx="1" cy="1" r="1"/></pattern></defs>'
        '<rect x="10" y="10" width="80" height="80" fill="url(#stip)"/></svg>'
    )

    radial_image = Image.new("L", (201, 201), 255)
    radial_draw = ImageDraw.Draw(radial_image)
    for endpoint in ((180, 100), (20, 100), (100, 20), (100, 180)):
        radial_draw.line((100, 100, *endpoint), fill=0, width=2)
    radial_array = np.asarray(radial_image, dtype=np.float32)
    ring_probe_ok = len(ring_strands(radial_array, 100, 100, 36, 128)) == 4
    radial_path = args.out / "ring-probe-source.png"
    radial_report_path = args.out / "ring-probe-report.json"
    radial_image.save(radial_path)
    run_utf8([
        sys.executable, str(Path(__file__).with_name("imgprobe.py")), str(radial_path),
        "--deg", "100", "100", "--radii", "28", "32", "36", "40",
        "--stable-samples", "3", "--thr", "128", "--report", str(radial_report_path),
    ], check=True, capture_output=True)

    cut_image = Image.new("L", (201, 201), 255)
    cut_draw = ImageDraw.Draw(cut_image)
    for y in (94, 100, 106):
        cut_draw.line((20, y, 180, y), fill=0, width=2)
    cut_array = np.asarray(cut_image, dtype=np.float32)
    cut_probe_ok = cut_crossings(cut_array, (20, 100), (180, 100), 128, 18) == 3
    cut_path = args.out / "cut-probe-source.png"
    cut_report_path = args.out / "cut-probe-report.json"
    cut_image.save(cut_path)
    run_utf8([
        sys.executable, str(Path(__file__).with_name("imgprobe.py")), str(cut_path),
        "--cut", "20", "100", "180", "100", "--half-lengths", "12", "15", "18", "21",
        "--stable-samples", "3", "--thr", "128", "--report", str(cut_report_path),
    ], check=True, capture_output=True)
    imgprobe_cli_ok = (
        json.loads(radial_report_path.read_text(encoding="utf-8"))["degree"]["stable_count"] == 4
        and json.loads(cut_report_path.read_text(encoding="utf-8"))["cut"]["stable_count"] == 3
    )
    utf8_probe = run_utf8(
        ["node", "-e", "process.stdout.write('한글 경로 UTF-8')"],
        check=True,
        capture_output=True,
    )
    stability_probe_ok = (
        stability_report([5, 5, 5, 6], 3)["stable_count"] == 5
        and stability_report([5, 5, 6, 6], 3)["ok"] is False
        and stability_report([5, 5, 5, 6, 6, 6], 3)["ok"] is False
    )

    ink_source = Image.new("L", (240, 180), 245)
    ink_source_draw = ImageDraw.Draw(ink_source)
    ink_source_draw.rectangle((35, 30, 205, 150), outline=20, width=4)
    ink_redraw_good = Image.new("L", (240, 180), 255)
    ImageDraw.Draw(ink_redraw_good).rectangle((35, 30, 205, 150), outline=20, width=3)
    ink_redraw_bad = Image.new("L", (240, 180), 255)
    ImageDraw.Draw(ink_redraw_bad).rectangle((75, 55, 225, 170), outline=20, width=3)
    ink_alignment_good = ink_alignment(ink_source, ink_redraw_good, minimum_coverage=0.90)
    ink_alignment_bad = ink_alignment(ink_source, ink_redraw_bad, minimum_coverage=0.90)

    omission_source = ink_source.copy()
    ImageDraw.Draw(omission_source).line((35, 30, 205, 150), fill=20, width=4)
    omission_redraw_missing = ink_redraw_good.copy()
    omission_redraw_complete = ink_redraw_good.copy()
    ImageDraw.Draw(omission_redraw_complete).line((35, 30, 205, 150), fill=20, width=3)
    omission_one_way = ink_alignment(
        omission_source, omission_redraw_missing, minimum_coverage=0.90
    )
    omitted_line_report = residual_line_report(omission_source, omission_redraw_missing)
    complete_line_report = residual_line_report(omission_source, omission_redraw_complete)

    chord_source = Image.new("L", (240, 180), 245)
    chord_draw = ImageDraw.Draw(chord_source)
    chord_draw.line((35, 150, 120, 90), fill=20, width=4)
    chord_draw.line((120, 90, 35, 30), fill=20, width=4)
    chord_manifest = {
        "type": "trace",
        "segments": [
            {"name": "A-I", "from": "A", "to": "I", "style": "solid"},
            {"name": "I-G", "from": "I", "to": "G", "style": "solid"},
        ],
        "trace_evidence": {
            "source_size": [240, 180],
            "points": {"A": [35, 150], "I": [120, 90], "G": [35, 30]},
        },
    }
    chord_manifest_wrong = json.loads(json.dumps(chord_manifest))
    chord_manifest_wrong["segments"] = [
        {"name": "A-G-invented", "from": "A", "to": "G", "style": "solid"}
    ]
    segment_support_good = segment_source_support(chord_source, chord_manifest)
    segment_support_bad = segment_source_support(chord_source, chord_manifest_wrong)
    stroke_limited_support = segment_source_support(
        chord_source,
        chord_manifest,
        radius_ratio=0.04,
    )

    flat_shading_source = Image.new("L", (240, 180), 245)
    ImageDraw.Draw(flat_shading_source).rectangle((20, 65, 220, 115), fill=160)
    flat_shading_manifest = {
        "type": "trace",
        "segments": [
            {"name": "flat-shading-is-not-an-edge", "from": "A", "to": "B", "style": "solid"}
        ],
        "trace_evidence": {
            "source_size": [240, 180],
            "points": {"A": [30, 90], "B": [210, 90]},
        },
    }
    flat_shading_report = segment_source_support(flat_shading_source, flat_shading_manifest)

    faint_line_source = Image.new("L", (240, 180), 245)
    ImageDraw.Draw(faint_line_source).line((30, 90, 210, 90), fill=190, width=2)
    faint_line_report = segment_source_support(faint_line_source, flat_shading_manifest)

    prepared_source = SourceEvidence.prepare(ink_source)
    prepared_redraw = RedrawEvidence.prepare(ink_redraw_good, ink_source.size)
    cached_ink_alignment = ink_alignment(
        ink_source,
        ink_redraw_good,
        minimum_coverage=0.90,
        source_evidence=prepared_source,
        redraw_evidence=prepared_redraw,
    )
    cached_residual = residual_line_report(
        omission_source,
        omission_redraw_complete,
        source_evidence=SourceEvidence.prepare(omission_source),
        redraw_evidence=RedrawEvidence.prepare(omission_redraw_complete, omission_source.size),
    )
    shared_chord_source = SourceEvidence.prepare(chord_source)
    ink_alignment(
        chord_source,
        chord_source,
        source_evidence=shared_chord_source,
        redraw_evidence=RedrawEvidence.prepare(chord_source, chord_source.size),
    )
    segment_source_support(
        chord_source,
        chord_manifest,
        source_evidence=shared_chord_source,
    )

    with tempfile.TemporaryDirectory(prefix="geometry-svg-cache-selftest-") as cache_directory:
        first_cache_stats: dict[str, object] = {}
        second_cache_stats: dict[str, object] = {}
        first_cached_raster = rasterize_svg(
            svg_paths["asymmetric-trace"],
            cache_dir=Path(cache_directory),
            cache_stats=first_cache_stats,
        )
        second_cached_raster = rasterize_svg(
            svg_paths["asymmetric-trace"],
            cache_dir=Path(cache_directory),
            cache_stats=second_cache_stats,
        )
        svg_cache_pixels_equal = np.array_equal(
            np.asarray(first_cached_raster), np.asarray(second_cached_raster)
        )

    readability_image = dict(rendered)["print-readability"]
    readability_array = np.asarray(readability_image.convert("L"), dtype=np.uint8)
    line_column = readability_array[150:190, 240]
    dark_positions = np.flatnonzero(line_column < 190)
    readable_line_thickness = 0
    if dark_positions.size:
        runs = np.split(dark_positions, np.where(np.diff(dark_positions) > 1)[0] + 1)
        readable_line_thickness = max(len(run) for run in runs)
    latin_label_ink = int((readability_array[35:100, 75:165] < 190).sum())
    hangul_label_ink = int((readability_array[35:100, 315:405] < 190).sum())
    adaptive_size, adaptive_scale, adaptive_mode = inspection_size(1080, 840)
    explicit_size, explicit_scale, explicit_mode = inspection_size(
        1080, 840, explicit_scale=2
    )
    grid_step = coordinate_grid_step(2.0)
    grid_image, generated_grid_step = coordinate_grid(
        Image.new("RGB", (200, 160), "white"),
        (100, 80),
        2.0,
    )
    preview_size = sheet_preview_size(2200, 1711)

    invalid_style_manifest = json.loads(json.dumps(readability))
    invalid_style_manifest["render_style"] = {"solid_width": 0.2}
    invalid_style_report = validate_manifest(invalid_style_manifest)
    invalid_style_rejected = False
    try:
        render_manifest(invalid_style_manifest)
    except ValueError:
        invalid_style_rejected = True
    invalid_style_rejected = invalid_style_rejected and (
        not invalid_style_report["ok"]
        and any("invalid render_style" in error for error in invalid_style_report["errors"])
    )

    real_source_coordinate_diagram = {
        "type": "diagram",
        "source": {"file": "generic-coordinate-source.png"},
        "semantic_check": "A real photographed coordinate drawing must not bypass trace verification.",
        "canvas": {"width": 120, "height": 100},
        "points": {"A": [20, 20], "B": [100, 80]},
        "segments": [{"name": "A-B", "from": "A", "to": "B", "style": "solid"}],
        "verification_evidence": {
            "kind": "independent-invariant",
            "description": "The synthetic routing case contains one observed segment.",
        },
        "source_comparison": {
            "artifact": "generic-coordinate-comparison.png",
            "report": "generic-coordinate-comparison.json",
            "review": "generic-coordinate-review.json",
            "review_sha256": "0" * 64,
            "text_match": True,
            "topology_match": True,
            "line_styles_match": True,
            "labels_match": True,
            "dimensions_match": True,
            "projection_match": True,
            "inspected_latest_render": True,
        },
    }
    real_source_coordinate_diagram_report = validate_manifest(
        real_source_coordinate_diagram
    )
    real_source_coordinate_routing_report = trace_routing_report(
        real_source_coordinate_diagram
    )
    composite_points = {
        "stage-A": [10, 20], "stage-B": [40, 20],
        "choice-A": [60, 70], "choice-B": [90, 70],
    }
    composite_manifest = {
        "type": "trace",
        "source": {"fixture": "synthetic-stage-and-choice-composite"},
        "semantic_check": "Both the given construction stage and the answer choice are inventoried.",
        "canvas": {"width": 100, "height": 100},
        "points": composite_points,
        "segments": [
            {"name": "stage-edge", "from": "stage-A", "to": "stage-B", "style": "solid"},
            {"name": "choice-edge", "from": "choice-A", "to": "choice-B", "style": "solid"},
        ],
        "component_inventory": [
            {"id": "stage", "point_count": 2, "segment_count": 1},
            {"id": "choice", "point_count": 2, "segment_count": 1},
        ],
        "trace_evidence": {
            "source_size": [100, 100],
            "max_normalized_error": 0.02,
            "points": composite_points,
        },
        "verification_evidence": {
            "invariant": {"total_components": 2},
        },
    }
    complete_composite_report = component_inventory_report(composite_manifest)
    incomplete_composite = json.loads(json.dumps(composite_manifest))
    incomplete_composite["segments"] = incomplete_composite["segments"][:1]
    incomplete_composite_report = component_inventory_report(incomplete_composite)

    graph_source_path = args.out / "graph-probe-source.png"
    graph_inspection_path = args.out / "graph-probe-inspection.png"
    Image.new("L", (120, 100), 255).save(graph_source_path)
    Image.new("L", (240, 200), 255).save(graph_inspection_path)
    source_hash = hashlib.sha256(graph_source_path.read_bytes()).hexdigest()
    inspection_hash = hashlib.sha256(graph_inspection_path.read_bytes()).hexdigest()
    trace_without_observation = json.loads(json.dumps(trace))
    trace_without_observation["source"] = {"file": graph_source_path.name}
    trace_without_observation["trace_evidence"]["inspection_artifact"] = graph_inspection_path.name
    trace_without_observation["trace_evidence"]["source_size"] = [120, 100]
    trace_without_observation["source_comparison"] = {
        "artifact": graph_inspection_path.name,
        "report": "missing-trace-comparison.json",
        "review": "missing-trace-review.json",
    }
    trace_without_observation_path = args.out / "trace-without-observation.json"
    trace_without_observation_report = validate_artifact_evidence(
        trace_without_observation, trace_without_observation_path, None
    )
    artifact_graph = json.loads(json.dumps(graph))
    artifact_evidence = artifact_graph["graph_evidence"]
    artifact_evidence["source_size"] = [120, 100]
    artifact_evidence["source_sha256"] = source_hash
    artifact_evidence["inspection_artifact"] = graph_inspection_path.name
    artifact_evidence["inspection_sha256"] = inspection_hash
    for name, record in artifact_evidence["vertices"].items():
        report_path = args.out / f"{name}-degree.json"
        record["probe_report"] = report_path.name
        report_path.write_text(json.dumps({
            "ok": True,
            "source_sha256": source_hash,
            "image_size": [120, 100],
            "degree": {
                "ok": True,
                "stable_count": record["degree"],
                "minimum_samples": 3,
                "counts": [record["degree"]] * 3,
            },
        }), encoding="utf-8")
    for group in artifact_evidence["edge_groups"]:
        edge_name = "-".join(group["vertices"])
        report_path = args.out / f"{edge_name}-cut.json"
        group["probe_report"] = report_path.name
        report_path.write_text(json.dumps({
            "ok": True,
            "source_sha256": source_hash,
            "image_size": [120, 100],
            "cut": {
                "ok": True,
                "stable_count": group["multiplicity"],
                "minimum_samples": 3,
                "counts": [group["multiplicity"]] * 3,
            },
        }), encoding="utf-8")
    graph_artifact_report = validate_graph_probe_artifacts(
        artifact_graph, args.out, graph_source_path
    )
    graph_svg_path = args.out / "measured-multigraph.svg"
    comparison_report_path = args.out / "graph-source-comparison.json"
    comparison_report_path.write_text(json.dumps({
        "ok": True,
        "source_sha256": source_hash,
        "redraw_sha256": hashlib.sha256(graph_svg_path.read_bytes()).hexdigest(),
        "artifact_sha256": inspection_hash,
        "ink_alignment": {"ok": True},
        "source_residual": {"ok": True},
        "segment_source_support": {"ok": True, "applied": True},
    }), encoding="utf-8")
    artifact_graph["source"] = {"file": graph_source_path.name}
    artifact_graph["verification_evidence"] = {
        "kind": "independent-measurement",
        "description": "Ring degrees and midpoint cuts constrain every edge multiplicity.",
    }
    artifact_graph["source_comparison"] = {
        "artifact": graph_inspection_path.name,
        "report": comparison_report_path.name,
        "review": "graph-source-comparison-review.json",
        "text_match": True,
        "topology_match": True,
        "line_styles_match": True,
        "labels_match": True,
        "dimensions_match": True,
        "projection_match": True,
        "inspected_latest_render": True,
    }
    review_path = args.out / artifact_graph["source_comparison"]["review"]
    review_path.write_text(json.dumps({
        "source_sha256": source_hash,
        "redraw_sha256": hashlib.sha256(graph_svg_path.read_bytes()).hexdigest(),
        "artifact_sha256": inspection_hash,
        "report_sha256": hashlib.sha256(comparison_report_path.read_bytes()).hexdigest(),
        "checks": {
            "text_match": True,
            "topology_match": True,
            "line_styles_match": True,
            "labels_match": True,
            "dimensions_match": True,
            "projection_match": True,
            "inspected_latest_render": True,
        },
    }), encoding="utf-8")
    artifact_graph["source_comparison"]["review_sha256"] = hashlib.sha256(
        review_path.read_bytes()
    ).hexdigest()
    artifact_manifest_path = args.out / "measured-multigraph-real-source.json"
    artifact_manifest_path.write_text(
        json.dumps(artifact_graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    full_graph_manifest_report = validate_manifest(artifact_graph)
    full_graph_artifact_report = validate_artifact_evidence(
        artifact_graph, artifact_manifest_path, graph_svg_path
    )
    stale_graph = json.loads(json.dumps(artifact_graph))
    stale_graph["graph_evidence"]["source_sha256"] = "f" * 64
    stale_graph_report = validate_graph_probe_artifacts(stale_graph, args.out, graph_source_path)
    tampered_graph = json.loads(json.dumps(artifact_graph))
    tampered_path = args.out / "tampered-K-degree.json"
    tampered_graph["graph_evidence"]["vertices"]["K"]["probe_report"] = tampered_path.name
    tampered_path.write_text(json.dumps({
        "ok": True,
        "source_sha256": source_hash,
        "image_size": [120, 100],
        "degree": {
            "ok": True,
            "stable_count": 5,
            "minimum_samples": 3,
            "counts": [2, 2, 2],
        },
    }), encoding="utf-8")
    tampered_graph_report = validate_graph_probe_artifacts(
        tampered_graph, args.out, graph_source_path
    )
    negative_gates = {
        "missing_required_segment_rejected": not missing_segment_report["ok"],
        "missing_source_comparison_rejected": not missing_comparison_report["ok"],
        "missing_trace_evidence_rejected": not missing_trace_report["ok"],
        "idealized_projection_rejected": not idealized_trace_report["ok"],
        "inflated_trace_tolerance_rejected": not inflated_tolerance_report["ok"],
        "duplicate_solid_dashed_edge_rejected": not duplicate_style_segment_report["ok"],
        "self_referential_expectation_rejected": not self_referential["ok"],
        "undercounted_multigraph_rejected": bool(undercounted_graph_report and not undercounted_graph_report["ok"]),
        "incomplete_degree_evidence_rejected": bool(incomplete_degrees_report and not incomplete_degrees_report["ok"]),
        "reused_drawn_edge_rejected": bool(reused_drawn_report and not reused_drawn_report["ok"]),
        "stale_graph_evidence_rejected": not stale_graph_report["ok"],
        "self_declared_probe_result_rejected": not tampered_graph_report["ok"],
        "clipped_svg_rejected": not clipped_svg_report["ok"],
        "unmatched_final_figure_rejected": not bad_delivery_report["ok"],
        "displaced_redraw_ink_rejected": not ink_alignment_bad["ok"],
        "omitted_source_line_rejected": (
            omission_one_way["ok"] and not omitted_line_report["ok"]
        ),
        "unsupported_chord_rejected": not segment_support_bad["ok"],
        "flat_shading_rejected_as_geometry": not flat_shading_report["ok"],
        "invalid_render_style_rejected": invalid_style_rejected,
        "missing_independent_trace_observation_rejected": (
            "trace observation artifact does not exist"
            in trace_without_observation_report["errors"]
        ),
        "real_source_coordinate_diagram_rejected": (
            not real_source_coordinate_diagram_report["ok"]
            and any(
                "real-source coordinate diagram must use type=trace" in error
                for error in real_source_coordinate_diagram_report["errors"]
            )
        ),
        "comparison_rejects_generic_coordinate_routing": (
            not real_source_coordinate_routing_report["ok"]
        ),
        "incomplete_composite_inventory_rejected": (
            bool(incomplete_composite_report)
            and not incomplete_composite_report["ok"]
        ),
    }

    positive_gates = {
        "defs_pattern_ignored_for_ink_bounds": defs_pattern_report["ok"],
        "ring_degree_probe": ring_probe_ok,
        "midpoint_cut_probe": cut_probe_ok,
        "measurement_stability_gate": stability_probe_ok,
        "imgprobe_cli_json_reports": imgprobe_cli_ok,
        "utf8_subprocess_output_decoded": utf8_probe.stdout.strip() == "한글 경로 UTF-8",
        "current_graph_probe_artifacts": graph_artifact_report["ok"],
        "full_real_source_graph_gate": (
            full_graph_manifest_report["ok"] and full_graph_artifact_report["ok"]
        ),
        "aligned_redraw_ink_accepted": ink_alignment_good["ok"],
        "complete_source_line_census_accepted": complete_line_report["ok"],
        "per_segment_source_support_accepted": segment_support_good["ok"],
        "faint_native_line_accepted_by_oriented_contrast": faint_line_report["ok"],
        "complete_composite_inventory_accepted": (
            bool(complete_composite_report) and complete_composite_report["ok"]
        ),
        "stroke_width_limited_segment_tolerance": (
            stroke_limited_support["radius_px"]
            < stroke_limited_support["nominal_radius_px"]
        ),
        "shared_comparison_evidence_preserves_results": (
            cached_ink_alignment == ink_alignment_good
            and cached_residual == complete_line_report
        ),
        "shared_distance_transform_computed_once": (
            shared_chord_source.distance_to_ink_computations == 1
        ),
        "content_addressed_svg_raster_cache": (
            first_cache_stats.get("hit") is False
            and second_cache_stats.get("hit") is True
            and first_cache_stats.get("key") == second_cache_stats.get("key")
            and svg_cache_pixels_equal
        ),
        "adaptive_inspection_size": (
            adaptive_size == (2200, 1711)
            and 2.03 < adaptive_scale < 2.04
            and adaptive_mode == "adaptive-max-long-edge"
            and explicit_size == (2160, 1680)
            and explicit_scale == 2
            and explicit_mode == "explicit"
        ),
        "native_coordinate_grid_and_capped_sheet": (
            grid_step == generated_grid_step == 50
            and grid_image.size == (200, 160)
            and preview_size == (1000, 778)
        ),
        "readable_default_style_reported": (
            reports["print-readability"]["render"]["render_style"]["solid_width"] >= 2.6
            and reports["print-readability"]["render"]["render_style"]["dashed_width"] >= 2.3
            and reports["print-readability"]["render"]["render_style"]["label_size"] >= 18
            and reports["print-readability"]["render"]["render_style"]["label_weight"] >= 700
        ),
        "readable_raster_line_weight": readable_line_thickness >= 4,
        "latin_label_raster_visible": latin_label_ink >= 40,
        "hangul_label_raster_visible": hangul_label_ink >= 25,
    }

    all_ok = (
        all(report["ok"] for report in reports.values())
        and delivery_report["ok"]
        and all(negative_gates.values())
        and all(positive_gates.values())
    )
    output = {
        "ok": all_ok,
        "cases": reports,
        "delivery_gate": delivery_report,
        "negative_gates": negative_gates,
        "positive_gates": positive_gates,
        "contact_sheet": str(sheet_path),
    }
    (args.out / "selftest-report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
