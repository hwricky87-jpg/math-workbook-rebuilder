#!/usr/bin/env python3
"""Focused regressions for authoritative trace ROI and diagnostic evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_geometry import _source_first_roi_mask  # noqa: E402
from build_observation_overlay import render_overlay  # noqa: E402
from validate_geometry import validate_artifact_evidence  # noqa: E402
from validate_visual_review import GEOMETRY_ARTIFACTS, GEOMETRY_CHECKS  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeometryEvidenceContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="geometry-contract-regression-"
        )
        self.base = Path(self.temporary.name)
        self.manifest_path = self.base / "figure.json"
        self.svg_path = self.base / "figure.svg"

        source = Image.new("RGB", (100, 100), "white")
        ImageDraw.Draw(source).line((10, 50, 90, 50), fill="black", width=3)
        source.save(self.base / "source.png")
        (self.base / "comparison.png").write_bytes(b"comparison-current")
        (self.base / "diagnostic.png").write_bytes(b"diagnostic-current")
        (self.base / "visual-review-sheet.png").write_bytes(b"visual-review-sheet")
        (self.base / "landmark-zoom-sheet.png").write_bytes(b"landmark-zoom-sheet")
        self.svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<line x1="10" y1="50" x2="90" y2="50"/></svg>',
            encoding="utf-8",
        )
        (self.base / "inspection.json").write_text(
            json.dumps(
                {
                    "crop_inspection": {
                        "crop_complete": True,
                        "edge_strokes": {"detected": False},
                    }
                }
            ),
            encoding="utf-8",
        )

        source_hash = sha256(self.base / "source.png")
        observation = {
            "source_sha256": source_hash,
            "source_size": [100, 100],
            "source_geometry_roi": [5, 40, 95, 60],
            "source_geometry_roi_method": "manual source ink census before redraw",
            "points": {"A": [10, 50], "B": [90, 50]},
            "segments": [
                {"name": "A-B", "from": "A", "to": "B", "style": "solid"}
            ],
            "curves": [],
            "markers": [],
            "labels": [],
            "dimensions": [],
        }
        observation_path = self.base / "observation.json"
        observation_path.write_text(json.dumps(observation), encoding="utf-8")

        overlay, _side, overlay_report = render_overlay(source, observation)
        overlay_path = self.base / "observation-overlay.png"
        overlay.save(overlay_path)
        overlay_report.update(
            {
                "source": str((self.base / "source.png").resolve()),
                "source_sha256": source_hash,
                "observation": str(observation_path.resolve()),
                "observation_sha256": sha256(observation_path),
            }
        )
        (self.base / "observation-overlay.json").write_text(
            json.dumps(overlay_report), encoding="utf-8"
        )

        comparison_report = {
            "ok": True,
            "source_sha256": source_hash,
            "redraw_sha256": sha256(self.svg_path),
            "artifact_sha256": sha256(self.base / "comparison.png"),
            "diagnostic_artifact": "diagnostic.png",
            "diagnostic_artifact_sha256": sha256(self.base / "diagnostic.png"),
            "trace": {"ok": True},
            "ink_alignment": {"ok": True},
            "source_residual": {"ok": True},
            "segment_source_support": {"ok": True, "applied": True},
        }
        report_path = self.base / "comparison.json"
        report_path.write_text(json.dumps(comparison_report), encoding="utf-8")

        artifact_files = {
            "source": self.base / "source.png",
            "observation": observation_path,
            "observation_overlay": self.base / "observation-overlay.png",
            "redraw": self.svg_path,
            "comparison": self.base / "comparison.png",
            "diagnostic": self.base / "diagnostic.png",
            "comparison_report": report_path,
            "visual_review_sheet": self.base / "visual-review-sheet.png",
            "landmark_zoom_sheet": self.base / "landmark-zoom-sheet.png",
        }
        review = {
            "status": "approved",
            "reviewer": "focused-regression",
            "review_method": "Opened both PNGs with view_image at original detail.",
            "source_sha256": source_hash,
            "redraw_sha256": sha256(self.svg_path),
            "artifact_sha256": sha256(self.base / "comparison.png"),
            "report_sha256": sha256(report_path),
            "artifacts": {
                name: {"file": path.name, "sha256": sha256(path)}
                for name, path in artifact_files.items()
                if name in GEOMETRY_ARTIFACTS
            },
            "checks": {name: True for name in GEOMETRY_CHECKS},
            "issues": [],
        }
        review_path = self.base / "review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")

        self.manifest = {
            "type": "trace",
            "source": {"file": "source.png"},
            "canvas": {"width": 100, "height": 100},
            "points": {"A": [10, 50], "B": [90, 50]},
            "segments": [
                {"name": "A-B", "from": "A", "to": "B", "style": "solid"}
            ],
            "curves": [],
            "markers": [],
            "labels": [],
            "dimensions": [],
            "required_segments": ["A-B"],
            "required_curves": [],
            "required_markers": [],
            "required_labels": [],
            "required_dimensions": [],
            "trace_evidence": {
                "method": "manual native source trace",
                "inspection_artifact": "inspection.json",
                "observation_artifact": "observation.json",
                "observation_sha256": sha256(observation_path),
                "source_size": [100, 100],
                "source_geometry_roi": [5, 40, 95, 60],
                "max_normalized_error": 0.01,
                "points": {"A": [10, 50], "B": [90, 50]},
            },
            "source_comparison": {
                "artifact": "comparison.png",
                "report": "comparison.json",
                "review": "review.json",
                "review_sha256": sha256(review_path),
                "text_match": True,
                "topology_match": True,
                "line_styles_match": True,
                "labels_match": True,
                "dimensions_match": True,
                "projection_match": True,
                "inspected_latest_render": True,
            },
        }
        self._write_manifest_and_review(self.manifest)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_manifest_and_review(self, manifest: dict[str, object]) -> None:
        review_path = self.base / "review.json"
        manifest["source_comparison"]["review_sha256"] = sha256(review_path)  # type: ignore[index]
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _refresh_review_report_hash(self, manifest: dict[str, object]) -> None:
        review_path = self.base / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["report_sha256"] = sha256(self.base / "comparison.json")
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self._write_manifest_and_review(manifest)

    def test_only_trace_evidence_source_roi_is_used(self) -> None:
        aliases_only = {
            "type": "trace",
            "source_geometry_roi": [0, 0, 100, 100],
            "source": {
                "source_geometry_roi": [1, 1, 99, 99],
                "figure_roi": [40, 40, 60, 60],
            },
            "trace_evidence": {"geometry_roi": [5, 5, 95, 95]},
        }
        mask, label = _source_first_roi_mask((100, 100), aliases_only)
        self.assertIsNone(mask)
        self.assertIsNone(label)

        authoritative = copy.deepcopy(aliases_only)
        authoritative["trace_evidence"]["source_geometry_roi"] = [5, 40, 95, 60]
        mask, label = _source_first_roi_mask((100, 100), authoritative)
        self.assertIsNotNone(mask)
        self.assertEqual(label, "trace_evidence.source_geometry_roi")
        self.assertEqual(int((mask > 0).sum()), 90 * 20)

    def test_root_and_source_aliases_cannot_replace_trace_roi(self) -> None:
        tampered = copy.deepcopy(self.manifest)
        tampered["trace_evidence"].pop("source_geometry_roi")
        tampered["source_geometry_roi"] = [5, 40, 95, 60]
        tampered["source"]["figure_roi"] = [10, 45, 90, 55]
        self._write_manifest_and_review(tampered)
        report = validate_artifact_evidence(
            tampered, self.manifest_path, self.svg_path
        )
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("trace_evidence.source_geometry_roi" in error for error in report["errors"])
        )
        self.assertTrue(any("source.figure_roi" in error for error in report["errors"]))

    def test_observation_roi_must_equal_trace_roi(self) -> None:
        tampered = copy.deepcopy(self.manifest)
        tampered["trace_evidence"]["source_geometry_roi"] = [10, 45, 90, 55]
        self._write_manifest_and_review(tampered)
        report = validate_artifact_evidence(
            tampered, self.manifest_path, self.svg_path
        )
        self.assertFalse(report["ok"])
        self.assertIn(
            "trace observation source_geometry_roi must match "
            "trace_evidence.source_geometry_roi unchanged",
            report["errors"],
        )

    def test_diagnostic_report_hash_is_recomputed(self) -> None:
        baseline = validate_artifact_evidence(
            self.manifest, self.manifest_path, self.svg_path
        )
        self.assertTrue(baseline["ok"], baseline["errors"])

        report_path = self.base / "comparison.json"
        comparison = json.loads(report_path.read_text(encoding="utf-8"))
        comparison["diagnostic_artifact_sha256"] = "0" * 64
        report_path.write_text(json.dumps(comparison), encoding="utf-8")
        self._refresh_review_report_hash(self.manifest)
        result = validate_artifact_evidence(
            self.manifest, self.manifest_path, self.svg_path
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "source comparison diagnostic artifact hash does not match its report",
            result["errors"],
        )

    def test_visual_review_diagnostic_is_bound_to_report_artifact(self) -> None:
        alternate = self.base / "alternate-diagnostic.png"
        alternate.write_bytes(b"different-but-valid-diagnostic")
        review_path = self.base / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["artifacts"]["diagnostic"] = {
            "file": alternate.name,
            "sha256": sha256(alternate),
        }
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self._write_manifest_and_review(self.manifest)
        result = validate_artifact_evidence(
            self.manifest, self.manifest_path, self.svg_path
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "visual review artifact is not the current diagnostic",
            result["errors"],
        )

    def test_visual_review_observation_overlay_cannot_be_self_hashed_substitute(self) -> None:
        alternate = self.base / "alternate-observation-overlay.png"
        Image.new("RGB", (600, 600), "magenta").save(alternate)
        original_report = json.loads(
            (self.base / "observation-overlay.json").read_text(encoding="utf-8")
        )
        (self.base / "alternate-observation-overlay.json").write_text(
            json.dumps(original_report), encoding="utf-8"
        )
        review_path = self.base / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["artifacts"]["observation_overlay"] = {
            "file": alternate.name,
            "sha256": sha256(alternate),
        }
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self._write_manifest_and_review(self.manifest)

        result = validate_artifact_evidence(
            self.manifest, self.manifest_path, self.svg_path
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "visual review observation_overlay pixels do not match the current "
            "source and observation",
            result["errors"],
        )


if __name__ == "__main__":
    unittest.main()
