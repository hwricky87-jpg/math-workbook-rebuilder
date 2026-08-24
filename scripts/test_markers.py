#!/usr/bin/env python3
"""Focused regression tests for non-topological visual geometry markers."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_observation_overlay import render_overlay
from geometry_svg import render_manifest, resolve_marker_points


class MarkerRenderingTests(unittest.TestCase):
    def test_right_angle_from_arms_uses_inset_square_corner(self) -> None:
        marker = {
            "name": "right-A",
            "kind": "right-angle",
            "at": "A",
            "arms": ["B", "C"],
            "size": 8,
        }
        result = resolve_marker_points(
            marker,
            {"A": (30.0, 30.0), "B": (60.0, 30.0), "C": (30.0, 60.0)},
        )
        self.assertEqual(result, [(38.0, 30.0), (38.0, 38.0), (30.0, 38.0)])

    def test_manifest_renders_named_markers_without_structural_segments(self) -> None:
        manifest = {
            "canvas": {"width": 120, "height": 120},
            "points": {"A": [30, 30], "B": [60, 30], "C": [30, 60]},
            "segments": [],
            "markers": [
                {
                    "name": "right-A",
                    "kind": "right-angle",
                    "at": "A",
                    "arms": ["B", "C"],
                    "size": 8,
                },
                {
                    "name": "tick-1",
                    "kind": "tick",
                    "points": [[80, 50], [86, 44]],
                },
                {
                    "name": "observed-L",
                    "kind": "short-polyline",
                    "points": [[80, 70], [86, 70], [86, 76]],
                },
            ],
        }
        svg, report = render_manifest(manifest)
        self.assertEqual(report["segments"], 0)
        self.assertEqual(report["markers"], 3)
        self.assertEqual(
            report["marker_kinds"],
            {"right-angle": 1, "short-polyline": 1, "tick": 1},
        )
        self.assertIn('data-name="right-A"', svg)
        self.assertIn('data-name="tick-1"', svg)
        self.assertIn('class="marker solid" data-marker-kind="right-angle"', svg)

    def test_observation_overlay_counts_and_censuses_markers(self) -> None:
        source = Image.new("RGB", (120, 120), "white")
        observation = {
            "source_size": [120, 120],
            "source_geometry_roi": [1, 1, 119, 119],
            "source_geometry_roi_method": "manually inspected source component boundary",
            "points": {"A": [30, 30], "B": [60, 30], "C": [30, 60]},
            "segments": [],
            "curves": [],
            "markers": [
                {
                    "name": "right-A",
                    "kind": "right-angle",
                    "at": "A",
                    "arms": ["B", "C"],
                    "size": 8,
                },
                {
                    "name": "tick-1",
                    "kind": "tick",
                    "points": [[80, 50], [86, 44]],
                },
            ],
            "labels": [],
            "dimensions": [],
        }
        manifest = {
            "source_geometry_roi": [1, 1, 119, 119],
            "required_markers": ["right-A", "tick-1"],
        }
        _overlay, _side, report = render_overlay(source, observation, manifest)
        self.assertTrue(report["ok"], report["unresolved"])
        self.assertEqual(report["counts"]["markers"], 2)

    def test_ambiguous_or_incomplete_marker_forms_fail(self) -> None:
        points = {"A": (0.0, 0.0), "B": (10.0, 0.0), "C": (0.0, 10.0)}
        with self.assertRaisesRegex(ValueError, "cannot mix"):
            resolve_marker_points(
                {
                    "name": "bad-right",
                    "kind": "right-angle",
                    "points": [[1, 0], [1, 1], [0, 1]],
                    "at": "A",
                    "arms": ["B", "C"],
                },
                points,
            )
        with self.assertRaisesRegex(ValueError, "requires explicit points"):
            resolve_marker_points({"name": "bad-tick", "kind": "tick"}, points)


if __name__ == "__main__":
    unittest.main()
